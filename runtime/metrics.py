"""
onixey3/runtime/metrics.py

Runtime Metrics System — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Passive telemetry collector for the Onixey V3 runtime.

This module receives event reports from other runtime modules, accumulates
them in bounded in-memory structures, and exposes a clean read API.
It does NOT drive any logic, does NOT modify any Blender state, and does NOT
have side effects. It is the runtime equivalent of an embedded StatsD sink.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT import bpy at module level.
    - Does NOT register handlers, operators, or panels.
    - Does NOT modify Scene, bpy.types, FCurves, or NLA data.
    - Does NOT write to disk.
    - Does NOT call frame_set().
    - Does NOT import: handlers, lifecycle, reload_manager, state, session.
    - Does NOT produce any side effects on import.
    - Does NOT interpret business rules (health computation lives in callers).
    - Does NOT know about cache tiers, handler names, or reload semantics.

ARCHITECTURE POSITION
─────────────────────
    runtime/
    ├── cache.py            ← records via record_counter / record_histogram
    ├── session.py          ← records via record_event / record_state
    ├── lifecycle.py        ← records via record_event / record_state
    ├── reload_manager.py   ← records via record_counter / record_event
    ├── handlers.py         ← records via record_histogram / record_counter
    ├── state.py            ← records via record_state / record_event
    └── metrics.py          ← THIS FILE (only receives, never calls above)

Dependency direction: all modules → metrics. metrics → nothing above it.

AGNOSTIC API DESIGN (v3.1.1)
─────────────────────────────
The previous API (record_cache_operation, record_handler_execution,
record_reload) was tightly coupled to specific internal subsystems.
Callers passed tier names ("L1"), handler function names, and reload fields
that were interpreted by metrics' own health logic.

This version exposes four primitive operations instead:

    record_counter(name, value=1)        — monotonically increasing count
    record_histogram(name, value_ms)     — latency / duration sample
    record_event(name, tags={})          — timestamped named occurrence
    record_state(name, value)            — current value of a named state

Health computation is the caller's responsibility. Metrics only stores and
returns raw data. This keeps metrics.py stable across changes to cache.py,
handlers.py, or reload_manager.py.

Migration guide for callers:
    record_cache_operation("L1", "get", hit=True)
        → record_counter("cache.L1.hits")
    record_cache_operation("L2", "get", hit=False)
        → record_counter("cache.L2.misses")
    record_cache_operation("L2", "set")
        → record_counter("cache.L2.sets")
    record_cache_operation("L2", "invalidate")
        → record_counter("cache.L2.invalidations")
    record_handler_execution("_handler_undo_post", 0.4, raised=False)
        → record_histogram("handler._handler_undo_post", 0.4)
    record_handler_execution("_handler_undo_post", 0.4, raised=True)
        → record_histogram("handler._handler_undo_post", 0.4)
        → record_counter("handler._handler_undo_post.errors")
    record_reload(success=True, duration_ms=12.3, reloaded=5, failed=0, orphans=2)
        → record_event("reload", tags={"success": True, "reloaded": 5, ...})
        → record_histogram("reload.duration", 12.3)
        → record_counter("reload.success")   or "reload.failure"

BOUNDED MEMORY GUARANTEE (v3.1.1)
──────────────────────────────────
Every collection in this module has a hard capacity cap. Four categories:

    Circular audit buffers (deque with maxlen):
        event_log:      _AUDIT_BUFFER_MAX entries × ~200 bytes ≈ 50 KB
        error_log:      _AUDIT_BUFFER_MAX entries × ~200 bytes ≈ 50 KB

    Bounded dicts (evict oldest on overflow via _BoundedDict):
        _counters:      _MAX_UNIQUE_COUNTERS unique names
        _histograms:    _MAX_UNIQUE_HISTOGRAMS unique names
        _states:        _MAX_UNIQUE_STATES unique names

    All three dict families use _BoundedDict, which evicts the OLDEST key
    when the cap is reached (insertion-order eviction, O(1) via dict ordering
    guarantee in Python 3.7+).

Under the worst-case scenario (F8 × 1000 with random metric names):
    Total RAM from metrics: O(_MAX_UNIQUE_COUNTERS + _MAX_UNIQUE_HISTOGRAMS
                              + _MAX_UNIQUE_STATES + _AUDIT_BUFFER_MAX × 2)
    Which is fully deterministic and independent of session length.

TIMESTAMP PRECISION
────────────────────
All timestamps use millisecond precision via _ts_now() to guarantee
distinguishability during fast reload cycles where multiple events can
fire within the same second.

Format: "YYYY-MM-DD HH:MM:SS.mmm"

PUBLIC API
──────────
    initialize()                                      → None
    record_counter(name, value=1)                     → None
    record_histogram(name, value_ms)                  → None
    record_event(name, tags={})                       → None
    record_state(name, value)                         → None
    get_counter(name)                                 → int
    get_histogram_stats(name)                         → dict | None
    get_state(name)                                   → Any | None
    get_runtime_metrics()                             → RuntimeMetricsSnapshot
    reset_metrics()                                   → None
    is_initialized()                                  → bool

LEGACY COMPATIBILITY SHIMS (deprecated, will be removed in 3.2.0)
──────────────────────────────────────────────────────────────────
    record_error(name, detail, exc)                   → record_counter + record_event
    record_reload(success, duration_ms, ...)          → record_counter + record_event + record_histogram
    record_timing(metric_name, elapsed_ms)            → record_histogram
    record_handler_execution(name, elapsed_ms, raised)→ record_histogram + record_counter
    record_cache_operation(tier, operation, hit)      → record_counter

SINGLETON LIFECYCLE
───────────────────
    initialize() → _store = _MetricsStore()
    reset_metrics() → explicit collection clear + _store = None

Calling record_*() before initialize() emits a log warning and is a no-op.
Calling get_runtime_metrics() before initialize() returns an empty snapshot.
Both behaviors are safe — callers do not need to guard against uninitialized state.

CHANGELOG
─────────
    3.1.0 — Initial implementation.
             Event/error audit logs, reload tracking, timing histograms,
             handler execution statistics, cache hit/miss accounting,
             health score computation, full snapshot API.
    3.1.1 — Agnostic API refactor + bounded dict protection.
             PROBLEM 1 FIX: Removed all business-rule coupling (cache tiers,
               handler names, reload semantics, health rules).
               Replaced record_cache_operation / record_handler_execution /
               record_reload with primitive record_counter / record_histogram /
               record_event / record_state.
               Health computation moved OUT of metrics (callers' responsibility).
             PROBLEM 2 FIX: Replaced unbounded dicts (_event_counts,
               _error_counts, _timings, _handlers) with _BoundedDict instances
               capped at _MAX_UNIQUE_COUNTERS / _MAX_UNIQUE_HISTOGRAMS /
               _MAX_UNIQUE_STATES. Evicts oldest key on overflow.
               Legacy shims preserved for backward compatibility (deprecated).
"""

