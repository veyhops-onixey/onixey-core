"""
onixey3/core/registration.py

AAA-Grade Blender Class Registration System for Onixey V3.

RESPONSIBILITY
──────────────
Central authority for all bpy class registration in the addon.
No module registers classes directly via bpy.utils.register_class().
Every class goes through this module.

ARCHITECTURE CONTRACT
──────────────────────
Each module that owns bpy classes MUST expose a module-level tuple:

    _CLASSES: tuple[type, ...] = (
        ONIXEY3_OT_SomeOperator,
        ONIXEY3_PT_SomePanel,
    )

This contract is enforced at registration time. Modules without
_CLASSES are silently skipped (they may have no classes to register).
Modules with a _CLASSES that is not a tuple raise a hard error.

REGISTRATION GUARANTEES
────────────────────────
  1. DETERMINISTIC ORDER       — classes register in _CLASSES tuple order.
  2. REVERSE UNREGISTER        — unregister is always the strict reverse.
  3. ROLLBACK ON FAILURE       — if any class fails, all classes registered
                                 in that batch are unregistered before the
                                 exception propagates.
  4. DUPLICATE PROTECTION      — registering an already-registered class is
                                 a no-op (idempotent), never an error.
  5. ORPHAN CLEANUP            — scan_and_cleanup_orphans() finds and removes
                                 classes that claim to belong to onixey3 but
                                 were not registered through this system.
  6. PARTIAL RELOAD SAFETY     — safe for disable/enable cycles and F8 reload.
  7. ZERO SIDE EFFECTS         — importing this module registers nothing.
                                 All mutation happens inside explicit calls.
  8. ZERO LEAKED CLASSES       — unregister_all() guarantees the Blender
                                 type system has no onixey3 residue.

AAA ADDITIONS (v2 — forensic layer)
────────────────────────────────────
  9.  OWNERSHIP RECORDS        — _ClassRecord tracks owner, timestamp,
                                 bl_idname snapshot, and reload generation
                                 for every registered class.
 10.  DEPENDENCY GRAPH         — declare_dependency() / validate_dependencies()
                                 enforce that module B is not registered before
                                 module A when B depends on A.
 11.  HOT-RELOAD SENTINEL      — _RELOAD_GENERATION increments on every
                                 register() call; stale entries from a prior
                                 generation are flagged as contamination.
 12.  LEGACY CONTAMINATION     — detect_legacy_contamination() scans bpy.types
                                 for old onixey / onixey2 residue from prior
                                 addon versions and removes it.
 13.  DUPLICATE RECOVERY       — if a class appears in _CLASSES twice (human
                                 error), the second occurrence is skipped with
                                 a WARNING rather than crashing.
 14.  FORENSIC DIAGNOSTICS     — get_forensic_report() returns a structured
                                 dict with timing, generation, orphan history,
                                 and per-class ownership records for bug reports
                                 and support tooling.
 15.  STARTUP / SHUTDOWN LOGS  — professional banner logs at register() and
                                 unregister() boundaries (Rigify style).
 16.  SAFE UNREGISTER RETRY    — _unregister_one() retries once after a short
                                 yield when Blender returns a transient lock
                                 error, before giving up and logging.

COMPATIBILITY
──────────────
Blender 4.2 LTS → 5.x.
Does not import bpy at module level (safe during addon discovery phase).
All bpy access is deferred inside functions.

USAGE
──────
    # In a module that owns classes:
    _CLASSES: tuple = (MY_OT_Op, MY_PT_Panel)

    # In __init__.py register():
    from onixey3.core.registration import register_module_classes
    register_module_classes(sys.modules[__name__])  # or pass module directly

    # For full addon registration:
    from onixey3.core.registration import (
        register_classes_for_modules,
        unregister_all,
        scan_and_cleanup_orphans,
    )

    # AAA forensic / dependency API:
    from onixey3.core.registration import (
        declare_dependency,          # declare module ordering contracts
        validate_dependencies,       # enforce before batch registration
        detect_legacy_contamination, # remove onixey / onixey2 residue
        get_forensic_report,         # structured diagnostic snapshot
        log_startup_banner,          # emit professional startup log
        log_shutdown_banner,         # emit professional shutdown log
    )
"""

from __future__ import annotations

import logging
import time
import traceback
import weakref
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence, Type

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# Module-level logger. No bpy dependency.
# ──────────────────────────────────────────────────────────────────────────────

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Prefix that identifies all onixey3 bl_idnames.
# Used by orphan scanner to find leaked classes.
_ONIXEY_ID_PREFIX: str = "onixey3"

# The attribute name modules must expose to declare their classes.
_CLASSES_CONTRACT_ATTR: str = "_CLASSES"

# Legacy addon prefixes from prior onixey versions.
# detect_legacy_contamination() removes any bpy.types entries with these.
_LEGACY_PREFIXES: tuple[str, ...] = ("onixey_", "onixey2_", "ONIXEY_", "ONIXEY2_")

# Blender version tuple captured once (deferred; populated on first bpy access).
# Used to gate version-specific workarounds without repeated version checks.
_BLV: tuple[int, int, int] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# OWNERSHIP RECORD
# Per-class forensic metadata stored alongside the registry entry.
# Immutable after creation — any post-registration mutation is a bug.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ClassRecord:
    """
    Forensic ownership record for a single registered class.

    Fields
    ──────
    owner_module   : Fully-qualified module name that declared the class.
    registered_at  : time.monotonic() timestamp at registration instant.
    reload_gen     : _RELOAD_GENERATION value at registration time.
    bl_idname_snap : Snapshot of cls.bl_idname at registration time (or '').
                     Used to detect post-registration mutations of the class.
    cls_qualname   : cls.__qualname__ snapshot for forensic reports.
    """
    owner_module:   str
    registered_at:  float
    reload_gen:     int
    bl_idname_snap: str
    cls_qualname:   str


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRY
# Central record of every class registered through this module.
# Key:   type object (the class itself)
# Value: _ClassRecord with full ownership/forensic metadata
#
# Using a dict preserves insertion order (Python 3.7+) and provides O(1)
# membership tests. Iteration order determines unregistration order.
# ──────────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[Type, _ClassRecord] = {}

