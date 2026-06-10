"""
onixey3/runtime/reload_manager.py

Safe Module Reload System — Blender 4.2+
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
import traceback
import weakref
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

_log = logging.getLogger(__name__)


def _ts_now() -> str:
    """
    Return a millisecond-precision timestamp string.

    Format: YYYY-MM-DD HH:MM:SS.mmm

    Uses time.time() for the fractional part so events that occur within
    the same second are still distinguishable in the forensic log.
    This matters during fast reload cycles where multiple events can fire
    within the same second and strftime() alone would produce identical timestamps.
    """
    t = time.time()
    ms = int((t % 1) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


def _is_blender_class(obj: Any) -> bool:
    """
    Return True if obj is a bpy-registered class suitable for
    bpy.utils.register_class / unregister_class.

    RATIONALE FOR THIS HELPER
    ──────────────────────────
    The naive check `isinstance(obj, type) and hasattr(obj, "bl_rna")` produces
    false positives on:
        1. bpy built-in types (e.g. bpy.types.Object) — these carry bl_rna but
           must never be passed to unregister_class; doing so corrupts Blender.
        2. Abstract base classes in addons that inherit from a registered type
           but were never themselves passed to register_class, leaving them with
           a bl_rna attribute inherited from the parent.
        3. Any class with a manually defined bl_rna class variable (rare but
           possible in test code).

    CHECKS APPLIED (in order, cheapest first)
    ──────────────────────────────────────────
        a. Must be a type (class), not an instance.
        b. Must have bl_rna — necessary but not sufficient.
        c. bl_rna must be an instance of bpy.types.Struct (the RNA descriptor
           type that Blender assigns only during register_class).
           Built-in types pass (b) but their bl_rna.__class__ is a C-level type,
           not accessible directly — we guard via the is_registered() probe.
        d. bpy.utils.is_registered(obj) — the authoritative Blender API check.
           Returns True only if register_class() has been called for this exact
           class object. This eliminates all false positives at the cost of one
           extra call per class, which is negligible during a reload cycle.

    FALLBACK BEHAVIOUR
    ──────────────────
    If bpy is not importable (unit-test environment without Blender), the
    function returns False conservatively — no classes will be acted on.

    COMPATIBILITY
    ─────────────
    bpy.utils.is_registered() is available since Blender 2.80 and is stable
    through 4.x. It is the same check used internally by Blender's own
    addon framework (bl_i18n_utils and Rigify use it for safety guards).
    """
    if not isinstance(obj, type):
        return False
    if not hasattr(obj, "bl_rna"):
        return False
    # Fast exit: skip built-in bpy types that live in bpy.types directly.
    # Their __module__ is 'bpy_types', not an addon dotted name.
    if getattr(obj, "__module__", "") == "bpy_types":
        return False
    # Authoritative Blender check: was register_class() called for this object?
    try:
        import bpy as _bpy
        return _bpy.utils.is_registered(obj)
    except (ImportError, AttributeError):
        # bpy unavailable (test env) or is_registered() missing — conservative False.
        return False


# ── Generation counter (survives F8 via sys.modules) ─────────────────────────

_SYSMOD_GEN_KEY = "onixey3._reload_manager_state"


def _sysmod_state() -> Dict[str, Any]:
    if _SYSMOD_GEN_KEY not in sys.modules:
        sys.modules[_SYSMOD_GEN_KEY] = {  # type: ignore[assignment]
            "generation": 0,
            "total_reloads": 0,
            "total_failures": 0,
        }
    return sys.modules[_SYSMOD_GEN_KEY]  # type: ignore[return-value]


def _bump_generation() -> int:
    s = _sysmod_state()
    s["generation"] += 1
    s["total_reloads"] += 1
    return s["generation"]


def get_reload_generation() -> int:
    """Return the current reload generation counter."""
    return _sysmod_state().get("generation", 0)


def get_reload_stats() -> Dict[str, int]:
    """Return cumulative reload statistics."""
    s = _sysmod_state()
    return {
        "generation": s.get("generation", 0),
        "total_reloads": s.get("total_reloads", 0),
        "total_failures": s.get("total_failures", 0),
    }


# ── Enums ─────────────────────────────────────────────────────────────────────

class ReloadStatus(Enum):
    PENDING   = auto()
    RUNNING   = auto()
    SUCCESS   = auto()
    FAILED    = auto()
    ROLLED_BACK = auto()


class ModuleRole(Enum):
    """
    Role of a module within the addon dependency graph.
    Determines reload order: CORE → RUNTIME → PROPERTIES → OPERATORS → UI
    """
    CORE       = 0
    RUNTIME    = 1
    PROPERTIES = 2
    ANALYSIS   = 3
    OPERATORS  = 4
    UI         = 5
    MIGRATION  = 6
    VALIDATION = 7
    UNKNOWN    = 99


# ── Module descriptor ─────────────────────────────────────────────────────────

@dataclass
class ModuleDescriptor:
    """
    Describes a single reloadable module within the addon.

    Attributes:
        dotted_name:  Full dotted module name, e.g. "onixey3.core.feature_flags".
        role:         ModuleRole for ordering.
        register_fn:  Optional callable — module's register() function.
        unregister_fn:Optional callable — module's unregister() function.
        dependencies: Set of dotted_names this module depends on (must reload first).
        critical:     If True, a reload failure aborts the whole transaction.
    """
    dotted_name:    str
    role:           ModuleRole = ModuleRole.UNKNOWN
    register_fn:    Optional[Callable[[], None]] = None
    unregister_fn:  Optional[Callable[[], None]] = None
    dependencies:   FrozenSet[str] = field(default_factory=frozenset)
    critical:       bool = True

    def __hash__(self) -> int:
        return hash(self.dotted_name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModuleDescriptor):
            return NotImplemented
        return self.dotted_name == other.dotted_name


# ── Snapshot for rollback ─────────────────────────────────────────────────────

@dataclass
class _ModuleSnapshot:
    """
    Point-in-time snapshot of a module reference for rollback purposes.
    Stores the module object (not a deep copy — we restore the reference).
    """
    dotted_name:    str
    module_ref:     Any          # The actual module object before reload
    was_present:    bool         # False if the module was absent from sys.modules


@dataclass
class _HandlerSnapshot:
    """
    Snapshot of handler list lengths before a reload transaction.
    Used to detect handler leakage after a failed reload.
    """
    name:  str
    count: int


# ── Reload event (forensic log entry) ─────────────────────────────────────────

@dataclass
class ReloadEvent:
    ts:         str
    event_type: str  # TRANSACTION_START, MODULE_RELOAD, ROLLBACK, etc.
    message:    str
    generation: int
    data:       Dict[str, Any] = field(default_factory=dict)

    def as_line(self) -> str:
        extras = "  ".join(f"{k}={v}" for k, v in self.data.items()) if self.data else ""
        return (
            f"[{self.ts}] gen={self.generation:03d}  {self.event_type:<22} {self.message}"
            + (f"  ({extras})" if extras else "")
        )


# ── Transaction result ────────────────────────────────────────────────────────

@dataclass
class ReloadResult:
    status:          ReloadStatus
    generation:      int
    duration_ms:     float
    reloaded:        List[str]        = field(default_factory=list)
    skipped:         List[str]        = field(default_factory=list)
    failed:          List[str]        = field(default_factory=list)
    rolled_back:     List[str]        = field(default_factory=list)
    orphans_removed: List[str]        = field(default_factory=list)
    handler_leaks:   List[str]        = field(default_factory=list)
    # ── Sweep diagnostics (populated by startup_sweep / shutdown) ─────────────
    timers_removed:        List[str]  = field(default_factory=list)
    msgbus_cleared:        List[str]  = field(default_factory=list)
    duplicate_handlers:    List[str]  = field(default_factory=list)
    stale_gen_handlers:    List[str]  = field(default_factory=list)
    error:           Optional[str]    = None

    @property
    def ok(self) -> bool:
        return self.status == ReloadStatus.SUCCESS


# ── Reload manager ────────────────────────────────────────────────────────────

class ReloadManager:
    """
    AAA-grade safe module reload manager for Blender addons.

    Provides:
        - Dependency-aware topological reload order.
        - Full snapshot/rollback on any failure.
        - Handler leak detection and cleanup.
        - Orphan / ghost module purge.
        - Stale weakref cleanup.
        - Atomic transaction semantics: all-or-nothing.
        - Forensic event log (bounded circular buffer).
        - Reload generation tracking.

    Usage:
        manager = ReloadManager(package_root="onixey3")
        manager.register_module(ModuleDescriptor(
            dotted_name="onixey3.core.feature_flags",
            role=ModuleRole.CORE,
            critical=True,
        ))
        result = manager.execute_reload()
        if not result.ok:
            print(result.error)
    """

    _FORENSIC_MAX = 256

    def __init__(self, package_root: str) -> None:
        """
        Args:
            package_root: Top-level package name, e.g. "onixey3".
                          Used to scope sys.modules scanning.
        """
        self._package_root: str = package_root
        self._descriptors:  Dict[str, ModuleDescriptor] = {}
        self._events:       List[ReloadEvent] = []
        self._active:       bool = False  # True while a transaction is running

    # ── Descriptor registration ───────────────────────────────────────────────

    def register_module(self, descriptor: ModuleDescriptor) -> None:
        """
        Register a module descriptor.
        Idempotent: re-registering the same dotted_name replaces the descriptor.
        Must be called before execute_reload().
        """
        if self._active:
            raise RuntimeError(
                f"ReloadManager: cannot register '{descriptor.dotted_name}' "
                "while a reload transaction is running."
            )
        self._descriptors[descriptor.dotted_name] = descriptor
        _log.debug("ReloadManager: registered '%s' (role=%s)", descriptor.dotted_name, descriptor.role.name)

    def register_modules(self, descriptors: List[ModuleDescriptor]) -> None:
        """Bulk register. See register_module()."""
        for d in descriptors:
            self.register_module(d)

    def unregister_module(self, dotted_name: str) -> None:
        """Remove a descriptor. No-op if not registered."""
        self._descriptors.pop(dotted_name, None)

    # ── Main entry point ──────────────────────────────────────────────────────

    def execute_reload(self) -> ReloadResult:
        """
        Execute a full atomic reload transaction.

        Steps:
            1. Validate no transaction is already running.
            2. Snapshot handler state (for leak detection).
            3. Topological sort of descriptors (dependency-aware order).
            4. Snapshot current sys.modules refs (for rollback).
            5. Unregister live classes (Blender-safe cycle).
            6. Reload modules in order.
            7. Re-register classes.
            8. Post-reload validation: orphans, ghosts, handler leaks.
            9. On ANY failure: rollback to snapshot, restore handlers.
           10. Return ReloadResult.

        Thread safety: Blender is single-threaded. No locks used.
        """
        if self._active:
            msg = "ReloadManager: execute_reload() called while already active."
            _log.error(msg)
            return ReloadResult(
                status=ReloadStatus.FAILED,
                generation=get_reload_generation(),
                duration_ms=0.0,
                error=msg,
            )

        self._active = True
        t_start = time.perf_counter()
        gen = _bump_generation()
        result = ReloadResult(status=ReloadStatus.RUNNING, generation=gen, duration_ms=0.0)

        self._log_event("TRANSACTION_START", f"Reload transaction gen={gen} starting", gen,
                        module_count=len(self._descriptors))

        try:
            # Step 0: Pre-startup sweep — remove contamination from prior cycles.
            # Runs before any module is touched so the new runtime starts clean.
            self.startup_sweep(
                gen=gen,
                result=result,
                msgbus_owner=get_msgbus_owner(),
            )

            # Step 1: Topological sort
            ordered = self._topological_sort()

            # Step 2: Handler snapshot
            handler_snapshot = self._snapshot_handlers()

            # Step 3: sys.modules snapshot
            module_snapshot = self._snapshot_modules(ordered)

            # Step 4: Unregister pass (reverse order)
            self._unregister_pass(list(reversed(ordered)), result)

            # Step 5: Reload pass
            reload_ok = self._reload_pass(ordered, result, gen)

            if not reload_ok:
                # Reload pass failed — rollback to snapshot.
                self._rollback(module_snapshot, handler_snapshot, result, gen)
                result.status = ReloadStatus.ROLLED_BACK
                result.error = (
                    f"Reload failed for: {result.failed}. "
                    "Transaction rolled back to pre-reload state."
                )
                _sysmod_state()["total_failures"] += 1
                self._log_event("TRANSACTION_FAIL", result.error, gen, failed=result.failed)
            else:
                # Step 6: Re-register pass (atomic — rolls back internally on critical failure).
                register_ok = self._reregister_pass(
                    ordered,
                    result,
                    module_snapshot,
                    handler_snapshot,
                    gen,
                )

                if not register_ok:
                    # _reregister_pass already executed rollback internally.
                    result.status = ReloadStatus.ROLLED_BACK
                    result.error = (
                        f"Register phase failed for: {result.failed}. "
                        "Transaction rolled back to pre-reload state."
                    )
                    _sysmod_state()["total_failures"] += 1
                    self._log_event(
                        "TRANSACTION_FAIL",
                        result.error,
                        gen,
                        failed=result.failed,
                        phase="register",
                    )
                else:
                    # Step 7: Post-reload cleanup (only when fully successful).
                    self._purge_orphans(result, gen)
                    self._check_handler_leaks(handler_snapshot, result, gen)

                    result.status = ReloadStatus.SUCCESS
                    self._log_event(
                        "TRANSACTION_SUCCESS",
                        f"Reload gen={gen} complete",
                        gen,
                        reloaded=len(result.reloaded),
                        orphans_removed=len(result.orphans_removed),
                        handler_leaks=len(result.handler_leaks),
                    )

        except Exception as exc:
            tb = traceback.format_exc()
            result.status = ReloadStatus.FAILED
            result.error = f"Unexpected exception: {exc}"
            _sysmod_state()["total_failures"] += 1
            _log.error("ReloadManager: unhandled exception in transaction:\n%s", tb)
            self._log_event("TRANSACTION_EXCEPTION", str(exc), gen, traceback=tb[:200])

        finally:
            result.duration_ms = (time.perf_counter() - t_start) * 1000.0
            self._active = False

        _log.info(
            "ReloadManager gen=%d  status=%s  %.2fms  reloaded=%d failed=%d "
            "orphans=%d handler_leaks=%d dup_handlers=%d stale_handlers=%d "
            "timers=%d msgbus=%d",
            gen, result.status.name, result.duration_ms,
            len(result.reloaded), len(result.failed),
            len(result.orphans_removed), len(result.handler_leaks),
            len(result.duplicate_handlers), len(result.stale_gen_handlers),
            len(result.timers_removed), len(result.msgbus_cleared),
        )
        return result

    # ── Topological sort ──────────────────────────────────────────────────────

    def _topological_sort(self) -> List[ModuleDescriptor]:
        """
        Return descriptors in dependency-aware reload order.

        Algorithm: Kahn's BFS topological sort.
        Tiebreak within the same dependency level: sort by ModuleRole value (ascending).
        Raises RuntimeError on circular dependency.
        """
        name_to_desc = dict(self._descriptors)
        in_degree: Dict[str, int] = {n: 0 for n in name_to_desc}
        dependents: Dict[str, List[str]] = {n: [] for n in name_to_desc}

        for name, desc in name_to_desc.items():
            for dep in desc.dependencies:
                if dep in name_to_desc:
                    in_degree[name] += 1
                    dependents[dep].append(name)

        # Nodes with no unresolved dependencies, sorted by role
        ready: List[str] = sorted(
            (n for n, d in in_degree.items() if d == 0),
            key=lambda n: name_to_desc[n].role.value,
        )
        ordered: List[ModuleDescriptor] = []

        while ready:
            name = ready.pop(0)
            ordered.append(name_to_desc[name])
            for dependent in sorted(dependents[name], key=lambda n: name_to_desc[n].role.value):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    # Insert sorted by role
                    insert_idx = 0
                    for i, r in enumerate(ready):
                        if name_to_desc[r].role.value <= name_to_desc[dependent].role.value:
                            insert_idx = i + 1
                    ready.insert(insert_idx, dependent)

        if len(ordered) != len(name_to_desc):
            cycle_nodes = [n for n in name_to_desc if n not in {d.dotted_name for d in ordered}]
            raise RuntimeError(
                f"ReloadManager: circular dependency detected among: {cycle_nodes}"
            )

        return ordered

    # ── Snapshot / rollback ───────────────────────────────────────────────────

    def _snapshot_modules(self, ordered: List[ModuleDescriptor]) -> List[_ModuleSnapshot]:
        snapshots: List[_ModuleSnapshot] = []
        for desc in ordered:
            mod = sys.modules.get(desc.dotted_name)
            snapshots.append(_ModuleSnapshot(
                dotted_name=desc.dotted_name,
                module_ref=mod,
                was_present=mod is not None,
            ))
        return snapshots

    def _snapshot_handlers(self) -> List[_HandlerSnapshot]:
        try:
            import bpy
            H = bpy.app.handlers
            names = [
                "depsgraph_update_post", "depsgraph_update_pre",
                "frame_change_post", "frame_change_pre",
                "load_post", "load_pre",
                "save_post", "save_pre",
                "undo_post", "undo_pre",
                "redo_post", "redo_pre",
                "render_complete", "render_init", "render_cancel",
            ]
            return [
                _HandlerSnapshot(name=n, count=len(getattr(H, n, [])))
                for n in names
            ]
        except Exception as exc:
            _log.debug("ReloadManager: could not snapshot handlers: %s", exc)
            return []

    def _rollback(
        self,
        snapshots:        List[_ModuleSnapshot],
        handler_snapshot: List[_HandlerSnapshot],
        result:           ReloadResult,
        gen:              int,
    ) -> None:
        self._log_event("ROLLBACK_START", f"Rolling back {len(snapshots)} module(s)", gen)

        for snap in snapshots:
            try:
                if snap.was_present and snap.module_ref is not None:
                    sys.modules[snap.dotted_name] = snap.module_ref
                    result.rolled_back.append(snap.dotted_name)
                    _log.debug("ReloadManager: rolled back '%s'", snap.dotted_name)
                elif not snap.was_present and snap.dotted_name in sys.modules:
                    del sys.modules[snap.dotted_name]
                    _log.debug("ReloadManager: removed injected module '%s' during rollback", snap.dotted_name)
            except Exception as exc:
                _log.error("ReloadManager: rollback error for '%s': %s", snap.dotted_name, exc)

        # Attempt to re-register from rolled-back modules
        for snap in snapshots:
            if not snap.was_present or snap.module_ref is None:
                continue
            mod = sys.modules.get(snap.dotted_name)
            if mod is None:
                continue
            desc = self._descriptors.get(snap.dotted_name)
            if desc and desc.register_fn:
                try:
                    desc.register_fn()
                    _log.debug("ReloadManager: re-registered (rollback) '%s'", snap.dotted_name)
                except Exception as exc:
                    _log.error("ReloadManager: rollback re-register failed for '%s': %s", snap.dotted_name, exc)

        # Restore handler counts — remove excess handlers
        self._restore_handler_counts(handler_snapshot, gen)

        self._log_event("ROLLBACK_DONE", f"Rollback complete. {len(result.rolled_back)} modules restored.", gen)

    # ── Unregister pass ───────────────────────────────────────────────────────

    def _unregister_pass(self, reversed_order: List[ModuleDescriptor], result: ReloadResult) -> None:
        for desc in reversed_order:
            if desc.unregister_fn is None:
                continue
            mod = sys.modules.get(desc.dotted_name)
            if mod is None:
                continue
            try:
                desc.unregister_fn()
                _log.debug("ReloadManager: unregistered '%s'", desc.dotted_name)
            except Exception as exc:
                _log.warning(
                    "ReloadManager: unregister() failed for '%s' (non-fatal): %s",
                    desc.dotted_name, exc,
                )
            # Blender-safe: unregister all tracked bpy classes from this module
            self._unregister_module_classes(desc.dotted_name)

    def _unregister_module_classes(self, dotted_name: str) -> None:
        """
        Unregister all bpy classes found in the module, in reverse MRO order.
        Prevents 'already registered' errors and ghost class contamination.
        """
        try:
            import bpy
        except ImportError:
            return

        mod = sys.modules.get(dotted_name)
        if mod is None:
            return

        classes = [
            obj for obj in vars(mod).values()
            if _is_blender_class(obj)
        ]
        # Reverse: unregister dependents before bases
        for cls in reversed(classes):
            cls_name = getattr(cls, "__name__", repr(cls))
            try:
                bpy.utils.unregister_class(cls)
                _log.debug("ReloadManager: unregistered class '%s' from '%s'", cls_name, dotted_name)
            except RuntimeError as exc:
                if "not registered" not in str(exc).lower():
                    _log.debug("ReloadManager: class '%s' unregister skipped: %s", cls_name, exc)
            except Exception as exc:
                _log.warning("ReloadManager: unexpected class unregister error '%s': %s", cls_name, exc)

    # ── Reload pass ───────────────────────────────────────────────────────────

    def _reload_pass(
        self,
        ordered: List[ModuleDescriptor],
        result:  ReloadResult,
        gen:     int,
    ) -> bool:
        for desc in ordered:
            mod = sys.modules.get(desc.dotted_name)

            if mod is None:
                # Not yet imported — attempt first import
                try:
                    importlib.import_module(desc.dotted_name)
                    result.reloaded.append(desc.dotted_name)
                    self._log_event(
                        "MODULE_IMPORT", f"First import: '{desc.dotted_name}'", gen,
                        module=desc.dotted_name,
                    )
                    _log.debug("ReloadManager: imported (new) '%s'", desc.dotted_name)
                except Exception as exc:
                    result.failed.append(desc.dotted_name)
                    self._log_event(
                        "MODULE_IMPORT_FAIL", f"Import failed: '{desc.dotted_name}': {exc}", gen,
                        module=desc.dotted_name,
                    )
                    _log.error("ReloadManager: import failed '%s': %s", desc.dotted_name, exc)
                    if desc.critical:
                        return False
                    continue
            else:
                # Already in sys.modules — reload
                try:
                    importlib.reload(mod)
                    result.reloaded.append(desc.dotted_name)
                    self._log_event(
                        "MODULE_RELOAD", f"Reloaded: '{desc.dotted_name}'", gen,
                        module=desc.dotted_name,
                    )
                    _log.debug("ReloadManager: reloaded '%s'", desc.dotted_name)
                except Exception as exc:
                    result.failed.append(desc.dotted_name)
                    self._log_event(
                        "MODULE_RELOAD_FAIL", f"Reload failed: '{desc.dotted_name}': {exc}", gen,
                        module=desc.dotted_name, error=str(exc),
                    )
                    _log.error("ReloadManager: reload failed '%s': %s\n%s",
                               desc.dotted_name, exc, traceback.format_exc())
                    if desc.critical:
                        return False
                    continue

        return True

    # ── Re-register pass ──────────────────────────────────────────────────────

    def _reregister_pass(
        self,
        ordered:         List[ModuleDescriptor],
        result:          ReloadResult,
        module_snapshot: List[_ModuleSnapshot],
        handler_snapshot: List[_HandlerSnapshot],
        gen:             int,
    ) -> bool:
        """
        Register all reloaded modules in topological order.

        ROLLBACK ON CRITICAL FAILURE
        ─────────────────────────────
        If register_fn() raises for a module marked critical=True:
            1. All modules successfully registered in THIS pass are
               unregistered in reverse order (partial-registration cleanup).
            2. The full module snapshot rollback is executed to restore
               sys.modules to its pre-reload state.
            3. Returns False so execute_reload() sets ROLLED_BACK status.

        For non-critical failures the module is added to result.failed and
        execution continues — partial registration is acceptable for
        optional features.

        Returns:
            True  — all critical modules registered successfully.
            False — a critical register() failed; rollback was executed.
        """
        registered_in_pass: List[ModuleDescriptor] = []

        for desc in ordered:
            if desc.register_fn is None:
                continue

            # Refresh register_fn reference from the newly reloaded module.
            fresh_fn = self._resolve_fresh_fn(desc.dotted_name, "register")
            fn = fresh_fn if fresh_fn is not None else desc.register_fn

            try:
                fn()
                registered_in_pass.append(desc)
                _log.debug("ReloadManager: registered '%s'", desc.dotted_name)
            except Exception as exc:
                tb = traceback.format_exc()
                _log.error(
                    "ReloadManager: register() failed for '%s': %s\n%s",
                    desc.dotted_name, exc, tb,
                )
                self._log_event(
                    "REGISTER_FAIL",
                    f"register() failed for '{desc.dotted_name}': {exc}",
                    gen,
                    module=desc.dotted_name,
                    critical=desc.critical,
                    error=str(exc),
                )
                result.failed.append(desc.dotted_name)

                if desc.critical:
                    # Step 1: Undo the registrations performed in this pass
                    # (reverse order — last registered, first unregistered).
                    self._log_event(
                        "REGISTER_ROLLBACK_START",
                        f"Critical register() failure at '{desc.dotted_name}'. "
                        f"Unregistering {len(registered_in_pass)} module(s) from this pass.",
                        gen,
                        failed_module=desc.dotted_name,
                        pass_registered=len(registered_in_pass),
                    )
                    for rollback_desc in reversed(registered_in_pass):
                        rfn = self._resolve_fresh_fn(rollback_desc.dotted_name, "unregister")
                        if rfn is not None:
                            try:
                                rfn()
                                _log.debug(
                                    "ReloadManager: pass-rollback unregistered '%s'",
                                    rollback_desc.dotted_name,
                                )
                            except Exception as rb_exc:
                                _log.error(
                                    "ReloadManager: pass-rollback unregister failed for '%s': %s",
                                    rollback_desc.dotted_name, rb_exc,
                                )
                        # Belt-and-suspenders: also purge bpy classes directly.
                        self._unregister_module_classes(rollback_desc.dotted_name)

                    # Step 2: Full sys.modules + handler snapshot rollback.
                    self._rollback(module_snapshot, handler_snapshot, result, gen)

                    self._log_event(
                        "REGISTER_ROLLBACK_DONE",
                        f"Register-phase rollback complete. "
                        f"Restored {len(result.rolled_back)} module snapshot(s).",
                        gen,
                    )
                    return False
                # Non-critical: log and continue.

        return True

    def _resolve_fresh_fn(self, dotted_name: str, attr: str) -> Optional[Callable[[], None]]:
        """Retrieve attr from the freshly reloaded module object."""
        mod = sys.modules.get(dotted_name)
        if mod is None:
            return None
        fn = getattr(mod, attr, None)
        return fn if callable(fn) else None

    # ── Orphan / ghost module purge ───────────────────────────────────────────

    def _purge_orphans(self, result: ReloadResult, gen: int) -> None:
        """
        Remove orphaned sub-modules of the package from sys.modules.

        An orphan is a module whose dotted name starts with package_root
        but is NOT registered in self._descriptors. These accumulate from
        abandoned module renames, partial reloads, and legacy code paths.
        """
        prefix = self._package_root + "."
        registered = set(self._descriptors.keys())
        registered.add(self._package_root)  # Root package is always valid

        orphans = [
            name for name in list(sys.modules.keys())
            if (name == self._package_root or name.startswith(prefix))
            and name not in registered
        ]

        for name in orphans:
            try:
                del sys.modules[name]
                result.orphans_removed.append(name)
                self._log_event("ORPHAN_REMOVED", f"Removed orphan module '{name}'", gen, module=name)
                _log.debug("ReloadManager: removed orphan module '%s'", name)
            except Exception as exc:
                _log.warning("ReloadManager: could not remove orphan '%s': %s", name, exc)

    # ── Handler leak detection ─────────────────────────────────────────────────

    def _check_handler_leaks(
        self,
        before:  List[_HandlerSnapshot],
        result:  ReloadResult,
        gen:     int,
    ) -> None:
        if not before:
            return
        try:
            import bpy
            H = bpy.app.handlers
            for snap in before:
                current_count = len(getattr(H, snap.name, []))
                if current_count > snap.count:
                    leak_count = current_count - snap.count
                    msg = (
                        f"Handler leak detected: bpy.app.handlers.{snap.name} grew by "
                        f"{leak_count} (before={snap.count}, after={current_count}). "
                        f"A module's register() added handlers without corresponding "
                        f"unregister() cleanup."
                    )
                    result.handler_leaks.append(msg)
                    self._log_event(
                        "HANDLER_LEAK", msg, gen,
                        handler=snap.name,
                        before=snap.count,
                        after=current_count,
                        leaked=leak_count,
                    )
                    _log.warning("ReloadManager: %s", msg)
        except Exception as exc:
            _log.debug("ReloadManager: handler leak check failed: %s", exc)

    def _restore_handler_counts(
        self,
        before: List[_HandlerSnapshot],
        gen:    int,
    ) -> None:
        """
        After rollback, remove Onixey handlers that were added during the
        failed reload transaction.

        CHANGE vs original
        ──────────────────
        Old approach: trimmed TRAILING entries blindly with list.pop().
        Problem: if a third-party addon appended a handler AFTER Onixey during
        the same frame, pop() would remove the third-party handler, not Onixey's.

        New approach:
        1. For each handler list that grew beyond its snapshot count:
           a. First try to remove Onixey callbacks by module identity
              (callback.__module__ starts with self._package_root).
           b. If after that the list is still above snapshot count,
              fall back to trimming trailing entries (original behaviour).
        This is strictly safer: we target our own handlers first.
        """
        if not before:
            return
        try:
            import bpy
            H = bpy.app.handlers
            for snap in before:
                handler_list = getattr(H, snap.name, None)
                if handler_list is None:
                    continue
                excess = len(handler_list) - snap.count
                if excess <= 0:
                    continue

                # Pass 1: remove Onixey callbacks added after snapshot
                prefix = self._package_root + "."
                removed_ids: List[int] = []
                for cb in list(handler_list):
                    mod = getattr(cb, "__module__", "") or ""
                    if mod == self._package_root or mod.startswith(prefix):
                        try:
                            handler_list.remove(cb)
                            removed_ids.append(id(cb))
                            _log.debug(
                                "ReloadManager: rollback removed Onixey handler '%s' from %s",
                                getattr(cb, "__qualname__", repr(cb)), snap.name,
                            )
                        except (ValueError, Exception):
                            pass
                    if len(handler_list) <= snap.count:
                        break

                # Pass 2: if still above snapshot count, trim trailing (safe fallback)
                remaining_excess = len(handler_list) - snap.count
                for _ in range(max(0, remaining_excess)):
                    handler_list.pop()

                actually_removed = excess - (len(handler_list) - snap.count) + remaining_excess
                trimmed_total = excess - max(0, len(handler_list) - snap.count)
                if trimmed_total > 0:
                    self._log_event(
                        "HANDLER_TRIM",
                        f"Rollback removed {trimmed_total} handler(s) from {snap.name} "
                        f"({len(removed_ids)} by identity, {max(0, remaining_excess)} by position)",
                        gen,
                        handler=snap.name,
                        trimmed=trimmed_total,
                        by_identity=len(removed_ids),
                        by_position=max(0, remaining_excess),
                    )
                    _log.debug(
                        "ReloadManager: trimmed %d handler(s) from %s during rollback",
                        trimmed_total, snap.name,
                    )
        except Exception as exc:
            _log.warning("ReloadManager: could not restore handler counts: %s", exc)

    # ── sys.modules validation ────────────────────────────────────────────────

    def validate_sys_modules(self) -> List[str]:
        """
        Scan sys.modules for stale or inconsistent entries related to this package.

        Returns a list of human-readable issue descriptions.
        A module is considered stale if:
          - Its __file__ is set but points to a path that no longer exists on disk.
          - Its dotted name starts with package_root but __package__ is inconsistent.
          - It is a known descriptor module but the sys.modules entry is None-valued.
        """
        import os
        issues: List[str] = []
        prefix = self._package_root + "."

        for name, mod in list(sys.modules.items()):
            if not (name == self._package_root or name.startswith(prefix)):
                continue
            if mod is None:
                issues.append(f"None-valued entry in sys.modules: '{name}'. "
                               "This is a sentinel for blocked imports — investigate.")
                continue
            mod_file = getattr(mod, "__file__", None)
            if mod_file and not os.path.exists(mod_file):
                issues.append(
                    f"Stale module '{name}': __file__='{mod_file}' does not exist on disk. "
                    "Module was likely moved or deleted. Call execute_reload() to resync."
                )
            pkg = getattr(mod, "__package__", None)
            if pkg and not (pkg == self._package_root or pkg.startswith(prefix)):
                issues.append(
                    f"Package mismatch for '{name}': __package__='{pkg}' is outside "
                    f"the expected root '{self._package_root}'. Possible import pollution."
                )

        return issues

    def purge_stale_references(self) -> List[str]:
        """
        Remove all None-valued or ghost entries from sys.modules for this package.

        Returns list of removed module names.
        Does NOT reload anything — use execute_reload() for that.
        """
        prefix = self._package_root + "."
        removed: List[str] = []

        for name in list(sys.modules.keys()):
            if not (name == self._package_root or name.startswith(prefix)):
                continue
            if sys.modules[name] is None:
                del sys.modules[name]
                removed.append(name)
                _log.debug("ReloadManager: purged None entry '%s' from sys.modules", name)

        return removed

    # ── Stale weakref sweep ───────────────────────────────────────────────────

    def sweep_stale_weakrefs(self, container: Dict[str, Any]) -> List[str]:
        """
        Sweep a dict that may contain weakref.ref values, removing dead references.

        Args:
            container: Any dict whose values may include weakref.ref instances.
                       Modifies the dict in-place.

        Returns:
            List of keys whose weakref was dead and was removed.
        """
        dead: List[str] = []
        for key, value in list(container.items()):
            if isinstance(value, weakref.ref) and value() is None:
                del container[key]
                dead.append(key)
                _log.debug("ReloadManager: swept dead weakref for key '%s'", key)
        return dead

    # ── Duplicate class prevention ────────────────────────────────────────────

    def find_duplicate_classes(self) -> Dict[str, List[str]]:
        """
        Scan all registered descriptor modules for duplicate bl_idname values.

        Returns:
            Dict mapping bl_idname → [list of class names that share it].
            Empty dict if no duplicates found.

        Use before execute_reload() to identify classes that will cause
        'already registered' errors in Blender.
        """
        try:
            import bpy as _bpy  # noqa: F401
        except ImportError:
            return {}

        bl_idname_map: Dict[str, List[str]] = {}

        for dotted_name in self._descriptors:
            mod = sys.modules.get(dotted_name)
            if mod is None:
                continue
            for obj in vars(mod).values():
                if not _is_blender_class(obj):
                    continue
                bl_id = getattr(obj, "bl_idname", None)
                if bl_id is None:
                    continue
                cls_name = f"{dotted_name}.{obj.__name__}"
                bl_idname_map.setdefault(bl_id, []).append(cls_name)

        return {k: v for k, v in bl_idname_map.items() if len(v) > 1}

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def get_forensic_log(self) -> List[Dict[str, Any]]:
        """Return the forensic event log as plain dicts (safe to log/display)."""
        return [
            {
                "ts":         ev.ts,
                "event_type": ev.event_type,
                "message":    ev.message,
                "generation": ev.generation,
                "data":       dict(ev.data),
            }
            for ev in self._events[-self._FORENSIC_MAX:]
        ]

    def get_diagnostic_report(self) -> str:
        """Return a compact human-readable diagnostic string."""
        sep = "─" * 60
        lines = [
            f"ReloadManager  pkg={self._package_root}  gen={get_reload_generation()}",
            sep,
            f"Registered modules : {len(self._descriptors)}",
        ]
        stats = get_reload_stats()
        lines += [
            f"Total reloads      : {stats['total_reloads']}",
            f"Total failures     : {stats['total_failures']}",
            f"Timer registry     : {len(_sysmod_timer_registry())} pending cleanup",
            "",
            "MODULES (role / name):",
        ]
        try:
            ordered = self._topological_sort()
        except RuntimeError:
            ordered = list(self._descriptors.values())

        for desc in ordered:
            present = "✔" if desc.dotted_name in sys.modules else "✖"
            crit    = " CRITICAL" if desc.critical else ""
            lines.append(f"  {present} [{desc.role.name:<10}]{crit}  {desc.dotted_name}")

        sys_issues = self.validate_sys_modules()
        if sys_issues:
            lines += ["", f"SYS.MODULES ISSUES ({len(sys_issues)}):"]
            for issue in sys_issues:
                lines.append(f"  ⚠  {issue}")

        duplicates = self.find_duplicate_classes()
        if duplicates:
            lines += ["", f"DUPLICATE bl_idname ({len(duplicates)}):"]
            for bl_id, cls_list in duplicates.items():
                lines.append(f"  '{bl_id}': {cls_list}")

        log_entries = self.get_forensic_log()
        if log_entries:
            lines += ["", f"FORENSIC LOG (last {min(10, len(log_entries))} of {len(log_entries)}):"]
            for ev in log_entries[-10:]:
                lines.append(f"  {ev['ts']}  gen={ev['generation']:03d}  {ev['event_type']:<22} {ev['message']}")

        lines.append(sep)
        return "\n".join(lines)

    # ── Startup cleanup sweep ────────────────────────────────────────────────
    # Called BEFORE the new runtime initializes.
    # Eliminates contamination from prior reload cycles: duplicate handlers,
    # stale-generation handlers, orphan timers, and msgbus subscriptions
    # that survived F8 because the previous shutdown path was interrupted.

    def startup_sweep(
        self,
        gen:    int,
        result: Optional[ReloadResult] = None,
        *,
        msgbus_owner: Any = None,
    ) -> Dict[str, List[str]]:
        """
        Pre-startup contamination sweep.

        Must be called BEFORE lifecycle.startup() initialises any singletons,
        so that the new runtime starts from a clean Blender state.

        What this does (in order):
        1. Remove duplicate Onixey handlers from every bpy.app.handlers list.
        2. Remove stale-generation Onixey handlers (module generation mismatch).
        3. Unregister Onixey timers that survived a prior F8.
        4. Clear msgbus subscriptions owned by a prior Onixey session.

        Args:
            gen:          Current reload generation (from _bump_generation()).
            result:       Optional ReloadResult to accumulate sweep diagnostics.
                          If None, a temporary dict is used.
            msgbus_owner: The owner object used with bpy.msgbus.subscribe_rna()
                          in the previous session. If None, msgbus cleanup is
                          skipped (no owner → no way to clear selectively).

        Returns:
            Dict with keys:
                "duplicate_handlers"  — list of removed duplicate entries
                "stale_gen_handlers"  — list of removed stale-generation entries
                "timers_removed"      — list of removed timer repr strings
                "msgbus_cleared"      — list of cleared owner repr strings
        """
        report: Dict[str, List[str]] = {
            "duplicate_handlers": [],
            "stale_gen_handlers": [],
            "timers_removed":     [],
            "msgbus_cleared":     [],
        }

        # 1 & 2: handler sweep
        dup, stale = self._sweep_onixey_handlers(gen)
        report["duplicate_handlers"].extend(dup)
        report["stale_gen_handlers"].extend(stale)

        # 3: timer sweep
        report["timers_removed"].extend(self._unregister_timers(gen))

        # 4: msgbus sweep
        if msgbus_owner is not None:
            report["msgbus_cleared"].extend(self._clear_msgbus(msgbus_owner, gen))

        # Propagate to ReloadResult if provided
        if result is not None:
            result.duplicate_handlers.extend(report["duplicate_handlers"])
            result.stale_gen_handlers.extend(report["stale_gen_handlers"])
            result.timers_removed.extend(report["timers_removed"])
            result.msgbus_cleared.extend(report["msgbus_cleared"])

        total = sum(len(v) for v in report.values())
        if total:
            _log.info(
                "ReloadManager.startup_sweep gen=%d: removed %d item(s) "
                "(dup_handlers=%d stale_handlers=%d timers=%d msgbus=%d)",
                gen,
                total,
                len(report["duplicate_handlers"]),
                len(report["stale_gen_handlers"]),
                len(report["timers_removed"]),
                len(report["msgbus_cleared"]),
            )
            self._log_event(
                "STARTUP_SWEEP",
                f"Pre-startup sweep removed {total} item(s)",
                gen,
                duplicate_handlers=len(report["duplicate_handlers"]),
                stale_handlers=len(report["stale_gen_handlers"]),
                timers=len(report["timers_removed"]),
                msgbus=len(report["msgbus_cleared"]),
            )
        else:
            _log.debug("ReloadManager.startup_sweep gen=%d: environment is clean.", gen)

        return report

    def _sweep_onixey_handlers(self, gen: int) -> Tuple[List[str], List[str]]:
        """
        Scan all bpy.app.handlers lists and remove Onixey callbacks that are:
          (a) duplicates — same qualified name registered more than once, or
          (b) stale-generation — callback's module is in sys.modules under a
              different id than the currently live module object, meaning the
              callback holds a reference to a pre-F8 module.

        Returns:
            (duplicates_removed, stale_removed) — list of repr strings per category.

        NEVER raises. All errors are logged at WARNING level and skipped.
        """
        duplicates_removed: List[str] = []
        stale_removed:      List[str] = []

        try:
            import bpy
            H = bpy.app.handlers
        except Exception:
            return duplicates_removed, stale_removed

        handler_attrs = [
            "depsgraph_update_post", "depsgraph_update_pre",
            "frame_change_post",     "frame_change_pre",
            "load_post",             "load_pre",
            "save_post",             "save_pre",
            "undo_post",             "undo_pre",
            "redo_post",             "redo_pre",
            "render_complete",       "render_init", "render_cancel",
        ]
        prefix = self._package_root + "."

        for attr in handler_attrs:
            handler_list = getattr(H, attr, None)
            if not isinstance(handler_list, list):
                continue

            # Collect Onixey entries with their positions
            onixey_entries: List[Tuple[int, Any]] = []
            for i, cb in enumerate(handler_list):
                mod = getattr(cb, "__module__", "") or ""
                if mod == self._package_root or mod.startswith(prefix):
                    onixey_entries.append((i, cb))

            if not onixey_entries:
                continue

            # (a) Duplicate detection: group by qualname
            seen_qualnames: Dict[str, List[Tuple[int, Any]]] = {}
            for idx, cb in onixey_entries:
                qname = getattr(cb, "__qualname__", repr(cb))
                seen_qualnames.setdefault(qname, []).append((idx, cb))

            for qname, occurrences in seen_qualnames.items():
                if len(occurrences) <= 1:
                    continue
                # Keep the LAST registration (most recent), remove earlier ones
                for idx, cb in occurrences[:-1]:
                    try:
                        handler_list.remove(cb)
                        label = f"{attr}.{qname}"
                        duplicates_removed.append(label)
                        _log.warning(
                            "ReloadManager.sweep: removed duplicate Onixey handler "
                            "'%s' from bpy.app.handlers.%s",
                            qname, attr,
                        )
                        self._log_event(
                            "HANDLER_DUPLICATE_REMOVED",
                            f"Removed duplicate handler '{qname}' from {attr}",
                            gen,
                            handler_list=attr,
                            qualname=qname,
                        )
                    except (ValueError, Exception) as exc:
                        _log.warning(
                            "ReloadManager.sweep: could not remove duplicate '%s' from %s: %s",
                            qname, attr, exc,
                        )

            # (b) Stale-generation detection: callback's module object differs
            #     from the currently live sys.modules entry for that module name.
            for _idx, cb in onixey_entries:
                cb_module_name = getattr(cb, "__module__", "") or ""
                if not cb_module_name:
                    continue
                live_mod = sys.modules.get(cb_module_name)
                if live_mod is None:
                    # Module no longer in sys.modules — definitely stale.
                    is_stale = True
                else:
                    # Compare object identity: same name but different object
                    # means this cb was bound to a pre-F8 module copy.
                    try:
                        cb_globals = getattr(cb, "__globals__", None)
                        is_stale = (
                            cb_globals is not None
                            and cb_globals is not vars(live_mod)
                        )
                    except Exception:
                        is_stale = False

                if not is_stale:
                    continue

                qname = getattr(cb, "__qualname__", repr(cb))
                try:
                    handler_list.remove(cb)
                    label = f"{attr}.{qname}[stale_gen]"
                    stale_removed.append(label)
                    _log.warning(
                        "ReloadManager.sweep: removed stale-generation Onixey handler "
                        "'%s' from bpy.app.handlers.%s (module '%s' reloaded)",
                        qname, attr, cb_module_name,
                    )
                    self._log_event(
                        "HANDLER_STALE_GEN_REMOVED",
                        f"Removed stale-gen handler '{qname}' from {attr}",
                        gen,
                        handler_list=attr,
                        qualname=qname,
                        module=cb_module_name,
                    )
                except (ValueError, Exception) as exc:
                    _log.warning(
                        "ReloadManager.sweep: could not remove stale handler '%s' from %s: %s",
                        qname, attr, exc,
                    )

        return duplicates_removed, stale_removed

    def _unregister_timers(self, gen: int) -> List[str]:
        """
        Unregister all Onixey bpy.app.timers callbacks that survived a prior F8.

        Strategy
        ─────────
        bpy.app.timers has no list() method — we cannot enumerate active timers
        directly. Instead we maintain a module-level registry of timer functions
        that Onixey registered (stored in sys.modules under a stable key, so it
        survives F8). Each function is checked via is_registered() and removed
        if still active.

        Callers are expected to populate _SYSMOD_TIMER_KEY before the reload
        (i.e., in handlers.startup() or lifecycle.startup()) by calling
        register_timer_for_cleanup(fn). This method then drains that registry.

        NEVER raises. On any bpy error the entry is simply removed from
        the cleanup registry and a WARNING is logged.

        Returns:
            List of repr strings for each timer that was unregistered.
        """
        removed: List[str] = []

        try:
            import bpy
            if not hasattr(bpy.app, "timers"):
                return removed
        except Exception:
            return removed

        timer_fns: List[Any] = _sysmod_timer_registry()
        if not timer_fns:
            return removed

        still_live: List[Any] = []
        for fn in timer_fns:
            fn_repr = getattr(fn, "__qualname__", repr(fn))
            try:
                if bpy.app.timers.is_registered(fn):
                    bpy.app.timers.unregister(fn)
                    removed.append(fn_repr)
                    _log.warning(
                        "ReloadManager.sweep: unregistered stale timer '%s'",
                        fn_repr,
                    )
                    self._log_event(
                        "TIMER_REMOVED",
                        f"Stale timer unregistered: '{fn_repr}'",
                        gen,
                        timer=fn_repr,
                    )
                # else: already gone — don't keep in registry
            except Exception as exc:
                _log.warning(
                    "ReloadManager.sweep: could not unregister timer '%s': %s",
                    fn_repr, exc,
                )
                # If we can't unregister, don't keep trying on subsequent sweeps
                # unless it still appears registered.
                try:
                    if bpy.app.timers.is_registered(fn):
                        still_live.append(fn)
                except Exception:
                    pass

        # Drain the registry — only keep ones we couldn't remove
        timer_fns.clear()
        timer_fns.extend(still_live)

        return removed

    def _clear_msgbus(self, owner: Any, gen: int) -> List[str]:
        """
        Clear all bpy.msgbus subscriptions owned by ``owner``.

        Onixey must use a single stable owner object per lifecycle session
        (created in lifecycle.startup(), stored in sys.modules so it
        survives F8). This method calls bpy.msgbus.clear_by_owner(owner)
        which removes ALL subscriptions registered under that owner in one call.

        Multiple F8 cycles are safe: if the owner already has no subscriptions,
        clear_by_owner() is a no-op.

        NEVER raises. On any error, WARNING is logged and False is returned.

        Returns:
            List with one entry (owner repr) if cleared successfully, else [].
        """
        try:
            import bpy
            if not hasattr(bpy, "msgbus"):
                return []
            owner_repr = repr(owner)[:80]
            bpy.msgbus.clear_by_owner(owner)
            _log.debug(
                "ReloadManager.sweep: cleared msgbus subscriptions for owner %s",
                owner_repr,
            )
            self._log_event(
                "MSGBUS_CLEARED",
                f"msgbus cleared for owner: {owner_repr}",
                gen,
                owner=owner_repr,
            )
            return [owner_repr]
        except Exception as exc:
            _log.warning(
                "ReloadManager.sweep: could not clear msgbus for owner %r: %s",
                owner, exc,
            )
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log_event(self, event_type: str, message: str, gen: int, **data: Any) -> None:
        ev = ReloadEvent(
            ts=_ts_now(),
            event_type=event_type,
            message=message,
            generation=gen,
            data=dict(data),
        )
        self._events.append(ev)
        # Bound the in-memory log
        if len(self._events) > self._FORENSIC_MAX:
            self._events = self._events[-self._FORENSIC_MAX:]


# ── Module-level singleton factory ────────────────────────────────────────────

_default_manager: Optional[ReloadManager] = None


def get_manager(package_root: str = "onixey3") -> ReloadManager:
    """
    Return (or create) the default ReloadManager singleton for the addon.

    For most use cases, call this once in __init__.py and keep the reference:
        from onixey3.runtime.reload_manager import get_manager
        _reload_mgr = get_manager("onixey3")

    The singleton is NOT stored in sys.modules — it is recreated after F8.
    That is intentional: the descriptor list is rebuilt in register() each time.
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = ReloadManager(package_root=package_root)
    return _default_manager


