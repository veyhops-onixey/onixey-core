"""
onixey3/runtime/lifecycle.py

Runtime Lifecycle Orchestrator for Onixey V3.

SINGLE RESPONSIBILITY
─────────────────────
This module is the ONLY place that:
    1. Coordinates startup of all runtime sub-systems in guaranteed order.
    2. Coordinates shutdown and cleanup in guaranteed reverse order.
    3. Provides a reset() entry point for partial invalidation
       (undo/redo without full shutdown).

This module is a PURE ORCHESTRATOR. It does not own any handlers,
does not maintain a handler registry, and does not call bpy.app.handlers
directly. All handler concerns are fully delegated to runtime/handlers.py.

WHAT DOES NOT BELONG HERE
─────────────────────────
    - Cache data structures          → runtime/cache.py
    - Session state                  → runtime/session.py
    - Handler registration/removal   → runtime/handlers.py   ← KEY CHANGE v3.1.1
    - bpy class registration         → core/registration.py
    - Feature flags                  → core/feature_flags.py
    - Migration logic                → migration/migrations.py
    - Analysis execution             → analysis/ modules

HOW __init__.py USES THIS MODULE
─────────────────────────────────
    # In onixey3/__init__.py, inside register():
    from .runtime import lifecycle
    lifecycle.startup()

    # Inside unregister():
    from .runtime import lifecycle
    lifecycle.shutdown()

    # No other module calls lifecycle.startup() or lifecycle.shutdown().

STARTUP ORDER
─────────────
    1. state.initialize()      — runtime flags, lifecycle phase, modal locks
    2. cache.initialize()      — L1/L2/L3 tiers ready
    3. session.initialize()    — SessionState singleton created
    4. handlers.startup()      — all Blender handlers registered

SHUTDOWN ORDER (strict reverse)
────────────────────────────────
    1. handlers.shutdown()     — unregister all Blender handlers FIRST
                                 (stops new events from firing into dead sub-systems)
    2. session.shutdown()      — drop weakrefs, force IDLE
    3. cache.shutdown()        — clear all tiers, mark uninitialized
    4. state.shutdown()        — lifecycle phase → UNREGISTERED

HANDLER OWNERSHIP (from ONIXEY_V3_AAA_ARCHITECTURE.md)
────────────────────────────────────────────────────────
    Handler               │ Owner              │ @persistent │ Max cost
    ──────────────────────┼────────────────────┼─────────────┼──────────
    load_post             │ runtime/handlers   │ YES         │ 2ms
    save_pre              │ runtime/handlers   │ NO          │ 0.5ms
    save_post             │ runtime/handlers   │ NO          │ 0.2ms
    undo_post             │ runtime/handlers   │ NO          │ 1ms
    redo_post             │ runtime/handlers   │ NO          │ 1ms
    depsgraph_update_post │ runtime/handlers   │ NO          │ 0.5ms
    frame_change_post     │ runtime/handlers   │ NO          │ 0.2ms

    lifecycle.py touches NONE of these lists directly.

DEPENDENCY CONTRACT
───────────────────
    Imports from: runtime.cache, runtime.session, runtime.handlers,
                  runtime.state (optional — degrades gracefully if absent)
    Must NOT import: operators, ui, analysis, properties, migration

CHANGELOG
─────────
  3.1.0 — New module (Iteration 2). Centralizes runtime startup/shutdown,
           undo/redo cache invalidation, depsgraph dirty tracking.
  3.1.1 — Refactored (Iteration 3). Converted to pure orchestrator.
           Removed: _REGISTERED_HANDLERS, _register_handler(),
           _unregister_all_handlers(), _register_all_handlers(),
           _handler_undo_post(), _handler_redo_post(),
           _handler_depsgraph_update_post(), _invalidate_l2_for_updated_objects(),
           _check_active_armature_dirty().
           All handler logic now lives exclusively in runtime/handlers.py.
           Replaced private _init()/_destroy() calls with public initialize()/shutdown().
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

_log = logging.getLogger(__name__)

# Whether lifecycle has been started. Guards against double-startup.
_started: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────────────────────────

def startup() -> None:
    """
    Initialize all runtime sub-systems and register Blender handlers.

    Called ONCE from onixey3/__init__.py register().
    Must be called AFTER core.compat.validate_environment() succeeds.
    Must be called BEFORE any analysis/ or operators/ module runs.

    Startup order:
        1. state.initialize()   — runtime flags, lifecycle phase, modal locks
        2. cache.initialize()   — L1/L2/L3 tiers
        3. session.initialize() — SessionState singleton
        4. handlers.startup()   — Blender handler registration

    Raises:
        RuntimeError if any sub-system fails to initialize.
        The caller (__init__.py) catches this and rolls back bpy class
        registration so Blender is left in a clean state.
    """
    global _started

    if _started:
        _log.warning(
            "lifecycle.startup() called while already started. "
            "Call shutdown() first. Ignoring."
        )
        return

    _log.debug("lifecycle.startup(): begin.")

    # Track which steps completed so _emergency_cleanup() can roll back precisely.
    _completed: list = []

    try:
        # ── Step 1: State ─────────────────────────────────────────────────────
        from . import state as _state
        _state.initialize()
        _completed.append("state")
        _log.debug("lifecycle.startup(): state ready.")

        # ── Step 2: Cache ─────────────────────────────────────────────────────
        from . import cache as _cache
        _cache.initialize()
        _completed.append("cache")
        _log.debug("lifecycle.startup(): cache ready.")

        # ── Step 3: Session ───────────────────────────────────────────────────
        from . import session as _session
        _session.initialize()
        _completed.append("session")
        _log.debug("lifecycle.startup(): session ready.")

        # ── Step 4: Handlers ──────────────────────────────────────────────────
        from . import handlers as _handlers
        _handlers.startup()
        _completed.append("handlers")
        _log.debug("lifecycle.startup(): handlers registered.")

        _started = True
        _log.info("Runtime startup complete.")

    except Exception as exc:
        _log.error(
            "lifecycle.startup() FAILED at step after [%s]: %s\n%s",
            ", ".join(_completed) if _completed else "none",
            exc,
            traceback.format_exc(),
        )
        _emergency_cleanup(_completed)
        raise RuntimeError(
            f"Onixey runtime startup failed: {exc}"
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# SHUTDOWN
# ──────────────────────────────────────────────────────────────────────────────

def shutdown() -> None:
    """
    Tear down all runtime sub-systems in strict reverse startup order.

    Called ONCE from onixey3/__init__.py unregister().
    Safe to call even if startup() never completed (no-op in that case).

    Shutdown order (reverse of startup):
        1. handlers.shutdown()  — unregister all Blender handlers FIRST.
                                  Stops new events from firing into dead systems.
        2. session.shutdown()   — drop weakrefs, force session to IDLE.
        3. cache.shutdown()     — clear all tiers, mark uninitialized.
        4. state.shutdown()     — lifecycle phase → UNREGISTERED, clear flags.
    """
    global _started

    if not _started:
        _log.debug("lifecycle.shutdown() called but runtime was not started. No-op.")
        return

    _log.debug("lifecycle.shutdown(): begin.")
    had_error = False

    # ── Step 1: Handlers — MUST be first ─────────────────────────────────────
    # Unregister before cache/session are destroyed. Edge-case: a handler may
    # still be in the middle of a bpy.app.handlers dispatch when we start
    # unregistering. Handlers check _started guard and return immediately — but
    # removing them from the list first is the safest approach.
    try:
        from . import handlers as _handlers
        _handlers.shutdown()
        _log.debug("lifecycle.shutdown(): handlers removed.")
    except Exception as exc:
        _log.error(
            "lifecycle.shutdown(): handlers.shutdown() failed: %s\n%s",
            exc, traceback.format_exc(),
        )
        had_error = True

    # ── Step 2: Session ───────────────────────────────────────────────────────
    try:
        from . import session as _session
        _session.shutdown()
        _log.debug("lifecycle.shutdown(): session destroyed.")
    except Exception as exc:
        _log.error(
            "lifecycle.shutdown(): session.shutdown() failed: %s\n%s",
            exc, traceback.format_exc(),
        )
        had_error = True

    # ── Step 3: Cache ─────────────────────────────────────────────────────────
    try:
        from . import cache as _cache
        _cache.shutdown()
        _log.debug("lifecycle.shutdown(): cache destroyed.")
    except Exception as exc:
        _log.error(
            "lifecycle.shutdown(): cache.shutdown() failed: %s\n%s",
            exc, traceback.format_exc(),
        )
        had_error = True

    # ── Step 4: State ─────────────────────────────────────────────────────────
    try:
        from . import state as _state
        _state.shutdown()
        _log.debug("lifecycle.shutdown(): state cleared.")
    except Exception as exc:
        _log.error(
            "lifecycle.shutdown(): state.shutdown() failed: %s\n%s",
            exc, traceback.format_exc(),
        )
        had_error = True

    _started = False

    if had_error:
        _log.warning("Runtime shutdown completed with errors. See log above.")
    else:
        _log.info("Runtime shutdown complete.")


# ──────────────────────────────────────────────────────────────────────────────
# RESET (partial — for undo/redo/load events)
# ──────────────────────────────────────────────────────────────────────────────

def reset() -> None:
    """
    Perform a partial reset: invalidate all caches and mark session dirty.
    Does NOT tear down sub-systems or re-register handlers.

    This is cheaper than a full shutdown/startup cycle and is the correct
    response to events that make ALL cached data stale (undo, redo, load).

    Callers:
        - handlers._handler_load_post() — after migration completes.
        - Any future code path that needs a full-invalidation without teardown.

    NOTE: undo_post and redo_post handlers call cache.invalidate_all() and
    session.mark_animation_dirty() directly via handlers.py for performance
    (avoids an extra function call in the hot path). reset() is the slightly
    heavier version used for file-load events where a single call site is
    preferred over duplicating the two-liner.
    """
    if not _started:
        return

    _log.debug("lifecycle.reset(): invalidating cache and marking session dirty.")

    try:
        from . import cache as _cache
        _cache.invalidate_all()
    except Exception as exc:
        _log.error(
            "lifecycle.reset(): cache.invalidate_all() failed: %s", exc
        )

    try:
        from . import session as _session
        state = _session.get()
        if state is not None:
            state.mark_animation_dirty(reason="lifecycle_reset")
    except Exception as exc:
        _log.error(
            "lifecycle.reset(): session.mark_animation_dirty() failed: %s", exc
        )


# ──────────────────────────────────────────────────────────────────────────────
# EMERGENCY CLEANUP
# ──────────────────────────────────────────────────────────────────────────────

def _emergency_cleanup(completed_steps: list) -> None:
    """
    Best-effort rollback for when startup() fails mid-way.

    Rolls back only the steps that actually completed, in reverse order.
    Does not raise — called from startup()'s except block.

    Args:
        completed_steps: List of step names that completed before the failure.
                         Populated by startup() as each step succeeds.
                         Values: "state", "cache", "session", "handlers"
    """
    global _started
    _log.warning(
        "lifecycle: emergency cleanup. Completed steps to roll back: %s",
        completed_steps,
    )

    # Roll back in strict reverse order of what completed.
    for step in reversed(completed_steps):
        if step == "handlers":
            try:
                from . import handlers as _handlers
                _handlers.shutdown()
                _log.debug("lifecycle._emergency_cleanup(): handlers shut down.")
            except Exception as exc:
                _log.error("lifecycle._emergency_cleanup(): handlers.shutdown() failed: %s", exc)

        elif step == "session":
            try:
                from . import session as _session
                _session.shutdown()
                _log.debug("lifecycle._emergency_cleanup(): session shut down.")
            except Exception as exc:
                _log.error("lifecycle._emergency_cleanup(): session.shutdown() failed: %s", exc)

        elif step == "cache":
            try:
                from . import cache as _cache
                _cache.shutdown()
                _log.debug("lifecycle._emergency_cleanup(): cache shut down.")
            except Exception as exc:
                _log.error("lifecycle._emergency_cleanup(): cache.shutdown() failed: %s", exc)

        elif step == "state":
            try:
                from . import state as _state
                _state.shutdown()
                _log.debug("lifecycle._emergency_cleanup(): state shut down.")
            except Exception as exc:
                _log.error("lifecycle._emergency_cleanup(): state.shutdown() failed: %s", exc)

    _started = False


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_runtime_report() -> dict:
    """
    Collect a complete runtime status snapshot for diagnostic display.

    Aggregates data from cache, session, handlers, and state.
    All values are plain Python types — safe to log or display in UI.

    Returns dict with keys:
        started           — bool: whether the runtime is active
        cache             — dict from cache.get_stats()
        session           — dict from session.get_debug_snapshot() or None
        handlers          — dict from handlers.get_handler_report()
        state             — dict from state.get_full_report() or {}
        blender_handlers  — dict: live counts from handlers.get_live_handler_counts()
    """
    report: dict = {"started": _started}

    # Cache stats.
    try:
        from . import cache as _cache
        report["cache"] = _cache.get_stats()
    except Exception as exc:
        report["cache"] = {"error": str(exc)}

    # Session snapshot.
    try:
        from . import session as _session
        state = _session.get()
        report["session"] = state.get_debug_snapshot() if state is not None else None
    except Exception as exc:
        report["session"] = {"error": str(exc)}

    # Handler registry report (now sourced from handlers.py — single source of truth).
    try:
        from . import handlers as _handlers
        report["handlers"] = _handlers.get_handler_report()
        report["blender_handlers"] = _handlers.get_live_handler_counts()
    except Exception as exc:
        report["handlers"] = {"error": str(exc)}
        report["blender_handlers"] = {}

    # Runtime state report.
    try:
        from . import state as _state
        report["state"] = _state.get_full_report()
    except Exception:
        # state module is optional — degrades gracefully.
        report["state"] = {}

    return report