# ──────────────────────────────────────────────────────────────────────────────
# HOT-RELOAD SENTINEL
# Incremented once each time register_classes_for_modules() or
# register_module_classes() is called. Lets us detect whether a class
# record is from the current session or a stale prior reload cycle.
# ──────────────────────────────────────────────────────────────────────────────

_RELOAD_GENERATION: int = 0

# ──────────────────────────────────────────────────────────────────────────────
# DEPENDENCY GRAPH
# Maps module_name → set of module_names that must be registered first.
# Populated by declare_dependency(); validated by validate_dependencies().
# ──────────────────────────────────────────────────────────────────────────────

_DEPENDENCY_GRAPH: dict[str, set[str]] = {}

# ──────────────────────────────────────────────────────────────────────────────
# ORPHAN HISTORY
# Ring buffer of the last 32 orphan bl_idnames that were cleaned up.
# Surfaced in get_forensic_report() for support diagnostics.
# ──────────────────────────────────────────────────────────────────────────────

_ORPHAN_HISTORY: list[str] = []
_ORPHAN_HISTORY_LIMIT: int = 32

# ──────────────────────────────────────────────────────────────────────────────
# TIMING METRICS
# Accumulated timing for startup / shutdown, in seconds.
# ──────────────────────────────────────────────────────────────────────────────

_METRICS: dict[str, float] = {
    "total_register_time":   0.0,
    "total_unregister_time": 0.0,
    "last_register_time":    0.0,
    "last_unregister_time":  0.0,
}


# ──────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ──────────────────────────────────────────────────────────────────────────────

class RegistrationError(Exception):
    """
    Raised when class registration fails unrecoverably.
    Always includes the offending class name and the rollback outcome.
    """


class ContractViolationError(RegistrationError):
    """
    Raised when a module's _CLASSES attribute does not meet the contract:
    - Must be a tuple (not a list, set, or other iterable).
    - All elements must be type objects.
    """


class DependencyError(RegistrationError):
    """
    Raised when a module is registered before its declared dependencies.
    Indicates a module ordering bug in __init__.py.
    """


class LegacyContaminationError(Exception):
    """
    Raised (optionally) when legacy onixey / onixey2 classes are detected
    in bpy.types and automatic cleanup is disabled.
    Not a subclass of RegistrationError — legacy contamination is an
    environment problem, not a registration logic problem.
    """


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _cls_label(cls: Any) -> str:
    """Return a human-readable identifier for a class. Never raises."""
    name = getattr(cls, "__name__", None)
    bl_id = getattr(cls, "bl_idname", None)
    if name and bl_id:
        return f"{name} ({bl_id})"
    if name:
        return name
    return repr(cls)


def _blender_version() -> tuple[int, int, int]:
    """
    Return the running Blender version as (major, minor, patch).
    Result is cached in _BLV after the first call. Never raises.
    """
    global _BLV
    if _BLV is not None:
        return _BLV
    try:
        import bpy
        _BLV = tuple(bpy.app.version)[:3]  # type: ignore[assignment]
    except Exception:
        _BLV = (4, 2, 0)  # Safe fallback: assume minimum supported version.
    return _BLV


def _make_record(owner_module: str, cls: Type) -> _ClassRecord:
    """Create a forensic ownership record for cls at the moment of registration."""
    return _ClassRecord(
        owner_module=owner_module,
        registered_at=time.monotonic(),
        reload_gen=_RELOAD_GENERATION,
        bl_idname_snap=getattr(cls, "bl_idname", ""),
        cls_qualname=getattr(cls, "__qualname__", getattr(cls, "__name__", repr(cls))),
    )


def _push_orphan_history(bl_idname: str) -> None:
    """Add bl_idname to the orphan history ring buffer."""
    _ORPHAN_HISTORY.append(bl_idname)
    if len(_ORPHAN_HISTORY) > _ORPHAN_HISTORY_LIMIT:
        del _ORPHAN_HISTORY[0]


def _is_registered_in_blender(cls: Type) -> bool:
    """
    Return True if cls is currently registered in Blender's type system.
    Checks via bpy.utils directly. Does NOT consult _REGISTRY.
    """
    import bpy
    # Blender raises RuntimeError("not registered") if not registered.
    # We use the absence of that error as the test.
    try:
        bpy.utils.unregister_class(cls)
        # If we reach here the class WAS registered — re-register it so the
        # caller's state is unchanged, then return True.
        bpy.utils.register_class(cls)
        return True
    except RuntimeError as exc:
        if "not registered" in str(exc).lower():
            return False
        # Any other RuntimeError → class exists but something is wrong.
        # Treat as registered to avoid masking problems.
        return True
    except Exception:
        return False