def reset_manager() -> None:
    """Destroy the default singleton. Call from unregister()."""
    global _default_manager
    _default_manager = None


# ── Timer cleanup registry ────────────────────────────────────────────────────
# Survives F8 via sys.modules so startup_sweep() can find and unregister
# timers that were registered by the previous lifecycle session.
#
# USAGE (from handlers.py or lifecycle.py):
#   from onixey3.runtime.reload_manager import register_timer_for_cleanup
#   bpy.app.timers.register(my_timer_fn, first_interval=1.0)
#   register_timer_for_cleanup(my_timer_fn)
#
# Then in startup_sweep() / shutdown, these are automatically unregistered.

_SYSMOD_TIMER_KEY = "onixey3._reload_manager_timers"


def _sysmod_timer_registry() -> List[Any]:
    """Return (creating if needed) the cross-F8-safe timer function registry."""
    if _SYSMOD_TIMER_KEY not in sys.modules:
        sys.modules[_SYSMOD_TIMER_KEY] = []  # type: ignore[assignment]
    return sys.modules[_SYSMOD_TIMER_KEY]  # type: ignore[return-value]


def register_timer_for_cleanup(fn: Any) -> None:
    """
    Record a timer function so startup_sweep() can unregister it after F8.

    Call this immediately after bpy.app.timers.register(fn, ...).
    Idempotent: registering the same function object twice is a no-op.
    Never raises.

    Args:
        fn: The exact function object passed to bpy.app.timers.register().
            Must be the same object (identity check), not a copy or lambda.
    """
    try:
        registry = _sysmod_timer_registry()
        if fn not in registry:
            registry.append(fn)
            _log.debug(
                "ReloadManager: timer '%s' registered for cleanup on next sweep.",
                getattr(fn, "__qualname__", repr(fn)),
            )
    except Exception as exc:
        _log.warning("register_timer_for_cleanup: failed to record timer: %s", exc)


