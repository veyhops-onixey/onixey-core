"""
onixey3/runtime/registry.py

Runtime Component Registry — Onixey V3 / Blender 4.2+

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSIBILITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A pure name→object directory for active runtime components.

This module acts as a phone book for the Onixey runtime:

    register_component("cache",   cache_instance)
    register_component("session", session_instance)
    component = get_component("cache")

It does NOT start components, does NOT stop them, does NOT know what
they do, and does NOT depend on any of them. It is a passive registry
of whatever lifecycle.py decides to put into it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT THIS MODULE DOES NOT DO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ✗ Does NOT import bpy at any level.
    ✗ Does NOT register Blender handlers, operators, or panels.
    ✗ Does NOT modify Scene, bpy.types, FCurves, or NLA data.
    ✗ Does NOT call frame_set().
    ✗ Does NOT write to disk.
    ✗ Does NOT import: handlers, lifecycle, reload_manager, state, session.
    ✗ Does NOT execute or invoke any registered component.
    ✗ Does NOT apply monkey patches to components.
    ✗ Does NOT produce any side effects on import.
    ✗ Does NOT interpret business logic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ARCHITECTURE POSITION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    runtime/
    ├── cache.py            ← registered as "cache"
    ├── session.py          ← registered as "session"
    ├── handlers.py         ← registered as "handlers"
    ├── state.py            ← registered as "state"
    ├── metrics.py          ← registered as "metrics"
    ├── diagnostics.py      ← registered as "diagnostics"
    ├── reload_manager.py   ← registered as "reload_manager"
    ├── guards.py           ← registered as "guards"
    ├── exceptions.py       ← (not a component — error types only)
    ├── lifecycle.py        ← CALLER: registers all of the above
    └── registry.py         ← THIS FILE (no imports from any of the above)

Dependency direction: lifecycle.py → registry.py.
                      registry.py → nothing in runtime/.

This module MAY be imported by diagnostics.py for snapshot display, but
diagnostics MUST NOT create a circular import through this module.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PUBLIC API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lifecycle:
    initialize()                → None
    reset_registry()            → None
    is_initialized()            → bool

Write:
    register_component(name, component, *, replace=False)  → None
    unregister_component(name)                             → bool

Read:
    get_component(name)         → Any | None
    get_component_or_raise(name)→ Any   (raises RegistryKeyError)
    has_component(name)         → bool
    list_components()           → List[str]

Diagnostics:
    get_registry_snapshot()     → RuntimeRegistrySnapshot

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SINGLETON LIFECYCLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Module import   → _registry = None (zero side effects)
    initialize()    → _registry = _ComponentRegistry()
    reset_registry()→ explicit clear + _registry = None

Calling register_component() / get_component() before initialize() raises
RegistryNotInitializedError. Callers (lifecycle.py) must always call
initialize() before using the registry.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MEMORY BEHAVIOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The registry stores object REFERENCES, not copies. Registered components
remain alive as long as they are in the registry. reset_registry() removes
all references, allowing CPython's reference counter to reclaim components
that have no other live references.

Hard capacity cap: _MAX_COMPONENTS (default 64). The runtime currently
has ~8 components — this cap prevents accidental unbounded growth from
code bugs (e.g., a loop calling register_component).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NAME RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Component names must satisfy _NAME_PATTERN:
    - Lowercase letters, digits, underscores only.
    - Must start with a letter.
    - No leading/trailing underscores.
    - 2–64 characters.
    - Examples: "cache", "session", "reload_manager", "l2_cache_v2"
    - Rejected: "", "Cache", "my-component", "_cache", "1session"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RELOAD / F8 SAFETY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
F8 re-executes ``_registry = None`` at module level, resetting the binding.
Any _ComponentRegistry alive before F8 still exists in RAM until its
refcount drops to zero. reset_registry() explicitly clears the component
dict before dropping the reference, guaranteeing deterministic release
even if external code holds a reference to the old registry object.

lifecycle.py is responsible for calling reset_registry() in its shutdown()
and for calling initialize() in its startup() — matching the pattern used
by all other runtime modules.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHANGELOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    3.1.0 — Initial implementation.
            Singleton registry, ComponentEntry dataclass, bounded dict,
            name validation, duplicate protection, snapshot API,
            RegistryNotInitializedError / RegistryKeyError /
            RegistryCapacityError / RegistryDuplicateError.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

_log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Hard cap on the number of simultaneously registered components.
# The Onixey runtime has ~8 components — 64 is a generous ceiling that
# prevents accidental unbounded growth (e.g. a loop calling register_component).
_MAX_COMPONENTS: int = 64

# Valid component name: lowercase letters/digits/underscores, starts with letter,
# no leading/trailing underscores, length 2–64.
_NAME_PATTERN: re.Pattern[str] = re.compile(r'^[a-z][a-z0-9_]{1,62}[a-z0-9]$|^[a-z]{2}$')

# Well-known component names used by lifecycle.py.
# Listed here for reference — registry does NOT enforce their presence.
KNOWN_COMPONENTS: FrozenSet[str] = frozenset({
    "cache",
    "session",
    "handlers",
    "state",
    "metrics",
    "diagnostics",
    "reload_manager",
    "guards",
})


# ──────────────────────────────────────────────────────────────────────────────
# ERROR TYPES
# ──────────────────────────────────────────────────────────────────────────────

class RegistryNotInitializedError(RuntimeError):
    """
    Raised when any registry operation is attempted before initialize()
    has been called, or after reset_registry() has been called.

    Recovery: call registry.initialize() before using any registry operation.
    This is typically done by lifecycle.startup().
    """


class RegistryKeyError(KeyError):
    """
    Raised by get_component_or_raise() when the requested component name
    is not registered.

    Prefer get_component() (returns None on miss) for non-critical paths.
    Use get_component_or_raise() in operators and analysis code where a
    missing component is a programming error that should fail loudly.
    """


class RegistryDuplicateError(ValueError):
    """
    Raised by register_component() when a component name is already registered
    and replace=False (the default).

    To intentionally replace a component (e.g., during hot-reload):
        register_component("cache", new_cache, replace=True)

    Using replace=True logs a WARNING — it should be an intentional, rare op.
    """


class RegistryCapacityError(RuntimeError):
    """
    Raised by register_component() when the registry has reached _MAX_COMPONENTS
    and a new component (with a new name) would exceed the cap.

    This indicates either:
        - A loop is accidentally calling register_component repeatedly.
        - The _MAX_COMPONENTS constant needs to be raised (unlikely — the
          runtime only has ~8 components).
    """


class RegistryNameError(ValueError):
    """
    Raised when a component name fails _NAME_PATTERN validation.

    Valid names: lowercase letters, digits, underscores; starts with letter;
    no leading/trailing underscores; length 2–64.

    Examples: "cache", "session", "l2_cache_v2"
    Invalid:  "", "Cache", "my-component", "_cache", "1session", "x"
    """


# ──────────────────────────────────────────────────────────────────────────────
# COMPONENT ENTRY  (immutable metadata envelope)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComponentEntry:
    """
    Immutable metadata envelope for a registered component.

    Wraps the component object with registration metadata.
    The component itself is stored as-is — registry does not copy or wrap it.

    Attributes:
        name:          Validated registration name (e.g. "cache").
        component:     The registered object. May be any type.
        registered_at: time.monotonic() timestamp at registration time.
        type_name:     type(component).__name__ at registration time.
                       Stored as string to avoid holding a type reference.
        replace_count: How many times this slot was replaced via replace=True.
                       Non-zero values indicate hot-reload or debug replace ops.
    """
    name:          str
    component:     Any
    registered_at: float
    type_name:     str
    replace_count: int = 0

    @property
    def age_s(self) -> float:
        """Seconds since this component was (last) registered."""
        return time.monotonic() - self.registered_at

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a plain-Python dict snapshot of this entry's metadata.
        Does NOT include the component object itself — only its type_name.
        Safe to log and serialize.
        """
        return {
            "name":          self.name,
            "type_name":     self.type_name,
            "registered_at": round(self.registered_at, 3),
            "age_s":         round(self.age_s, 2),
            "replace_count": self.replace_count,
        }