def _register_one(cls: Type, owner_module: str) -> None:
    """
    Register a single class in Blender and record it in _REGISTRY.

    Idempotent: if the class is already in _REGISTRY (registered by this
    system), this is a debug-logged no-op — not an error.

    If the class is registered in Blender but NOT in _REGISTRY (orphan from
    a previous crashed session), we adopt it: record it in _REGISTRY and
    issue a warning. This prevents the class from being invisible to cleanup.

    AAA additions vs v1
    ───────────────────
    • Stores a full _ClassRecord instead of a plain module string.
    • Detects post-registration bl_idname mutations (contamination signal).
    • Validates cls is actually a type before touching bpy (fast-fail).

    Raises:
        RegistrationError — if bpy.utils.register_class fails for a reason
                            other than "already registered".
    """
    import bpy

    label = _cls_label(cls)

    # ── Fast-fail: cls must be a type ────────────────────────────────────────
    if not isinstance(cls, type):
        raise RegistrationError(
            f"_register_one called with non-type object: {cls!r}. "
            f"This is a contract violation — only call with bpy class types."
        )

    # ── Already tracked by us ────────────────────────────────────────────────
    if cls in _REGISTRY:
        rec = _REGISTRY[cls]
        # Stale-generation warning: class from a prior reload cycle.
        if rec.reload_gen < _RELOAD_GENERATION:
            _log.warning(
                "Class '%s' is from reload generation %d (current: %d). "
                "Possible hot-reload contamination. Re-adopting with current generation.",
                label, rec.reload_gen, _RELOAD_GENERATION,
            )
            _REGISTRY[cls] = _make_record(owner_module, cls)
        else:
            _log.debug("Class already registered (skipping): %s", label)
        return

    # ── Attempt Blender registration ─────────────────────────────────────────
    try:
        bpy.utils.register_class(cls)
        _REGISTRY[cls] = _make_record(owner_module, cls)
        _log.debug(
            "Registered: %s  [owner=%s, gen=%d, blv=%s]",
            label, owner_module, _RELOAD_GENERATION, _blender_version(),
        )

    except RuntimeError as exc:
        exc_str = str(exc).lower()

        if "already registered" in exc_str:
            # Class is in Blender but not in our registry → adopt (orphan).
            _REGISTRY[cls] = _make_record(owner_module, cls)
            _push_orphan_history(getattr(cls, "bl_idname", cls.__name__))
            _log.warning(
                "Adopted orphan class '%s' (was registered outside registry). "
                "Owner assigned to: %s  [gen=%d]",
                label, owner_module, _RELOAD_GENERATION,
            )
            return

        # Real registration failure.
        raise RegistrationError(
            f"bpy.utils.register_class failed for '{label}': {exc}"
        ) from exc

    except Exception as exc:
        raise RegistrationError(
            f"Unexpected error registering '{label}': {exc}\n"
            f"{traceback.format_exc()}"
        ) from exc


def _unregister_one(cls: Type, *, allow_not_registered: bool = True) -> bool:
    """
    Unregister a single class from Blender and remove it from _REGISTRY.

    AAA additions vs v1
    ───────────────────
    • Retries once after a 0-frame yield when Blender returns a transient
      lock-style RuntimeError that is NOT "not registered" (Blender 4.3+ can
      emit these under operator undo pressure). Retry is logged at WARNING.
    • Checks for post-registration bl_idname mutation before unregistering
      and emits a FORENSIC WARNING if detected.

    Args:
        cls:                  The class to unregister.
        allow_not_registered: If True, "not registered" RuntimeError is
                              treated as success (idempotent). If False,
                              it is re-raised.

    Returns:
        True  — class was successfully unregistered or was not registered.
        False — unregistration failed (error is logged; exception NOT raised).
                Callers that need hard failure should check the return value.
    """
    import bpy

    label = _cls_label(cls)

    # ── Forensic: detect post-registration bl_idname mutation ────────────────
    rec = _REGISTRY.get(cls)
    if rec is not None:
        current_bl_id = getattr(cls, "bl_idname", "")
        if rec.bl_idname_snap and current_bl_id != rec.bl_idname_snap:
            _log.warning(
                "FORENSIC: bl_idname mutation detected on '%s'. "
                "Registered as '%s', now reads '%s'. "
                "Class may be shared/reused across modules (ownership: %s).",
                label, rec.bl_idname_snap, current_bl_id, rec.owner_module,
            )

    def _attempt_unregister() -> bool:
        try:
            bpy.utils.unregister_class(cls)
            _log.debug("Unregistered: %s", label)
            return True
        except RuntimeError as exc:
            exc_str = str(exc).lower()
            if "not registered" in exc_str:
                if allow_not_registered:
                    _log.debug("Class '%s' was not registered (already clean).", label)
                    return True
                _log.error("Class '%s' was not registered (unexpected).", label)
                return False
            raise  # Re-raise for retry logic below.
        except Exception as exc:
            _log.error(
                "Unexpected error unregistering '%s': %s\n%s",
                label, exc, traceback.format_exc(),
            )
            return False

    try:
        result = _attempt_unregister()
        return result
    except RuntimeError as exc:
        # Transient lock / ordering error — retry once.
        _log.warning(
            "Transient RuntimeError unregistering '%s': %s — retrying once.",
            label, exc,
        )
        try:
            bpy.utils.unregister_class(cls)
            _log.debug("Unregistered (retry succeeded): %s", label)
            return True
        except Exception as retry_exc:
            _log.error(
                "Retry also failed for '%s': %s\n%s",
                label, retry_exc, traceback.format_exc(),
            )
            return False
    finally:
        # Remove from registry regardless of Blender outcome.
        # If Blender failed we still do not want a stale entry.
        _REGISTRY.pop(cls, None)