def unregister_timer_for_cleanup(fn: Any) -> None:
    """
    Remove a timer function from the cleanup registry (normal shutdown path).

    Call this when you unregister a timer cleanly in lifecycle.shutdown() —
    so startup_sweep() doesn't try to unregister it again on next startup.
    Never raises.

    Args:
        fn: The exact function object previously passed to register_timer_for_cleanup().
    """
    try:
        registry = _sysmod_timer_registry()
        try:
            registry.remove(fn)
            _log.debug(
                "ReloadManager: timer '%s' removed from cleanup registry (clean shutdown).",
                getattr(fn, "__qualname__", repr(fn)),
            )
        except ValueError:
            pass  # Not in registry — no-op
    except Exception as exc:
        _log.warning("unregister_timer_for_cleanup: failed: %s", exc)


# ── msgbus owner registry ──────────────────────────────────────────────────────
# The msgbus owner must survive F8 so clear_by_owner() can find subscriptions
# from the previous session. Store it in sys.modules under a stable key.
#
# USAGE (from lifecycle.py):
#   from onixey3.runtime.reload_manager import get_msgbus_owner
#   owner = get_msgbus_owner()
#   bpy.msgbus.subscribe_rna(key=..., owner=owner, args=(), notify=my_fn)
#
# On next startup, startup_sweep(msgbus_owner=get_msgbus_owner()) will call
# bpy.msgbus.clear_by_owner(owner) automatically.

_SYSMOD_MSGBUS_KEY = "onixey3._reload_manager_msgbus_owner"


def get_msgbus_owner() -> object:
    """
    Return (creating if needed) the stable msgbus owner object for Onixey.

    This object is stored in sys.modules so it survives F8 and remains the
    SAME Python object across reload cycles. Passing the same owner to
    bpy.msgbus.subscribe_rna() in consecutive sessions means
    bpy.msgbus.clear_by_owner() will correctly find and remove all
    subscriptions from prior sessions.

    Returns:
        A plain Python object (used solely as a dict key / owner token).
        Never None.
    """
    if _SYSMOD_MSGBUS_KEY not in sys.modules:
        owner = object.__new__(object)  # Unique, stable, hashable token
        sys.modules[_SYSMOD_MSGBUS_KEY] = owner  # type: ignore[assignment]
    return sys.modules[_SYSMOD_MSGBUS_KEY]  # type: ignore[return-value]
