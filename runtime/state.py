"""
onixey3/runtime/state.py

Central Runtime State Manager — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Single authoritative source of truth for all runtime flags, lifecycle phases,
modal locks, anti-reentry guards, and integrity signals.

No other module in the codebase stores lifecycle or lock state outside this
module. Every read and every write goes through the public API defined here.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT register bpy handlers.
    - Does NOT register bpy classes or modify bpy.types.
    - Does NOT store Scene or Object properties.
    - Does NOT call frame_set() or any animation-evaluation API.
    - Does NOT write to disk.
    - Does NOT import bpy at module level.

ARCHITECTURE PATTERN
────────────────────
Inspired by Unreal Engine's UGameInstance / FModuleManager lifecycle model
and Rigify's careful addon-state discipline:

    RuntimeStateManager  (this module)
    ├── LifecyclePhase    FSM:  UNLOADED → INITIALIZING → ACTIVE → SHUTTING_DOWN → UNLOADED
    ├── RuntimeFlags      Bitfield-style bools: analysis_running, overlay_active, etc.
    ├── ModalLockRegistry Reentry guard for operators that must not overlap.
    ├── IntegritySignals  Detected inconsistencies that callers can inspect.
    └── ReloadState       Tracks reload generation and whether a reload is in flight.

STATE MACHINE
─────────────
LifecyclePhase transitions:

    UNLOADED
        ↓  begin_init()
    INITIALIZING
        ↓  complete_init()             ↓  abort_init()
    ACTIVE                         UNLOADED
        ↓  begin_shutdown()
    SHUTTING_DOWN
        ↓  complete_shutdown()
    UNLOADED

Invalid transitions are rejected and logged. Force-reset is available for
emergency recovery (unregister() crash path).

MODAL LOCK REGISTRY
───────────────────
Any code path that must not execute concurrently (e.g., an analysis operator
while a correction operator is running) acquires a named lock before entering
and releases it on exit — even on exception:

    lock = state.acquire_modal_lock("arc_analysis")
    if lock is None:
        self.report({'WARNING'}, "Another operation is already running.")
        return {'CANCELLED'}
    try:
        ...
    finally:
        state.release_modal_lock("arc_analysis")

ANTI-REENTRY GUARD
──────────────────
For handlers (depsgraph_update_post, frame_change_post) that must not call
themselves recursively through Blender's event system:

    if state.is_reentrant("depsgraph_handler"):
        return
    with state.reentry_guard("depsgraph_handler"):
        ...

RELOAD SAFETY
─────────────
This module's global state is intentionally minimal:
    - _manager: Optional[RuntimeStateManager] — one instance per addon lifecycle.
    - The instance is created in get() on first access, reset in reset().
    - Reload generation is tracked via sys.modules (survives F8).

DEPENDENCY CONTRACT
───────────────────
    Imports from:    (nothing — zero dependencies)
    Must NOT import: cache, session, lifecycle, operators, ui, analysis,
                     properties, migration, core

    Zero import-time dependencies makes state.py safe to import from any
    module in the codebase without risk of circular imports.

CHANGELOG
─────────
    3.1.0 — Initial implementation. LifecyclePhase FSM, RuntimeFlags,
             ModalLockRegistry, IntegritySignals, ReloadState, full audit API.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto, unique
from typing import (
    Dict, FrozenSet, Generator, Iterator, List, Optional, Set, Tuple
)

_log = logging.getLogger(__name__)

# ── Cross-reload persistent counter ──────────────────────────────────────────

_SYSMOD_KEY = "onixey3._runtime_state_meta"


def _sysmod_meta() -> Dict[str, int]:
    if _SYSMOD_KEY not in sys.modules:
        sys.modules[_SYSMOD_KEY] = {"reload_gen": 0, "instance_count": 0}  # type: ignore[assignment]
    return sys.modules[_SYSMOD_KEY]  # type: ignore[return-value]


def _bump_instance_count() -> int:
    meta = _sysmod_meta()
    meta["instance_count"] += 1
    return meta["instance_count"]


def get_state_generation() -> int:
    """Return how many RuntimeStateManager instances have been created this session."""
    return _sysmod_meta().get("instance_count", 0)


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE PHASE
# ══════════════════════════════════════════════════════════════════════════════

@unique
class LifecyclePhase(Enum):
    """
    Ordered phases of the addon's runtime lifecycle.

    Transitions are validated by RuntimeStateManager. No direct assignment.
    """
    UNLOADED      = auto()   # Addon not registered or fully shut down.
    INITIALIZING  = auto()   # register() in progress; subsystems starting up.
    ACTIVE        = auto()   # Fully operational. UI/analysis/overlays available.
    SHUTTING_DOWN = auto()   # unregister() in progress; cleanup running.

    def can_transition_to(self, target: LifecyclePhase) -> bool:
        """Return True if the transition self → target is valid."""
        return target in _VALID_TRANSITIONS.get(self, frozenset())


# Explicit transition table. Any edge not listed here is INVALID.
_VALID_TRANSITIONS: Dict[LifecyclePhase, FrozenSet[LifecyclePhase]] = {
    LifecyclePhase.UNLOADED:      frozenset({LifecyclePhase.INITIALIZING}),
    LifecyclePhase.INITIALIZING:  frozenset({LifecyclePhase.ACTIVE,
                                             LifecyclePhase.UNLOADED}),
    LifecyclePhase.ACTIVE:        frozenset({LifecyclePhase.SHUTTING_DOWN}),
    LifecyclePhase.SHUTTING_DOWN: frozenset({LifecyclePhase.UNLOADED}),
}


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME FLAGS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RuntimeFlags:
    """
    Mutable collection of boolean runtime flags.

    Each flag represents a discrete runtime condition that multiple modules
    need to read. Centralising them here prevents scattered global booleans
    and makes the full runtime condition inspectable in one place.

    Rules:
        - Flags are set/cleared only through RuntimeStateManager setters.
        - No module reads _flags directly; all access is via the manager API.
        - Default value for every flag is False (safe / inactive).
    """

    # ── Analysis pipeline ─────────────────────────────────────────────────────
    analysis_running:       bool = False  # Any analysis operator is executing.
    analysis_batch_active:  bool = False  # Batch (multi-frame) analysis in progress.
    analysis_results_valid: bool = False  # Cached results reflect current animation.

    # ── Overlay system ────────────────────────────────────────────────────────
    overlay_active:         bool = False  # SpaceView3D draw handlers are registered.
    overlay_dirty:          bool = False  # Overlay needs redraw on next draw callback.

    # ── Cache ─────────────────────────────────────────────────────────────────
    cache_warming:          bool = False  # Cache pre-population in progress.
    cache_valid:            bool = False  # L2/L3 cache is consistent with bpy data.

    # ── Reload system ─────────────────────────────────────────────────────────
    reload_in_progress:     bool = False  # ReloadManager transaction is running.
    post_reload_validation: bool = False  # Post-reload integrity check pending.

    # ── Session ───────────────────────────────────────────────────────────────
    session_active:         bool = False  # SessionState singleton is live.
    rig_context_valid:      bool = False  # Active armature is set and weakref is alive.

    # ── Migration ─────────────────────────────────────────────────────────────
    migration_pending:      bool = False  # Loaded .blend has pre-V3 data needing upgrade.
    migration_running:      bool = False  # Migration is currently executing.

    # ── Environment ───────────────────────────────────────────────────────────
    feature_flags_sealed:   bool = False  # feature_flags.initialize() succeeded.
    hard_requirements_met:  bool = False  # All fallback_ok=False flags are True.

    def snapshot(self) -> Dict[str, bool]:
        """Return all flags as a plain dict. Safe for logging and UI display."""
        return {
            k: v for k, v in vars(self).items()
            if not k.startswith("_")
        }

    def any_critical_lock(self) -> bool:
        """
        Return True if any flag indicates a state where unsafe operations
        (reload, unregister, scene load) should be delayed or refused.
        """
        return (
            self.analysis_running
            or self.analysis_batch_active
            or self.reload_in_progress
            or self.migration_running
        )


# ══════════════════════════════════════════════════════════════════════════════
# MODAL LOCK
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModalLock:
    """
    Represents a held exclusive lock for a named operation.

    Returned by RuntimeStateManager.acquire_modal_lock().
    Must be passed back to release_modal_lock() to free the slot.

    Attributes:
        name:       The lock name (e.g., "arc_analysis", "correction_euler").
        acquired_at: Monotonic timestamp of acquisition.
        instance_id: Unique per-acquisition ID for debugging stacked acquisitions.
    """
    name:        str
    acquired_at: float
    instance_id: int

    @property
    def held_seconds(self) -> float:
        """How long this lock has been held."""
        return time.monotonic() - self.acquired_at


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRITY SIGNAL
# ══════════════════════════════════════════════════════════════════════════════

@unique
class IntegritySignal(Enum):
    """
    Discrete integrity issues that RuntimeStateManager can detect and report.

    Callers inspect get_integrity_signals() to decide whether to abort,
    warn the user, or attempt recovery.
    """
    # Lifecycle
    INIT_WITHOUT_FEATURE_FLAGS   = auto()  # complete_init() called before feature_flags sealed.
    SHUTDOWN_WITH_LOCKS_HELD     = auto()  # begin_shutdown() called with active modal locks.
    UNEXPECTED_PHASE_TRANSITION  = auto()  # Invalid FSM transition attempted.

    # Flags
    ANALYSIS_RUNNING_AT_RELOAD   = auto()  # Reload attempted while analysis is running.
    MIGRATION_RUNNING_AT_SHUTDOWN= auto()  # Shutdown attempted during migration.

    # Reload
    RELOAD_REENTRY               = auto()  # execute_reload() called while reload_in_progress.
    POST_RELOAD_VALIDATION_MISSED= auto()  # post_reload_validation flag set but never cleared.

    # Reentry
    HANDLER_REENTRY_DETECTED     = auto()  # A handler attempted to call itself recursively.

    # Modal locks
    LOCK_TIMEOUT                 = auto()  # A lock has been held longer than _LOCK_WARN_SECONDS.
    LOCK_DOUBLE_RELEASE          = auto()  # release_modal_lock() called for an unknown lock.


_LOCK_WARN_SECONDS: float = 30.0  # Warn if a modal lock is held longer than this.


# ══════════════════════════════════════════════════════════════════════════════
# RELOAD STATE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReloadState:
    """
    Tracks the state of the most recent reload transaction.

    Updated by ReloadManager callbacks (or directly via the manager API).
    """
    generation:       int   = 0      # Matches ReloadManager's generation counter.
    last_reload_at:   Optional[float] = None  # Monotonic timestamp of last successful reload.
    last_duration_ms: float = 0.0    # Duration of last reload transaction.
    reloaded_count:   int   = 0      # How many modules were reloaded last cycle.
    failed_count:     int   = 0      # How many modules failed last cycle.
    orphans_removed:  int   = 0      # Orphan modules purged last cycle.

    def mark_reload_complete(
        self,
        generation:   int,
        duration_ms:  float,
        reloaded:     int,
        failed:       int,
        orphans:      int,
    ) -> None:
        self.generation       = generation
        self.last_reload_at   = time.monotonic()
        self.last_duration_ms = duration_ms
        self.reloaded_count   = reloaded
        self.failed_count     = failed
        self.orphans_removed  = orphans

    def seconds_since_reload(self) -> Optional[float]:
        if self.last_reload_at is None:
            return None
        return time.monotonic() - self.last_reload_at

    def snapshot(self) -> Dict[str, object]:
        return {
            "generation":       self.generation,
            "last_reload_ago_s": (
                round(self.seconds_since_reload(), 1)
                if self.seconds_since_reload() is not None else None
            ),
            "last_duration_ms": round(self.last_duration_ms, 2),
            "reloaded_count":   self.reloaded_count,
            "failed_count":     self.failed_count,
            "orphans_removed":  self.orphans_removed,
        }


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME STATE MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class RuntimeStateManager:
    """
    Central runtime state container for Onixey V3.

    Owns:
        - LifecyclePhase FSM
        - RuntimeFlags collection
        - ModalLock registry
        - Reentry guard set
        - IntegritySignal accumulator
        - ReloadState tracker
        - Structured audit log

    Lifecycle:
        Created by state.get() on first access after reset().
        Destroyed (replaced) by state.reset().
        One instance per addon register/unregister cycle.

    Thread safety:
        Blender is single-threaded. No locking required.
    """

    __slots__ = (
        "_phase",
        "_flags",
        "_locks",
        "_lock_counter",
        "_reentry_set",
        "_signals",
        "_reload_state",
        "_audit_log",
        "_instance_id",
        "_created_at",
    )

    _AUDIT_MAX = 512

    def __init__(self) -> None:
        self._phase:        LifecyclePhase          = LifecyclePhase.UNLOADED
        self._flags:        RuntimeFlags             = RuntimeFlags()
        self._locks:        Dict[str, ModalLock]    = {}
        self._lock_counter: int                     = 0
        self._reentry_set:  Set[str]                = set()
        self._signals:      List[Tuple[IntegritySignal, str]] = []
        self._reload_state: ReloadState             = ReloadState()
        self._audit_log:    List[Dict[str, object]] = []
        self._instance_id:  int                     = _bump_instance_count()
        self._created_at:   float                   = time.monotonic()

        self._write_audit("CREATED", f"RuntimeStateManager #{self._instance_id} created")
        _log.debug(
            "RuntimeStateManager #%d created. gen=%d",
            self._instance_id, get_state_generation(),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # LIFECYCLE PHASE FSM
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def phase(self) -> LifecyclePhase:
        """Current lifecycle phase. Read-only; advance via begin_init() etc."""
        return self._phase

    def begin_init(self) -> bool:
        """
        Transition UNLOADED → INITIALIZING.

        Called at the start of onixey3/__init__.py register(), before any
        subsystem is started.

        Returns:
            True if transition succeeded.
            False if the current phase does not allow this transition.
        """
        return self._transition(LifecyclePhase.INITIALIZING, "begin_init")

    def complete_init(self) -> bool:
        """
        Transition INITIALIZING → ACTIVE.

        Called after all subsystems (cache, session, lifecycle handlers) have
        started successfully.

        Emits INIT_WITHOUT_FEATURE_FLAGS signal if feature_flags_sealed is False.

        Returns:
            True if transition succeeded.
            False if current phase is not INITIALIZING.
        """
        if not self._flags.feature_flags_sealed:
            self._record_signal(
                IntegritySignal.INIT_WITHOUT_FEATURE_FLAGS,
                "complete_init() called but feature_flags_sealed=False. "
                "Ensure core.compat.validate_environment() ran before complete_init().",
            )
            _log.warning(
                "RuntimeStateManager: complete_init() called without feature_flags sealed. "
                "Hard requirement checks may have been skipped."
            )
        return self._transition(LifecyclePhase.ACTIVE, "complete_init")

    def abort_init(self, reason: str = "") -> bool:
        """
        Transition INITIALIZING → UNLOADED.

        Called when initialization fails partway through (e.g., a hard
        requirement is not met, or a subsystem raises).

        Args:
            reason: Human-readable description of why init was aborted.

        Returns:
            True if transition succeeded.
        """
        msg = f"abort_init: {reason}" if reason else "abort_init"
        self._write_audit("ABORT_INIT", msg)
        _log.error("RuntimeStateManager: %s", msg)
        return self._transition(LifecyclePhase.UNLOADED, "abort_init")

    def begin_shutdown(self) -> bool:
        """
        Transition ACTIVE → SHUTTING_DOWN.

        Called at the start of onixey3/__init__.py unregister().

        Emits SHUTDOWN_WITH_LOCKS_HELD if any modal locks are active —
        the caller should warn the user but proceed with shutdown.

        Returns:
            True if transition succeeded.
        """
        if self._locks:
            lock_names = list(self._locks.keys())
            self._record_signal(
                IntegritySignal.SHUTDOWN_WITH_LOCKS_HELD,
                f"begin_shutdown() called with active locks: {lock_names}. "
                "Locks will be force-released.",
            )
            _log.warning(
                "RuntimeStateManager: shutdown with active locks %s. Force-releasing.",
                lock_names,
            )
            self._locks.clear()

        if self._flags.migration_running:
            self._record_signal(
                IntegritySignal.MIGRATION_RUNNING_AT_SHUTDOWN,
                "begin_shutdown() called while migration_running=True.",
            )

        return self._transition(LifecyclePhase.SHUTTING_DOWN, "begin_shutdown")

    def complete_shutdown(self) -> bool:
        """
        Transition SHUTTING_DOWN → UNLOADED.

        Called after all subsystems have been torn down.

        Returns:
            True if transition succeeded.
        """
        return self._transition(LifecyclePhase.UNLOADED, "complete_shutdown")

    def force_reset_phase(self, reason: str = "emergency") -> None:
        """
        Force-set phase to UNLOADED regardless of current state.

        Use ONLY in:
            - unregister() exception handlers (must complete even if crashed)
            - Unit test teardown

        Normal code MUST use the FSM methods above.
        """
        prev = self._phase
        self._phase = LifecyclePhase.UNLOADED
        self._locks.clear()
        self._reentry_set.clear()
        self._write_audit("FORCE_RESET", f"phase forced UNLOADED from {prev.name}. reason={reason}")
        _log.warning(
            "RuntimeStateManager: force_reset_phase() from %s. reason=%s",
            prev.name, reason,
        )

    def _transition(self, target: LifecyclePhase, caller: str) -> bool:
        """Internal FSM transition. Validates, executes, logs."""
        if not self._phase.can_transition_to(target):
            self._record_signal(
                IntegritySignal.UNEXPECTED_PHASE_TRANSITION,
                f"{caller}(): invalid transition {self._phase.name} → {target.name}.",
            )
            _log.error(
                "RuntimeStateManager: INVALID transition %s → %s (caller=%s). "
                "Valid targets: %s",
                self._phase.name, target.name, caller,
                [p.name for p in _VALID_TRANSITIONS.get(self._phase, frozenset())],
            )
            return False

        prev = self._phase
        self._phase = target
        self._write_audit(
            "PHASE_TRANSITION",
            f"{prev.name} → {target.name}",
            caller=caller,
        )
        _log.debug(
            "RuntimeStateManager: phase %s → %s (%s)",
            prev.name, target.name, caller,
        )
        return True

    # ── Convenience phase checks ──────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """True if the addon is fully initialized and operational."""
        return self._phase == LifecyclePhase.ACTIVE

    @property
    def is_initializing(self) -> bool:
        return self._phase == LifecyclePhase.INITIALIZING

    @property
    def is_shutting_down(self) -> bool:
        return self._phase == LifecyclePhase.SHUTTING_DOWN

    @property
    def is_unloaded(self) -> bool:
        return self._phase == LifecyclePhase.UNLOADED

    # ══════════════════════════════════════════════════════════════════════════
    # RUNTIME FLAGS API
    # ══════════════════════════════════════════════════════════════════════════

    def set_flag(self, name: str, value: bool) -> None:
        """
        Set a RuntimeFlags attribute by name.

        Args:
            name:  Attribute name on RuntimeFlags (e.g., "analysis_running").
            value: New boolean value.

        Raises:
            AttributeError if name is not a valid RuntimeFlags field.
            TypeError if value is not bool.

        Logs:
            DEBUG on every change.
            WARNING for specific high-risk transitions (e.g., reload during analysis).
        """
        if not isinstance(value, bool):
            raise TypeError(
                f"RuntimeStateManager.set_flag('{name}'): value must be bool, "
                f"got {type(value).__name__}."
            )
        if not hasattr(self._flags, name):
            raise AttributeError(
                f"RuntimeStateManager.set_flag('{name}'): "
                f"'{name}' is not a valid RuntimeFlags field. "
                f"Valid fields: {list(self._flags.snapshot().keys())}"
            )

        prev = getattr(self._flags, name)
        if prev == value:
            return  # No-op: value unchanged

        # High-risk transition warnings
        if name == "reload_in_progress" and value and self._flags.analysis_running:
            self._record_signal(
                IntegritySignal.ANALYSIS_RUNNING_AT_RELOAD,
                "reload_in_progress set True while analysis_running=True.",
            )
            _log.warning(
                "RuntimeStateManager: reload_in_progress=True while analysis_running=True. "
                "This may cause cache inconsistency."
            )

        if name == "reload_in_progress" and value and prev:
            self._record_signal(
                IntegritySignal.RELOAD_REENTRY,
                "reload_in_progress set True while already True (reload reentry).",
            )
            _log.error(
                "RuntimeStateManager: reload_in_progress set True while already True. "
                "Reentry in reload system detected."
            )

        setattr(self._flags, name, value)
        self._write_audit("FLAG_CHANGE", f"{name}: {prev} → {value}")
        _log.debug("RuntimeStateManager: flag '%s' %s → %s", name, prev, value)

    def get_flag(self, name: str) -> bool:
        """
        Get a RuntimeFlags attribute by name.

        Args:
            name: Attribute name on RuntimeFlags.

        Returns:
            The current bool value of the flag.

        Raises:
            AttributeError if name is not a valid RuntimeFlags field.
        """
        if not hasattr(self._flags, name):
            raise AttributeError(
                f"RuntimeStateManager.get_flag('{name}'): not a valid RuntimeFlags field."
            )
        return getattr(self._flags, name)  # type: ignore[return-value]

    def get_flags_snapshot(self) -> Dict[str, bool]:
        """
        Return all flags as a plain dict.

        Safe to log, print, or display in a diagnostic panel.
        Returns a copy — mutations do not affect internal state.
        """
        return self._flags.snapshot()

    def any_critical_lock_active(self) -> bool:
        """
        Return True if any flag indicates an unsafe state for reload/shutdown.

        Callers (e.g., addon preferences "Reload" button) use this to decide
        whether to show a warning before triggering a reload.
        """
        return self._flags.any_critical_lock()

    # ══════════════════════════════════════════════════════════════════════════
    # MODAL LOCK REGISTRY
    # ══════════════════════════════════════════════════════════════════════════

    def acquire_modal_lock(self, name: str) -> Optional[ModalLock]:
        """
        Attempt to acquire an exclusive named lock.

        A lock prevents two operations of the same type from running
        simultaneously. Example: two arc analysis operators launched quickly.

        Usage:
            lock = state_mgr.acquire_modal_lock("arc_analysis")
            if lock is None:
                self.report({'WARNING'}, "Arc analysis already running.")
                return {'CANCELLED'}
            try:
                run_analysis()
            finally:
                state_mgr.release_modal_lock("arc_analysis")

        Args:
            name: Unique operation name, e.g. "arc_analysis", "euler_correction".

        Returns:
            A ModalLock instance if acquired.
            None if the lock is already held by another call.
        """
        if name in self._locks:
            existing = self._locks[name]
            held_s = existing.held_seconds
            _log.warning(
                "RuntimeStateManager: lock '%s' already held for %.1fs. Acquisition denied.",
                name, held_s,
            )
            if held_s > _LOCK_WARN_SECONDS:
                self._record_signal(
                    IntegritySignal.LOCK_TIMEOUT,
                    f"Lock '{name}' held for {held_s:.1f}s (threshold={_LOCK_WARN_SECONDS}s). "
                    "Possible deadlock or operator that crashed without releasing.",
                )
            return None

        self._lock_counter += 1
        lock = ModalLock(
            name=name,
            acquired_at=time.monotonic(),
            instance_id=self._lock_counter,
        )
        self._locks[name] = lock
        self._write_audit("LOCK_ACQUIRE", f"Lock '{name}' acquired", instance_id=lock.instance_id)
        _log.debug("RuntimeStateManager: lock '%s' acquired (id=%d)", name, lock.instance_id)
        return lock

    def release_modal_lock(self, name: str) -> None:
        """
        Release a held named lock.

        Idempotent: releasing a lock that is not held emits a WARNING and
        records a LOCK_DOUBLE_RELEASE signal, but does not raise.

        Args:
            name: The lock name passed to acquire_modal_lock().
        """
        if name not in self._locks:
            self._record_signal(
                IntegritySignal.LOCK_DOUBLE_RELEASE,
                f"release_modal_lock('{name}') called but lock is not held. "
                "Possible double-release or mismatched acquire/release.",
            )
            _log.warning(
                "RuntimeStateManager: release_modal_lock('%s') — lock not found. "
                "Double-release or mismatched acquire/release.",
                name,
            )
            return

        lock = self._locks.pop(name)
        held_s = lock.held_seconds
        self._write_audit(
            "LOCK_RELEASE",
            f"Lock '{name}' released after {held_s:.3f}s",
            instance_id=lock.instance_id,
            held_s=round(held_s, 3),
        )
        _log.debug(
            "RuntimeStateManager: lock '%s' released after %.3fs (id=%d)",
            name, held_s, lock.instance_id,
        )

        if held_s > _LOCK_WARN_SECONDS:
            self._record_signal(
                IntegritySignal.LOCK_TIMEOUT,
                f"Lock '{name}' (id={lock.instance_id}) was held for {held_s:.1f}s "
                f"(threshold={_LOCK_WARN_SECONDS}s).",
            )

    def is_lock_held(self, name: str) -> bool:
        """Return True if the named lock is currently held."""
        return name in self._locks

    def get_active_locks(self) -> Dict[str, Dict[str, object]]:
        """
        Return all currently held locks as plain dicts.

        Keys: lock name → {instance_id, held_s, acquired_at}.
        Safe for diagnostic display.
        """
        return {
            name: {
                "instance_id": lock.instance_id,
                "held_s":      round(lock.held_seconds, 3),
                "acquired_at": lock.acquired_at,
            }
            for name, lock in self._locks.items()
        }

    def force_release_all_locks(self, reason: str = "force_release") -> List[str]:
        """
        Release all held locks unconditionally.

        Use ONLY in:
            - Emergency shutdown paths.
            - Unit test teardown.

        Returns:
            List of lock names that were released.
        """
        names = list(self._locks.keys())
        if names:
            self._write_audit("LOCK_FORCE_RELEASE_ALL", f"Force-releasing {len(names)} lock(s). reason={reason}")
            _log.warning(
                "RuntimeStateManager: force_release_all_locks(%s): releasing %s",
                reason, names,
            )
        self._locks.clear()
        return names

    # ══════════════════════════════════════════════════════════════════════════
    # ANTI-REENTRY GUARD
    # ══════════════════════════════════════════════════════════════════════════

    def is_reentrant(self, guard_name: str) -> bool:
        """
        Return True if guard_name is already active (reentry detected).

        Usage (in a handler):
            if runtime_state.is_reentrant("depsgraph_handler"):
                return
            with runtime_state.reentry_guard("depsgraph_handler"):
                do_work()

        Args:
            guard_name: Unique string identifier for the guarded code path.
        """
        return guard_name in self._reentry_set

    @contextmanager
    def reentry_guard(self, guard_name: str) -> Iterator[None]:
        """
        Context manager that sets and clears a reentry guard name.

        Automatically clears the guard on exit — even on exception.

        Usage:
            with state_mgr.reentry_guard("frame_change_handler"):
                expensive_work()

        If the guard is already active when this context is entered, the
        HANDLER_REENTRY_DETECTED signal is recorded and a RuntimeError is raised
        to prevent the reentrant path from executing.

        Raises:
            RuntimeError if guard_name is already active.
        """
        if guard_name in self._reentry_set:
            self._record_signal(
                IntegritySignal.HANDLER_REENTRY_DETECTED,
                f"Reentry guard '{guard_name}' triggered. "
                "Handler called itself recursively.",
            )
            _log.error(
                "RuntimeStateManager: reentry detected for '%s'. "
                "This guard should have been checked with is_reentrant() first.",
                guard_name,
            )
            raise RuntimeError(
                f"Reentry guard '{guard_name}' is already active. "
                "Use is_reentrant() before entering the guarded block."
            )

        self._reentry_set.add(guard_name)
        try:
            yield
        finally:
            self._reentry_set.discard(guard_name)

    def get_active_guards(self) -> FrozenSet[str]:
        """Return a frozenset of all currently active reentry guard names."""
        return frozenset(self._reentry_set)

    # ══════════════════════════════════════════════════════════════════════════
    # INTEGRITY SIGNALS
    # ══════════════════════════════════════════════════════════════════════════

    def _record_signal(self, signal: IntegritySignal, detail: str) -> None:
        """Append a signal. Internal — callers use the public read API."""
        self._signals.append((signal, detail))
        self._write_audit("SIGNAL", f"{signal.name}: {detail}", signal=signal.name)

    def get_integrity_signals(self) -> List[Tuple[str, str]]:
        """
        Return all recorded integrity signals as plain string tuples.

        Returns:
            List of (signal_name, detail_message) pairs.
            Empty list if no issues detected.

        Does not clear the signal list — signals accumulate for the lifetime
        of this RuntimeStateManager instance.
        """
        return [(sig.name, detail) for sig, detail in self._signals]

    def has_signals(self) -> bool:
        """Return True if any integrity signals have been recorded."""
        return bool(self._signals)

    def clear_signals(self) -> int:
        """
        Clear all recorded integrity signals.

        Use after the caller has acknowledged and logged the signals.
        Returns the number of signals cleared.
        """
        count = len(self._signals)
        self._signals.clear()
        if count:
            self._write_audit("SIGNALS_CLEARED", f"{count} signal(s) cleared")
        return count

    # ══════════════════════════════════════════════════════════════════════════
    # RELOAD STATE
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def reload_state(self) -> ReloadState:
        """Direct access to the ReloadState tracker."""
        return self._reload_state

    def record_reload_complete(
        self,
        generation:  int,
        duration_ms: float,
        reloaded:    int,
        failed:      int,
        orphans:     int,
    ) -> None:
        """
        Update ReloadState after a completed reload transaction.

        Called by ReloadManager (or lifecycle.py) after execute_reload() returns.

        Args:
            generation:  Reload generation counter value.
            duration_ms: Total transaction duration in milliseconds.
            reloaded:    Number of modules successfully reloaded.
            failed:      Number of modules that failed to reload.
            orphans:     Number of orphan modules removed.
        """
        self._reload_state.mark_reload_complete(
            generation=generation,
            duration_ms=duration_ms,
            reloaded=reloaded,
            failed=failed,
            orphans=orphans,
        )
        self.set_flag("reload_in_progress", False)

        # CONTRACT: if any modules failed, set post_reload_validation=True so
        # lifecycle.py knows to run integrity checks before marking the addon ACTIVE.
        # Owner: the caller (ReloadManager.execute_reload or lifecycle.py) is
        # responsible for clearing this flag after validation passes.
        # Clearing it here would hide failures — intentionally NOT cleared here.
        if failed > 0:
            self.set_flag("post_reload_validation", True)
            _log.warning(
                "RuntimeStateManager: %d module(s) failed during reload gen=%d. "
                "post_reload_validation=True — caller must run integrity checks "
                "and clear this flag via set_flag('post_reload_validation', False).",
                failed, generation,
            )

        self._write_audit(
            "RELOAD_COMPLETE",
            f"gen={generation} reloaded={reloaded} failed={failed} orphans={orphans} "
            f"duration={duration_ms:.2f}ms",
        )
        _log.info(
            "RuntimeStateManager: reload gen=%d complete in %.2fms "
            "(reloaded=%d failed=%d orphans=%d)",
            generation, duration_ms, reloaded, failed, orphans,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # AUDIT LOG
    # ══════════════════════════════════════════════════════════════════════════

    def _write_audit(self, event_type: str, message: str, **data: object) -> None:
        """Append a structured entry to the bounded audit log."""
        # Millisecond precision: strftime gives seconds; monotonic gives sub-second.
        # Two events in the same second are now distinguishable in forensic reports.
        _ms = int(time.monotonic() * 1000) % 1000
        entry: Dict[str, object] = {
            "ts":         time.strftime("%Y-%m-%dT%H:%M:%S") + f".{_ms:03d}",
            "event_type": event_type,
            "message":    message,
            "phase":      self._phase.name,
        }
        if data:
            entry.update(data)
        self._audit_log.append(entry)
        if len(self._audit_log) > self._AUDIT_MAX:
            self._audit_log = self._audit_log[-self._AUDIT_MAX:]

    def get_audit_log(self, last_n: int = 50) -> List[Dict[str, object]]:
        """
        Return the most recent audit log entries.

        Args:
            last_n: Maximum entries to return (default 50, max _AUDIT_MAX).

        Returns:
            List of plain dicts. Safe to log or display in UI.
        """
        return [dict(e) for e in self._audit_log[-max(1, last_n):]]

    # ══════════════════════════════════════════════════════════════════════════
    # FULL DIAGNOSTIC SNAPSHOT
    # ══════════════════════════════════════════════════════════════════════════

    def get_diagnostic_snapshot(self) -> Dict[str, object]:
        """
        Return a complete snapshot of all runtime state as plain Python types.

        Safe to log, serialize, or display in a diagnostic panel.
        Never returns bpy objects. Never raises.

        Returns dict with keys:
            instance_id          — manager instance number (from sysmod counter)
            phase                — current LifecyclePhase name
            age_s                — seconds since this manager was created
            flags                — RuntimeFlags snapshot dict
            active_locks         — currently held ModalLock info
            active_guards        — currently active reentry guard names
            integrity_signals    — list of (signal_name, detail) tuples
            reload_state         — ReloadState snapshot
            audit_log_size       — total entries in audit log
            any_critical_lock    — bool convenience flag
        """
        try:
            return {
                "instance_id":       self._instance_id,
                "phase":             self._phase.name,
                "age_s":             round(time.monotonic() - self._created_at, 1),
                "flags":             self._flags.snapshot(),
                "active_locks":      self.get_active_locks(),
                "active_guards":     sorted(self._reentry_set),
                "integrity_signals": self.get_integrity_signals(),
                "reload_state":      self._reload_state.snapshot(),
                "audit_log_size":    len(self._audit_log),
                "any_critical_lock": self.any_critical_lock_active(),
            }
        except Exception as exc:
            _log.error(
                "RuntimeStateManager.get_diagnostic_snapshot() failed: %s\n%s",
                exc, traceback.format_exc(),
            )
            return {"error": str(exc), "phase": self._phase.name}

    def get_diagnostic_report(self) -> str:
        """
        Return a formatted human-readable diagnostic report string.

        Includes all sections: phase, flags, locks, guards, signals, reload.
        Safe to call at any time. Handles internal errors gracefully.
        """
        SEP = "─" * 60
        try:
            snap = self.get_diagnostic_snapshot()
            lines = [
                f"RuntimeStateManager #{snap['instance_id']}  age={snap['age_s']}s",
                f"Phase: {snap['phase']}",
                SEP,
                "RUNTIME FLAGS:",
            ]
            for k, v in sorted(snap["flags"].items()):
                indicator = "✔" if v else " "
                lines.append(f"  [{indicator}] {k}")

            locks = snap["active_locks"]
            lines += ["", f"MODAL LOCKS ({len(locks)} active):"]
            if locks:
                for name, info in locks.items():
                    lines.append(f"  '{name}'  held={info['held_s']}s  id={info['instance_id']}")
            else:
                lines.append("  (none)")

            guards = snap["active_guards"]
            lines += ["", f"REENTRY GUARDS ({len(guards)} active):"]
            if guards:
                for g in guards:
                    lines.append(f"  '{g}'")
            else:
                lines.append("  (none)")

            signals = snap["integrity_signals"]
            lines += ["", f"INTEGRITY SIGNALS ({len(signals)}):"]
            if signals:
                for sig_name, detail in signals:
                    lines.append(f"  ⚠ {sig_name}: {detail}")
            else:
                lines.append("  (none)")

            rs = snap["reload_state"]
            lines += ["", "RELOAD STATE:"]
            for k, v in rs.items():
                lines.append(f"  {k:<22} {v}")

            lines.append(SEP)
            return "\n".join(lines)

        except Exception as exc:
            return (
                f"RuntimeStateManager.get_diagnostic_report() ERROR: {exc}\n"
                + traceback.format_exc()
            )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON  (reload-safe)
# ══════════════════════════════════════════════════════════════════════════════

_manager: Optional[RuntimeStateManager] = None


def get() -> RuntimeStateManager:
    """
    Return the active RuntimeStateManager, creating it if necessary.

    This is the canonical access point for all runtime state reads and writes:

        from onixey3.runtime import state
        mgr = state.get()

        mgr.begin_init()
        mgr.set_flag("feature_flags_sealed", True)
        mgr.complete_init()

        if mgr.is_active:
            lock = mgr.acquire_modal_lock("arc_analysis")
            ...

    The manager is created on first call after module import or reset().
    Creating it does NOT transition the lifecycle phase — call begin_init() explicitly.

    Never returns None.
    """
    global _manager
    if _manager is None:
        _manager = RuntimeStateManager()
    return _manager


def reset() -> None:
    """
    Destroy the current RuntimeStateManager and allow get() to create a fresh one.

    Called from onixey3/__init__.py unregister() after complete_shutdown().

    Idempotent: safe to call when _manager is already None.
    """
    global _manager
    if _manager is not None:
        _log.debug(
            "RuntimeStateManager: reset() — destroying instance #%d (phase=%s)",
            _manager._instance_id, _manager.phase.name,
        )
        # Best-effort: force phase to UNLOADED before discarding
        if _manager.phase not in (LifecyclePhase.UNLOADED,):
            try:
                _manager.force_reset_phase("state.reset()")
            except Exception as exc:
                _log.error("RuntimeStateManager: force_reset_phase error in reset(): %s", exc)
    _manager = None


def is_active() -> bool:
    """
    Return True if a manager exists AND is in the ACTIVE phase.

    Safe to call at any time, including before get() has been called.
    Used by handlers and operators to guard against running during init or shutdown:

        from onixey3.runtime import state
        if not state.is_active():
            return
    """
    return _manager is not None and _manager.is_active