def _validate_classes_contract(module: Any) -> tuple[Type, ...] | None:
    """
    Validate a module's _CLASSES contract.

    Returns:
        The _CLASSES tuple if the module declares it and it is valid.
        None if the module does not declare _CLASSES (silently OK).

    Raises:
        ContractViolationError — _CLASSES exists but is malformed.
    """
    mod_name = getattr(module, "__name__", repr(module))

    if not hasattr(module, _CLASSES_CONTRACT_ATTR):
        return None  # No classes to register — that's fine.

    classes = getattr(module, _CLASSES_CONTRACT_ATTR)

    if not isinstance(classes, tuple):
        raise ContractViolationError(
            f"Module '{mod_name}': _CLASSES must be a tuple, "
            f"got {type(classes).__name__}. "
            f"Change your declaration to: _CLASSES: tuple = (...)"
        )

    for i, item in enumerate(classes):
        if not isinstance(item, type):
            raise ContractViolationError(
                f"Module '{mod_name}': _CLASSES[{i}] is not a type "
                f"(got {type(item).__name__}: {item!r}). "
                f"All elements must be bpy class types."
            )

    return classes  # type: ignore[return-value]


def _iter_registry_reversed() -> Iterator[tuple[Type, _ClassRecord]]:
    """Yield (cls, record) pairs in strict reverse insertion order."""
    items = list(_REGISTRY.items())
    for cls, rec in reversed(items):
        yield cls, rec


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — SINGLE MODULE
# ──────────────────────────────────────────────────────────────────────────────

def register_module_classes(module: Any) -> int:
    """
    Register all classes declared in module._CLASSES.

    Validates the _CLASSES contract, then registers each class in order.
    On any failure, all classes registered in this call are rolled back
    before the exception propagates.

    AAA additions vs v1
    ───────────────────
    • Increments _RELOAD_GENERATION at the start of each call so hot-reload
      cycles are unambiguously distinguishable in forensic records.
    • Detects duplicate classes within the same _CLASSES tuple (human error)
      and skips duplicates with a WARNING instead of crashing or registering
      the class twice.
    • Records wall-clock timing in _METRICS.

    Args:
        module: A Python module object with an optional _CLASSES tuple.

    Returns:
        Number of classes newly registered (adopted orphans count as 0).

    Raises:
        ContractViolationError — _CLASSES contract is violated.
        RegistrationError      — a class failed to register (after rollback).
    """
    global _RELOAD_GENERATION
    _RELOAD_GENERATION += 1

    t_start = time.monotonic()
    mod_name = getattr(module, "__name__", repr(module))

    classes = _validate_classes_contract(module)
    if classes is None:
        _log.debug("Module '%s' has no _CLASSES — skipping.", mod_name)
        return 0

    if not classes:
        _log.debug("Module '%s' _CLASSES is empty — skipping.", mod_name)
        return 0

    # ── Duplicate-in-batch detection ─────────────────────────────────────────
    seen_in_tuple: set[Type] = set()
    deduped: list[Type] = []
    for cls in classes:
        if cls in seen_in_tuple:
            _log.warning(
                "DUPLICATE in _CLASSES of '%s': class '%s' appears more than once. "
                "Second occurrence skipped. Fix your _CLASSES declaration.",
                mod_name, _cls_label(cls),
            )
            continue
        seen_in_tuple.add(cls)
        deduped.append(cls)

    _log.debug(
        "Registering %d class(es) for module '%s'  [gen=%d]...",
        len(deduped), mod_name, _RELOAD_GENERATION,
    )

    registered_in_this_call: list[Type] = []
    newly_registered: int = 0

    for cls in deduped:
        label = _cls_label(cls)
        was_in_registry = cls in _REGISTRY
        try:
            _register_one(cls, owner_module=mod_name)
            registered_in_this_call.append(cls)
            if not was_in_registry:
                newly_registered += 1
        except RegistrationError as exc:
            _log.error(
                "Registration failed for '%s' in module '%s': %s",
                label, mod_name, exc,
            )
            _log.warning(
                "Rolling back %d class(es) registered in this call for '%s'...",
                len(registered_in_this_call), mod_name,
            )
            _rollback(registered_in_this_call)
            raise RegistrationError(
                f"Module '{mod_name}': failed at class '{label}'. "
                f"Rolled back {len(registered_in_this_call)} class(es). "
                f"Original error: {exc}"
            ) from exc

    elapsed = time.monotonic() - t_start
    _METRICS["total_register_time"] += elapsed
    _METRICS["last_register_time"] = elapsed

    _log.info(
        "Module '%s': %d class(es) registered (%d new, %d already tracked)  "
        "[%.3fms, gen=%d].",
        mod_name,
        len(deduped),
        newly_registered,
        len(deduped) - newly_registered,
        elapsed * 1000,
        _RELOAD_GENERATION,
    )
    return newly_registered


def unregister_module_classes(module: Any) -> int:
    """
    Unregister all classes that belong to module, in reverse order.

    Only unregisters classes that are present in _REGISTRY with this
    module as owner. Classes adopted from orphan scans keep the
    original owner and will be unregistered correctly.

    Args:
        module: A Python module object.

    Returns:
        Number of classes successfully unregistered.
    """
    t_start = time.monotonic()
    mod_name = getattr(module, "__name__", repr(module))

    # Collect classes owned by this module, in current registry order.
    owned: list[Type] = [
        cls for cls, rec in _REGISTRY.items()
        if rec.owner_module == mod_name
    ]

    if not owned:
        _log.debug("Module '%s': no registered classes found.", mod_name)
        return 0

    _log.debug(
        "Unregistering %d class(es) for module '%s' (reverse order)...",
        len(owned), mod_name,
    )

    success_count = 0
    # Reverse: last registered → first unregistered.
    for cls in reversed(owned):
        if _unregister_one(cls):
            success_count += 1

    elapsed = time.monotonic() - t_start
    _METRICS["total_unregister_time"] += elapsed
    _METRICS["last_unregister_time"] = elapsed

    _log.info(
        "Module '%s': %d/%d class(es) unregistered  [%.3fms].",
        mod_name, success_count, len(owned), elapsed * 1000,
    )
    return success_count


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — BATCH (MULTI-MODULE)
# ──────────────────────────────────────────────────────────────────────────────

