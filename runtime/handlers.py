"""
onixey3/runtime/handlers.py

Onixey V3 — Centralized Blender Handler Registry (AAA Production Grade)

SINGLE RESPONSIBILITY
─────────────────────
This module is the SOLE owner of every bpy.app.handlers registration and
unregistration in the Onixey runtime. No other module in the codebase is
allowed to call bpy.app.handlers.<list>.append() or .remove() directly.
All handler access goes through this module's public API.

WHAT BELONGS HERE
─────────────────
    ✓ load_post               — New .blend loaded; full cache/session reset.
    ✓ save_pre                — .blend about to be saved; quiesce volatile state.
    ✓ save_post               — .blend save completed; restore state if needed.
    ✓ undo_post               — Ctrl+Z completed; full cache invalidation.
    ✓ redo_post               — Ctrl+Y completed; full cache invalidation.
    ✓ depsgraph_update_post   — Dependency graph re-evaluated; targeted L1/L2 invalidation.
    ✓ frame_change_post       — Active frame changed; L1 invalidation only.

WHAT DOES NOT BELONG HERE
──────────────────────────
    ✗ Animation analysis (arc, spacing, energy, IK/FK) — analysis/ modules
    ✗ FCurve reads or writes                           — analysis/fcurve.py
    ✗ frame_set() calls                                — operators only (batch ops)
    ✗ bpy.ops.* calls                                  — operators only
    ✗ UI redraws (tag_redraw, area.redraw)             — ui/ modules
    ✗ bpy class registration                           — core/registration.py
    ✗ Migration logic                                  — migration/migrations.py

HANDLER OWNERSHIP TABLE
───────────────────────
    Handler                │ Owner        │ @persistent │ Cost target
    ───────────────────────┼──────────────┼─────────────┼────────────
    load_post              │ HERE         │ YES         │ 2ms (file load amortized)
    save_pre               │ HERE         │ NO          │ 0.5ms
    save_post              │ HERE         │ NO          │ 0.2ms
    undo_post              │ HERE         │ NO          │ 1ms
    redo_post              │ HERE         │ NO          │ 1ms
    depsgraph_update_post  │ HERE         │ NO          │ 0.5ms
    frame_change_post      │ HERE         │ NO          │ 0.2ms

    @persistent is used ONLY on load_post because that handler MUST survive
    the file load that wipes all other handlers. Non-persistent handlers are
    automatically removed when a new .blend loads — this is INTENTIONAL for
    all handlers except load_post. After load_post fires and triggers startup(),
    lifecycle.py re-registers everything cleanly.

RELOAD-SAFE DESIGN
──────────────────
Every F8 (Python module reload) cycle:
    1. The previous module's handler function objects are replaced by new ones.
       Old function objects remain in bpy.app.handlers lists as stale references.
    2. On the NEXT register() call, _register_handler() checks for and removes
       stale instances before appending the new function object.
    3. The _HANDLER_REGISTRY table provides a name-indexed lookup so we can
       find and remove stale handlers from previous reload cycles even when
       we no longer hold a reference to the old function objects.
    4. Module-level _MODULE_ID (based on id(module)) lets us detect reload
       identity changes for forensic logging.

ANTI-DUPLICATE GUARANTEE
─────────────────────────
_register_handler() performs a pre-registration sweep:
    - Removes any existing instance of the exact function object.
    - Removes any function with the same __name__ that isn't the current object.
      (This catches stale references from prior F8 reloads.)
Both sweeps log a WARNING if they remove anything — stale handlers indicate
a missed unregister() call and should be investigated.

HANDLER SAFETY RULES (from ONIXEY_V3_AAA_ARCHITECTURE.md)
──────────────────────────────────────────────────────────
    1. Handlers MUST NOT raise exceptions. All paths are wrapped in try/except.
       Unexpected exceptions are logged with traceback and swallowed.
    2. Handlers MUST be fast. See cost targets above. No frame_set(), no ops,
       no analysis, no disk I/O.
    3. Handlers check _started guard at entry. If the runtime is not active,
       the handler returns immediately without touching cache or session.
    4. Handlers import cache/session lazily (inside the function body) to avoid
       circular import issues and to ensure the module is fully loaded.
    5. Handlers never call each other directly.

DEPENDENCY CONTRACT
───────────────────
    Imports from: runtime.cache, runtime.session, core.api_wrappers
    Must NOT import: operators, ui, analysis, properties, migration, lifecycle

    lifecycle.py calls startup() / shutdown() on this module.
    This module does NOT import lifecycle.py (avoids circular dependency).

USAGE FROM lifecycle.py
───────────────────────
    from .handlers import startup as handlers_startup
    from .handlers import shutdown as handlers_shutdown

    # In lifecycle.startup():
    handlers_startup()

    # In lifecycle.shutdown():
    handlers_shutdown()

CHANGELOG
─────────
  3.1.0 — New module (Iteration 3). Extracted from lifecycle.py to give
           each handler its own documented function, add frame_change_post,
           save_pre/save_post, and formalize the anti-duplicate registry.
  3.1.1 — Animation change detection (Iteration 1 fix).
           Added _detect_animation_change() helper that catches FCurve,
           keyframe, Action, NlaStrip, AnimData, and driver changes that
           do NOT set is_updated_transform / is_updated_geometry.
           Extended _invalidate_l2_targeted() to call this helper for Object
           ID types, ensuring Fix Animation changes are never missed.
           Extended _mark_session_dirty_if_armature_updated() to also mark
           dirty when animation data changes without a transform update.
           Added [Runtime] diagnostic log lines for animation change events.
  3.1.2 — False-positive fix in _detect_animation_change() (Object path).
           v3.1.1 Rule 2 checked anim_data.action is not None and
           use_nla == True — these are existence checks, not change checks.
           Every animated rig was firing True on every depsgraph tick,
           invalidating L2 cache 24×/second during playback.
           Fix: gate Object-path detection on is_updated_shading first
           (Blender's signal that a channel actually changed value).
           Then require real content: fcurves non-empty, nla_tracks
           non-empty, or drivers non-empty. Pure existence no longer fires.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# MODULE IDENTITY (reload detection)
#
# id(sys.modules[__name__]) changes on every F8 reload. We capture it here
# at import time and embed it in log messages so that stale-handler warnings
# can be correlated to the correct module generation.
# ──────────────────────────────────────────────────────────────────────────────

_MODULE_GEN: int = id(sys.modules.get(__name__, object()))
_MODULE_GEN_SHORT: str = hex(_MODULE_GEN)[-6:]   # Last 6 hex digits — readable in logs.

_log.debug(
    "handlers.py loaded (module_gen=%s). "
    "If you see this twice, a reload occurred.",
    _MODULE_GEN_SHORT,
)


# ──────────────────────────────────────────────────────────────────────────────
# HANDLER REGISTRY
#
# _HANDLER_REGISTRY maps a stable name → (handler_list_attr, handler_fn, is_persistent)
# for every handler registered in the current session.
#
# "stable name" is the handler's __name__ string — stable across reloads,
# unlike id(fn) which changes. This allows _unregister_by_name() to find
# and remove handlers from previous reload cycles by name scanning.
#
# Only mutated by _register_handler() and _unregister_all_handlers().
# ──────────────────────────────────────────────────────────────────────────────

# Entry: (bpy_handler_list_attr_name, handler_fn, is_persistent)
_HandlerEntry = Tuple[str, Callable, bool]

_HANDLER_REGISTRY: Dict[str, _HandlerEntry] = {}

# Guards against double-startup / double-shutdown.
_started: bool = False

# Counter for forensic metrics — how many handler registrations occurred.
_registration_count: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRATION HELPERS
# All bpy.app.handlers manipulation is funneled through these two functions.
# No other function in this module touches handler lists directly.
# ──────────────────────────────────────────────────────────────────────────────

def _register_handler(
    attr:          str,
    handler_fn:    Callable,
    is_persistent: bool = False,
) -> bool:
    """
    Register a single handler with full anti-duplicate and reload-safe guarantees.

    Algorithm:
        1. Import bpy and resolve the handler list at attr (e.g. "undo_post").
        2. Scan the list for stale entries:
           a. Remove any object that IS the current handler_fn (exact identity).
              Indicates a missed unregister() from a prior session.
           b. Remove any object whose __name__ matches handler_fn.__name__ but
              is NOT the current handler_fn. Indicates a stale F8 reload ref.
           Both removals emit WARNING logs.
        3. Append the current handler_fn.
        4. Record in _HANDLER_REGISTRY.

    Args:
        attr:          Attribute name on bpy.app.handlers (e.g. "undo_post").
        handler_fn:    The Python function to register.
        is_persistent: If True, decorate with @bpy.app.handlers.persistent.
                       Use ONLY for load_post.

    Returns:
        True if registration succeeded. False if the attribute doesn't exist
        on bpy.app.handlers (version guard) or any error occurred.
    """
    global _registration_count

    fn_name = getattr(handler_fn, "__name__", repr(handler_fn))

    try:
        import bpy as _bpy

        handler_list = getattr(_bpy.app.handlers, attr, None)
        if handler_list is None:
            _log.warning(
                "handlers: bpy.app.handlers.%s does not exist on Blender %s. "
                "Handler '%s' will not be registered.",
                attr, _bpy.app.version, fn_name,
            )
            return False

        # ── Anti-duplicate sweep ──────────────────────────────────────────────

        stale_exact   = [f for f in handler_list if f is handler_fn]
        stale_by_name = [
            f for f in handler_list
            if f is not handler_fn and getattr(f, "__name__", None) == fn_name
        ]

        if stale_exact:
            _log.warning(
                "handlers: Found %d EXACT stale instance(s) of '%s' in %s. "
                "Removing before re-registration. (Missed shutdown()? F8 loop?)",
                len(stale_exact), fn_name, attr,
            )
            for stale in stale_exact:
                handler_list.remove(stale)

        if stale_by_name:
            _log.warning(
                "handlers: Found %d RELOAD stale instance(s) named '%s' in %s "
                "(different object identity — prior F8 reload). "
                "Removing. module_gen=%s",
                len(stale_by_name), fn_name, attr, _MODULE_GEN_SHORT,
            )
            for stale in stale_by_name:
                try:
                    handler_list.remove(stale)
                except ValueError:
                    # Already removed by something else — harmless.
                    pass

        # ── Apply @persistent if needed ───────────────────────────────────────

        actual_fn = handler_fn
        if is_persistent:
            actual_fn = _bpy.app.handlers.persistent(handler_fn)
            # Preserve __name__ so name-based stale detection still works.
            actual_fn.__name__ = fn_name  # type: ignore[attr-defined]

        # ── Append ───────────────────────────────────────────────────────────

        handler_list.append(actual_fn)

        # ── Record in registry ────────────────────────────────────────────────

        _HANDLER_REGISTRY[fn_name] = (attr, actual_fn, is_persistent)
        _registration_count += 1

        _log.debug(
            "handlers: registered '%s' → bpy.app.handlers.%s "
            "(persistent=%s, gen=%s)",
            fn_name, attr, is_persistent, _MODULE_GEN_SHORT,
        )
        return True

    except Exception as exc:
        _log.error(
            "handlers: FAILED to register '%s' → %s: %s",
            fn_name, attr, exc,
        )
        traceback.print_exc()
        return False


def _unregister_handler(fn_name: str) -> bool:
    """
    Unregister a single handler by its stable __name__.

    Looks up the handler in _HANDLER_REGISTRY and removes it from the
    corresponding bpy.app.handlers list. Also performs a name-based sweep
    to catch any duplicates that may have snuck in (defensive).

    Args:
        fn_name: The __name__ of the handler function to remove.

    Returns:
        True if at least one instance was removed. False if not found.
    """
    entry = _HANDLER_REGISTRY.pop(fn_name, None)
    if entry is None:
        _log.debug(
            "handlers: _unregister_handler('%s'): not in registry. "
            "Already removed or never registered.",
            fn_name,
        )
        return False

    attr, actual_fn, _ = entry

    try:
        import bpy as _bpy
        handler_list = getattr(_bpy.app.handlers, attr, None)
        if handler_list is None:
            # Handler list itself is gone (shouldn't happen mid-session).
            _log.warning(
                "handlers: bpy.app.handlers.%s missing during unregister of '%s'.",
                attr, fn_name,
            )
            return False

        # Remove by exact identity first.
        removed = 0
        while actual_fn in handler_list:
            handler_list.remove(actual_fn)
            removed += 1

        # Defensive sweep: remove any remaining by name (reload survivors).
        name_survivors = [f for f in handler_list if getattr(f, "__name__", None) == fn_name]
        if name_survivors:
            _log.warning(
                "handlers: %d name-survivor(s) of '%s' found after exact removal. "
                "Removing. (Reload artifact?)",
                len(name_survivors), fn_name,
            )
            for s in name_survivors:
                try:
                    handler_list.remove(s)
                    removed += 1
                except ValueError:
                    pass

        if removed > 0:
            _log.debug(
                "handlers: unregistered '%s' (%d instance(s) removed).", fn_name, removed
            )
        return removed > 0

    except Exception as exc:
        _log.error(
            "handlers: FAILED to unregister '%s': %s", fn_name, exc
        )
        traceback.print_exc()
        return False


def _unregister_all_handlers() -> int:
    """
    Unregister ALL handlers currently in _HANDLER_REGISTRY.

    Called from shutdown(). Iterates a snapshot of the registry so that
    _unregister_handler() can safely mutate _HANDLER_REGISTRY during iteration.

    Returns:
        Number of handler names that were successfully processed.
    """
    names = list(_HANDLER_REGISTRY.keys())   # Snapshot — avoid mutating while iterating.
    count = 0
    for name in names:
        _unregister_handler(name)
        count += 1

    if _HANDLER_REGISTRY:
        # Should be empty after iterating all names.
        _log.warning(
            "handlers: _HANDLER_REGISTRY still has %d entry/ies after full unregister. "
            "Registry: %s",
            len(_HANDLER_REGISTRY), list(_HANDLER_REGISTRY.keys()),
        )

    _log.debug("handlers: _unregister_all_handlers() complete. %d processed.", count)
    return count


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HANDLER IMPLEMENTATIONS
#
# Naming convention: _handler_<blender_event_name>
#
# Every handler follows this contract:
#   1. Check `_started` — return immediately if runtime is not active.
#   2. Wrap the entire body in try/except Exception.
#   3. Log errors with _log.error() + traceback.print_exc().
#   4. NEVER re-raise. Handlers must not propagate exceptions to Blender.
#   5. Lazy-import cache and session inside the function body.
#   6. Measure elapsed time at DEBUG level for performance monitoring.
#
# Handler signature: (scene, *args) for non-persistent handlers.
#                    Blender 4.2+ passes (scene, depsgraph) for some handlers.
#                    We use *args to absorb any extra arguments defensively.
# ──────────────────────────────────────────────────────────────────────────────

def _handler_load_post(scene: Any, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.load_post  [@persistent]

    Fired after a new .blend file is fully loaded.

    Responsibilities:
        1. Full cache invalidation — all cached data refers to the old file.
        2. Mark session animation dirty — the active armature/action may have
           changed completely (different rig, different action, or none).
        3. Force session state to IDLE — any in-flight analysis from the
           previous file is now invalid.

    @persistent: This handler MUST survive the file load. Blender removes all
    non-persistent handlers during load. load_post then fires with the new
    scene, allowing us to reset and re-initialize from a clean state.

    IMPORTANT: Migration logic runs AFTER this handler via migration/migrations.py.
    We do not perform migration here — only raw cache/session invalidation.

    Cost target: ≤ 2ms. This runs after the heavy file I/O is done, so a
    slightly higher budget is acceptable.
    """
    if not _started:
        # Runtime not active. This can legitimately happen when Blender starts
        # and loads a .blend before the addon is registered.
        _log.debug("_handler_load_post: runtime not started. Skipping.")
        return

    t0 = time.perf_counter()
    try:
        from . import cache   as _cache
        from . import session as _session

        # Full cache invalidation — old file's data is foreign.
        _cache.invalidate_all()

        # Reset session state.
        state = _session.get()
        if state is not None:
            # Clear the active armature — the new file may have a different rig.
            state.active_armature = None
            state.active_action   = None
            state.force_idle()
            state.mark_animation_dirty(reason="load_post")
        else:
            _log.debug("_handler_load_post: SessionState is None (unregistered race?)")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.info(
            "load_post handler: cache invalidated, session reset. (%.2fms)",
            elapsed_ms,
        )

        if elapsed_ms > 2.0:
            _log.warning(
                "load_post handler exceeded 2ms budget: %.2fms. "
                "Investigate what's slow in cache.invalidate_all() or session reset.",
                elapsed_ms,
            )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_load_post: Unhandled exception after %.2fms: %s",
            elapsed_ms, exc,
        )
        traceback.print_exc()