# ──────────────────────────────────────────────────────────────────────────────
# RUNTIME REGISTRY SNAPSHOT  (immutable read output)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuntimeRegistrySnapshot:
    """
    Immutable point-in-time snapshot of the registry state.

    Returned by get_registry_snapshot(). All fields are plain Python types —
    safe to log, serialize to JSON, or display in a Blender panel.
    No live object references. No callables.

    Attributes:
        ts:              Wall-clock timestamp at snapshot time (ms precision).
        initialized:     Whether the registry is active.
        component_count: Number of currently registered components.
        capacity:        Maximum allowed components (_MAX_COMPONENTS).
        entries:         List of per-component metadata dicts (no objects).
                         Each dict: {name, type_name, registered_at, age_s,
                                     replace_count}
        registered_names: Sorted list of all registered component names.
        unknown_names:    Names not in KNOWN_COMPONENTS (may indicate typos).
        missing_known:    KNOWN_COMPONENTS names not currently registered.
        total_registered: Cumulative registrations since initialize().
        total_replaced:   Cumulative replace=True operations since initialize().
        total_unregistered: Cumulative unregister_component() calls since init.
        session_age_s:    Seconds since initialize() was called.
    """
    ts:                  str
    initialized:         bool
    component_count:     int
    capacity:            int
    entries:             List[Dict[str, Any]]
    registered_names:    List[str]
    unknown_names:       List[str]
    missing_known:       List[str]
    total_registered:    int
    total_replaced:      int
    total_unregistered:  int
    session_age_s:       float