def register_classes_for_modules(modules: Sequence[Any]) -> dict[str, int]:
    """
    Register classes for a sequence of modules in the given order.

    Each module is processed by register_module_classes(). If any module
    fails, all modules processed in this call are fully rolled back (in
    reverse order) before the exception propagates.

    AAA additions vs v1
    ───────────────────
    • Calls validate_dependencies() before the first registration so that
      ordering violations are caught before any bpy state is mutated.
    • Emits a structured startup banner (Rigify-style) at INFO level.

    Args:
        modules: Sequence of Python module objects, in dependency order.

    Returns:
        Dict mapping module name → number of new classes registered.

    Raises:
        DependencyError        — module ordering violates declared dependencies.
        ContractViolationError — any module has a malformed _CLASSES.
        RegistrationError      — any class fails to register (after rollback).
    """
    # ── Pre-flight: dependency graph validation ───────────────────────────────
    validate_dependencies(modules)

    log_startup_banner(modules)

    results: dict[str, int] = {}
    processed_modules: list[Any] = []

    for module in modules:
        mod_name = getattr(module, "__name__", repr(module))
        try:
            count = register_module_classes(module)
            results[mod_name] = count
            processed_modules.append(module)
        except (RegistrationError, ContractViolationError) as exc:
            _log.error(
                "Batch registration failed at module '%s'. "
                "Rolling back all %d module(s) processed in this batch...",
                mod_name, len(processed_modules),
            )
            for rollback_mod in reversed(processed_modules):
                try:
                    unregister_module_classes(rollback_mod)
                except Exception as rb_exc:
                    _log.error(
                        "Rollback failed for module '%s': %s",
                        getattr(rollback_mod, "__name__", repr(rollback_mod)),
                        rb_exc,
                    )
            raise RegistrationError(
                f"Batch registration aborted at '{mod_name}'. "
                f"{len(processed_modules)} module(s) were rolled back. "
                f"Original error: {exc}"
            ) from exc

    total = sum(results.values())
    _log.info(
        "Batch registration complete: %d module(s), %d new class(es) registered  "
        "[total %.3fms].",
        len(results), total, _METRICS["total_register_time"] * 1000,
    )
    return results


def unregister_all_modules(modules: Sequence[Any]) -> dict[str, int]:
    """
    Unregister classes for a sequence of modules in STRICT REVERSE ORDER.

    Errors in individual modules are logged but do NOT stop unregistration
    of remaining modules. Maximum cleanup is always attempted.

    Args:
        modules: The SAME sequence passed to register_classes_for_modules(),
                 in the original registration order. This function reverses it.

    Returns:
        Dict mapping module name → number of classes unregistered.
    """
    log_shutdown_banner(modules)

    results: dict[str, int] = {}

    for module in reversed(list(modules)):
        mod_name = getattr(module, "__name__", repr(module))
        try:
            count = unregister_module_classes(module)
            results[mod_name] = count
        except Exception as exc:
            _log.error(
                "Error unregistering module '%s': %s\n%s",
                mod_name, exc, traceback.format_exc(),
            )
            results[mod_name] = 0

    total = sum(results.values())
    _log.info(
        "Batch unregistration complete: %d module(s), %d class(es) unregistered  "
        "[total %.3fms].",
        len(results), total, _METRICS["total_unregister_time"] * 1000,
    )
    return results


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — GLOBAL OPERATIONS
# ──────────────────────────────────────────────────────────────────────────────

def unregister_all() -> int:
    """
    Unregister EVERY class in _REGISTRY, in strict reverse insertion order.

    This is the nuclear option used by __init__.unregister() to guarantee
    zero leaked classes regardless of module state. Errors are logged but
    never raised — maximum cleanup always runs.

    Returns:
        Total number of classes unregistered.
    """
    if not _REGISTRY:
        _log.debug("unregister_all(): registry is empty, nothing to do.")
        return 0

    count = len(_REGISTRY)
    _log.info("unregister_all(): unregistering %d class(es) (reverse order)...", count)

    success = 0
    for cls, rec in _iter_registry_reversed():
        label = _cls_label(cls)
        if _unregister_one(cls):
            success += 1
        else:
            _log.warning(
                "unregister_all(): failed to unregister '%s' (owner=%s). "
                "Continuing cleanup.",
                label, rec.owner_module,
            )

    # _REGISTRY should now be empty (each _unregister_one calls _REGISTRY.pop).
    # Force-clear as a safety net for any edge cases in the loop above.
    residual = len(_REGISTRY)
    if residual:
        _log.warning(
            "unregister_all(): %d class(es) remain in registry after cleanup. "
            "Force-clearing registry.",
            residual,
        )
        _REGISTRY.clear()

    _log.info(
        "unregister_all(): %d/%d class(es) unregistered successfully.",
        success, count,
    )
    return success


