"""
onixey3/runtime/session.py

Per-Session State with Weakref-Safe bpy Object References.

SINGLE RESPONSIBILITY
─────────────────────
Own the transient session state Onixey needs between frames and between
operator calls: which armature is active, is analysis dirty, what was the
last analyzed range, what is the current analysis pipeline state.

This module does NOT cache analysis results — that belongs to cache.py.
This module does NOT register handlers — that belongs to lifecycle.py.

WEAKREF POLICY
──────────────
All references to bpy objects (Object, Armature, Action...) are stored
as weakref.ref. This is mandatory for two reasons:

  1. bpy objects can become invalid at any time (undo, new file, deletion).
     A strong ref to an invalidated object causes undefined behavior.

  2. Strong Python refs prevent Blender's memory manager from reclaiming
     objects marked for deletion, creating memory leaks in long sessions.

Every property that returns a bpy object MUST:
  a. Dereference the weakref: obj = self._ref()
  b. Check for None before use: if obj is None: return
  c. Handle the name lookup fallback for logging.

Callers MUST always check for None before using any bpy object returned
by this module.

ANALYSIS STATE MACHINE
──────────────────────
Session tracks the analysis pipeline through a strict state machine:

    IDLE ──── user requests ────► PENDING
    PENDING ── operator starts ──► RUNNING
    RUNNING ── success ──────────► DONE
    RUNNING ── failure ──────────► ERROR
    DONE / RUNNING ── anim changes ─► DIRTY
    DIRTY / ERROR ── user retries ──► PENDING
    ANY ─── unregister / cancel ──► IDLE

Invalid transitions are rejected and logged. This prevents operators
from proceeding with stale state machine assumptions.

CONCURRENCY
───────────
Blender is single-threaded. SessionState is NOT thread-safe by design.
Do not access from threading.Thread.

DEPENDENCY CONTRACT
───────────────────
  Imports from:    (nothing — zero dependencies)
  Must NOT import: cache, lifecycle, operators, ui, analysis, properties, core

  Zero import dependencies makes session.py the safest module to import
  anywhere in the codebase without risk of circular imports.

CHANGELOG
─────────
  3.1.0 — Iteration 2. AAA rewrite with formal state machine, weakref pattern,
           dirty tracking, diagnostic snapshot, and clean lifecycle hooks.
"""

from __future__ import annotations

import time
import weakref
import logging
from typing import Any, Dict, Optional, Tuple

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# ANALYSIS STATE MACHINE
# Defined as a class with class-level string constants + transition table.
# Using strings (not IntEnum) keeps repr() readable in logs.
# ──────────────────────────────────────────────────────────────────────────────

class AnalysisState:
    """
    String constants and transition validation for the analysis pipeline.

    Usage:
        from onixey3.runtime.session import AnalysisState
        ok = session_state.transition_to(AnalysisState.RUNNING)
    """

    IDLE    = "idle"     # No analysis requested or active.
    PENDING = "pending"  # Analysis requested; operator not yet started.
    RUNNING = "running"  # Operator actively running (frame_set loop, etc.).
    DONE    = "done"     # Analysis complete; results in cache.
    DIRTY   = "dirty"    # Results exist but animation changed; re-analysis needed.
    ERROR   = "error"    # Last analysis attempt failed.

    # Explicit transition table. Any transition not listed here is INVALID.
    # "IDLE" as destination is always available (emergency reset path).
    _TRANSITIONS: Dict[str, Tuple[str, ...]] = {
        IDLE:    (PENDING,),
        PENDING: (RUNNING, IDLE),
        RUNNING: (DONE, ERROR, IDLE),
        DONE:    (DIRTY, PENDING, IDLE),
        DIRTY:   (PENDING, IDLE),
        ERROR:   (PENDING, IDLE),
    }

    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        """Return True if the transition from_state → to_state is valid."""
        return to_state in cls._TRANSITIONS.get(from_state, ())

    @classmethod
    def all_states(cls) -> Tuple[str, ...]:
        return (cls.IDLE, cls.PENDING, cls.RUNNING, cls.DONE, cls.DIRTY, cls.ERROR)


# ──────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ──────────────────────────────────────────────────────────────────────────────