def _handler_save_pre(scene: Any, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.save_pre

    Fired immediately before Blender writes the .blend to disk.

    Responsibilities:
        1. Quiesce any volatile state that should not be serialized.
           Onixey stores no scene properties in V3, so this is currently a
           no-op data-wise — but we mark the session to prevent any
           in-flight analysis from writing results to cache during the save.
        2. Log the save event for forensic audit trail.

    NOTE: Onixey does NOT store analysis results in scene properties.
    All state is in-memory only. save_pre/save_post are here for:
        - Future sprint: if we add scene property caching, ensure clean state.
        - Audit logging for support/debugging.
        - Defensive: mark session dirty so any analysis triggered right after
          save doesn't use pre-save cached data.

    Cost target: ≤ 0.5ms.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        from . import session as _session

        state = _session.get()
        scene_name = getattr(scene, "name", "(unknown)") if scene is not None else "(none)"

        if state is not None:
            # If analysis is RUNNING during save, we can't interrupt it here.
            # We simply log the anomaly. Operators should guard against saves
            # during active analysis via their poll() methods.
            from .session import AnalysisState
            if state.analysis_state == AnalysisState.RUNNING:
                _log.warning(
                    "save_pre: Analysis is RUNNING during .blend save ('%s'). "
                    "This is unexpected — operators should block save during analysis.",
                    scene_name,
                )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.debug(
            "save_pre handler: scene='%s' (%.2fms).", scene_name, elapsed_ms,
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_save_pre: exception after %.2fms: %s", elapsed_ms, exc
        )
        traceback.print_exc()


def _handler_save_post(scene: Any, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.save_post

    Fired after Blender successfully writes the .blend to disk.

    Responsibilities:
        1. Restore any state quiesced in save_pre (currently: nothing to restore).
        2. Log the successful save for forensic audit trail.
        3. Future: if save_pre cleared any UI flags, restore them here.

    Cost target: ≤ 0.2ms.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        scene_name = getattr(scene, "name", "(unknown)") if scene is not None else "(none)"
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.debug(
            "save_post handler: .blend saved successfully. scene='%s' (%.2fms).",
            scene_name, elapsed_ms,
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_save_post: exception after %.2fms: %s", elapsed_ms, exc
        )
        traceback.print_exc()


def _handler_undo_post(scene: Any, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.undo_post

    Fired after Ctrl+Z successfully restores a previous .blend state.

    After undo, animation data for ANY object in the scene may have changed.
    We cannot determine which objects were affected without re-scanning the
    entire scene — so full cache invalidation is the only safe choice.

    Responsibilities:
        1. Full cache invalidation (all tiers — L1, L2, L3).
        2. Mark session animation dirty.
        3. If session state was DONE, drive it to DIRTY (stale results).

    Cost target: ≤ 1ms. Runs synchronously in Blender's undo system.

    NOTE: We do NOT transition session to IDLE here — the session still
    knows which armature was active. We only mark results as stale (DIRTY).
    The user can re-analyze without re-selecting the rig.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        from . import cache   as _cache
        from . import session as _session

        _cache.invalidate_all()

        state = _session.get()
        if state is not None:
            state.mark_animation_dirty(reason="undo_post")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.debug("undo_post handler: complete. (%.2fms)", elapsed_ms)

        if elapsed_ms > 1.0:
            _log.warning(
                "undo_post handler exceeded 1ms budget: %.2fms. "
                "cache.invalidate_all() or session.mark_dirty() is slower than expected.",
                elapsed_ms,
            )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_undo_post: exception after %.2fms: %s", elapsed_ms, exc
        )
        traceback.print_exc()


def _handler_redo_post(scene: Any, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.redo_post

    Fired after Ctrl+Y re-applies a previously undone operation.

    Symmetric with undo_post in every respect. Redo restores .blend state
    just as undo does — cached analysis data is equally stale after either.

    Responsibilities:
        1. Full cache invalidation.
        2. Mark session animation dirty.

    Cost target: ≤ 1ms.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        from . import cache   as _cache
        from . import session as _session

        _cache.invalidate_all()

        state = _session.get()
        if state is not None:
            state.mark_animation_dirty(reason="redo_post")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.debug("redo_post handler: complete. (%.2fms)", elapsed_ms)

        if elapsed_ms > 1.0:
            _log.warning(
                "redo_post handler exceeded 1ms budget: %.2fms.",
                elapsed_ms,
            )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_redo_post: exception after %.2fms: %s", elapsed_ms, exc
        )
        traceback.print_exc()


def _handler_depsgraph_update_post(scene: Any, depsgraph: Any = None, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.depsgraph_update_post

    Fired after Blender's dependency graph is re-evaluated. This fires on
    virtually every user interaction: keyframe edits, pose changes, mode
    switches, property updates, etc.

    This is the HOTTEST handler in the system. It MUST be extremely fast.

    Strategy (in order of cost):
        1. Always invalidate L1 (frame-tier data is always stale). O(L1 count).
        2. If depsgraph is available, scan depsgraph.updates for targeted L2
           invalidation — only invalidate L2 for objects that actually changed.
           This avoids wiping 120-second analysis results on e.g. a material
           change that doesn't affect animation.
        3. If depsgraph is None (edge case, old Blender, or API change),
           fall back to full L2 wipe. Better safe than stale.
        4. If the session's active armature was among the updated objects,
           mark session animation dirty. This is an optimization: if the user
           is tweaking a different object, we don't trigger re-analysis UI.

    Responsibilities:
        - L1 cache invalidation (mandatory, every call).
        - Targeted L2 invalidation for changed objects (best effort).
        - Session dirty marking if the active armature changed.

    NOT RESPONSIBLE FOR:
        - L3 (topology) invalidation — that requires armature Edit Mode exit.
        - Any analysis. Not even checking if analysis is needed.
        - frame_set(), bpy.ops.*, UI redraws.

    Cost target: ≤ 0.5ms. Violations are logged at WARNING level.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        from . import cache   as _cache
        from . import session as _session

        # Step 1: Always invalidate L1.
        _cache.invalidate_l1()

        # Step 2: Targeted or conservative L2 invalidation.
        if depsgraph is not None:
            _invalidate_l2_targeted(depsgraph, _cache)
        else:
            # No depsgraph info — conservatively wipe all L2.
            _log.debug(
                "depsgraph_update_post: depsgraph=None. "
                "Falling back to full L2 invalidation."
            )
            _cache.invalidate_by_prefix("L2:")

        # Step 3: Mark session dirty if active armature was updated.
        state = _session.get()
        if state is not None and depsgraph is not None:
            _mark_session_dirty_if_armature_updated(state, depsgraph)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.debug("depsgraph_update_post: complete. (%.2fms)", elapsed_ms)

        if elapsed_ms > 0.5:
            _log.warning(
                "depsgraph_update_post exceeded 0.5ms budget: %.2fms. "
                "depsgraph.updates iteration may be too expensive.",
                elapsed_ms,
            )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_depsgraph_update_post: exception after %.2fms: %s",
            elapsed_ms, exc,
        )
        traceback.print_exc()


def _handler_frame_change_post(scene: Any, depsgraph: Any = None, *args: Any) -> None:
    """
    Handler: bpy.app.handlers.frame_change_post

    Fired when the active frame changes: playback, scrubbing, or an operator
    calling frame_set(). In Blender 4.2+, receives (scene, depsgraph).

    This handler is called during PLAYBACK on every single frame — it MUST
    be as close to zero-cost as possible.

    Responsibilities:
        ONLY: L1 cache invalidation.
        Nothing else.

    Justification for L1-only:
        L2 (analysis results over frame ranges) is NOT affected by frame
        changes — those results span multiple frames and remain valid as long
        as the underlying animation data hasn't changed. Only frame_change
        makes the per-frame evaluated world positions (L1) stale.

    NOT responsible for:
        - L2 or L3 invalidation (those are handled by depsgraph_update_post).
        - Triggering overlays or UI redraws (owned by ui/ in a future sprint).
        - Session dirty marking (frame change ≠ animation data change).

    Cost target: ≤ 0.2ms. This is the strictest budget of any handler.
    Playback at 24fps means this fires 24 times/second. At 0.2ms each,
    it consumes 4.8ms/second of frame budget — already non-trivial.
    """
    if not _started:
        return

    t0 = time.perf_counter()
    try:
        from . import cache as _cache
        _cache.invalidate_l1()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Log only in debug mode — this fires on every frame during playback.
        _log.debug("frame_change_post: L1 invalidated. (%.3fms)", elapsed_ms)

        if elapsed_ms > 0.2:
            _log.warning(
                "frame_change_post exceeded 0.2ms budget: %.3fms. "
                "cache.invalidate_l1() should be O(L1 entries) — check cache size.",
                elapsed_ms,
            )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _log.error(
            "_handler_frame_change_post: exception after %.3fms: %s",
            elapsed_ms, exc,
        )
        traceback.print_exc()


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS FOR DEPSGRAPH ANALYSIS
#
# These are extracted from _handler_depsgraph_update_post to keep the
# handler function body readable and to allow independent testing.
# ──────────────────────────────────────────────────────────────────────────────

def _invalidate_l2_targeted(depsgraph: Any, cache_mod: Any) -> None:
    """
    Scan depsgraph.updates and invalidate L2 entries only for objects whose
    animation or transform data actually changed.

    Design: targeted invalidation preserves expensive L2 analysis results
    (120-second TTL) for objects not touched in this depsgraph evaluation.
    Example: user tweaks a light — we don't wipe the rig's arc analysis.

    ID type handling:
        - "Object" with is_updated_transform → invalidate that object's L2 keys.
        - "Action" → conservative: wipe all L2 (we can't cheaply map action
          to all objects that use it without an O(objects) scan).
          TODO Sprint 3: use session.active_armature_name for targeted Action check.
        - Other types (Material, Light, Mesh, etc.) → skip entirely.

    Args:
        depsgraph: The Blender depsgraph object (guaranteed non-None by caller).
        cache_mod: The cache module (passed in to avoid repeated imports in loop).

    Raises:
        Never. All exceptions logged internally and re-raised to caller's except.
        (Caller's try/except handles the outer boundary.)
    """
    try:
        for update in depsgraph.updates:
            updated_id = update.id
            if updated_id is None:
                continue

            # type(updated_id).__name__ is cheaper than isinstance() when we
            # don't have the bpy type available at import time.
            id_type = type(updated_id).__name__

            if id_type == "Object":
                # Existing path: transform / geometry changes.
                if update.is_updated_transform or update.is_updated_geometry:
                    obj_name = getattr(updated_id, "name", None)
                    if obj_name:
                        cache_mod.invalidate_l2_for(obj_name)

                # v3.1.1: animation change detection — catches FCurve / keyframe
                # edits that do NOT produce a transform update (e.g. Fix Animation
                # modifying keyframe_points, handles, timing, or driver values).
                elif _detect_animation_change(updated_id, update, id_type):
                    obj_name = getattr(updated_id, "name", None)
                    if obj_name:
                        _log.debug(
                            "[Runtime] Animation change detected on Object '%s' "
                            "(no transform update). Invalidating animation cache.",
                            obj_name,
                        )
                        cache_mod.invalidate_l2_for(obj_name)

            elif id_type == "Action":
                # An Action datablock changed (keyframe added/moved/deleted).
                # _detect_animation_change would return True here, but we handle
                # Action explicitly for the full-L2-wipe path.
                # We cannot cheaply find all objects that use this action,
                # so we conservatively wipe all L2.
                action_name = getattr(updated_id, "name", "?")
                _log.debug(
                    "[Runtime] Animation change detected: Action '%s' changed. "
                    "[Runtime] Invalidating animation cache (full L2 wipe).",
                    action_name,
                )
                cache_mod.invalidate_by_prefix("L2:")
                break   # Action → full L2 wipe; no need to scan further updates.

            elif id_type == "Armature":
                # Armature datablock changed — topology may have changed.
                # Note: this does NOT cover pose changes (those come via Object).
                # Wipe L3 topology cache for this armature's object.
                # We don't have the Object name here (only the Armature data name),
                # so we use a name-prefix search on L3 keys.
                arma_name = getattr(updated_id, "name", None)
                if arma_name:
                    _log.debug(
                        "_invalidate_l2_targeted: Armature data '%s' changed. "
                        "Wiping L3 for this armature.",
                        arma_name,
                    )
                    # L3 keys are keyed by Object name, not Armature data name.
                    # We do a conservative full L3 wipe to avoid stale topology.
                    cache_mod.invalidate_by_prefix("L3:")

            # v3.1.1: NlaStrip / NlaTrack / ShapeKey changes.
            # These are caught by _detect_animation_change but need a full L2 wipe
            # because we cannot cheaply map them to a specific object name.
            elif id_type in _ANIMATION_ID_TYPES:
                id_name = getattr(updated_id, "name", "?")
                _log.debug(
                    "[Runtime] Animation change detected: %s '%s' changed. "
                    "[Runtime] Invalidating animation cache (full L2 wipe).",
                    id_type, id_name,
                )
                cache_mod.invalidate_by_prefix("L2:")
                break   # Full L2 wipe covers all; no need to continue.

            # All other types (Material, Light, World, etc.) → skip.

    except Exception as exc:
        # If depsgraph.updates iteration fails (API change in future Blender),
        # fall back to full L2 invalidation to stay safe.
        _log.warning(
            "_invalidate_l2_targeted: depsgraph.updates iteration failed (%s). "
            "Falling back to full L2 invalidation.",
            exc,
        )
        cache_mod.invalidate_by_prefix("L2:")


def _mark_session_dirty_if_armature_updated(state: Any, depsgraph: Any) -> None:
    """
    Mark session animation dirty only if the currently tracked armature was
    among the objects updated in this depsgraph evaluation.

    This is an optimization: if the user is scrubbing keyframes on a prop
    object while working near a rig, we don't want to invalidate the rig's
    analysis results unnecessarily.

    Strategy:
        - Get the active armature name from session (safe — name string persists
          even if the weakref expired).
        - Scan depsgraph.updates for a matching object name.
        - If found with transform or geometry update → mark dirty.

    Args:
        state:     The active SessionState (guaranteed non-None by caller).
        depsgraph: The Blender depsgraph (guaranteed non-None by caller).

    Raises:
        Never. Non-critical — failure here means dirty tracking just misses
        this event; the user sees slightly stale UI until the next analysis.
    """
    armature_name = state.active_armature_name   # str — safe even if weakref dead
    if not armature_name:
        return   # No armature tracked — nothing to check.

    try:
        for update in depsgraph.updates:
            updated_id = update.id
            if updated_id is None:
                continue
            if getattr(updated_id, "name", None) != armature_name:
                continue

            id_type = type(updated_id).__name__

            # Existing path: transform or geometry change on the tracked armature.
            if update.is_updated_transform or update.is_updated_geometry:
                state.mark_animation_dirty(
                    reason=f"depsgraph_update:{armature_name}"
                )
                _log.debug(
                    "_mark_session_dirty_if_armature_updated: "
                    "armature '%s' updated (transform/geometry) → session marked dirty.",
                    armature_name,
                )
                return   # Found — no need to scan further.

            # v3.1.1: animation change on the tracked armature object without
            # a transform update. This is the Fix Animation blind spot:
            # keyframe edits / FCurve handle moves don't move the object in
            # world space at the CURRENT frame, so transform stays False.
            if _detect_animation_change(updated_id, update, id_type):
                state.mark_animation_dirty(
                    reason=f"animation_data_change:{armature_name}"
                )
                _log.debug(
                    "[Runtime] Animation change detected on tracked armature '%s' "
                    "(no transform update — FCurve/keyframe/driver edit). "
                    "[Runtime] Invalidating animation cache.",
                    armature_name,
                )
                return   # Found — no need to scan further.

    except Exception as exc:
        # Non-critical. Dirty tracking just doesn't fire for this event.
        _log.debug(
            "_mark_session_dirty_if_armature_updated: scan failed (%s). "
            "Dirty tracking skipped for this depsgraph eval.",
            exc,
        )


# ──────────────────────────────────────────────────────────────────────────────
# ANIMATION CHANGE DETECTION HELPER  (v3.1.1, false-positive fix v3.1.2)
#
# Detects animation-data mutations that do NOT produce is_updated_transform
# or is_updated_geometry on the depsgraph update entry.
#
# Problem this solves:
#   Fix Animation modifies FCurves, keyframe_points, handles, timing, and
#   NLA strips entirely through the bpy data API — these changes cause
#   Blender to mark the Action or AnimData datablock as updated, but the
#   OBJECT that owns the animation keeps is_updated_transform = False.
#   The old detection logic missed all of these, leaving L2 caches stale.
#
# v3.1.2 false-positive fix:
#   The v3.1.1 Object path checked anim_data.action is not None and
#   use_nla == True — pure EXISTENCE, not change. This fired True on every
#   depsgraph tick for every animated rig, causing 24×/second L2 wipes
#   during playback. Fixed by gating on is_updated_shading (Blender's
#   per-tick signal that an animation channel actually changed value) and
#   requiring non-empty fcurves / nla_tracks / drivers.
#
# Detection strategy (cheapest → most expensive):
#   1. id_type in _ANIMATION_ID_TYPES  — Action/NlaStrip/NlaTrack/ShapeKey
#      These IDs only appear in depsgraph.updates when genuinely modified.
#   2. id_type == "Object"
#      Gate: is_updated_shading must be True (real channel re-evaluation).
#      Then: action.fcurves non-empty OR nla_tracks non-empty OR drivers.
#
# Cost: O(1) per update entry — frozenset lookup + attribute reads.
# ──────────────────────────────────────────────────────────────────────────────

# ID type names that always indicate an animation data change.
_ANIMATION_ID_TYPES: frozenset = frozenset({
    "Action",
    "NlaStrip",
    "NlaTrack",
    "ShapeKey",
})


def _detect_animation_change(
    updated_id: Any,
    update:     Any,
    id_type:    str,
) -> bool:
    """
    Detect whether a single depsgraph update entry represents a REAL animation
    data change — not just the existence of animation data on the object.

    Called from _invalidate_l2_targeted() for each update in depsgraph.updates.
    Also called from _mark_session_dirty_if_armature_updated() for Object entries.

    FALSE POSITIVE PROBLEM (v3.1.1 → v3.1.2 fix)
    ───────────────────────────────────────────────
    The previous v3.1.1 implementation detected animation changes on Objects
    by checking ``anim_data.action is not None`` and ``use_nla == True``.
    These are EXISTENCE checks, not CHANGE checks. An object that has an
    assigned action will fire True on every single depsgraph evaluation
    regardless of whether any animation data actually changed. During 24fps
    playback this produces 24 false-positive invalidations per second per rig,
    wiping L2 cache results that are perfectly valid.

    FIX: Use Blender's is_updated_shading flag as the gating signal for
    Object entries. Blender sets this flag when driver values, custom property
    keyframes, or non-transform animation channels are actually re-evaluated
    to a new value. It is NOT set on idle depsgraph ticks or frame scrubs
    where no animation data changed.

    Detection rules (evaluated in order, returns True on first match):

        Rule 1 — Direct animation datablock types:
            id_type in {"Action", "NlaStrip", "NlaTrack", "ShapeKey"}
            These ID types are produced by the depsgraph ONLY when that
            specific datablock was actually modified. No false positives.

        Rule 2 — Object: require is_updated_shading gate first.
            is_updated_shading=True is Blender's signal that a channel
            on the object (driver, custom prop keyframe, non-transform
            FCurve) was re-evaluated to a new value this tick.
            Without this gate, every animated object fires True constantly.

            After the gate passes, require real content in animation_data:
            - Rule 2a: action assigned AND action.fcurves is non-empty.
                       Guards against a newly-created empty action.
            - Rule 2b: use_nla=True AND nla_tracks is non-empty.
                       Guards against an empty NLA stack.
            - Rule 2c: animation_data.drivers is non-empty.
                       Catches driver-only objects (no action, no NLA).

    Args:
        updated_id: The bpy ID datablock from update.id.
        update:     The depsgraph update object (has .is_updated_transform,
                    .is_updated_geometry, .is_updated_shading, etc.).
        id_type:    type(updated_id).__name__ — pre-computed by caller.

    Returns:
        True  — a real animation change occurred. Caller must invalidate L2
                and mark session dirty.
        False — no real animation change (may still be a transform/geometry
                change handled by the existing path, or a genuine idle tick).

    Raises:
        Never. All attribute access is guarded. Non-critical — a False return
        just means we miss one dirty signal; the user re-triggers analysis.
    """
    # Rule 1: direct animation datablock types — always animation changes.
    if id_type in _ANIMATION_ID_TYPES:
        return True

    # Rules 2 & 3: Object type — check animation_data without transform change.
    if id_type == "Object":
        # If transform or geometry changed, the existing path already handles it.
        # We only need to catch the NON-transform animation mutations here.
        if update.is_updated_transform or update.is_updated_geometry:
            return False

        # REQUIRE is_updated_shading as the Blender-side signal that something
        # on the object's data (including animation channels / drivers) actually
        # changed this evaluation. Without this gate the mere existence of an
        # action or NLA stack would fire True on every single depsgraph eval,
        # causing false-positive invalidations on every frame scrub and playback.
        #
        # Blender sets is_updated_shading when:
        #   - A driver value changed (output re-evaluated by the driver engine).
        #   - A custom property keyframed on the object changed value.
        #   - An action FCurve targeting a non-transform channel was re-evaluated.
        # It does NOT set it for pure transform/geometry moves (those have their
        # own flags) or for unrelated events like viewport visibility toggles.
        #
        # This is deliberately conservative: we may miss the first frame of an
        # edit before Blender sets the flag, but that is far preferable to
        # invalidating the cache on every depsgraph tick for every animated rig.
        if not getattr(update, "is_updated_shading", False):
            return False

        # Safely probe animation_data — require both presence AND actual content.
        try:
            anim_data = getattr(updated_id, "animation_data", None)
            if anim_data is None:
                return False  # Object has no animation data at all.

            # Rule 2: action assigned AND it has FCurves (not an empty action).
            # Checking fcurves avoids a newly-created but empty action firing.
            action = getattr(anim_data, "action", None)
            if action is not None:
                fcurves = getattr(action, "fcurves", None)
                if fcurves is not None and len(fcurves) > 0:
                    return True

            # Rule 3: NLA active AND has at least one NLA track with strips.
            # Avoids firing when use_nla=True but the NLA stack is empty.
            if getattr(anim_data, "use_nla", False):
                nla_tracks = getattr(anim_data, "nla_tracks", None)
                if nla_tracks is not None and len(nla_tracks) > 0:
                    return True

            # Rule 4: driver on the object (not via an action).
            # Drivers are stored in animation_data.drivers directly.
            drivers = getattr(anim_data, "drivers", None)
            if drivers is not None and len(drivers) > 0:
                return True

        except (AttributeError, ReferenceError):
            # Dead bpy reference or unexpected attribute absence — safe to ignore.
            return False

    return False


# ──────────────────────────────────────────────────────────────────────────────
# HANDLER DEFINITION TABLE
#
# Single source of truth for what gets registered and how.
# Processed by startup() in order. Each entry is:
#   (bpy_attr_name, handler_fn, is_persistent)
#
# Order matters for shutdown: handlers are unregistered in reverse order
# to mirror the startup sequence.
# ──────────────────────────────────────────────────────────────────────────────

_HANDLER_DEFINITIONS: Tuple[Tuple[str, Callable, bool], ...] = (
    # (bpy.app.handlers attribute,   handler function,                 @persistent?)
    ("load_post",              _handler_load_post,              True ),   # MUST be first — @persistent
    ("save_pre",               _handler_save_pre,               False),
    ("save_post",              _handler_save_post,              False),
    ("undo_post",              _handler_undo_post,              False),
    ("redo_post",              _handler_redo_post,              False),
    ("depsgraph_update_post",  _handler_depsgraph_update_post,  False),
    ("frame_change_post",      _handler_frame_change_post,      False),
)

# Re-expose Tuple for the type annotation above (used before it's imported).
from typing import Tuple  # noqa: E402  (already imported at top, harmless re-import)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC LIFECYCLE API
# Called exclusively from runtime/lifecycle.py.
# ──────────────────────────────────────────────────────────────────────────────

def startup() -> None:
    """
    Register all Onixey runtime handlers with Blender.

    Called ONCE from lifecycle.startup() AFTER cache and session are
    initialized. Safe to call only when bpy is fully available.

    Idempotent at the _register_handler() level: double-startup emits a
    WARNING and returns early without double-registering anything.

    Raises:
        RuntimeError if registration of any HARD REQUIREMENT handler fails.
        Currently: load_post (@persistent) is the only hard requirement.
        All other handlers are soft — their absence is logged but non-fatal.
    """
    global _started

    if _started:
        _log.warning(
            "handlers.startup() called while already started (gen=%s). "
            "Call shutdown() first. Ignoring duplicate startup.",
            _MODULE_GEN_SHORT,
        )
        return

    _log.debug("handlers.startup(): registering %d handlers. gen=%s",
               len(_HANDLER_DEFINITIONS), _MODULE_GEN_SHORT)

    failed_hard: List[str] = []

    for attr, fn, persistent in _HANDLER_DEFINITIONS:
        ok = _register_handler(attr, fn, is_persistent=persistent)
        if not ok:
            fn_name = getattr(fn, "__name__", repr(fn))
            if persistent:
                # @persistent handlers are hard requirements.
                failed_hard.append(fn_name)
                _log.error(
                    "handlers.startup(): HARD REQUIREMENT FAILED: "
                    "could not register @persistent handler '%s' on '%s'.",
                    fn_name, attr,
                )
            else:
                # Non-persistent handler missing — log WARNING, continue.
                _log.warning(
                    "handlers.startup(): optional handler '%s' (%s) not registered. "
                    "Functionality degraded but addon continues.",
                    fn_name, attr,
                )

    if failed_hard:
        # Roll back all successful registrations before raising.
        _log.error(
            "handlers.startup(): hard requirement(s) failed. "
            "Rolling back %d successful registration(s). Failed: %s",
            len(_HANDLER_REGISTRY), failed_hard,
        )
        _unregister_all_handlers()
        raise RuntimeError(
            f"Onixey handlers.startup() failed: "
            f"could not register hard requirement handler(s): {failed_hard}. "
            f"Blender API may be missing expected attributes."
        )

    _started = True
    _log.info(
        "handlers.startup(): %d handler(s) registered. gen=%s",
        len(_HANDLER_REGISTRY), _MODULE_GEN_SHORT,
    )


def shutdown() -> None:
    """
    Unregister all Onixey runtime handlers from Blender.

    Called ONCE from lifecycle.shutdown() BEFORE cache and session are
    destroyed. This ordering ensures that if any handler fires during the
    shutdown window (edge case), it finds the runtime still active and
    handles the event cleanly — but the very next handler call after this
    returns will be blocked by `_started = False`.

    Idempotent: safe to call when already shut down (logs DEBUG, no-op).

    Does NOT raise. All errors are logged.
    """
    global _started

    if not _started:
        _log.debug(
            "handlers.shutdown() called but not started (gen=%s). No-op.",
            _MODULE_GEN_SHORT,
        )
        return

    # Set _started = False BEFORE unregistering.
    # This means any handler that fires during the removal process will
    # see _started=False and return immediately — safe, fast, no stale access.
    _started = False

    _log.debug(
        "handlers.shutdown(): unregistering %d handler(s). gen=%s",
        len(_HANDLER_REGISTRY), _MODULE_GEN_SHORT,
    )

    count = _unregister_all_handlers()

    _log.info(
        "handlers.shutdown(): %d handler(s) unregistered. gen=%s",
        count, _MODULE_GEN_SHORT,
    )


def is_started() -> bool:
    """
    Return True if handlers are currently registered and the runtime is active.

    Safe to call at any time. Does not raise.
    """
    return _started


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_handler_report() -> Dict[str, Any]:
    """
    Return a snapshot of handler registration state for diagnostic display.

    All values are plain Python primitives — safe to log, print, or display
    in a UI panel. Never contains bpy objects.

    Returns dict with keys:
        started             — bool: whether handlers are registered
        registered_count    — int: number of handlers currently in _HANDLER_REGISTRY
        total_registrations — int: cumulative count since module load (across reloads)
        module_gen          — str: short module identity token (reload detection)
        handler_names       — list[str]: names of currently registered handlers
        registry            — dict[str, dict]: per-handler details:
                                attr:       bpy.app.handlers attribute name
                                persistent: bool
    """
    registry_detail: Dict[str, Dict[str, Any]] = {}
    for name, (attr, fn, persistent) in _HANDLER_REGISTRY.items():
        registry_detail[name] = {
            "attr":       attr,
            "persistent": persistent,
        }

    return {
        "started":             _started,
        "registered_count":    len(_HANDLER_REGISTRY),
        "total_registrations": _registration_count,
        "module_gen":          _MODULE_GEN_SHORT,
        "handler_names":       sorted(_HANDLER_REGISTRY.keys()),
        "registry":            registry_detail,
    }


def get_live_handler_counts() -> Dict[str, int]:
    """
    Return the live count of functions in each bpy.app.handlers list that
    this module cares about.

    Useful for verifying that handlers are not leaking across reloads.

    Returns:
        Dict mapping bpy attribute name → number of registered functions.
        Empty dict if bpy is not available.
    """
    try:
        import bpy as _bpy
        return {
            attr: len(getattr(_bpy.app.handlers, attr, []))
            for attr, _, _ in _HANDLER_DEFINITIONS
        }
    except Exception as exc:
        _log.debug("get_live_handler_counts: bpy unavailable (%s).", exc)
        return {}


def dump_handler_report_to_log() -> None:
    """
    Emit a full handler state report to the Onixey logger at INFO level.

    Intended for: "Copy Debug Info" button, bug reports, console diagnostics.
    NOT for use in handlers or hot paths.
    """
    report = get_handler_report()
    live   = get_live_handler_counts()

    _log.info("=" * 60)
    _log.info("Onixey handlers.py — State Report (gen=%s)", _MODULE_GEN_SHORT)
    _log.info("  started             : %s", report["started"])
    _log.info("  registered_count    : %d", report["registered_count"])
    _log.info("  total_registrations : %d", report["total_registrations"])
    _log.info("  registered handlers :")
    for name, detail in sorted(report["registry"].items()):
        _log.info(
            "    %-42s attr=%-28s persistent=%s",
            name, detail["attr"], detail["persistent"],
        )
    _log.info("  live bpy handler counts:")
    for attr, count in sorted(live.items()):
        _log.info("    bpy.app.handlers.%-28s : %d", attr, count)
    _log.info("=" * 60)