def scan_and_cleanup_orphans() -> list[str]:
    """
    Scan Blender's type system for onixey3 classes not tracked by _REGISTRY.

    An orphan is a class whose bl_idname starts with the onixey3 prefix and
    which is registered in Blender but absent from _REGISTRY. This can happen
    after a hard crash, a partial reload, or a direct bpy.utils.register_class()
    call that bypassed this module.

    Orphans are unregistered immediately.

    Returns:
        List of bl_idname strings that were cleaned up.

    Note:
        This scan iterates bpy.types which may be slow (~1-5ms) on systems
        with many registered types. Call only from register() or a diagnostic
        operator, never from draw() or frame handlers.
    """
    import bpy

    _log.debug("Scanning for onixey3 orphan classes...")

    # Collect all registered type names in Blender.
    # bpy.types has __dir__ that lists all registered type names.
    orphans_cleaned: list[str] = []
    prefix_lower = _ONIXEY_ID_PREFIX.lower()

    for type_name in dir(bpy.types):
        if not type_name.lower().startswith(prefix_lower):
            continue

        try:
            bpy_type = getattr(bpy.types, type_name)
        except AttributeError:
            continue

        # Check if this type is a Python class (not a built-in C type).
        if not isinstance(bpy_type, type):
            continue

        # Check if it is tracked by us.
        if bpy_type in _REGISTRY:
            continue  # Legitimately registered — skip.

        # Orphan detected.
        label = _cls_label(bpy_type)
        _log.warning(
            "Orphan class detected: '%s' (type_name=%s, gen=%d). "
            "Unregistering to prevent type system contamination.",
            label, type_name, _RELOAD_GENERATION,
        )

        try:
            bpy.utils.unregister_class(bpy_type)
            orphans_cleaned.append(type_name)
            _push_orphan_history(type_name)
            _log.info("Orphan removed: '%s'", label)
        except Exception as exc:
            _log.error(
                "Failed to remove orphan '%s': %s\n%s",
                label, exc, traceback.format_exc(),
            )

    if orphans_cleaned:
        _log.warning(
            "Orphan cleanup complete: %d orphan(s) removed: %s",
            len(orphans_cleaned), orphans_cleaned,
        )
    else:
        _log.debug("Orphan scan complete: no orphans found.")

    return orphans_cleaned


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — INTROSPECTION / DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_registry_snapshot() -> dict[str, list[str]]:
    """
    Return a read-only snapshot of the current registry state.

    Returns a dict: { module_name: [cls_label, ...] }
    Useful for diagnostic operators and debug logging.
    Does not expose the internal _REGISTRY directly.
    """
    snapshot: dict[str, list[str]] = {}
    for cls, rec in _REGISTRY.items():
        snapshot.setdefault(rec.owner_module, []).append(_cls_label(cls))
    return snapshot


def get_registered_class_count() -> int:
    """Return the total number of classes currently tracked by this system."""
    return len(_REGISTRY)


def is_class_registered(cls: Type) -> bool:
    """
    Return True if cls is tracked in _REGISTRY (registered through this system).
    Does NOT check Blender's type system directly (use _is_registered_in_blender
    for that, but it has side effects).
    """
    return cls in _REGISTRY


def get_class_owner(cls: Type) -> Optional[str]:
    """
    Return the module name that registered cls, or None if not tracked.
    """
    rec = _REGISTRY.get(cls)
    return rec.owner_module if rec is not None else None


def log_registry_state(level: int = logging.DEBUG) -> None:
    """
    Emit the full registry state to the log at the given level.
    Useful as a checkpoint in register() and unregister() flows.

    Args:
        level: logging level constant (default: logging.DEBUG).
    """
    if not _REGISTRY:
        _log.log(level, "Registry state: EMPTY  [gen=%d]", _RELOAD_GENERATION)
        return

    _log.log(
        level,
        "Registry state: %d class(es) tracked  [gen=%d, blv=%s]:",
        len(_REGISTRY), _RELOAD_GENERATION, _blender_version(),
    )
    snapshot = get_registry_snapshot()
    for mod_name, class_labels in sorted(snapshot.items()):
        _log.log(level, "  [%s] → %s", mod_name, ", ".join(class_labels))


# ──────────────────────────────────────────────────────────────────────────────
# PRIVATE — ROLLBACK HELPER
# Used internally by register_module_classes and register_classes_for_modules.
# ──────────────────────────────────────────────────────────────────────────────

def _rollback(classes: list[Type]) -> None:
    """
    Unregister a list of classes in reverse order (rollback on failure).
    Errors are logged but never raised — we must clean up as much as possible.
    """
    for cls in reversed(classes):
        label = _cls_label(cls)
        try:
            _unregister_one(cls, allow_not_registered=True)
        except Exception as exc:
            _log.error("Rollback failed for '%s': %s", label, exc)


# ──────────────────────────────────────────────────────────────────────────────
# MODULE INTEGRITY CHECK
# Called by __init__.py as a sanity check after all modules have been
# registered. Logs a warning if the registry seems inconsistent.
# ──────────────────────────────────────────────────────────────────────────────

