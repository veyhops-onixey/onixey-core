"""
onixey3/runtime/event_bus.py

Runtime Event Bus — Onixey V3 / Blender 4.2+

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSIBILITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Decoupled publish/subscribe message bus for the Onixey runtime.

Producers emit named events. Consumers subscribe callbacks.
Neither side knows about the other — coupling is zero.

    # Producer (e.g. handlers.py after undo):
    event_bus.emit("CACHE_INVALIDATED", {"tier": "L2", "reason": "undo"})

    # Consumer (e.g. diagnostics.py):
    event_bus.subscribe("CACHE_INVALIDATED", on_cache_invalidated)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT THIS MODULE DOES NOT DO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ✗ Does NOT import bpy at any level.
    ✗ Does NOT import: cache, session, handlers, lifecycle, state, metrics,
                       diagnostics, guards, reload_manager.
    ✗ Does NOT implement asyncio, queues, event sourcing, or replay.
    ✗ Does NOT persist events to disk.
    ✗ Does NOT implement priority ordering or dependency graphs.
    ✗ Does NOT contain animation, analysis, or Fix logic.
    ✗ Does NOT produce side effects on import.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ARCHITECTURE POSITION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    runtime/
    ├── event_bus.py        ← THIS FILE
    │       ↑ emit()            ↑ subscribe()
    │   handlers.py         diagnostics.py
    │   lifecycle.py        metrics.py
    │   session.py          state.py
    │   cache.py            guards.py
    └── (all runtime modules are peers — no module imports event_bus
        at the top level; they call it lazily after initialize())

event_bus.py imports from: threading, weakref, logging, time, dataclasses,
                            typing — stdlib only.
Nobody imports FROM event_bus at module level (avoids circular import risk).
All callers import and use event_bus lazily inside functions/methods.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THREAD SAFETY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All mutations of the subscriber registry and stats counters are guarded by
a single threading.RLock (_lock). Blender 4.x runs Python on a single thread,
but:
    - Addon reload (F8) can invoke cleanup from a different call stack.
    - Future Blender versions may introduce threaded Python.
    - The lock is reentrant (RLock), so emit() can safely call subscribe()
      from within a callback without deadlocking.

Emit() captures a snapshot of the listener list under the lock, then
dispatches outside the lock. This guarantees:
    - No deadlock if a callback calls subscribe()/unsubscribe()/emit().
    - No ConcurrentModificationError during iteration.
    - New subscriptions made during emit() take effect on the NEXT emit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WEAKREF STRATEGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Callbacks are stored as weakrefs when possible to avoid memory leaks:

    - Bound methods:  stored as (weakref(instance), method_name) pair.
                      When the instance is GC'd, the subscription is
                      auto-removed on next emit().
    - Functions/lambdas/staticmethods: stored as weakref.ref directly.
                      Note: lambdas are anonymous and may be GC'd
                      immediately after subscribe() if the caller does not
                      hold a reference. subscribe() emits a WARNING for
                      lambdas to prevent silent non-delivery.
    - Callable objects (instances with __call__): stored as weakref.ref
                      to the instance. Subject to GC if caller drops ref.

Dead weakrefs are pruned lazily on every emit() and subscribe() call.
This is O(N listeners) and runs only during dispatch — never in idle hot paths.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANONICAL EVENT NAMES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Defined as string constants in EventName to enable IDE autocomplete,
typo detection, and a single source of truth. Producers and consumers
should use these constants rather than raw strings.

    EventName.ANIMATION_CHANGED     — FCurve / keyframe / driver modified
    EventName.SESSION_RESET         — session state cleared (undo, load, F8)
    EventName.CACHE_INVALIDATED     — one or more cache tiers invalidated
    EventName.RUNTIME_RELOADED      — F8 or full addon reload completed
    EventName.LIFECYCLE_PHASE_CHANGED — lifecycle phase transition occurred
    EventName.HANDLER_ERROR         — a Blender handler raised an exception
    EventName.COMPONENT_REGISTERED  — registry.register_component() succeeded
    EventName.COMPONENT_UNREGISTERED — registry.unregister_component() called
    EventName.METRICS_RECORDED      — metrics.record_*() called (sampling)

Using raw strings is supported — the bus does not validate event names.
Unknown names are silently dispatched (no subscribers = no-op).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SINGLETON LIFECYCLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Module import    → _bus = None (zero side effects)
    initialize()     → _bus = _EventBus()
    reset_bus()      → _bus.clear() + _bus = None
    is_initialized() → bool

Calling subscribe() / emit() before initialize() raises BusNotInitializedError.
Calling get_stats() before initialize() returns a safe empty snapshot.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PUBLIC API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lifecycle:
    initialize()                                → None
    reset_bus()                                 → None
    is_initialized()                            → bool

Write:
    subscribe(event_name, callback)             → None
    unsubscribe(event_name, callback)           → bool

Emit:
    emit(event_name, payload=None)              → int  (listeners called)

Diagnostics:
    get_stats()                                 → EventBusStats
    dump_stats_to_log()                         → None

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGELOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    3.1.0 — Initial implementation (Iteration 1 Runtime Foundation).
            Thread-safe RLock, weakref callbacks, duplicate protection,
            snapshot-based dispatch (no lock during callbacks), lazy dead
            weakref pruning, canonical EventName constants, full stats API.
    3.1.1 — Surgical improvements (no API changes).
            • _MAX_RECENT_ERRORS moved to module-level constants block.
            • traceback moved to module-level imports (was lazily imported
              inside _record_callback_error on every call).
            • Removed unused Set, Tuple from typing imports.
            • total_emits now counted before early-return for no-listener
              events (was silently undercounting in stats snapshots).
            • Dead weakref pruning changed from O(N²) list.remove() loop
              to O(N) slice assignment in both subscribe() and emit().
            • emit() scoping: pruned variable declared before lock block
              to ensure it is accessible in the post-dispatch log.
            • _record_callback_error: removed redundant error_record alias.
            • err_rate: removed redundant max(...,1) guard.
            • _WeakCallback.__hash__: removed dead isinstance branch.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
import traceback
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Hard cap: maximum unique event names the bus will track.
# Prevents unbounded dict growth from dynamic event names.
_MAX_EVENT_TYPES: int = 256

# Hard cap: maximum subscribers per single event name.
# Prevents a single event from accumulating thousands of listeners.
_MAX_LISTENERS_PER_EVENT: int = 64

# Hard cap: maximum recent error records kept in memory.
# Bounds _recent_errors list; old entries evicted FIFO on overflow.
_MAX_RECENT_ERRORS: int = 32


# ──────────────────────────────────────────────────────────────────────────────
# CANONICAL EVENT NAMES
# ──────────────────────────────────────────────────────────────────────────────

class EventName:
    """
    Canonical string constants for all Onixey runtime events.

    Use these instead of raw strings to get IDE autocomplete and
    to catch typos at definition time rather than at runtime.

    Future events for Iteration 2+ should be added here rather than
    using raw strings at the call site.
    """
    # Animation pipeline events.
    ANIMATION_CHANGED:       str = "ANIMATION_CHANGED"
    SESSION_RESET:           str = "SESSION_RESET"
    CACHE_INVALIDATED:       str = "CACHE_INVALIDATED"

    # Lifecycle events.
    RUNTIME_RELOADED:        str = "RUNTIME_RELOADED"
    LIFECYCLE_PHASE_CHANGED: str = "LIFECYCLE_PHASE_CHANGED"

    # Handler events.
    HANDLER_ERROR:           str = "HANDLER_ERROR"

    # Registry events.
    COMPONENT_REGISTERED:    str = "COMPONENT_REGISTERED"
    COMPONENT_UNREGISTERED:  str = "COMPONENT_UNREGISTERED"

    # Metrics events (sampling — high frequency, use sparingly).
    METRICS_RECORDED:        str = "METRICS_RECORDED"


# ──────────────────────────────────────────────────────────────────────────────
# ERROR TYPES
# ──────────────────────────────────────────────────────────────────────────────

class BusNotInitializedError(RuntimeError):
    """
    Raised when subscribe() or emit() is called before initialize().

    Recovery: call event_bus.initialize() from lifecycle.startup().
    """


class BusCapacityError(RuntimeError):
    """
    Raised when:
        - A new event name would exceed _MAX_EVENT_TYPES.
        - A new subscriber would exceed _MAX_LISTENERS_PER_EVENT.

    Indicates either a logic bug (subscribe loop) or capacity constants
    that need to be raised for a legitimately larger system.
    """


class BusSubscriptionError(ValueError):
    """
    Raised when subscribe() receives an invalid callback.

    Covers:
        - Non-callable values.
        - None callbacks.
        - Empty event names.
    """


# ──────────────────────────────────────────────────────────────────────────────
# WEAKREF WRAPPER  (internal)
# ──────────────────────────────────────────────────────────────────────────────

class _WeakCallback:
    """
    Unified weakref wrapper that handles both bound methods and plain callables.

    WHY NOT plain weakref.ref(callback)?
    ─────────────────────────────────────
    Bound methods are temporary objects. ``weakref.ref(obj.method)`` creates
    a weak reference to the temporary bound method object, which is immediately
    eligible for garbage collection because nothing else holds it. The ref
    is dead before the next line executes.

    Solution: store a weak reference to the INSTANCE separately, and look up
    the method by name on dereference. This keeps the instance's own lifetime
    as the governing lifetime of the subscription.

    Callable objects (instances with __call__) are handled like plain callables —
    a weakref.ref to the instance. If the caller drops their reference, the
    subscription is automatically pruned on next emit.

    Attributes:
        _is_method: True if this wraps a bound method.
        _ref:       weakref.ref to the callable (non-method) or instance (method).
        _method_name: __name__ of the method (method path only).
        _identity:  Unique identity key for duplicate detection.
                    For methods: (id(instance), method_name).
                    For callables: id(function_object).
    """

    __slots__ = ("_is_method", "_ref", "_method_name", "_identity")

    def __init__(self, callback: Callable) -> None:
        if inspect.ismethod(callback):
            # Bound method: store weakref to the instance, remember method name.
            instance = callback.__self__
            self._is_method   = True
            self._ref         = weakref.ref(instance)
            self._method_name = callback.__func__.__name__
            self._identity    = (id(instance), self._method_name)
        else:
            # Plain function, static method, or callable object.
            self._is_method   = False
            self._ref         = weakref.ref(callback)
            self._method_name = ""
            self._identity    = id(callback)

    def is_alive(self) -> bool:
        """Return True if the referenced object is still alive."""
        return self._ref() is not None

    def resolve(self) -> Optional[Callable]:
        """
        Resolve to the actual callable, or None if the reference is dead.

        For bound methods: looks up the method name on the live instance.
        For plain callables: returns the callable directly.
        """
        target = self._ref()
        if target is None:
            return None
        if self._is_method:
            method = getattr(target, self._method_name, None)
            return method if callable(method) else None
        return target

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _WeakCallback):
            return NotImplemented
        return self._identity == other._identity

    def __hash__(self) -> int:
        return hash(self._identity)

    def __repr__(self) -> str:
        alive = "alive" if self.is_alive() else "dead"
        if self._is_method:
            return f"<_WeakCallback method={self._method_name!r} {alive}>"
        return f"<_WeakCallback fn={self._identity} {alive}>"


# ──────────────────────────────────────────────────────────────────────────────
# EVENT BUS STATS SNAPSHOT  (immutable read output)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EventBusStats:
    """
    Immutable snapshot of event bus state.

    All fields are plain Python types — safe to log, serialize, or display.

    Attributes:
        ts:                  Wall-clock timestamp (ms precision) at snapshot.
        initialized:         Whether the bus is active.
        session_age_s:       Seconds since initialize() was called.
        registered_events:   Number of unique event names with subscribers.
        total_listeners:     Total live subscriber count across all events.
        listeners_per_event: Dict mapping event_name → listener count.
        total_emits:         Cumulative emit() calls since initialize().
        total_deliveries:    Cumulative successful callback invocations.
        total_errors:        Cumulative callback exceptions caught.
        total_dead_pruned:   Cumulative dead weakrefs pruned.
        total_duplicates_rejected: Cumulative duplicate subscribe() attempts.
        error_rate:          total_errors / max(total_deliveries, 1).
        recent_errors:       Last N error records (event_name, error_str, ts).
    """
    ts:                        str
    initialized:               bool
    session_age_s:             float
    registered_events:         int
    total_listeners:           int
    listeners_per_event:       Dict[str, int]
    total_emits:               int
    total_deliveries:          int
    total_errors:              int
    total_dead_pruned:         int
    total_duplicates_rejected: int
    error_rate:                float
    recent_errors:             List[Dict[str, str]]


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL EVENT BUS
# ──────────────────────────────────────────────────────────────────────────────

class _EventBus:
    """
    Internal implementation. Access only via module-level public functions.

    DISPATCH SEQUENCE (emit):
        1. Acquire lock.
        2. Retrieve listener list for event_name (may be empty).
        3. Prune dead weakrefs from the list (in-place under lock).
        4. Capture a SNAPSHOT (shallow copy) of the live listener list.
        5. Release lock.
        6. Iterate snapshot and call each resolved callback.
           Exceptions are caught per-callback, logged, and counted.
           They do NOT propagate — one broken callback cannot block others.
        7. Return count of successfully called listeners.

    MEMORY LAYOUT:
        _subscribers: Dict[event_name → List[_WeakCallback]]
        All lists are bounded by _MAX_LISTENERS_PER_EVENT.
        The dict is bounded by _MAX_EVENT_TYPES.
    """

    __slots__ = (
        "_lock",
        "_subscribers",
        "_created_at",
        "_total_emits",
        "_total_deliveries",
        "_total_errors",
        "_total_dead_pruned",
        "_total_duplicates_rejected",
        "_recent_errors",
    )

    def __init__(self) -> None:
        self._lock:                       threading.RLock              = threading.RLock()
        self._subscribers:                Dict[str, List[_WeakCallback]] = {}
        self._created_at:                 float                        = time.monotonic()
        self._total_emits:                int                          = 0
        self._total_deliveries:           int                          = 0
        self._total_errors:               int                          = 0
        self._total_dead_pruned:          int                          = 0
        self._total_duplicates_rejected:  int                          = 0
        self._recent_errors:              List[Dict[str, str]]         = []

    # ── Subscribe ─────────────────────────────────────────────────────────────

    def subscribe(self, event_name: str, callback: Callable) -> None:
        """
        Register callback to be called when event_name is emitted.

        Validation (raises BusSubscriptionError or BusCapacityError):
            - event_name must be a non-empty str.
            - callback must be callable and not None.
            - callback must not already be subscribed (duplicate protection).
            - listener count for event_name must not exceed _MAX_LISTENERS_PER_EVENT.
            - unique event type count must not exceed _MAX_EVENT_TYPES.

        Lambda warning:
            Lambdas are anonymous objects. If the caller does not hold a
            reference to the lambda after subscribe(), it may be GC'd
            immediately, making the subscription silently dead. subscribe()
            emits a WARNING when a lambda is detected.

        Thread safety: guarded by RLock.
        """
        # Input validation — outside lock for speed.
        if not isinstance(event_name, str) or not event_name:
            raise BusSubscriptionError(
                f"event_bus.subscribe(): event_name must be a non-empty str, "
                f"got {event_name!r} ({type(event_name).__name__})."
            )
        if callback is None:
            raise BusSubscriptionError(
                f"event_bus.subscribe({event_name!r}): callback is None. "
                f"Pass a callable."
            )
        if not callable(callback):
            raise BusSubscriptionError(
                f"event_bus.subscribe({event_name!r}): callback {callback!r} "
                f"is not callable (type: {type(callback).__name__})."
            )

        # Lambda detection — warn but do not reject.
        if inspect.isfunction(callback) and callback.__code__.co_name == "<lambda>":
            _log.warning(
                "event_bus.subscribe(%r): callback is a lambda. "
                "Lambdas may be garbage-collected immediately if the caller "
                "does not hold a reference. Consider using a named function "
                "or storing the lambda in a variable with a longer lifetime.",
                event_name,
            )

        wc = _WeakCallback(callback)

        with self._lock:
            listeners = self._subscribers.get(event_name)

            if listeners is None:
                # New event name — check global event type cap.
                if len(self._subscribers) >= _MAX_EVENT_TYPES:
                    raise BusCapacityError(
                        f"event_bus.subscribe({event_name!r}): "
                        f"maximum unique event types ({_MAX_EVENT_TYPES}) reached. "
                        f"Cannot register a new event name. "
                        f"Registered: {sorted(self._subscribers.keys())}."
                    )
                listeners = []
                self._subscribers[event_name] = listeners

            # Prune dead refs before duplicate check and capacity check.
            # Slice assignment is O(N) — avoids repeated list.remove() calls.
            before = len(listeners)
            listeners[:] = [wc_i for wc_i in listeners if wc_i.is_alive()]
            self._total_dead_pruned += before - len(listeners)

            # Duplicate check.
            if wc in listeners:
                self._total_duplicates_rejected += 1
                _log.debug(
                    "event_bus.subscribe(%r): duplicate callback ignored: %r.",
                    event_name, callback,
                )
                return

            # Per-event capacity check.
            if len(listeners) >= _MAX_LISTENERS_PER_EVENT:
                raise BusCapacityError(
                    f"event_bus.subscribe({event_name!r}): "
                    f"maximum listeners per event ({_MAX_LISTENERS_PER_EVENT}) reached. "
                    f"Cannot add another subscriber."
                )

            listeners.append(wc)
            _log.debug(
                "event_bus: subscribed %r → %r (total listeners: %d).",
                event_name, callback, len(listeners),
            )

    # ── Unsubscribe ───────────────────────────────────────────────────────────

    def unsubscribe(self, event_name: str, callback: Callable) -> bool:
        """
        Remove a previously registered callback for event_name.

        Returns True if the callback was found and removed.
        Returns False if not found — does NOT raise (safe for cleanup code).

        Thread safety: guarded by RLock.
        """
        if not isinstance(event_name, str) or not event_name:
            _log.warning(
                "event_bus.unsubscribe(): invalid event_name %r. No-op.", event_name
            )
            return False
        if not callable(callback):
            _log.warning(
                "event_bus.unsubscribe(%r): callback is not callable: %r. No-op.",
                event_name, callback,
            )
            return False

        wc_target = _WeakCallback(callback)

        with self._lock:
            listeners = self._subscribers.get(event_name)
            if not listeners:
                _log.debug(
                    "event_bus.unsubscribe(%r): no listeners registered for this event.",
                    event_name,
                )
                return False

            original_len = len(listeners)
            # Remove by equality (uses _WeakCallback.__eq__ → identity comparison).
            self._subscribers[event_name] = [
                wc for wc in listeners if wc != wc_target
            ]
            removed = original_len - len(self._subscribers[event_name])

            # Clean up empty event slots to keep the dict tidy.
            if not self._subscribers[event_name]:
                del self._subscribers[event_name]

            if removed > 0:
                _log.debug(
                    "event_bus: unsubscribed %r from %r.", callback, event_name
                )
                return True

            _log.debug(
                "event_bus.unsubscribe(%r): callback %r not found.",
                event_name, callback,
            )
            return False

    # ── Emit ──────────────────────────────────────────────────────────────────

    def emit(self, event_name: str, payload: Any = None) -> int:
        """
        Emit event_name to all live subscribers.

        DISPATCH PROTOCOL:
            1. Acquire lock — get listeners, prune dead refs, copy snapshot.
            2. Release lock — dispatch happens OUTSIDE the lock.
            3. Call each resolved callback(payload).
            4. Exceptions caught per-callback — logged, counted, never re-raised.
            5. Return count of successfully called listeners.

        This guarantees:
            - Callbacks can call subscribe()/unsubscribe()/emit() without deadlock.
            - New subscriptions during emit() are NOT called in the current emit.
            - One crashing callback does not prevent others from being called.

        Args:
            event_name: Name of the event to emit.
            payload:    Optional data dict or any plain value to pass to callbacks.
                        Convention: use a plain dict with string keys.

        Returns:
            Number of callbacks successfully invoked (not counting errors).

        Thread safety: lock acquired only for snapshot; dispatch is lock-free.
        """
        if not isinstance(event_name, str) or not event_name:
            _log.warning(
                "event_bus.emit(): invalid event_name %r. No-op.", event_name
            )
            return 0

        # Step 1–2: acquire lock, prune, snapshot, release.
        pruned   = 0
        snapshot: List[_WeakCallback] = []
        with self._lock:
            self._total_emits += 1          # Count every emit, including no-listener ones.
            listeners = self._subscribers.get(event_name)
            if not listeners:
                return 0

            # Prune dead weakrefs in-place under lock. Slice assignment is O(N).
            before = len(listeners)
            listeners[:] = [wc for wc in listeners if wc.is_alive()]
            pruned = before - len(listeners)
            self._total_dead_pruned += pruned

            # Clean up empty event slots.
            if not listeners:
                del self._subscribers[event_name]
                return 0

            # Capture snapshot — dispatch will iterate this, not the live list.
            snapshot = list(listeners)

        # Step 3–4: dispatch outside the lock.
        delivered = 0
        t0        = time.perf_counter()

        for wc in snapshot:
            cb = wc.resolve()
            if cb is None:
                # Went dead between snapshot and resolve — skip silently.
                continue
            try:
                cb(payload)
                delivered += 1
            except Exception as exc:
                self._record_callback_error(event_name, wc, exc)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        with self._lock:
            self._total_deliveries += delivered

        if delivered > 0 or pruned:
            _log.debug(
                "event_bus: emitted %r → %d delivered, %d dead pruned (%.2fms).",
                event_name, delivered, pruned, elapsed_ms,
            )

        return delivered

    # ── Clear ─────────────────────────────────────────────────────────────────

    def clear(self) -> int:
        """
        Remove all subscriptions for all events.

        Returns the total number of listener slots cleared.
        Called by reset_bus() before dropping the module-level reference.
        Thread safety: guarded by RLock.
        """
        with self._lock:
            total = sum(len(v) for v in self._subscribers.values())
            self._subscribers.clear()
            _log.debug("event_bus: cleared %d listener slot(s).", total)
            return total

    # ── Stats ─────────────────────────────────────────────────────────────────

    def build_stats(self) -> EventBusStats:
        """Construct an immutable EventBusStats snapshot from current state."""
        with self._lock:
            per_event = {
                name: len(listeners)
                for name, listeners in self._subscribers.items()
            }
            total_listeners = sum(per_event.values())
            err_rate = (
                self._total_errors / self._total_deliveries
                if self._total_deliveries > 0
                else 0.0
            )
            recent_errors = list(self._recent_errors)   # copy under lock

        return EventBusStats(
            ts                        = _ts_now(),
            initialized               = True,
            session_age_s             = round(time.monotonic() - self._created_at, 2),
            registered_events         = len(per_event),
            total_listeners           = total_listeners,
            listeners_per_event       = per_event,
            total_emits               = self._total_emits,
            total_deliveries          = self._total_deliveries,
            total_errors              = self._total_errors,
            total_dead_pruned         = self._total_dead_pruned,
            total_duplicates_rejected = self._total_duplicates_rejected,
            error_rate                = round(err_rate, 4),
            recent_errors             = recent_errors,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _record_callback_error(
        self,
        event_name: str,
        wc:         _WeakCallback,
        exc:        Exception,
    ) -> None:
        """Log and count a callback exception. Never re-raises."""
        tb_str = traceback.format_exc()
        _log.error(
            "event_bus: callback error for event %r, callback %r: %s\n%s",
            event_name, wc, exc, tb_str,
        )
        record = {
            "ts":         _ts_now(),
            "event_name": event_name,
            "callback":   repr(wc),
            "error":      repr(exc),
        }
        with self._lock:
            self._total_errors += 1
            self._recent_errors.append(record)
            if len(self._recent_errors) > _MAX_RECENT_ERRORS:
                self._recent_errors.pop(0)


# ──────────────────────────────────────────────────────────────────────────────
# TIMESTAMP HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _ts_now() -> str:
    """Return a millisecond-precision wall-clock timestamp: YYYY-MM-DD HH:MM:SS.mmm"""
    t  = time.time()
    ms = int((t % 1.0) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


# ──────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ──────────────────────────────────────────────────────────────────────────────

_bus: Optional[_EventBus] = None


def _get_bus(caller: str = "") -> _EventBus:
    """
    Internal: return the active bus, raising if not initialized.
    All public write/emit functions use this — a missing bus is a
    programming error that must fail loudly.
    """
    if _bus is None:
        raise BusNotInitializedError(
            f"event_bus: not initialized (caller={caller!r}). "
            "Call event_bus.initialize() from lifecycle.startup() first."
        )
    return _bus


def _destroy_bus() -> None:
    """
    Clear the bus and drop the module-level reference.
    Drop-first pattern: racing code sees None immediately and raises
    BusNotInitializedError rather than accessing partially-cleared state.
    """
    global _bus
    old  = _bus
    _bus = None
    if old is not None:
        count = old.clear()
        _log.debug("event_bus: _destroy_bus(): cleared %d listener(s).", count)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC LIFECYCLE API
# ──────────────────────────────────────────────────────────────────────────────

def initialize() -> None:
    """
    Initialize the event bus for a new addon lifecycle.

    MUST be called from lifecycle.startup() before any subscribe() or emit().
    Idempotent: if already initialized, destroys the stale bus and creates
    a new one (guards against double-startup; logs WARNING).

    F8 / HOT-RELOAD SAFETY:
        F8 re-executes ``_bus = None`` at module level. initialize() calls
        _destroy_bus() before constructing a new instance, guaranteeing no
        stale subscriptions leak across reload cycles.

    Raises: Nothing — errors are logged and suppressed to prevent bus
    initialization from blocking addon startup.
    """
    global _bus

    if _bus is not None:
        _log.warning(
            "event_bus.initialize(): already initialized (double-init without "
            "reset_bus()). Destroying stale bus before reinitializing."
        )
        _destroy_bus()

    try:
        _bus = _EventBus()
        _log.debug("event_bus: initialized.")
    except Exception as exc:
        _log.error(
            "event_bus.initialize() failed: %s. "
            "Event bus will be unavailable this session.",
            exc,
        )


def reset_bus() -> None:
    """
    Destroy the current event bus and allow initialize() to create a new one.

    MUST be called from lifecycle.shutdown(). Idempotent: safe to call when
    already reset. Explicitly clears all subscriptions before dropping the
    reference — deterministic memory release, no GC cycle dependency.

    After reset():
        - All subscriptions are cleared.
        - subscribe()/emit() raise BusNotInitializedError.
        - get_stats() returns a safe uninitialized snapshot.
    """
    if _bus is None:
        _log.debug("event_bus.reset_bus(): already reset. No-op.")
        return
    _destroy_bus()
    _log.debug("event_bus: reset complete.")


def is_initialized() -> bool:
    """Return True if initialize() has been called and reset_bus() has not."""
    return _bus is not None


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

def subscribe(event_name: str, callback: Callable) -> None:
    """
    Register callback to be called when event_name is emitted.

    The callback signature must accept one argument (payload):
        def on_cache_invalidated(payload):
            tier = (payload or {}).get("tier")

    Callbacks are held via weakref. If the caller drops the reference to
    the callable (or its owning object), the subscription is silently
    pruned on the next emit(). Store a reference to avoid this.

    Args:
        event_name: Name of the event to subscribe to. Use EventName constants.
        callback:   Callable(payload) → None. Must not be None.

    Raises:
        BusNotInitializedError: Bus not initialized.
        BusSubscriptionError:   Invalid event_name or callback.
        BusCapacityError:       Event type or listener cap reached.

    Example:
        event_bus.subscribe(EventName.CACHE_INVALIDATED, on_cache_invalidated)
        event_bus.subscribe("CUSTOM_EVENT", my_handler)
    """
    _get_bus("subscribe").subscribe(event_name, callback)


def unsubscribe(event_name: str, callback: Callable) -> bool:
    """
    Remove a previously registered callback for event_name.

    Safe to call even if the callback is not registered — returns False.
    Safe to call from finally blocks and cleanup code without extra guards.

    Args:
        event_name: Event name the callback was subscribed to.
        callback:   The same callable object passed to subscribe().

    Returns:
        True  — callback was found and removed.
        False — callback was not registered (no-op).

    Raises:
        BusNotInitializedError: Bus not initialized.

    Example:
        event_bus.unsubscribe(EventName.CACHE_INVALIDATED, on_cache_invalidated)
    """
    return _get_bus("unsubscribe").unsubscribe(event_name, callback)


def emit(event_name: str, payload: Any = None) -> int:
    """
    Emit event_name to all live subscribers, passing payload to each.

    Dispatch is synchronous and single-threaded. All callbacks run before
    emit() returns. Exceptions in callbacks are caught, logged, and counted
    — they do NOT propagate to the caller and do NOT prevent other callbacks
    from being called.

    Callbacks added during emit() are NOT called in the current dispatch
    (snapshot-based dispatch pattern).

    Args:
        event_name: Name of the event to emit. Use EventName constants.
        payload:    Optional data to pass to callbacks. Convention: plain dict.
                    Example: {"tier": "L2", "reason": "undo", "count": 3}

    Returns:
        Number of callbacks successfully invoked (errors not counted).

    Raises:
        BusNotInitializedError: Bus not initialized.

    Examples:
        event_bus.emit(EventName.CACHE_INVALIDATED, {"tier": "L2"})
        event_bus.emit(EventName.SESSION_RESET)
        event_bus.emit(EventName.ANIMATION_CHANGED, {
            "armature": "Armature",
            "reason":   "undo_post",
        })
    """
    return _get_bus("emit").emit(event_name, payload)


def clear() -> int:
    """
    Remove all subscriptions for all events.

    Intended for testing and emergency cleanup. In normal operation,
    prefer reset_bus() which also destroys the bus instance.

    Returns the number of listener slots cleared.

    Raises:
        BusNotInitializedError: Bus not initialized.
    """
    return _get_bus("clear").clear()


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_stats() -> EventBusStats:
    """
    Return an immutable snapshot of event bus state.

    All fields are plain Python types — safe to log, serialize, or display
    in a Blender panel. No live callbacks or weakrefs exposed.

    If called before initialize(), returns a safe "not initialized" snapshot
    rather than raising. Diagnostic code can call this at any time.

    Raises: Never.
    """
    b = _bus
    if b is None:
        return EventBusStats(
            ts                        = _ts_now(),
            initialized               = False,
            session_age_s             = 0.0,
            registered_events         = 0,
            total_listeners           = 0,
            listeners_per_event       = {},
            total_emits               = 0,
            total_deliveries          = 0,
            total_errors              = 0,
            total_dead_pruned         = 0,
            total_duplicates_rejected = 0,
            error_rate                = 0.0,
            recent_errors             = [],
        )
    try:
        return b.build_stats()
    except Exception as exc:
        _log.error("event_bus.get_stats() failed: %s", exc)
        return EventBusStats(
            ts                        = _ts_now(),
            initialized               = True,
            session_age_s             = 0.0,
            registered_events         = 0,
            total_listeners           = 0,
            listeners_per_event       = {},
            total_emits               = 0,
            total_deliveries          = 0,
            total_errors              = 0,
            total_dead_pruned         = 0,
            total_duplicates_rejected = 0,
            error_rate                = 0.0,
            recent_errors             = [{"error": str(exc)}],
        )


def dump_stats_to_log() -> None:
    """
    Emit a full event bus stats report to the logger at INFO level.

    For "Copy Debug Info" button, bug reports, and console diagnostics.
    NOT for use in handlers or tight loops.
    """
    stats = get_stats()
    SEP   = "─" * 56

    _log.info("=" * 56)
    _log.info("Onixey Event Bus — Stats Snapshot")
    _log.info(SEP)
    _log.info("  initialized          : %s", stats.initialized)
    _log.info("  session_age_s        : %.1f", stats.session_age_s)
    _log.info("  registered_events    : %d", stats.registered_events)
    _log.info("  total_listeners      : %d", stats.total_listeners)
    _log.info("  total_emits          : %d", stats.total_emits)
    _log.info("  total_deliveries     : %d", stats.total_deliveries)
    _log.info("  total_errors         : %d (rate=%.2f%%)",
              stats.total_errors, stats.error_rate * 100)
    _log.info("  total_dead_pruned    : %d", stats.total_dead_pruned)
    _log.info("  duplicates_rejected  : %d", stats.total_duplicates_rejected)

    if stats.listeners_per_event:
        _log.info(SEP)
        _log.info("  LISTENERS PER EVENT:")
        for name, count in sorted(stats.listeners_per_event.items()):
            _log.info("    %-40s : %d", name, count)

    if stats.recent_errors:
        _log.info(SEP)
        _log.info("  RECENT ERRORS (%d):", len(stats.recent_errors))
        for err in stats.recent_errors[-5:]:   # Show last 5.
            _log.info("    [%s] %s → %s",
                      err.get("ts", "?"),
                      err.get("event_name", "?"),
                      err.get("error", "?"))

    _log.info("=" * 56)