# ──────────────────────────────────────────────────────────────────────────────
# COMPONENT REGISTRY  (internal — not exported directly)
# ──────────────────────────────────────────────────────────────────────────────

class _ComponentRegistry:
    """
    Internal registry container. One instance per addon lifecycle.

    All access goes through the module-level public functions — never
    instantiate or access this class directly from outside this module.

    THREAD SAFETY: Blender is single-threaded. No locking used.

    MEMORY:
        - _entries: plain dict, bounded to _MAX_COMPONENTS.
        - ComponentEntry stores a direct reference to the component object.
          reset_registry() clears _entries, dropping all references.
    """

    __slots__ = (
        "_entries",
        "_created_at",
        "_total_registered",
        "_total_replaced",
        "_total_unregistered",
    )

    def __init__(self) -> None:
        self._entries:            Dict[str, ComponentEntry] = {}
        self._created_at:         float = time.monotonic()
        self._total_registered:   int   = 0
        self._total_replaced:     int   = 0
        self._total_unregistered: int   = 0

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_name(name: str) -> None:
        """
        Validate a component name against _NAME_PATTERN.

        Raises:
            RegistryNameError if name is not a non-empty string matching
            the pattern. Error message includes the rejected name and the
            full pattern for easy debugging.
        """
        if not isinstance(name, str):
            raise RegistryNameError(
                f"registry: component name must be a str, "
                f"got {type(name).__name__!r}: {name!r}"
            )
        if not _NAME_PATTERN.match(name):
            raise RegistryNameError(
                f"registry: invalid component name {name!r}. "
                f"Must be lowercase letters/digits/underscores, "
                f"start with a letter, length 2-64, no leading/trailing underscores. "
                f"Pattern: {_NAME_PATTERN.pattern}"
            )

    @staticmethod
    def _validate_component(component: Any, name: str) -> None:
        """
        Reject None components.

        Storing None is almost always a bug — the caller constructed the
        component incorrectly. A named error is clearer than a silent None
        flowing through get_component() downstream.

        Raises:
            ValueError if component is None.
        """
        if component is None:
            raise ValueError(
                f"registry: cannot register None as component {name!r}. "
                f"Ensure the component is fully constructed before registering."
            )

    # ── Write operations ─────────────────────────────────────────────────────

    def register(self, name: str, component: Any, replace: bool) -> None:
        """
        Register a component under name.

        Args:
            name:      Validated component name (e.g. "cache").
            component: The component object. Must not be None.
            replace:   If True and name already registered, replace silently
                       (logs WARNING). If False and name exists, raises
                       RegistryDuplicateError.

        Raises:
            RegistryNameError:      name fails pattern validation.
            ValueError:             component is None.
            RegistryDuplicateError: name already registered and replace=False.
            RegistryCapacityError:  registry is at _MAX_COMPONENTS capacity
                                    and name is not already present.
        """
        self._validate_name(name)
        self._validate_component(component, name)

        existing = self._entries.get(name)

        if existing is not None:
            if not replace:
                raise RegistryDuplicateError(
                    f"registry: component {name!r} is already registered "
                    f"(type: {existing.type_name}). "
                    f"Call unregister_component({name!r}) first, or use "
                    f"register_component({name!r}, ..., replace=True) to force-replace."
                )
            # replace=True: replace the existing entry.
            new_entry = ComponentEntry(
                name          = name,
                component     = component,
                registered_at = time.monotonic(),
                type_name     = type(component).__name__,
                replace_count = existing.replace_count + 1,
            )
            self._entries[name] = new_entry
            self._total_registered += 1
            self._total_replaced   += 1
            _log.warning(
                "registry: REPLACED component %r (was: %s, now: %s, replace_count=%d). "
                "This is unusual — expected only during hot-reload or debug.",
                name,
                existing.type_name,
                type(component).__name__,
                new_entry.replace_count,
            )
            return

        # New registration — check capacity.
        if len(self._entries) >= _MAX_COMPONENTS:
            raise RegistryCapacityError(
                f"registry: capacity limit ({_MAX_COMPONENTS}) reached. "
                f"Cannot register {name!r}. "
                f"Current components: {sorted(self._entries.keys())}. "
                f"If this is unexpected, check for a loop calling "
                f"register_component()."
            )

        entry = ComponentEntry(
            name          = name,
            component     = component,
            registered_at = time.monotonic(),
            type_name     = type(component).__name__,
            replace_count = 0,
        )
        self._entries[name] = entry
        self._total_registered += 1

        # Warn if name is not in KNOWN_COMPONENTS — may indicate a typo.
        if name not in KNOWN_COMPONENTS:
            _log.warning(
                "registry: registered unknown component %r (type: %s). "
                "Known names: %s. "
                "If intentional, add %r to KNOWN_COMPONENTS.",
                name,
                entry.type_name,
                sorted(KNOWN_COMPONENTS),
                name,
            )
        else:
            _log.debug(
                "registry: registered %r (type: %s).", name, entry.type_name
            )

    def unregister(self, name: str) -> bool:
        """
        Remove a component by name.

        Args:
            name: Component name to remove.

        Returns:
            True  if the component was found and removed.
            False if the name was not registered (logs DEBUG, does not raise).

        Raises:
            RegistryNameError: name fails pattern validation.
        """
        self._validate_name(name)

        entry = self._entries.pop(name, None)
        if entry is None:
            _log.debug(
                "registry: unregister_component(%r): not found. "
                "Already removed or never registered.",
                name,
            )
            return False

        self._total_unregistered += 1
        _log.debug(
            "registry: unregistered %r (type: %s, was_registered_for: %.2fs).",
            name, entry.type_name, entry.age_s,
        )
        return True

    # ── Read operations ───────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[Any]:
        """
        Return the component registered under name, or None if not found.

        Does NOT validate the name pattern — intentionally fast for hot paths.
        Returns None for both "not found" and "name is invalid".
        """
        entry = self._entries.get(name)
        return entry.component if entry is not None else None

    def get_or_raise(self, name: str) -> Any:
        """
        Return the component registered under name.

        Raises:
            RegistryNameError: name fails pattern validation.
            RegistryKeyError:  name is not registered.
        """
        self._validate_name(name)
        entry = self._entries.get(name)
        if entry is None:
            raise RegistryKeyError(
                f"registry: component {name!r} is not registered. "
                f"Registered components: {sorted(self._entries.keys())}."
            )
        return entry.component

    def has(self, name: str) -> bool:
        """Return True if name is currently registered. No validation — O(1)."""
        return name in self._entries

    def list_names(self) -> List[str]:
        """Return a sorted list of all currently registered component names."""
        return sorted(self._entries.keys())

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def build_snapshot(self) -> RuntimeRegistrySnapshot:
        """Construct an immutable RuntimeRegistrySnapshot from current state."""
        names = sorted(self._entries.keys())
        now   = time.monotonic()

        unknown = sorted(n for n in names if n not in KNOWN_COMPONENTS)
        missing = sorted(k for k in KNOWN_COMPONENTS if k not in self._entries)

        return RuntimeRegistrySnapshot(
            ts                 = _ts_now(),
            initialized        = True,
            component_count    = len(self._entries),
            capacity           = _MAX_COMPONENTS,
            entries            = [self._entries[n].to_dict() for n in names],
            registered_names   = names,
            unknown_names      = unknown,
            missing_known      = missing,
            total_registered   = self._total_registered,
            total_replaced     = self._total_replaced,
            total_unregistered = self._total_unregistered,
            session_age_s      = round(now - self._created_at, 2),
        )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def clear(self) -> int:
        """
        Remove all registered components and return the count of cleared entries.

        Called by reset_registry() before dropping the module-level reference.
        Explicitly clearing _entries drops all component references, allowing
        CPython's reference counter to reclaim them immediately rather than
        waiting for a GC cycle.
        """
        count = len(self._entries)
        self._entries.clear()
        return count