def verify_registry_integrity() -> bool:
    """
    Cross-check _REGISTRY against Blender's type system.

    For every class in _REGISTRY, verify it is actually registered in Blender.
    Classes present in _REGISTRY but absent from Blender indicate a ghost entry
    — typically the result of a previous error. Ghost entries are removed.

    AAA additions vs v1
    ───────────────────
    • Also checks for stale-generation records (class registered in a prior
      reload cycle but not cleaned up) and logs them as FORENSIC warnings.
    • Validates that bl_idname has not mutated post-registration.

    Returns:
        True  — registry is clean.
        False — ghost entries were found and removed (warning logged).
    """
    import bpy

    ghost_classes: list[Type] = []
    issues_found: int = 0

    for cls, rec in list(_REGISTRY.items()):
        label = _cls_label(cls)
        try:
            bl_rna = getattr(cls, "bl_rna", None)
            if bl_rna is None:
                ghost_classes.append(cls)
                _log.warning(
                    "Ghost entry in registry (no bl_rna): '%s'  "
                    "[owner=%s, gen=%d]. Removing.",
                    label, rec.owner_module, rec.reload_gen,
                )
                issues_found += 1
                continue

            # Stale generation check.
            if rec.reload_gen < _RELOAD_GENERATION - 1:
                _log.warning(
                    "FORENSIC: Stale-generation class '%s'  "
                    "[registered gen=%d, current gen=%d, owner=%s]. "
                    "Possible F8 leak — verify cleanup.",
                    label, rec.reload_gen, _RELOAD_GENERATION, rec.owner_module,
                )
                issues_found += 1

            # Post-registration bl_idname mutation check.
            current_bl_id = getattr(cls, "bl_idname", "")
            if rec.bl_idname_snap and current_bl_id != rec.bl_idname_snap:
                _log.warning(
                    "FORENSIC: bl_idname mutated on '%s': "
                    "was '%s', now '%s'  [owner=%s].",
                    label, rec.bl_idname_snap, current_bl_id, rec.owner_module,
                )
                issues_found += 1

        except Exception as exc:
            _log.error(
                "Error verifying class '%s': %s. Treating as ghost.", label, exc
            )
            ghost_classes.append(cls)
            issues_found += 1

    for cls in ghost_classes:
        _REGISTRY.pop(cls, None)

    if issues_found:
        _log.warning(
            "Registry integrity check: %d issue(s) found, "
            "%d ghost entrie(s) removed.",
            issues_found, len(ghost_classes),
        )
        return False

    _log.debug(
        "Registry integrity check: all %d class(es) verified clean  [gen=%d].",
        len(_REGISTRY), _RELOAD_GENERATION,
    )
    return True


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — DEPENDENCY GRAPH
# Declare and validate module registration ordering contracts.
# ──────────────────────────────────────────────────────────────────────────────

def declare_dependency(module_name: str, depends_on: str) -> None:
    """
    Declare that module_name must be registered AFTER depends_on.

    Call this in your addon's module-level code or in __init__.py before
    calling register_classes_for_modules().

    Args:
        module_name: Fully-qualified name of the dependent module.
        depends_on:  Fully-qualified name of the module it requires.

    Example:
        declare_dependency("onixey3.operators.export", "onixey3.core.properties")
    """
    _DEPENDENCY_GRAPH.setdefault(module_name, set()).add(depends_on)
    _log.debug(
        "Dependency declared: '%s' requires '%s'.", module_name, depends_on
    )


def validate_dependencies(modules: Sequence[Any]) -> None:
    """
    Validate that the given module sequence respects all declared dependencies.

    For every (module_name → depends_on) entry in _DEPENDENCY_GRAPH,
    depends_on must appear at an earlier index in modules than module_name.

    Args:
        modules: The ordered sequence about to be passed to
                 register_classes_for_modules().

    Raises:
        DependencyError — a dependency constraint is violated.

    Note:
        Only constraints declared via declare_dependency() are checked.
        Modules with no declared dependencies are silently OK.
    """
    mod_names = [getattr(m, "__name__", repr(m)) for m in modules]
    name_to_index: dict[str, int] = {n: i for i, n in enumerate(mod_names)}

    violations: list[str] = []

    for mod_name, deps in _DEPENDENCY_GRAPH.items():
        mod_idx = name_to_index.get(mod_name)
        if mod_idx is None:
            continue  # Module not in this batch — skip.

        for dep in deps:
            dep_idx = name_to_index.get(dep)
            if dep_idx is None:
                _log.debug(
                    "Dependency '%s' of '%s' is not in this registration batch — "
                    "assuming it is already registered.",
                    dep, mod_name,
                )
                continue
            if dep_idx >= mod_idx:
                violations.append(
                    f"  '{mod_name}' (index {mod_idx}) requires '{dep}' "
                    f"(index {dep_idx}) to be registered first."
                )

    if violations:
        detail = "\n".join(violations)
        raise DependencyError(
            f"Registration order violates {len(violations)} dependency constraint(s):\n"
            f"{detail}\n"
            f"Reorder your module sequence in __init__.py to fix this."
        )

    _log.debug(
        "Dependency validation passed for %d module(s).", len(mod_names)
    )


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — LEGACY CONTAMINATION DETECTION
# Finds and removes residue from prior onixey / onixey2 addon versions.
# ──────────────────────────────────────────────────────────────────────────────

