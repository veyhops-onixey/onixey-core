"""
onixey3/runtime/context.py

Runtime Context — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Lightweight, isolated carrier of transient runtime state shared between
Onixey subsystems during a single execution flow (operator call, analysis
pass, migration run, reload cycle).

The canonical problem this module solves: deep call chains between
analysis/, operators/, and runtime/ modules end up passing the same 6–10
parameters through every function signature. RuntimeContext replaces that
with a single typed, validated, bounded container that every module in the
same execution flow can read from and write to without importing each other.

Think of it as a scoped message envelope, not a global god-object. It is
created at the start of an execution flow, populated as execution proceeds,
read by downstream systems, and discarded when the flow ends.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT import bpy (at any scope).
    - Does NOT register handlers, operators, or panels.
    - Does NOT modify Scene, bpy.types, FCurves, or NLA data.
    - Does NOT call frame_set() or any animation-evaluation API.
    - Does NOT write to disk.
    - Does NOT execute analysis or correction logic.
    - Does NOT import: handlers, lifecycle, reload_manager, state, session.
    - Does NOT produce any side effects on import.
    - Does NOT store references to live Blender objects (Object, Action, etc.).

ARCHITECTURE POSITION
─────────────────────
    Dependency direction: context.py ← (cache, session, analysis, operators)
    context.py imports from: nothing (zero runtime dependencies)

    runtime/
    ├── cache.py            reads/writes context fields during cache operations
    ├── session.py          populates context on session state changes
    ├── metrics.py          reads context for event tagging (optional)
    ├── exceptions.py       attaches context snapshot to exception payloads
    └── context.py          THIS FILE — zero imports from siblings

    analysis/               reads active_rig, active_analysis, feature_flags
    operators/              creates context per execute(), passes to analysis

PUBLIC API
──────────
    initialize(initial_values?)         → None
    reset_context()                     → None
    get_context()                       → RuntimeContext
    set_context_value(key, value)       → None
    get_context_value(key, default?)    → PrimitiveValue
    remove_context_value(key)           → bool
    clear_context()                     → None
    get_context_snapshot()              → RuntimeContextSnapshot
    is_initialized()                    → bool

ALLOWED VALUE TYPES
────────────────────
    PrimitiveValue = str | int | float | bool | None | dict | list

    Only plain Python primitives and JSON-serializable containers are allowed
    as context values. Blender objects (Object, Action, PoseBone, FCurve),
    callables, and class instances are rejected at write time with a
    TypeError. This guarantee makes snapshots safe to log and serialize.

LIFECYCLE
─────────
    Flow entry point (operator, analysis pass, reload):
        initialize({"session_id": "abc", "active_rig": "Rig001", ...})

    During execution (any module in the flow):
        set_context_value("active_analysis", "spacing")
        val = get_context_value("active_rig")

    Flow exit / shutdown:
        reset_context()   ← called by lifecycle.shutdown() or operator finally block

    The module-level singleton (_ctx) is None between flows. Accessing it
    via get_context() before initialize() raises RuntimeContextNotInitialized.
    All other public functions (get_context_value, get_context_snapshot, etc.)
    return safe defaults when uninitialized rather than raising.

MEMORY BEHAVIOR
───────────────
    All context values are stored in a single dict bounded by _MAX_ENTRIES.
    Attempting to write beyond _MAX_ENTRIES raises RuntimeContextOverflow.
    Eviction is NOT automatic — callers must manage context scope explicitly.

    A RuntimeContextSnapshot is an immutable copy of the context dict at a
    point in time. It holds no references to the live _ctx singleton and is
    safe to store indefinitely.

RELOAD / F8 SAFETY
──────────────────
    The module-level ``_ctx: Optional[RuntimeContext] = None`` line re-executes
    on F8, resetting the name binding. Any live RuntimeContext held by other
    modules (e.g., a running operator) becomes unreachable from this module
    but remains valid Python — it will be GC'd when the last reference drops.

    _destroy_ctx() explicitly clears the internal dict before dropping the
    reference, mirroring the pattern in metrics.py, to give CPython's
    reference counter an unambiguous signal.

CHANGELOG
─────────
    3.1.0 — Initial implementation.
             RuntimeContext, RuntimeContextSnapshot, singleton lifecycle,
             primitive-only type enforcement, bounded capacity,
             full public API with safe defaults.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Union

_log = logging.getLogger(__name__)

# ── Allowed value types ───────────────────────────────────────────────────────

# PrimitiveValue: the only types allowed as context values.
# Using Union rather than a Protocol keeps compatibility with Python 3.9 (Blender 4.2).
PrimitiveValue = Union[str, int, float, bool, None, dict, list]

# frozenset of the raw Python types accepted by _validate_value().
_ALLOWED_TYPES: FrozenSet[type] = frozenset({
    str, int, float, bool, type(None), dict, list,
})

# ── Capacity constants ────────────────────────────────────────────────────────

_MAX_ENTRIES:    int = 64    # Hard cap on number of context keys.
_MAX_KEY_LENGTH: int = 128   # Maximum character length for a context key.
_MAX_STR_VALUE:  int = 4096  # Maximum character length for a string value.


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _ts_now() -> str:
    """Millisecond-precision wall-clock timestamp. Format: YYYY-MM-DD HH:MM:SS.mmm"""
    t  = time.time()
    ms = int((t % 1.0) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


# ══════════════════════════════════════════════════════════════════════════════
# EXCEPTIONS  (defined here to avoid importing exceptions.py → circular risk)
# ══════════════════════════════════════════════════════════════════════════════

class RuntimeContextError(RuntimeError):
    """Base for all context-subsystem errors."""


class RuntimeContextNotInitialized(RuntimeContextError):
    """
    Raised by get_context() when no context has been initialized.

    Recovery: call initialize() at the start of the execution flow.
    """
    def __init__(self, caller: str = "") -> None:
        loc = f" (caller: {caller})" if caller else ""
        super().__init__(
            f"RuntimeContext is not initialized{loc}. "
            "Call context.initialize() before accessing the context."
        )


class RuntimeContextTypeError(RuntimeContextError, TypeError):
    """
    Raised when a value of a disallowed type is written to the context.

    Only str, int, float, bool, None, dict, and list are permitted.
    Blender objects, callables, and class instances are rejected.
    """
    def __init__(self, key: str, value: Any) -> None:
        super().__init__(
            f"Context value for key '{key}' has disallowed type "
            f"'{type(value).__name__}'. "
            f"Allowed types: str, int, float, bool, None, dict, list. "
            f"Do NOT store live Blender objects in the context."
        )


class RuntimeContextOverflow(RuntimeContextError):
    """
    Raised when set_context_value() would exceed _MAX_ENTRIES.

    Recovery: remove unused keys with remove_context_value() or call
    clear_context() before populating a new flow's context.
    """
    def __init__(self, key: str, current: int, maximum: int) -> None:
        super().__init__(
            f"Context capacity exceeded: cannot add key '{key}'. "
            f"Current entries: {current}, maximum: {maximum}. "
            "Call remove_context_value() or clear_context() to free space."
        )


class RuntimeContextKeyError(RuntimeContextError, KeyError):
    """Raised by get_context_value() when a required key is absent and no default given."""
    def __init__(self, key: str) -> None:
        super().__init__(
            f"Context key '{key}' not found. "
            "Use get_context_value(key, default=...) to provide a fallback."
        )


# ══════════════════════════════════════════════════════════════════════════════
# WELL-KNOWN KEYS
# ══════════════════════════════════════════════════════════════════════════════

class ContextKey:
    """
    Namespace of well-known context key strings.

    Using these constants prevents typo-based key mismatches between modules
    that write and modules that read the same context field.

    Callers are free to use arbitrary string keys for their own fields.
    Well-known keys are defined here to serve as a canonical contract between
    the analysis pipeline, operators, and the runtime subsystems.

    Convention: SCREAMING_SNAKE_CASE attribute = lowercase_snake_case value.
    """

    # ── Session identity ──────────────────────────────────────────────────────
    SESSION_ID:        str = "session_id"
    """Unique string ID for the current Onixey session (e.g. UUID or monotonic int)."""

    # ── Active rig ────────────────────────────────────────────────────────────
    ACTIVE_RIG:        str = "active_rig"
    """Name (str) of the currently targeted armature object."""

    ACTIVE_ACTION:     str = "active_action"
    """Name (str) of the currently targeted bpy.types.Action."""

    # ── Analysis ──────────────────────────────────────────────────────────────
    RUNTIME_MODE:      str = "runtime_mode"
    """
    Execution mode for the current flow.
    Valid values: "ANALYZE", "CORRECT", "MIGRATE", "RELOAD", "IDLE".
    """

    ACTIVE_ANALYSIS:   str = "active_analysis"
    """
    Name of the analysis module currently executing.
    E.g.: "arc", "spacing", "energy", "ikfk", "euler".
    """

    FRAME_START:       str = "frame_start"
    """int — first frame of the analysis range."""

    FRAME_END:         str = "frame_end"
    """int — last frame of the analysis range."""

    FRAME_CURRENT:     str = "frame_current"
    """int — frame at context creation time (for logging, not for frame_set)."""

    # ── Feature flags snapshot ────────────────────────────────────────────────
    FEATURE_FLAGS:     str = "feature_flags"
    """
    dict — shallow copy of the feature_flags snapshot at context creation time.
    Populated by the entry-point operator so downstream analysis modules do not
    need to call feature_flags.get_flags() directly.
    """

    # ── Reload ────────────────────────────────────────────────────────────────
    RELOAD_GENERATION: str = "reload_generation"
    """int — reload generation counter value at context creation time."""

    # ── Error tracking ────────────────────────────────────────────────────────
    LAST_ERROR:        str = "last_error"
    """str — human-readable description of the most recent error in this flow."""

    ABORT_REQUESTED:   str = "abort_requested"
    """bool — True if the current execution flow was asked to stop early."""


# ══════════════════════════════════════════════════════════════════════════════
# VALUE VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

def _validate_value(key: str, value: Any) -> None:
    """
    Raise RuntimeContextTypeError if value is not an allowed primitive type.

    RECURSIVE CONTAINER CHECK
    ──────────────────────────
    For dict and list values, we perform a shallow type check of their
    immediate contents. Deep recursive validation would be O(N) in nesting
    depth and is not warranted for context data — callers are responsible
    for not nesting Blender objects inside containers.

    KEY LENGTH CHECK
    ─────────────────
    Keys must be non-empty strings of at most _MAX_KEY_LENGTH characters.
    This prevents accidental use of repr(some_object) as a key.
    """
    if not isinstance(key, str) or not key:
        raise RuntimeContextTypeError(key, key)
    if len(key) > _MAX_KEY_LENGTH:
        raise RuntimeContextTypeError(
            key[:32] + "...",
            f"Key too long: {len(key)} chars (max {_MAX_KEY_LENGTH})",
        )

    value_type = type(value)
    if value_type not in _ALLOWED_TYPES:
        raise RuntimeContextTypeError(key, value)

    # Shallow check for dict values
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise RuntimeContextTypeError(
                    key,
                    f"dict key {k!r} is {type(k).__name__}, must be str",
                )
            if type(v) not in _ALLOWED_TYPES:
                raise RuntimeContextTypeError(
                    key,
                    f"dict['{k}'] has disallowed type {type(v).__name__}",
                )

    # Shallow check for list values
    if isinstance(value, list):
        for i, item in enumerate(value):
            if type(item) not in _ALLOWED_TYPES:
                raise RuntimeContextTypeError(
                    key,
                    f"list[{i}] has disallowed type {type(item).__name__}",
                )

    # String length guard
    if isinstance(value, str) and len(value) > _MAX_STR_VALUE:
        raise RuntimeContextTypeError(
            key,
            f"str value too long: {len(value)} chars (max {_MAX_STR_VALUE})",
        )


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME CONTEXT SNAPSHOT  (immutable, safe to hold indefinitely)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RuntimeContextSnapshot:
    """
    Immutable point-in-time copy of all context values.

    Returned by get_context_snapshot(). All values are plain Python
    primitives — safe to log, serialize to JSON, attach to exception
    payloads, or store in the forensic event log.

    No references to the live RuntimeContext singleton. Mutations to the
    live context after snapshot creation do NOT affect this object.

    Attributes:
        ts:       Millisecond-precision wall-clock at snapshot time.
        values:   Shallow copy of the context dict at snapshot time.
        size:     Number of entries at snapshot time.
        capacity: Maximum number of entries (_MAX_ENTRIES constant).
    """
    ts:       str
    values:   Dict[str, PrimitiveValue]
    size:     int
    capacity: int

    def get(self, key: str, default: PrimitiveValue = None) -> PrimitiveValue:
        """Read a value from the snapshot. Returns default if key absent."""
        return self.values.get(key, default)

    def as_dict(self) -> Dict[str, object]:
        """Return a plain dict representation for logging and serialization."""
        return {
            "ts":       self.ts,
            "size":     self.size,
            "capacity": self.capacity,
            "values":   dict(self.values),
        }


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

class RuntimeContext:
    """
    Transient, bounded carrier of runtime execution state.

    One instance per execution flow. Created by initialize(), accessed via
    get_context(), destroyed by reset_context() or shutdown.

    DESIGN PRINCIPLES
    ─────────────────
    1. Primitive values only. No Blender objects, no callables, no class
       instances. Enforced at write time by _validate_value().

    2. Bounded capacity. At most _MAX_ENTRIES keys at any time.
       RuntimeContextOverflow is raised on overflow rather than silently
       evicting data.

    3. No logic. This class stores data; it never executes analysis,
       calls bpy APIs, or modifies other runtime systems.

    4. Explicit lifecycle. initialize() / reset_context() are the only
       entry and exit points. There is no auto-cleanup on garbage collection.

    THREAD SAFETY
    ─────────────
    Blender is single-threaded. No locking is used or required.
    """

    __slots__ = ("_data", "_created_at", "_access_count")

    def __init__(self, initial_values: Optional[Dict[str, PrimitiveValue]] = None) -> None:
        """
        Create a RuntimeContext with optional initial values.

        Args:
            initial_values: dict of key→value pairs to populate immediately.
                            All values are validated — RuntimeContextTypeError
                            is raised if any value has a disallowed type.
                            RuntimeContextOverflow is raised if the dict
                            has more than _MAX_ENTRIES entries.

        Raises:
            RuntimeContextTypeError: if any value fails type validation.
            RuntimeContextOverflow:  if initial_values exceeds _MAX_ENTRIES.
        """
        self._data:         Dict[str, PrimitiveValue] = {}
        self._created_at:   float                    = time.monotonic()
        self._access_count: int                      = 0

        if initial_values:
            if len(initial_values) > _MAX_ENTRIES:
                raise RuntimeContextOverflow(
                    "<initial_values>",
                    len(initial_values),
                    _MAX_ENTRIES,
                )
            for k, v in initial_values.items():
                _validate_value(k, v)
                self._data[k] = v

    # ── Write API ─────────────────────────────────────────────────────────────

    def set(self, key: str, value: PrimitiveValue) -> None:
        """
        Write a key→value pair to the context.

        If the key already exists, its value is overwritten (no overflow check
        for updates). New keys are rejected when the context is at capacity.

        Args:
            key:   Non-empty string of at most _MAX_KEY_LENGTH chars.
            value: Primitive value — str, int, float, bool, None, dict, or list.

        Raises:
            RuntimeContextTypeError: key or value fails validation.
            RuntimeContextOverflow:  context is at capacity and key is new.
        """
        _validate_value(key, value)
        is_new = key not in self._data
        if is_new and len(self._data) >= _MAX_ENTRIES:
            raise RuntimeContextOverflow(key, len(self._data), _MAX_ENTRIES)
        self._data[key] = value
        _log.debug("RuntimeContext.set: [%s] = %r", key, value)

    def remove(self, key: str) -> bool:
        """
        Remove a key from the context.

        Args:
            key: The key to remove.

        Returns:
            True if the key existed and was removed.
            False if the key was not present (idempotent, does not raise).
        """
        if key in self._data:
            del self._data[key]
            _log.debug("RuntimeContext.remove: [%s]", key)
            return True
        return False

    def clear(self) -> int:
        """
        Remove all entries from the context dict.

        Returns:
            The number of entries that were cleared.

        Does NOT destroy the RuntimeContext instance — the singleton remains
        initialized and ready for new values. Use reset_context() to destroy
        the instance entirely.
        """
        count = len(self._data)
        self._data.clear()
        if count:
            _log.debug("RuntimeContext.clear: removed %d entries.", count)
        return count

    # ── Read API ──────────────────────────────────────────────────────────────

    def get(self, key: str, default: PrimitiveValue = None) -> PrimitiveValue:
        """
        Read a value by key, returning default if absent.

        Args:
            key:     The context key.
            default: Value to return if key is not present (default: None).

        Returns:
            The stored value, or default if the key is absent.
        """
        self._access_count += 1
        return self._data.get(key, default)

    def get_required(self, key: str, caller: str = "") -> PrimitiveValue:
        """
        Read a value by key, raising RuntimeContextKeyError if absent.

        Use when the calling code cannot proceed without this value:
            frame_start = ctx.get_required(ContextKey.FRAME_START, "arc_analysis")

        Args:
            key:    The context key.
            caller: Optional caller name for the exception message.

        Returns:
            The stored value.

        Raises:
            RuntimeContextKeyError if the key is not present.
        """
        self._access_count += 1
        if key not in self._data:
            raise RuntimeContextKeyError(key)
        return self._data[key]

    def has(self, key: str) -> bool:
        """Return True if key is present in the context."""
        return key in self._data

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Current number of entries."""
        return len(self._data)

    @property
    def is_empty(self) -> bool:
        """True if no entries are stored."""
        return not self._data

    @property
    def keys(self) -> List[str]:
        """Sorted list of current keys. Copy — mutations do not affect context."""
        return sorted(self._data.keys())

    @property
    def age_s(self) -> float:
        """Seconds since this RuntimeContext was created."""
        return round(time.monotonic() - self._created_at, 3)

    @property
    def access_count(self) -> int:
        """Total get()/get_required() calls on this instance."""
        return self._access_count

    def snapshot(self) -> RuntimeContextSnapshot:
        """
        Return an immutable copy of the current context state.

        The snapshot is decoupled from the live context — subsequent writes
        to the context do not affect it. Safe to store indefinitely.

        Returns:
            RuntimeContextSnapshot with ts, values (copy), size, capacity.
        """
        return RuntimeContextSnapshot(
            ts       = _ts_now(),
            values   = dict(self._data),
            size     = len(self._data),
            capacity = _MAX_ENTRIES,
        )

    def __repr__(self) -> str:
        return (
            f"RuntimeContext(size={self.size}/{_MAX_ENTRIES}, "
            f"age={self.age_s}s, accesses={self._access_count})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

_ctx: Optional[RuntimeContext] = None


def _destroy_ctx() -> None:
    """
    Explicitly clear the active context dict and drop the module reference.

    Mirrors the _destroy_store() pattern from metrics.py:
    clearing the dict before dropping the reference gives CPython's
    reference counter an immediate reclaim signal for all stored values,
    rather than waiting for a GC cycle.

    Sets ``_ctx = None`` first so that any racing read (impossible in
    single-threaded Blender, but defensive) becomes a safe no-op.
    """
    global _ctx
    c = _ctx
    _ctx = None
    if c is not None:
        try:
            c._data.clear()
        except Exception as exc:
            _log.error(
                "RuntimeContext._destroy_ctx(): "
                "error clearing internal dict (non-fatal): %s", exc,
            )


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def is_initialized() -> bool:
    """Return True if initialize() has been called and reset_context() has not."""
    return _ctx is not None


def initialize(
    initial_values: Optional[Dict[str, PrimitiveValue]] = None,
) -> None:
    """
    Create the RuntimeContext singleton for a new execution flow.

    IDEMPOTENCY / DOUBLE-INIT BEHAVIOR
    ────────────────────────────────────
    If a context already exists when initialize() is called:
        - The existing context is destroyed via _destroy_ctx() (explicit clear).
        - A new context is created with the supplied initial_values.
        - A WARNING is logged — double-init without reset_context() indicates
          a flow that forgot to clean up.

    This matches the behavior of metrics.initialize() and state.initialize():
    never silently ignore double-init, never leave a stale singleton alive.

    F8 / HOT-RELOAD SAFETY
    ───────────────────────
    F8 re-executes ``_ctx: Optional[RuntimeContext] = None``, resetting the
    name binding. The old context (if any) is unreferenced from this module
    but its _data dict is not explicitly cleared. By always going through
    initialize() → _destroy_ctx() → new RuntimeContext(), we guarantee that
    even a stale context held elsewhere has its _data cleared deterministically.

    Args:
        initial_values: Optional dict of key→value pairs. All values are
                        validated. RuntimeContextTypeError or
                        RuntimeContextOverflow raised on violation.

    Raises:
        RuntimeContextTypeError: if any value in initial_values is disallowed.
        RuntimeContextOverflow:  if initial_values exceeds _MAX_ENTRIES.
        All other errors are logged and suppressed (non-fatal for startup).
    """
    global _ctx

    if _ctx is not None:
        _log.warning(
            "onixey3.runtime.context.initialize(): context already exists "
            "(double-init without reset_context). Destroying stale context."
        )
        _destroy_ctx()

    try:
        _ctx = RuntimeContext(initial_values=initial_values)
        _log.debug(
            "RuntimeContext initialized with %d initial value(s).",
            len(initial_values) if initial_values else 0,
        )
    except (RuntimeContextTypeError, RuntimeContextOverflow):
        # Re-raise validation errors — callers must fix the data.
        raise
    except Exception as exc:
        _log.error(
            "onixey3.runtime.context.initialize() failed: %s. "
            "Context will be unavailable.",
            exc,
        )


def reset_context() -> None:
    """
    Destroy the current RuntimeContext singleton.

    Called at the end of an execution flow (operator finally block, shutdown,
    reload cycle completion). Ensures that context data from one flow does
    not leak into the next.

    IDEMPOTENT: calling when already reset emits DEBUG and returns immediately.

    After this call:
        - get_context() raises RuntimeContextNotInitialized.
        - get_context_value() returns its default argument safely.
        - get_context_snapshot() returns an empty snapshot.
        - is_initialized() returns False.
    """
    global _ctx
    if _ctx is None:
        _log.debug(
            "onixey3.runtime.context.reset_context(): already reset. No-op."
        )
        return
    count = _ctx.size
    _destroy_ctx()
    _log.debug(
        "RuntimeContext reset. Cleared %d context value(s).", count,
    )


def get_context() -> RuntimeContext:
    """
    Return the active RuntimeContext.

    Raises:
        RuntimeContextNotInitialized: if initialize() has not been called.

    Use this when the calling code requires a live context to proceed.
    For optional context access (e.g., logging helpers), prefer
    get_context_value() which returns a default rather than raising.
    """
    if _ctx is None:
        raise RuntimeContextNotInitialized(caller="get_context")
    return _ctx


def set_context_value(key: str, value: PrimitiveValue) -> None:
    """
    Write a single key→value pair to the active context.

    Silent no-op if context is not initialized (logs a DEBUG).
    This matches the pattern of metrics.record_event() — callers in
    execution flows should not crash if context was not set up.

    Args:
        key:   Non-empty string key, max _MAX_KEY_LENGTH chars.
        value: str, int, float, bool, None, dict, or list.

    Raises:
        RuntimeContextTypeError: value has a disallowed type.
        RuntimeContextOverflow:  context is at capacity and key is new.
    """
    if _ctx is None:
        _log.debug(
            "onixey3.runtime.context.set_context_value('%s'): "
            "context not initialized. Ignored.",
            key,
        )
        return
    _ctx.set(key, value)


def get_context_value(
    key:     str,
    default: PrimitiveValue = None,
) -> PrimitiveValue:
    """
    Read a single value from the active context.

    Returns default if the context is not initialized or the key is absent.
    Never raises — suitable for logging helpers and optional reads.

    Args:
        key:     The context key.
        default: Value to return if context is absent or key not found.

    Returns:
        The stored value, or default.
    """
    if _ctx is None:
        return default
    return _ctx.get(key, default)


def remove_context_value(key: str) -> bool:
    """
    Remove a single key from the active context.

    Args:
        key: The key to remove.

    Returns:
        True  — key existed and was removed.
        False — context not initialized, or key was not present.
    """
    if _ctx is None:
        _log.debug(
            "onixey3.runtime.context.remove_context_value('%s'): "
            "context not initialized. Ignored.",
            key,
        )
        return False
    return _ctx.remove(key)


def clear_context() -> int:
    """
    Remove all entries from the active context without destroying the singleton.

    Equivalent to calling remove_context_value() for every key, but O(1).
    The context remains initialized — set_context_value() works after this call.
    Use reset_context() to destroy the singleton entirely.

    Returns:
        Number of entries cleared. 0 if context is not initialized.
    """
    if _ctx is None:
        _log.debug(
            "onixey3.runtime.context.clear_context(): "
            "context not initialized. Ignored."
        )
        return 0
    return _ctx.clear()


def get_context_snapshot() -> RuntimeContextSnapshot:
    """
    Return an immutable snapshot of the current context state.

    Safe to call at any time — returns an empty snapshot if context
    is not initialized rather than raising.

    Returns:
        RuntimeContextSnapshot with ts, values, size, capacity.
        If context is not initialized: snapshot with empty values dict.

    This is the primary read API for diagnostic tools, exception payloads,
    and the forensic event log.
    """
    if _ctx is None:
        return RuntimeContextSnapshot(
            ts       = _ts_now(),
            values   = {},
            size     = 0,
            capacity = _MAX_ENTRIES,
        )
    return _ctx.snapshot()