from __future__ import annotations

import bisect as _bisect
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ── Module-level capacity constants ──────────────────────────────────────────

_AUDIT_BUFFER_MAX:      int   = 256   # Max entries in each circular audit log.
_TIMING_SAMPLES_MAX:    int   = 128   # Max timing samples per histogram.

# ── PROBLEM 2 FIX: Hard caps on unique metric name dictionaries ──────────────
# Without these, long sessions or F8-loops with dynamic names (e.g.
# "bone_arm_001", "curve_" + id) grow dicts without bound.
_MAX_UNIQUE_COUNTERS:   int   = 256   # Max unique counter names in _counters.
_MAX_UNIQUE_HISTOGRAMS: int   = 128   # Max unique histogram names in _histograms.
_MAX_UNIQUE_STATES:     int   = 64    # Max unique state names in _states.

_HEALTH_ERROR_DECAY:    float = 0.95  # Kept for external health-computation callers.


# ══════════════════════════════════════════════════════════════════════════════
# BOUNDED DICT  (PROBLEM 2 FIX)
# ══════════════════════════════════════════════════════════════════════════════

class _BoundedDict(dict):
    """
    A dict subclass with a hard maximum key count.

    When a new key would cause len() to exceed ``maxsize``, the OLDEST key
    (insertion order, guaranteed by Python 3.7+ dict ordering) is evicted
    before the new key is inserted.

    Eviction strategy: insertion-order LRU (oldest-first).
    Complexity: O(1) amortized for both insert and eviction (dict + next(iter)).

    WHY NOT OrderedDict?
    ────────────────────
    Python 3.7+ regular dicts preserve insertion order, so we get the same
    FIFO eviction without the extra memory overhead of OrderedDict.

    WHY NOT a true LRU (access-order)?
    ────────────────────────────────────
    Metrics names are typically stable (not dynamic after warm-up). The edge
    case that triggers eviction is pathological dynamic names like
    "bone_" + random_id. In that scenario, any eviction policy discards
    equally-stale data — insertion-order is simplest and least surprising.

    OVERFLOW LOGGING
    ────────────────
    The first overflow per _BoundedDict instance logs a WARNING (once per
    _BoundedDict lifetime) and subsequent overflows log at DEBUG to avoid
    log spam during hot-reload loops.
    """

    __slots__ = ("_maxsize", "_overflow_warned", "_eviction_count")

    def __init__(self, maxsize: int, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._maxsize:         int  = maxsize
        self._overflow_warned: bool = False
        self._eviction_count:  int  = 0

    def __setitem__(self, key: Any, value: Any) -> None:
        if key not in self and len(self) >= self._maxsize:
            # Evict the oldest (first) key.
            evicted_key = next(iter(self))
            super().__delitem__(evicted_key)
            self._eviction_count += 1

            if not self._overflow_warned:
                self._overflow_warned = True
                _log.warning(
                    "onixey3.runtime.metrics._BoundedDict: capacity %d reached. "
                    "Evicting oldest key %r. "
                    "This may indicate dynamic metric names (e.g. per-object names). "
                    "Consider using stable, category-level metric names.",
                    self._maxsize, evicted_key,
                )
            else:
                _log.debug(
                    "onixey3.runtime.metrics._BoundedDict: eviction #%d (key=%r).",
                    self._eviction_count, evicted_key,
                )

        super().__setitem__(key, value)

    def clear_and_reset(self) -> None:
        """Clear all entries and reset the overflow warning flag."""
        super().clear()
        self._overflow_warned = False
        self._eviction_count  = 0


# ══════════════════════════════════════════════════════════════════════════════
# TIMESTAMP HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _ts_now() -> str:
    """
    Return a millisecond-precision wall-clock timestamp.

    Format: YYYY-MM-DD HH:MM:SS.mmm

    Uses time.time() modulo arithmetic for the fractional millisecond component.
    Standard time.strftime() has one-second resolution and produces identical
    strings for events that fire within the same second — unacceptable for
    a metrics system that may record dozens of events per reload cycle.
    """
    t  = time.time()
    ms = int((t % 1.0) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


def _monotonic_ms() -> float:
    """Return time.monotonic() in milliseconds. Used for duration arithmetic."""
    return time.monotonic() * 1000.0


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT ENTRY  (immutable, slots for memory efficiency)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class AuditEntry:
    """
    Immutable record of a single audited event or error.

    Stored in the circular audit buffers. frozen=True + slots=True minimizes
    per-entry memory overhead in long-running sessions.

    Attributes:
        ts:        Millisecond-precision wall-clock timestamp.
        name:      Event or error category name (e.g., "reload_complete", "cache_miss").
        detail:    Optional freeform context string supplied by the caller.
        monotonic: time.monotonic() at record time, for duration arithmetic.
    """
    ts:        str
    name:      str
    detail:    str
    monotonic: float


# ══════════════════════════════════════════════════════════════════════════════
# TIMING ACCUMULATOR  (slots for memory efficiency)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class _TimingAccumulator:
    """
    Rolling statistics accumulator for a single named histogram metric.

    PERCENTILE STRATEGY — bisect-maintained sorted mirror
    ──────────────────────────────────────────────────────
    Maintains a second bounded list, ``_sorted``, kept in sorted order via
    ``bisect.insort`` on every ``record()`` call:

        Insert cost:  O(N)  — list shift after bisect.insort
        Read cost:    O(1)  — direct index access in ``p50_ms`` / ``p95_ms``
        Memory cost:  2 × _TIMING_SAMPLES_MAX × 8 bytes ≈ 2 KB extra per metric

    With _TIMING_SAMPLES_MAX = 128, O(N) insertion is a 128-element list shift
    — negligible compared to the O(N log N) sort it replaces for read-heavy
    workloads (diagnostic panels, health scoring).

    EVICTION OF THE SORTED MIRROR
    ──────────────────────────────
    When ``self.samples`` (the deque) evicts its oldest element due to
    ``maxlen``, the corresponding value is removed from ``_sorted`` via
    ``list.remove()`` (O(N)) before the new value is inserted. Both
    structures remain in perfect sync at all times.

    Attributes:
        name:        Metric name (e.g., "handler.depsgraph", "analysis.arc").
        samples:     Bounded deque of elapsed_ms values (insertion order).
        _sorted:     Mirror of samples, maintained in ascending sorted order.
        total_ms:    Cumulative sum of ALL recorded samples (not just buffer).
        call_count:  Total invocations since last reset().
        min_ms:      Global minimum (all time, not just buffer window).
        max_ms:      Global maximum (all time, not just buffer window).
    """
    name:       str
    samples:    Deque[float] = field(default_factory=lambda: deque(maxlen=_TIMING_SAMPLES_MAX))
    _sorted:    List[float]  = field(default_factory=list)
    total_ms:   float = 0.0
    call_count: int   = 0
    min_ms:     float = float("inf")
    max_ms:     float = 0.0

    def record(self, elapsed_ms: float) -> None:
        """
        Record one timing sample.

        Maintains ``_sorted`` as a sorted mirror of ``samples``:
            1. If the deque is at capacity, the oldest value is about to be
               evicted. Remove it from ``_sorted`` first (O(N) list.remove).
            2. Append to the deque (evicts oldest if at capacity).
            3. Insert into ``_sorted`` via bisect.insort (O(N) list shift).

        Running stats (total_ms, min_ms, max_ms) are updated every call,
        covering the full history — not just the rolling window.
        """
        if len(self.samples) == _TIMING_SAMPLES_MAX:
            evicted = self.samples[0]
            try:
                self._sorted.remove(evicted)
            except ValueError:
                _log.debug(
                    "_TimingAccumulator '%s': sorted mirror desync during eviction "
                    "(evicted=%.3f). Rebuilding.",
                    self.name, evicted,
                )
                self._sorted = sorted(self.samples)

        self.samples.append(elapsed_ms)
        _bisect.insort(self._sorted, elapsed_ms)

        self.total_ms   += elapsed_ms
        self.call_count += 1
        if elapsed_ms < self.min_ms:
            self.min_ms = elapsed_ms
        if elapsed_ms > self.max_ms:
            self.max_ms = elapsed_ms

    @property
    def avg_ms(self) -> float:
        """Mean elapsed time across all calls (all-time). 0.0 if no calls."""
        return self.total_ms / self.call_count if self.call_count > 0 else 0.0

    @property
    def p50_ms(self) -> float:
        """Approximate median from the rolling sample window. O(1)."""
        n = len(self._sorted)
        if n == 0:
            return 0.0
        return self._sorted[n // 2]

    @property
    def p95_ms(self) -> float:
        """
        Approximate 95th percentile from the rolling sample window. O(1).
        Returns max_ms if fewer than 20 samples (not statistically meaningful).
        """
        n = len(self._sorted)
        if n < 20:
            return self.max_ms
        return self._sorted[min(int(n * 0.95), n - 1)]

    def as_dict(self) -> Dict[str, object]:
        return {
            "name":       self.name,
            "call_count": self.call_count,
            "avg_ms":     round(self.avg_ms, 3),
            "p50_ms":     round(self.p50_ms, 3),
            "p95_ms":     round(self.p95_ms, 3),
            "min_ms":     round(self.min_ms, 3) if self.min_ms != float("inf") else 0.0,
            "max_ms":     round(self.max_ms, 3),
            "total_ms":   round(self.total_ms, 3),
        }


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME METRICS SNAPSHOT  (read-only output type)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RuntimeMetricsSnapshot:
    """
    Immutable point-in-time snapshot of all runtime metrics.

    Returned by get_runtime_metrics(). All fields are plain Python types —
    safe to log, serialize to JSON, or display in a Blender panel label.
    No bpy objects. No weak references. No callables.

    Health computation is NOT performed here. Callers that need a health score
    should compute it from counters, histogram_stats, and recent_events using
    their own domain-specific rules. Metrics only stores raw data.

    Attributes:
        ts:              Snapshot timestamp (millisecond precision).
        initialized:     Whether the metrics store is active.
        session_age_s:   Seconds since initialize() was called.
        counters:        Dict[name → total_count] for all record_counter() calls.
        histogram_stats: Dict[name → timing_dict] for all record_histogram() calls.
        states:          Dict[name → value] — most recent record_state() per name.
        recent_events:   Last N AuditEntry records as plain dicts.
        eviction_counts: Dict["counters"|"histograms"|"states" → eviction_count]
                         Non-zero values indicate dynamic metric name problems.
    """
    ts:              str
    initialized:     bool
    session_age_s:   float
    counters:        Dict[str, int]
    histogram_stats: Dict[str, Dict[str, object]]
    states:          Dict[str, Any]
    recent_events:   List[Dict[str, object]]
    eviction_counts: Dict[str, int]


# ══════════════════════════════════════════════════════════════════════════════
# METRICS STORE  (internal — not exported)
# ══════════════════════════════════════════════════════════════════════════════

class _MetricsStore:
    """
    Internal container for all live metrics data.

    One instance per addon lifecycle. Created by initialize(), destroyed by
    reset_metrics(). Never accessed directly from outside this module —
    all reads go through get_runtime_metrics() (immutable snapshot),
    all writes go through the record_*() public functions.

    MEMORY LAYOUT (all bounded — PROBLEM 2 FIX)
    ─────────────────────────────────────────────
        _event_log:    deque(maxlen=_AUDIT_BUFFER_MAX)           — circular
        _counters:     _BoundedDict(_MAX_UNIQUE_COUNTERS)        — evicts oldest
        _histograms:   _BoundedDict(_MAX_UNIQUE_HISTOGRAMS)      — evicts oldest
        _states:       _BoundedDict(_MAX_UNIQUE_STATES)          — evicts oldest

    AGNOSTIC DESIGN (PROBLEM 1 FIX)
    ─────────────────────────────────
    This store knows nothing about cache tiers, handler names, reload fields,
    or health rules. It only stores raw counters, histograms, events, and
    state values. Interpretation is the caller's responsibility.
    """

    __slots__ = (
        "_created_at",
        "_event_log",
        "_counters",
        "_histograms",
        "_states",
    )

    def __init__(self) -> None:
        self._created_at:  float = time.monotonic()
        self._event_log:   Deque[AuditEntry]               = deque(maxlen=_AUDIT_BUFFER_MAX)
        # PROBLEM 2 FIX: all three dicts are now _BoundedDict instances.
        self._counters:    _BoundedDict                    = _BoundedDict(_MAX_UNIQUE_COUNTERS)
        self._histograms:  _BoundedDict                    = _BoundedDict(_MAX_UNIQUE_HISTOGRAMS)
        self._states:      _BoundedDict                    = _BoundedDict(_MAX_UNIQUE_STATES)

    # ── Counter ───────────────────────────────────────────────────────────────

    def add_counter(self, name: str, value: int = 1) -> None:
        current = self._counters.get(name, 0)
        self._counters[name] = current + value

    # ── Histogram ─────────────────────────────────────────────────────────────

    def add_histogram(self, name: str, value_ms: float) -> None:
        if name not in self._histograms:
            self._histograms[name] = _TimingAccumulator(name=name)
        self._histograms[name].record(value_ms)

    # ── Event ─────────────────────────────────────────────────────────────────

    def add_event(self, name: str, tags: Dict[str, Any]) -> None:
        detail = ", ".join(f"{k}={v}" for k, v in tags.items()) if tags else ""
        entry = AuditEntry(
            ts        = _ts_now(),
            name      = name,
            detail    = detail,
            monotonic = time.monotonic(),
        )
        self._event_log.append(entry)

    # ── State ─────────────────────────────────────────────────────────────────

    def add_state(self, name: str, value: Any) -> None:
        self._states[name] = value

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def get_histogram_stats(self, name: str) -> Optional[Dict[str, object]]:
        acc = self._histograms.get(name)
        return acc.as_dict() if acc is not None else None

    def get_state_value(self, name: str) -> Any:
        return self._states.get(name)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def build_snapshot(self) -> RuntimeMetricsSnapshot:
        """Construct an immutable RuntimeMetricsSnapshot from current state."""
        return RuntimeMetricsSnapshot(
            ts              = _ts_now(),
            initialized     = True,
            session_age_s   = round(time.monotonic() - self._created_at, 2),
            counters        = dict(self._counters),
            histogram_stats = {
                name: acc.as_dict()
                for name, acc in self._histograms.items()
            },
            states          = dict(self._states),
            recent_events   = [
                {"ts": e.ts, "name": e.name, "detail": e.detail}
                for e in self._event_log
            ],
            eviction_counts = {
                "counters":   self._counters._eviction_count,
                "histograms": self._histograms._eviction_count,
                "states":     self._states._eviction_count,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_store: Optional[_MetricsStore] = None


def is_initialized() -> bool:
    """Return True if initialize() has been called and reset_metrics() has not."""
    return _store is not None


def _destroy_store() -> None:
    """
    Explicitly clear all internal collections of the active store, then drop
    the module-level reference.

    MOTIVATION
    ──────────
    Setting ``_store = None`` alone only removes the name binding. CPython's
    reference counter reclaims the _MetricsStore when its refcount drops to
    zero — but only when no other object holds a reference. By explicitly
    calling .clear() on every collection before dropping the reference we:
        1. Guarantee deterministic release of AuditEntry, _TimingAccumulator,
           and _BoundedDict contents, independent of GC timing.
        2. Ensure any external reference to the old store sees empty collections
           rather than stale data.
        3. Reset _BoundedDict overflow warning flags so the next session
           starts with a clean warning state.

    This function is the ONLY place that sets ``_store = None``.
    """
    global _store
    s = _store
    if s is None:
        return
    # Drop reference first so record_*() calls that arrive during cleanup
    # become no-ops immediately (single-threaded Blender, but defensive).
    _store = None
    try:
        s._event_log.clear()
        s._counters.clear_and_reset()
        s._histograms.clear_and_reset()
        s._states.clear_and_reset()
    except Exception as exc:
        _log.error(
            "onixey3.runtime.metrics._destroy_store(): "
            "error during collection cleanup (non-fatal): %s",
            exc,
        )


def _get_store(caller: str = "") -> Optional[_MetricsStore]:
    """
    Internal: return the active store, logging at DEBUG if not initialized.

    All record_*() functions use this to gracefully handle calls that arrive
    before initialize() or after reset_metrics() without raising.
    """
    if _store is None:
        _log.debug(
            "onixey3.runtime.metrics: record called before initialize() "
            "(caller=%s). Operation ignored.",
            caller or "unknown",
        )
        return None
    return _store


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — PRIMITIVE OPERATIONS  (PROBLEM 1 FIX)
# ══════════════════════════════════════════════════════════════════════════════

def initialize() -> None:
    """
    Initialize the metrics store for a new addon lifecycle.

    MUST be called from lifecycle.startup() before any other runtime module
    begins reporting events. Safe to call multiple times — if already
    initialized, destroys the stale store and creates a new one (idempotent
    with a WARNING log).

    HOT-RELOAD / F8 SAFETY
    ──────────────────────
    F8 re-executes ``_store = None`` at module level, resetting the binding.
    initialize() calls _destroy_store() before constructing a new instance,
    guaranteeing no stale _MetricsStore leaks across reload cycles.

    Raises:
        Nothing. All errors are logged and suppressed.
    """
    global _store
    if _store is not None:
        _log.debug(
            "onixey3.runtime.metrics.initialize(): store already exists "
            "(double-init). Destroying stale store before reinitializing."
        )
        _destroy_store()

    try:
        _store = _MetricsStore()
        _log.debug("onixey3.runtime.metrics: initialized.")
    except Exception as exc:
        _log.error(
            "onixey3.runtime.metrics.initialize() failed: %s. "
            "Metrics will be unavailable this session.",
            exc,
        )


def record_counter(name: str, value: int = 1) -> None:
    """
    Increment a named counter by value (default 1).

    Use for monotonically increasing counts: cache hits, handler calls,
    error occurrences, reload events, etc.

    Counter names are arbitrary strings. Use dot-separated namespacing:
        "cache.L1.hits", "handler.undo_post.calls", "reload.success"

    Memory: stored in a _BoundedDict capped at _MAX_UNIQUE_COUNTERS.
    If the cap is reached, the OLDEST counter is evicted (insertion-order).
    Overflow logs a WARNING on the first occurrence.

    Args:
        name:  Dot-separated metric name. Keep stable — avoid dynamic suffixes.
        value: Amount to add. Must be >= 1. Negative values are ignored.
    """
    if value < 1:
        _log.warning(
            "onixey3.runtime.metrics.record_counter('%s'): "
            "value must be >= 1, got %d. Ignored.",
            name, value,
        )
        return
    s = _get_store("record_counter")
    if s is None:
        return
    try:
        s.add_counter(name, value)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.record_counter('%s') failed: %s", name, exc)


def record_histogram(name: str, value_ms: float) -> None:
    """
    Record one latency/duration sample for a named histogram.

    Use for any operation where timing matters: handler execution,
    analysis passes, cache operations, migration runs.

    Histogram names are arbitrary strings. Use dot-separated namespacing:
        "handler.undo_post", "analysis.arc", "cache.l2.rebuild"

    Memory: stored in a _BoundedDict capped at _MAX_UNIQUE_HISTOGRAMS.
    Each histogram keeps a rolling window of _TIMING_SAMPLES_MAX samples
    with an O(1) sorted mirror for p50/p95 computation.

    Args:
        name:     Dot-separated metric name. Keep stable.
        value_ms: Duration in milliseconds. Negative values are logged and ignored.
    """
    if value_ms < 0:
        _log.warning(
            "onixey3.runtime.metrics.record_histogram('%s'): "
            "negative value_ms=%.3f ignored.",
            name, value_ms,
        )
        return
    s = _get_store("record_histogram")
    if s is None:
        return
    try:
        s.add_histogram(name, value_ms)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.record_histogram('%s') failed: %s", name, exc)


def record_event(name: str, tags: Optional[Dict[str, Any]] = None) -> None:
    """
    Record a named runtime event with optional metadata tags.

    Events are stored in a bounded circular log (_AUDIT_BUFFER_MAX entries).
    Unlike counters, events carry a timestamp and arbitrary tag dict —
    useful for timeline reconstruction and forensic analysis.

    Use for: lifecycle phase changes, session state transitions, reload
    completions, undo/redo occurrences, file load events.

    Tags are serialized as "key=value, ..." in the AuditEntry.detail field.
    They do NOT need to be bounded — the circular buffer is the bound.

    Args:
        name: Event category name. Use snake_case: "phase_active", "undo_fired".
        tags: Optional dict of key→value metadata. Values are stringified.
              Example: {"success": True, "duration_ms": 12.3, "reloaded": 5}
    """
    s = _get_store("record_event")
    if s is None:
        return
    try:
        s.add_event(name, tags or {})
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.record_event('%s') failed: %s", name, exc)


def record_state(name: str, value: Any) -> None:
    """
    Record the current value of a named runtime state variable.

    Unlike counters (cumulative) and histograms (rolling), states are
    last-write-wins: calling record_state("phase", "active") twice just
    overwrites the previous value. Only the most recent value is retained.

    Use for: current lifecycle phase, active rig name, analysis state,
    frame count at last analysis, boolean feature flags.

    Memory: stored in a _BoundedDict capped at _MAX_UNIQUE_STATES.
    Values must be plain Python types (str, int, float, bool, None) to
    ensure the snapshot is safely serializable.

    Args:
        name:  State name. Use snake_case: "lifecycle_phase", "active_rig".
        value: Current value. Prefer plain Python types.
    """
    s = _get_store("record_state")
    if s is None:
        return
    try:
        s.add_state(name, value)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.record_state('%s') failed: %s", name, exc)


# ── Read functions ─────────────────────────────────────────────────────────────

def get_counter(name: str) -> int:
    """
    Return the current value of a named counter.

    Returns 0 if the counter has never been recorded or if the store is not
    initialized. Never raises.
    """
    s = _get_store("get_counter")
    if s is None:
        return 0
    try:
        return s.get_counter(name)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.get_counter('%s') failed: %s", name, exc)
        return 0


def get_histogram_stats(name: str) -> Optional[Dict[str, object]]:
    """
    Return the statistics dict for a named histogram, or None if not recorded.

    Dict keys: name, call_count, avg_ms, p50_ms, p95_ms, min_ms, max_ms, total_ms.
    All values are plain Python types (str, int, float).
    """
    s = _get_store("get_histogram_stats")
    if s is None:
        return None
    try:
        return s.get_histogram_stats(name)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.get_histogram_stats('%s') failed: %s", name, exc)
        return None


def get_state_value(name: str) -> Any:
    """
    Return the most recent value recorded via record_state(name), or None.
    Never raises.
    """
    s = _get_store("get_state_value")
    if s is None:
        return None
    try:
        return s.get_state_value(name)
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.get_state_value('%s') failed: %s", name, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════════

def get_runtime_metrics() -> RuntimeMetricsSnapshot:
    """
    Return an immutable snapshot of all current runtime metrics.

    All fields are plain Python types — safe to log, serialize to JSON,
    or display in a Blender panel.

    If called before initialize(), returns a minimal "not initialized" snapshot
    rather than raising. Diagnostic code can call this safely at any time.

    HEALTH COMPUTATION
    ──────────────────
    Unlike v3.1.0, this snapshot does NOT include a health_score or
    health_signals field. Health computation has been moved OUT of metrics
    (it was tightly coupled to specific subsystem names). Callers that need
    a health score should derive it from snapshot.counters and
    snapshot.histogram_stats using their own domain rules.

    Performance: O(N) where N = total unique metric names. Expected < 1ms.
    Do NOT call from draw() callbacks or tight loops.
    """
    s = _store
    if s is None:
        return RuntimeMetricsSnapshot(
            ts              = _ts_now(),
            initialized     = False,
            session_age_s   = 0.0,
            counters        = {},
            histogram_stats = {},
            states          = {},
            recent_events   = [],
            eviction_counts = {"counters": 0, "histograms": 0, "states": 0},
        )
    try:
        return s.build_snapshot()
    except Exception as exc:
        _log.error("onixey3.runtime.metrics.get_runtime_metrics() failed: %s", exc)
        return RuntimeMetricsSnapshot(
            ts              = _ts_now(),
            initialized     = True,
            session_age_s   = 0.0,
            counters        = {},
            histogram_stats = {},
            states          = {},
            recent_events   = [{"ts": _ts_now(), "name": "snapshot_error", "detail": str(exc)}],
            eviction_counts = {"counters": 0, "histograms": 0, "states": 0},
        )


def reset_metrics() -> None:
    """
    Destroy the current metrics store and allow initialize() to create a new one.

    Called from lifecycle.shutdown() during unregister(). Ensures a subsequent
    register() cycle starts with a clean metrics baseline.

    Idempotent: calling when already reset emits DEBUG and returns immediately.

    Uses _destroy_store() to explicitly clear all bounded collections before
    dropping the reference, giving CPython's reference counter an unambiguous
    signal to reclaim memory immediately rather than relying on GC cycles.

    After this call:
        - All counters/histograms/states return to zero on next initialize().
        - All audit buffers are empty.
        - record_*() calls are silent no-ops until initialize() is called again.
        - get_runtime_metrics() returns the uninitialized empty snapshot.
    """
    if _store is None:
        _log.debug("onixey3.runtime.metrics.reset_metrics(): already reset. No-op.")
        return
    _destroy_store()
    _log.debug("onixey3.runtime.metrics: reset complete.")


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY COMPATIBILITY SHIMS  (deprecated — will be removed in 3.2.0)
#
# These preserve backward compatibility for callers not yet migrated to
# the new agnostic API. Each shim translates the old call into one or more
# primitive record_counter / record_histogram / record_event / record_state
# calls. No business logic is added here.
#
# Deprecation log: each shim logs at DEBUG the first time it is called,
# to help identify callers that still need migration.
# ══════════════════════════════════════════════════════════════════════════════

_legacy_warned: Dict[str, bool] = {}   # Tracks first-call warning per shim name.


def _legacy_warn(shim_name: str) -> None:
    if not _legacy_warned.get(shim_name):
        _legacy_warned[shim_name] = True
        _log.debug(
            "onixey3.runtime.metrics: deprecated API '%s' called. "
            "Migrate to record_counter / record_histogram / record_event / "
            "record_state. Shims will be removed in v3.2.0.",
            shim_name,
        )


def record_error(
    name:   str,
    detail: str = "",
    exc:    Optional[BaseException] = None,
) -> None:
    """
    DEPRECATED (v3.1.1). Use record_counter + record_event instead.

    Migration:
        record_error("reload_failed", "bad module", exc)
        →
        record_counter("error.reload_failed")
        record_event("error", {"name": "reload_failed", "detail": "bad module",
                                "exc": str(exc)})
    """
    _legacy_warn("record_error")
    full_detail = detail
    if exc is not None:
        exc_part    = f"{type(exc).__name__}: {exc}"
        full_detail = f"{detail}  [{exc_part}]" if detail else exc_part
    record_counter(f"error.{name}")
    record_event("error", {"name": name, "detail": full_detail})


def record_reload(
    success:     bool,
    duration_ms: float,
    reloaded:    int   = 0,
    failed:      int   = 0,
    orphans:     int   = 0,
) -> None:
    """
    DEPRECATED (v3.1.1). Use record_counter + record_histogram + record_event.

    Migration:
        record_reload(True, 12.3, reloaded=5, failed=0, orphans=2)
        →
        record_counter("reload.success")           # or "reload.failure"
        record_histogram("reload.duration", 12.3)
        record_event("reload", {"success": True, "reloaded": 5, ...})
    """
    _legacy_warn("record_reload")
    status = "success" if success else "failure"
    record_counter(f"reload.{status}")
    record_histogram("reload.duration", duration_ms)
    record_event("reload", {
        "success":  success,
        "duration": round(duration_ms, 2),
        "reloaded": reloaded,
        "failed":   failed,
        "orphans":  orphans,
    })


def record_timing(metric_name: str, elapsed_ms: float) -> None:
    """
    DEPRECATED (v3.1.1). Use record_histogram instead.

    Migration:
        record_timing("analysis.arc", 4.2)
        →
        record_histogram("analysis.arc", 4.2)
    """
    _legacy_warn("record_timing")
    record_histogram(metric_name, elapsed_ms)


def record_handler_execution(
    handler_name: str,
    elapsed_ms:   float,
    raised:       bool = False,
) -> None:
    """
    DEPRECATED (v3.1.1). Use record_histogram + record_counter instead.

    Migration:
        record_handler_execution("_handler_undo_post", 0.4, raised=False)
        →
        record_histogram("handler._handler_undo_post", 0.4)
        # (only if raised=True):
        record_counter("handler._handler_undo_post.errors")
    """
    _legacy_warn("record_handler_execution")
    record_histogram(f"handler.{handler_name}", elapsed_ms)
    if raised:
        record_counter(f"handler.{handler_name}.errors")


def record_cache_operation(
    tier:      str,
    operation: str,
    hit:       Optional[bool] = None,
) -> None:
    """
    DEPRECATED (v3.1.1). Use record_counter instead.

    Migration:
        record_cache_operation("L1", "get", hit=True)
        →
        record_counter("cache.L1.hits")

        record_cache_operation("L2", "get", hit=False)
        →
        record_counter("cache.L2.misses")

        record_cache_operation("L2", "set")
        →
        record_counter("cache.L2.sets")

        record_cache_operation("L2", "invalidate")
        →
        record_counter("cache.L2.invalidations")
    """
    _legacy_warn("record_cache_operation")
    if operation == "get":
        if hit is True:
            record_counter(f"cache.{tier}.hits")
        elif hit is False:
            record_counter(f"cache.{tier}.misses")
    elif operation == "set":
        record_counter(f"cache.{tier}.sets")
    elif operation == "invalidate":
        record_counter(f"cache.{tier}.invalidations")