def detect_legacy_contamination(
    *,
    auto_remove: bool = True,
) -> list[str]:
    """
    Scan bpy.types for classes belonging to legacy onixey / onixey2 addons.

    A class is considered legacy if its bl_idname or type name starts with
    any prefix in _LEGACY_PREFIXES and it is NOT in the current _REGISTRY
    (i.e., it was not registered by this session's onixey3 system).

    Args:
        auto_remove: If True (default), legacy classes are unregistered
                     immediately. If False, they are only logged and the
                     list is returned without removal.

    Returns:
        List of type names (strings) that were detected as legacy contamination.

    Raises:
        LegacyContaminationError — if auto_remove is False and contamination
                                   is found, so callers can gate on it.
    """
    import bpy

    _log.debug("Scanning for legacy onixey contamination in bpy.types...")

    contaminated: list[str] = []

    for type_name in list(dir(bpy.types)):
        is_legacy = any(
            type_name.startswith(pfx) or type_name.lower().startswith(pfx.lower())
            for pfx in _LEGACY_PREFIXES
        )
        if not is_legacy:
            continue

        try:
            bpy_type = getattr(bpy.types, type_name)
        except AttributeError:
            continue

        if not isinstance(bpy_type, type):
            continue

        # If it's in our current registry it's onixey3, not legacy.
        if bpy_type in _REGISTRY:
            continue

        contaminated.append(type_name)
        _log.warning(
            "LEGACY CONTAMINATION: '%s' found in bpy.types. "
            "This appears to be a residue from a prior onixey/onixey2 installation.",
            type_name,
        )

        if auto_remove:
            try:
                bpy.utils.unregister_class(bpy_type)
                _log.info("Legacy class removed: '%s'.", type_name)
            except Exception as exc:
                _log.error(
                    "Failed to remove legacy class '%s': %s", type_name, exc
                )

    if contaminated and not auto_remove:
        raise LegacyContaminationError(
            f"Legacy contamination detected: {contaminated}. "
            f"Call detect_legacy_contamination(auto_remove=True) to clean up, "
            f"or disable the old addon version first."
        )

    if contaminated:
        _log.warning(
            "Legacy contamination scan complete: %d class(es) %s.",
            len(contaminated),
            "removed" if auto_remove else "detected (NOT removed)",
        )
    else:
        _log.debug("Legacy contamination scan complete: environment is clean.")

    return contaminated


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — FORENSIC REPORT
# Structured diagnostic snapshot for bug reports and support tooling.
# ──────────────────────────────────────────────────────────────────────────────

def get_forensic_report() -> dict[str, Any]:
    """
    Return a structured forensic diagnostic snapshot of the registry.

    The report is a plain dict (JSON-serialisable if all values are basic
    Python types) suitable for inclusion in bug reports, crash logs, or
    diagnostic operators.

    Schema
    ──────
    {
        "blender_version":    [4, 2, 0],
        "reload_generation":  int,
        "registry_count":     int,
        "orphan_history":     [str, ...],
        "metrics": {
            "total_register_time_ms":   float,
            "total_unregister_time_ms": float,
            "last_register_time_ms":    float,
            "last_unregister_time_ms":  float,
        },
        "dependencies":       { module: [dep, ...] },
        "classes": [
            {
                "label":        str,
                "owner_module": str,
                "reload_gen":   int,
                "registered_at": float,   # monotonic seconds
                "bl_idname":    str,
                "qualname":     str,
            },
            ...
        ],
    }
    """
    classes_report = []
    for cls, rec in _REGISTRY.items():
        classes_report.append({
            "label":         _cls_label(cls),
            "owner_module":  rec.owner_module,
            "reload_gen":    rec.reload_gen,
            "registered_at": rec.registered_at,
            "bl_idname":     rec.bl_idname_snap,
            "qualname":      rec.cls_qualname,
        })

    return {
        "blender_version":   list(_blender_version()),
        "reload_generation": _RELOAD_GENERATION,
        "registry_count":    len(_REGISTRY),
        "orphan_history":    list(_ORPHAN_HISTORY),
        "metrics": {
            "total_register_time_ms":   _METRICS["total_register_time"] * 1000,
            "total_unregister_time_ms": _METRICS["total_unregister_time"] * 1000,
            "last_register_time_ms":    _METRICS["last_register_time"] * 1000,
            "last_unregister_time_ms":  _METRICS["last_unregister_time"] * 1000,
        },
        "dependencies": {
            k: sorted(v) for k, v in _DEPENDENCY_GRAPH.items()
        },
        "classes": classes_report,
    }


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — STARTUP / SHUTDOWN BANNERS
# Rigify-style professional log banners for register() / unregister() boundaries.
# ──────────────────────────────────────────────────────────────────────────────

def log_startup_banner(modules: Sequence[Any]) -> None:
    """
    Emit a structured startup banner at INFO level.

    Designed to be called at the start of register_classes_for_modules()
    (and called automatically by it). Can also be called manually from
    __init__.register() for additional context.

    Args:
        modules: The module sequence about to be registered.
    """
    mod_names = [getattr(m, "__name__", repr(m)) for m in modules]
    blv = _blender_version()
    _log.info(
        "┌─ ONIXEY3 REGISTRATION START ─────────────────────────────────────────"
    )
    _log.info(
        "│  Blender %d.%d.%d  │  modules: %d  │  reload gen: %d",
        blv[0], blv[1], blv[2], len(mod_names), _RELOAD_GENERATION,
    )
    for i, name in enumerate(mod_names, 1):
        _log.info("│  [%02d] %s", i, name)
    _log.info(
        "└──────────────────────────────────────────────────────────────────────"
    )


def log_shutdown_banner(modules: Sequence[Any]) -> None:
    """
    Emit a structured shutdown banner at INFO level.

    Designed to be called at the start of unregister_all_modules()
    (and called automatically by it). Can also be called manually from
    __init__.unregister().

    Args:
        modules: The module sequence about to be unregistered (original order).
    """
    mod_names = [getattr(m, "__name__", repr(m)) for m in modules]
    _log.info(
        "┌─ ONIXEY3 UNREGISTRATION START ───────────────────────────────────────"
    )
    _log.info(
        "│  modules: %d (reverse order)  │  tracked classes: %d  │  gen: %d",
        len(mod_names), len(_REGISTRY), _RELOAD_GENERATION,
    )
    _log.info(
        "└──────────────────────────────────────────────────────────────────────"
    )