class SessionState:
    """
    Volatile per-session state container.

    Lifecycle: created by session._init(), destroyed by session._destroy().
    One instance per addon lifecycle. Access via session.get().

    All bpy object references are weakrefs.
    All scalar state is plain Python types.
    Zero disk I/O. Zero bpy.types registration.
    """

    __slots__ = (
        # Weakref-tracked bpy objects
        "_armature_ref",
        "_armature_name",
        "_action_ref",
        "_action_name",
        # Analysis state machine
        "_state",
        "_state_changed_at",
        "_last_error",
        # Dirty tracking
        "_animation_dirty",
        "_dirty_reason",
        # Last analyzed context
        "_last_frame_start",
        "_last_frame_end",
        "_last_analyzed_obj_name",
        "_last_analyzed_at",
        # Session counters (for diagnostics — never used for control flow)
        "_analysis_runs",
        "_analysis_errors",
        "_cache_invalidations",
        "_created_at",
    )

    def __init__(self) -> None:
        # Weakrefs
        self._armature_ref:           Optional[weakref.ref] = None
        self._armature_name:          str = ""
        self._action_ref:             Optional[weakref.ref] = None
        self._action_name:            str = ""
        # State machine
        self._state:                  str   = AnalysisState.IDLE
        self._state_changed_at:       float = time.monotonic()
        self._last_error:             Optional[str] = None
        # Dirty tracking
        self._animation_dirty:        bool = True
        self._dirty_reason:           str  = "initial"
        # Last analyzed context
        self._last_frame_start:       int  = 0
        self._last_frame_end:         int  = 0
        self._last_analyzed_obj_name: str  = ""
        self._last_analyzed_at:       Optional[float] = None
        # Counters
        self._analysis_runs:          int  = 0
        self._analysis_errors:        int  = 0
        self._cache_invalidations:    int  = 0
        self._created_at:             float = time.monotonic()

        _log.debug("SessionState.__init__()")

    # ── Active Armature ──────────────────────────────────────────────────────

    @property
    def active_armature(self) -> Optional[Any]:
        """
        The currently tracked armature bpy.types.Object, or None.

        Returns None if:
          - Nothing was set (fresh session).
          - The object was deleted, undone away, or GC'd by Blender.

        CALLERS MUST CHECK FOR NONE before any attribute access.
        """
        if self._armature_ref is None:
            return None
        obj = self._armature_ref()
        if obj is None:
            # Weakref expired. Object was invalidated.
            _log.debug(
                "SessionState: armature '%s' weakref expired (undo/delete/GC).",
                self._armature_name,
            )
            self._armature_ref = None
            # Don't clear _armature_name — keep for logging even after invalidation.
            self.mark_animation_dirty(reason="armature_invalidated")
        return obj

    @active_armature.setter
    def active_armature(self, obj: Optional[Any]) -> None:
        """
        Track a bpy.types.Object with type='ARMATURE'.

        Args:
            obj: An ARMATURE object, or None to clear tracking.

        Validates type. Marks animation dirty on armature change.
        """
        if obj is None:
            self._armature_ref = None
            self._armature_name = ""
            _log.debug("SessionState: active_armature cleared.")
            return

        obj_type = getattr(obj, "type", None)
        if obj_type != "ARMATURE":
            _log.warning(
                "SessionState: Rejected non-armature object '%s' (type=%s). "
                "Only ARMATURE objects may be set as active_armature.",
                getattr(obj, "name", "?"), obj_type,
            )
            return

        new_name = getattr(obj, "name", "?")
        changed = new_name != self._armature_name

        self._armature_ref = weakref.ref(obj)
        self._armature_name = new_name

        if changed:
            self.mark_animation_dirty(reason=f"armature_changed:{new_name}")
            _log.debug(
                "SessionState: active_armature: '%s' → '%s'.",
                self._armature_name, new_name,
            )

    @property
    def active_armature_name(self) -> str:
        """
        The name of the currently tracked armature, even if the weakref is dead.
        Safe to read at any time — never raises.
        """
        return self._armature_name

    # ── Active Action ────────────────────────────────────────────────────────

    @property
    def active_action(self) -> Optional[Any]:
        """
        The bpy.types.Action currently being analyzed, or None.
        Same weakref semantics as active_armature.
        """
        if self._action_ref is None:
            return None
        action = self._action_ref()
        if action is None:
            _log.debug(
                "SessionState: action '%s' weakref expired.", self._action_name,
            )
            self._action_ref = None
            self.mark_animation_dirty(reason="action_invalidated")
        return action

    @active_action.setter
    def active_action(self, action: Optional[Any]) -> None:
        """
        Track a bpy.types.Action.
        Marks animation dirty if the action changes.
        """
        if action is None:
            self._action_ref = None
            self._action_name = ""
            return

        new_name = getattr(action, "name", "?")
        changed = new_name != self._action_name

        self._action_ref = weakref.ref(action)
        self._action_name = new_name

        if changed:
            self.mark_animation_dirty(reason=f"action_changed:{new_name}")

    @property
    def active_action_name(self) -> str:
        """The name of the tracked action. Safe to read even if weakref is dead."""
        return self._action_name

    # ── Analysis State Machine ───────────────────────────────────────────────

    @property
    def analysis_state(self) -> str:
        """Current state in the analysis pipeline. One of AnalysisState.*."""
        return self._state

    def transition_to(self, new_state: str) -> bool:
        """
        Attempt to transition to new_state.

        Returns:
            True  — transition accepted and applied.
            False — transition invalid from current state (error logged).

        Increments counters on RUNNING and ERROR transitions.
        """
        if not AnalysisState.can_transition(self._state, new_state):
            _log.error(
                "SessionState: Invalid transition %s → %s. "
                "Valid targets: %s",
                self._state, new_state,
                AnalysisState._TRANSITIONS.get(self._state, ()),
            )
            return False

        prev = self._state
        self._state = new_state
        self._state_changed_at = time.monotonic()

        if new_state == AnalysisState.RUNNING:
            self._analysis_runs += 1
        elif new_state == AnalysisState.ERROR:
            self._analysis_errors += 1
        elif new_state == AnalysisState.DONE:
            self._last_analyzed_at = time.monotonic()

        _log.debug("SessionState: %s → %s", prev, new_state)
        return True

    def force_idle(self) -> None:
        """
        Force-reset to IDLE regardless of current state.

        Use ONLY for:
          - unregister() cleanup (must succeed even in RUNNING state)
          - Exception recovery in operator execute()

        Normal code must use transition_to() with state machine validation.
        """
        prev = self._state
        self._state = AnalysisState.IDLE
        self._state_changed_at = time.monotonic()
        if prev != AnalysisState.IDLE:
            _log.debug("SessionState: force_idle() from %s.", prev)

    @property
    def last_error(self) -> Optional[str]:
        """The error message from the last failed analysis, or None."""
        return self._last_error

    def record_error(self, message: str) -> None:
        """
        Record an error message and transition to ERROR state.
        message: human-readable description of what went wrong.
        """
        self._last_error = message
        self.transition_to(AnalysisState.ERROR)
        _log.error("SessionState: analysis error recorded: %s", message)

    def clear_error(self) -> None:
        """Clear the last error message. Called on successful analysis start."""
        self._last_error = None

    # ── Dirty Tracking ───────────────────────────────────────────────────────

    @property
    def is_animation_dirty(self) -> bool:
        """
        True if animation data has changed since the last completed analysis.

        Operators should check this to decide whether cached results are valid:
            state = session.get()
            if state and not state.is_animation_dirty:
                results = cache_get(my_key)
                if results:
                    return use_results(results)
            # Cache miss or dirty — run analysis.
        """
        return self._animation_dirty

    @property
    def dirty_reason(self) -> str:
        """Human-readable reason for the current dirty flag. For diagnostics."""
        return self._dirty_reason

    def mark_animation_dirty(self, reason: str = "unknown") -> None:
        """
        Signal that animation data has changed and cached results may be stale.

        CALLED BY:
            - depsgraph_update_post handler (primary trigger)
            - active_armature setter (on armature change)
            - active_action setter (on action change)
            - Operators that modify FCurves (before execute)

        Side effect:
            If currently DONE, transitions to DIRTY.
            Increments cache_invalidations counter.
        """
        was_dirty = self._animation_dirty
        self._animation_dirty = True
        self._dirty_reason = reason

        if not was_dirty:
            self._cache_invalidations += 1
            # Transition DONE → DIRTY to reflect stale results.
            if self._state == AnalysisState.DONE:
                self.transition_to(AnalysisState.DIRTY)
            _log.debug("SessionState: animation marked dirty (reason=%s).", reason)

    def mark_animation_clean(self) -> None:
        """
        Signal that analysis completed and results reflect current animation.

        CALLED BY:
            - Operator execute() when analysis succeeds (→ DONE state).

        After this, is_animation_dirty returns False until the next change.
        """
        self._animation_dirty = False
        self._dirty_reason = "clean"
        _log.debug("SessionState: animation marked clean.")

    # ── Frame Range Tracking ─────────────────────────────────────────────────

    def set_last_analyzed_range(
        self,
        obj_name: str,
        frame_start: int,
        frame_end: int,
    ) -> None:
        """
        Record the context of the last successful analysis.
        Called by operators when analysis completes.
        """
        self._last_analyzed_obj_name = obj_name
        self._last_frame_start = frame_start
        self._last_frame_end = frame_end
        self._last_analyzed_at = time.monotonic()

    def is_range_current(
        self,
        obj_name: str,
        frame_start: int,
        frame_end: int,
    ) -> bool:
        """
        Return True if the given range matches the last analyzed range AND
        animation has not changed since then.

        Operators use this to skip re-analysis when nothing has changed:
            if state.is_range_current(obj.name, f_start, f_end):
                return {'FINISHED'}  # Use cached results.
        """
        return (
            not self._animation_dirty
            and self._state == AnalysisState.DONE
            and self._last_analyzed_obj_name == obj_name
            and self._last_frame_start == frame_start
            and self._last_frame_end == frame_end
        )

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def get_debug_snapshot(self) -> Dict[str, Any]:
        """
        Return a complete snapshot of session state as plain Python types.

        Safe to log, print, or display in a UI label.
        Never returns bpy objects.
        """
        now = time.monotonic()
        return {
            # Object tracking
            "armature_name":         self._armature_name or "(none)",
            "armature_valid":        self.active_armature is not None,
            "action_name":           self._action_name or "(none)",
            "action_valid":          self.active_action is not None,
            # State machine
            "analysis_state":        self._state,
            "state_age_s":           round(now - self._state_changed_at, 2),
            "last_error":            self._last_error,
            # Dirty tracking
            "animation_dirty":       self._animation_dirty,
            "dirty_reason":          self._dirty_reason,
            # Last analysis
            "last_obj":              self._last_analyzed_obj_name or "(none)",
            "last_range":            (self._last_frame_start, self._last_frame_end),
            "last_analyzed_ago_s":   (
                round(now - self._last_analyzed_at, 1)
                if self._last_analyzed_at else None
            ),
            # Counters
            "analysis_runs":         self._analysis_runs,
            "analysis_errors":       self._analysis_errors,
            "cache_invalidations":   self._cache_invalidations,
            "session_age_s":         round(now - self._created_at, 1),
        }

    # ── Reset / Cleanup ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """
        Drop all state and release all weakrefs.

        Called by session._destroy() during unregister().
        After this call, all bpy object refs are None and GC can reclaim them.
        """
        # Release weakrefs explicitly (not strictly necessary but communicates intent).
        self._armature_ref = None
        self._armature_name = ""
        self._action_ref = None
        self._action_name = ""
        # Reset state machine to a known state.
        self._state = AnalysisState.IDLE
        # Mark everything dirty so next session starts fresh.
        self._animation_dirty = True
        self._dirty_reason = "reset"
        self._last_error = None
        # Clear range tracking.
        self._last_frame_start = 0
        self._last_frame_end = 0
        self._last_analyzed_obj_name = ""
        self._last_analyzed_at = None
        _log.debug("SessionState.reset() complete.")


# ──────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# One SessionState per addon lifecycle.
# Created by _init(). Destroyed by _destroy(). Accessed via get().
# ──────────────────────────────────────────────────────────────────────────────

_session: Optional[SessionState] = None


def get() -> Optional[SessionState]:
    """
    Return the active SessionState, or None if the addon is not registered.

    The canonical access pattern for ALL handlers and operators:

        from onixey3.runtime import session
        state = session.get()
        if state is None:
            return   # Addon not active or in the middle of unregister.

        armature = state.active_armature
        if armature is None:
            return   # No armature tracked or object was invalidated.

        # Safe to proceed.

    Never raises. Returns None instead.
    """
    return _session


def get_or_raise() -> SessionState:
    """
    Return the active SessionState, or raise RuntimeError.

    Use in operators where a None session is a programming error
    (poll() should have prevented the call):

        def execute(self, context):
            state = session.get_or_raise()
            ...

    Prefer get() in handlers and any code that might be called at
    unexpected times — handlers can fire after unregister() in edge cases.
    """
    if _session is None:
        raise RuntimeError(
            "onixey3.runtime.session: SessionState is None. "
            "Was register() called? Is the addon active?"
        )
    return _session


# ──────────────────────────────────────────────────────────────────────────────
# LIFECYCLE  (called exclusively by runtime/lifecycle.py)
# ──────────────────────────────────────────────────────────────────────────────

def _init() -> None:
    """
    Create the singleton SessionState for this addon lifecycle.
    Called ONCE by lifecycle.startup().
    """
    global _session

    if _session is not None:
        _log.warning(
            "session._init(): A SessionState already exists. "
            "Resetting. (Was _destroy() skipped?)"
        )
        _session.reset()
        _session = None

    _session = SessionState()
    _log.debug("session._init(): SessionState created.")


def _destroy() -> None:
    """
    Destroy the singleton SessionState.
    Called ONCE by lifecycle.shutdown().
    Releases all weakrefs and drops the singleton reference.
    """
    global _session
    if _session is not None:
        _session.reset()
        _session = None
    _log.debug("session._destroy(): SessionState destroyed.")