# ──────────────────────────────────────────────────────────────────────────────
# TIMESTAMP HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _ts_now() -> str:
    """
    Return a millisecond-precision wall-clock timestamp.
    Format: YYYY-MM-DD HH:MM:SS.mmm
    """
    t  = time.time()
    ms = int((t % 1.0) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


# ──────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ──────────────────────────────────────────────────────────────────────────────

_registry: Optional[_ComponentRegistry] = None


def _get_registry(caller: str = "") -> _ComponentRegistry:
    """
    Internal: return the active registry, raising if not initialized.

    All public write/read functions use this. Unlike metrics.py (which uses
    a silent no-op pattern for record_*), registry operations are always
    intentional and a missing registry is a programming error that must
    fail loudly.

    Raises:
        RegistryNotInitializedError if _registry is None.
    """
    if _registry is None:
        raise RegistryNotInitializedError(
            f"registry: not initialized (caller={caller!r}). "
            "Call registry.initialize() before using any registry operation. "
            "This is typically done by lifecycle.startup()."
        )
    return _registry


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC LIFECYCLE API
# ──────────────────────────────────────────────────────────────────────────────

def initialize() -> None:
    """
    Initialize the component registry for a new addon lifecycle.

    MUST be called from lifecycle.startup() before any register_component()
    or get_component() calls. Idempotent: if already initialized, logs a
    WARNING and destroys the stale registry before creating a new one
    (guards against double-startup without an intervening reset_registry()).

    F8 / HOT-RELOAD SAFETY
    ──────────────────────
    F8 re-executes ``_registry = None`` at module level, resetting the binding.
    Any _ComponentRegistry alive before F8 still exists in RAM if external
    code holds a reference. initialize() calls _destroy_registry() before
    constructing the new instance, guaranteeing no stale state leaks.

    Raises:
        Nothing. All errors are logged and re-raised only for unexpected
        exceptions in _ComponentRegistry.__init__() (should not occur).
    """
    global _registry

    if _registry is not None:
        _log.warning(
            "registry.initialize(): already initialized (double-init without "
            "reset_registry()). Destroying stale registry before reinitializing."
        )
        _destroy_registry()

    _registry = _ComponentRegistry()
    _log.debug("registry: initialized (capacity=%d).", _MAX_COMPONENTS)


def reset_registry() -> None:
    """
    Destroy the current registry and allow initialize() to create a new one.

    MUST be called from lifecycle.shutdown() during unregister() to ensure
    the next startup cycle begins with a clean registry.

    Idempotent: calling when already reset logs DEBUG and returns. Safe to
    call from exception handlers and cleanup paths without additional guards.

    MEMORY RELEASE
    ──────────────
    Explicitly calls _destroy_registry() which calls registry.clear() before
    dropping the reference. This removes all component object references,
    giving CPython's reference counter an unambiguous signal to reclaim
    components that have no other live references — rather than relying on
    the GC cycle to detect the dict graph.
    """
    global _registry

    if _registry is None:
        _log.debug("registry.reset_registry(): already reset. No-op.")
        return

    _destroy_registry()
    _log.debug("registry: reset complete.")


def is_initialized() -> bool:
    """
    Return True if initialize() has been called and reset_registry() has not.
    Does NOT raise. Always returns bool.
    """
    return _registry is not None


def _destroy_registry() -> None:
    """
    Explicitly clear registry contents and drop the module-level reference.

    This is the ONLY place that sets ``_registry = None``.
    Both initialize() and reset_registry() delegate to it.

    Pattern: drop the module-level reference FIRST so any racing code (e.g.
    a handler that fires during shutdown) sees _registry=None and raises
    RegistryNotInitializedError rather than accessing partially-cleared state.
    Then clear the dict on the old object to drop all component references.
    """
    global _registry
    old = _registry
    _registry = None
    if old is not None:
        count = old.clear()
        _log.debug("registry: _destroy_registry(): cleared %d component(s).", count)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC WRITE API
# ──────────────────────────────────────────────────────────────────────────────

def register_component(
    name:      str,
    component: Any,
    *,
    replace:   bool = False,
) -> None:
    """
    Register a component under the given name.

    This is the primary write operation. Call from lifecycle.startup() after
    each sub-system is initialized, before other modules need to look it up.

    Args:
        name:      Unique component name. Must match _NAME_PATTERN:
                   lowercase letters/digits/underscores, starts with letter,
                   length 2–64, no leading/trailing underscores.
                   Examples: "cache", "session", "reload_manager"
        component: The component object to register. Must not be None.
        replace:   (keyword-only) If True and name is already registered,
                   silently replace with a WARNING log. Default False.

    Raises:
        RegistryNotInitializedError: Registry not initialized.
        RegistryNameError:           name fails pattern validation.
        ValueError:                  component is None.
        RegistryDuplicateError:      name already registered and replace=False.
        RegistryCapacityError:       Registry at max capacity (_MAX_COMPONENTS).

    Examples:
        register_component("cache", cache_module)
        register_component("session", session_state)
        register_component("cache", new_cache, replace=True)   # hot-reload
    """
    _get_registry("register_component").register(name, component, replace)


def unregister_component(name: str) -> bool:
    """
    Remove a component from the registry by name.

    Safe to call even if the component is not registered — returns False
    and logs DEBUG. This makes it safe to call from cleanup paths without
    additional existence checks.

    Args:
        name: Component name to remove.

    Returns:
        True  — component was found and removed.
        False — component was not registered.

    Raises:
        RegistryNotInitializedError: Registry not initialized.
        RegistryNameError:           name fails pattern validation.

    Example:
        unregister_component("cache")
    """
    return _get_registry("unregister_component").unregister(name)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC READ API
# ──────────────────────────────────────────────────────────────────────────────

def get_component(name: str) -> Optional[Any]:
    """
    Return the component registered under name, or None if not found.

    This is the primary read operation for non-critical paths where a missing
    component is handled gracefully by the caller.

    Does NOT validate the name pattern for performance — returns None for
    both "not found" and "invalid name". Use has_component() + get_component()
    if you need to distinguish these cases.

    Args:
        name: Component name to look up.

    Returns:
        The registered component object, or None.

    Raises:
        RegistryNotInitializedError: Registry not initialized.

    Example:
        cache = get_component("cache")
        if cache is not None:
            cache.invalidate_all()
    """
    return _get_registry("get_component").get(name)


def get_component_or_raise(name: str) -> Any:
    """
    Return the component registered under name, or raise RegistryKeyError.

    Use in operators, analysis code, and any path where a missing component
    is a programming error that should fail immediately and loudly.
    Prefer get_component() for optional/degraded-path lookups.

    Args:
        name: Component name to look up.

    Returns:
        The registered component object.

    Raises:
        RegistryNotInitializedError: Registry not initialized.
        RegistryNameError:           name fails pattern validation.
        RegistryKeyError:            name is not registered.

    Example:
        cache = get_component_or_raise("cache")
        cache.invalidate_all()
    """
    return _get_registry("get_component_or_raise").get_or_raise(name)


def has_component(name: str) -> bool:
    """
    Return True if a component is registered under name.

    O(1) dict lookup. No name validation — returns False for invalid names.
    Safe to call in tight loops (e.g., checking before a conditional lookup).

    Args:
        name: Component name to check.

    Returns:
        True if registered, False otherwise.

    Raises:
        RegistryNotInitializedError: Registry not initialized.

    Example:
        if has_component("diagnostics"):
            diagnostics = get_component("diagnostics")
            diagnostics.report()
    """
    return _get_registry("has_component").has(name)


def list_components() -> List[str]:
    """
    Return a sorted list of all currently registered component names.

    Returns a new list on every call — safe to store and iterate without
    holding a reference into the registry.

    Raises:
        RegistryNotInitializedError: Registry not initialized.

    Example:
        names = list_components()
        # ["cache", "handlers", "metrics", "session", "state"]
    """
    return _get_registry("list_components").list_names()


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_registry_snapshot() -> RuntimeRegistrySnapshot:
    """
    Return an immutable snapshot of the current registry state.

    All fields are plain Python types — safe to log, serialize to JSON,
    or display in a Blender panel. No live object references.

    The snapshot includes:
        - Count and names of registered components.
        - Per-component metadata (name, type, age, replace_count).
        - unknown_names: components not in KNOWN_COMPONENTS (possible typos).
        - missing_known: KNOWN_COMPONENTS not currently registered (gaps).
        - Cumulative registration/replacement/unregistration counters.

    If called before initialize(), returns a minimal "not initialized" snapshot
    rather than raising. Allows diagnostic code to call this safely at any time.

    Raises:
        Never.

    Example:
        snap = get_registry_snapshot()
        if snap.missing_known:
            _log.warning("Missing components: %s", snap.missing_known)
    """
    r = _registry
    if r is None:
        return RuntimeRegistrySnapshot(
            ts                 = _ts_now(),
            initialized        = False,
            component_count    = 0,
            capacity           = _MAX_COMPONENTS,
            entries            = [],
            registered_names   = [],
            unknown_names      = [],
            missing_known      = sorted(KNOWN_COMPONENTS),
            total_registered   = 0,
            total_replaced     = 0,
            total_unregistered = 0,
            session_age_s      = 0.0,
        )
    try:
        return r.build_snapshot()
    except Exception as exc:
        _log.error("registry.get_registry_snapshot() failed: %s", exc)
        return RuntimeRegistrySnapshot(
            ts                 = _ts_now(),
            initialized        = True,
            component_count    = 0,
            capacity           = _MAX_COMPONENTS,
            entries            = [],
            registered_names   = [],
            unknown_names      = [],
            missing_known      = [],
            total_registered   = 0,
            total_replaced     = 0,
            total_unregistered = 0,
            session_age_s      = 0.0,
        )


def dump_snapshot_to_log() -> None:
    """
    Emit a full registry snapshot to the Onixey logger at INFO level.

    For "Copy Debug Info" button, bug reports, and console diagnostics.
    NOT for use in handlers or hot paths.
    """
    snap = get_registry_snapshot()
    SEP  = "─" * 56

    _log.info("=" * 56)
    _log.info("Onixey Runtime Registry — Snapshot")
    _log.info(SEP)
    _log.info("  initialized      : %s",  snap.initialized)
    _log.info("  component_count  : %d / %d", snap.component_count, snap.capacity)
    _log.info("  session_age_s    : %.1f", snap.session_age_s)
    _log.info("  total_registered : %d",  snap.total_registered)
    _log.info("  total_replaced   : %d",  snap.total_replaced)
    _log.info("  total_unregistered: %d", snap.total_unregistered)
    _log.info(SEP)

    if snap.entries:
        _log.info("  COMPONENTS:")
        for e in snap.entries:
            replaced_tag = f" [replaced×{e['replace_count']}]" if e.get("replace_count") else ""
            _log.info(
                "    %-20s  %-32s  age=%.1fs%s",
                e["name"], e["type_name"], e["age_s"], replaced_tag,
            )
    else:
        _log.info("  COMPONENTS: (none)")

    if snap.unknown_names:
        _log.info(SEP)
        _log.info("  UNKNOWN NAMES (not in KNOWN_COMPONENTS): %s", snap.unknown_names)

    if snap.missing_known:
        _log.info(SEP)
        _log.info("  MISSING KNOWN COMPONENTS: %s", snap.missing_known)

    _log.info("=" * 56)
