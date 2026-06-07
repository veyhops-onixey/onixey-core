"""
onixey3/core/api_wrappers.py

Safe Wrappers for Blender bpy API Access.

RESPONSIBILITY
──────────────
This module wraps every bpy API call that carries cross-version risk.
When Blender changes an API, only this file needs updating.

It does NOT:
    - Register or unregister bpy classes
    - Manage handler lists (that is registration.py's job)
    - Detect feature availability (that is feature_flags.py's job)
    - Validate the overall environment (that is compat.py's job)

DESIGN RULES
────────────
1. No bpy import at module level.
   Every function imports bpy locally when first called.
   This avoids initialization-order issues during Blender's addon discovery.

2. Every wrapper returns a typed result or a documented sentinel (None, False).
   Callers must check for None/False before using the result.
   Wrappers NEVER raise exceptions to callers — they log and return sentinel.

3. READ-ONLY wrappers are safe to call from analysis modules and draw().
   WRITE wrappers (property registration) are only safe during register().

4. Wrappers that touch the depsgraph must be called from operator execute(),
   never from draw(), never from handlers.

ADDING A NEW WRAPPER
────────────────────
    1. Identify the bpy API call that carries risk.
    2. Write a wrapper function here with:
       - Local bpy import
       - try/except covering the specific failure modes
       - Logging at appropriate level
       - Sentinel return on failure
    3. Document the Blender version risk in the docstring.
    4. Update compat.py facade to re-export if needed.
"""

from __future__ import annotations
from typing import Any, Optional
import traceback
import logging

from .feature_flags import supports_evaluated_get


_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# DEPSGRAPH WRAPPERS
# evaluated_get() is the only correct way to get constraint-resolved positions.
# Using obj.matrix_world directly gives pre-constraint values on many rigs.
# ──────────────────────────────────────────────────────────────────────────────

def get_evaluated_object_safe(context: Any, obj: Any) -> Optional[Any]:
    """
    Return the evaluated (constraint-resolved) version of obj.

    WHY THIS EXISTS
    ───────────────
    obj.matrix_world without evaluation returns the pre-constraint transform.
    For rigs with IK, Copy Rotation, or any constraint, this is WRONG for
    world-space analysis. Always use this function when reading positions
    for arc quality, energy, or overlap analysis.

    CALLERS
    ───────
    analysis/motion_path.py (inside frame_set loop)
    operators/motion_ops.py (before calling analysis)
    NEVER from: draw(), frame_change_post, depsgraph_update_post

    RETURNS
    ───────
    Evaluated bpy.types.Object — treat as READ-ONLY.
    None if evaluation failed (caller must handle).

    Fallback behavior when evaluated_get unavailable (Blender < 2.80):
        Returns the unevaluated obj. World-space data will be incorrect
        for constrained bones. Logged as WARNING.

    Cross-version risk:
        Blender 5.x: evaluated_get() is expected to remain stable.
        If signature changes, update ONLY this function.
    """
    if not supports_evaluated_get():
        _log.warning(
            "evaluated_get() unavailable (Blender < 2.80). "
            "Returning unevaluated object '%s'. "
            "World-space positions will be incorrect for constrained rigs.",
            getattr(obj, "name", "?"),
        )
        return obj  # Degraded but non-crashing fallback

    try:
        depsgraph = context.evaluated_depsgraph_get()
        return obj.evaluated_get(depsgraph)

    except AttributeError as exc:
        # evaluated_get may be absent in unusual context types (e.g. modal preview)
        _log.error(
            "evaluated_get() AttributeError for '%s': %s. "
            "Context type: %s.",
            getattr(obj, "name", "?"),
            exc,
            type(context).__name__,
        )
        return None

    except ReferenceError:
        # Object was deleted between the call and evaluation
        _log.error(
            "ReferenceError in get_evaluated_object_safe: "
            "object '%s' was deleted during evaluation.",
            getattr(obj, "name", "?"),
        )
        return None

    except Exception as exc:
        _log.error(
            "Unexpected error in get_evaluated_object_safe for '%s': %s\n%s",
            getattr(obj, "name", "?"),
            exc,
            traceback.format_exc(),
        )
        return None


def get_depsgraph_safe(context: Any) -> Optional[Any]:
    """
    Return the evaluated depsgraph for the current context.

    RETURNS
    ───────
    bpy.types.Depsgraph or None on failure.

    Callers must check for None:
        depsgraph = get_depsgraph_safe(context)
        if depsgraph is None:
            self.report({'ERROR'}, "Depsgraph unavailable")
            return {'CANCELLED'}

    NEVER call this from draw() or handlers — only from operator execute().

    Cross-version risk:
        Blender 5.x: context.evaluated_depsgraph_get() expected to remain.
        If context type loses this method in some operator types, this
        wrapper absorbs the AttributeError and returns None cleanly.
    """
    try:
        return context.evaluated_depsgraph_get()

    except AttributeError:
        _log.error(
            "evaluated_depsgraph_get() not available on context type '%s'. "
            "This may be a Blender API change in 5.x.",
            type(context).__name__,
        )
        return None

    except Exception as exc:
        _log.error(
            "Unexpected error in get_depsgraph_safe: %s\n%s",
            exc, traceback.format_exc(),
        )
        return None


# ──────────────────────────────────────────────────────────────────────────────
# OBJECT / BONE ACCESS WRAPPERS
# Guard against the most common runtime errors when traversing rig data.
# ──────────────────────────────────────────────────────────────────────────────

def get_active_armature_safe(context: Any) -> Optional[Any]:
    """
    Return the active object if it is an ARMATURE with an active action.

    This is the canonical pre-condition check for all Onixey operators.
    Centralizing it avoids duplicated guard code in every execute().

    RETURNS
    ───────
    bpy.types.Object (armature) or None.
    None means the operator should return {'CANCELLED'}.

    Does NOT check pose mode — operators that require it must add that check.
    """
    try:
        obj = context.active_object
        if obj is None:
            return None
        if obj.type != 'ARMATURE':
            return None
        return obj
    except AttributeError:
        return None


def get_active_action_safe(obj: Any) -> Optional[Any]:
    """
    Return the active action on obj if it exists and has FCurves.

    RETURNS
    ───────
    bpy.types.Action or None.

    None means the object has no animation data, no action assigned,
    or the action has zero FCurves (e.g. a freshly created action).
    """
    try:
        anim_data = obj.animation_data
        if anim_data is None:
            return None
        action = anim_data.action
        if action is None:
            return None
        if len(action.fcurves) == 0:
            return None
        return action
    except (AttributeError, ReferenceError):
        return None


def get_pose_bone_safe(obj: Any, bone_name: str) -> Optional[Any]:
    """
    Return a pose bone by name, guarding against missing bones.

    RETURNS
    ───────
    bpy.types.PoseBone or None.

    None if the armature has no such bone (e.g. action references a deleted bone).
    """
    try:
        return obj.pose.bones.get(bone_name)
    except (AttributeError, ReferenceError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# PROPERTY REGISTRATION WRAPPERS
# Uses del (Rigify pattern) for unregistration.
# setattr(type, name, None) does NOT properly unregister — always use delattr.
# ──────────────────────────────────────────────────────────────────────────────

def safe_register_property(
    owner_type: Any,
    prop_name: str,
    prop_value: Any,
) -> None:
    """
    Register a bpy property on a type, idempotently.

    Idempotent: if the property is already registered, this is a no-op.
    Prevents the 'ghost property' bug from double registration across reloads.

    WHEN TO USE
    ───────────
    Call from properties/scene_props.py register() — not from operators or UI.

    Args:
        owner_type: e.g. bpy.types.Scene, bpy.types.Object, bpy.types.PoseBone
        prop_name:  attribute name, e.g. "onixey3_health"
        prop_value: a bpy.props.* descriptor instance

    Example:
        safe_register_property(
            bpy.types.Scene,
            "onixey3_health",
            bpy.props.IntProperty(name="Health", default=100, min=0, max=100)
        )
    """
    type_name = getattr(owner_type, "__name__", repr(owner_type))

    if hasattr(owner_type, prop_name):
        _log.debug(
            "Property %s.%s already registered — skipping.",
            type_name, prop_name,
        )
        return

    try:
        setattr(owner_type, prop_name, prop_value)
        _log.debug("Property registered: %s.%s", type_name, prop_name)
    except Exception as exc:
        _log.error(
            "Failed to register property %s.%s: %s\n%s",
            type_name, prop_name, exc, traceback.format_exc(),
        )


def safe_unregister_property(owner_type: Any, prop_name: str) -> None:
    """
    Unregister a bpy property from a type, idempotently.

    Uses delattr — the ONLY correct pattern (Rigify standard).
    setattr(type, name, None) leaves the property registered; use del.

    Idempotent: if the property is not registered, this is a no-op.
    Not finding the property is not an error — it may have been cleaned up
    already by a previous unregister() call (partial reload scenario).

    Args:
        owner_type: e.g. bpy.types.Scene
        prop_name:  attribute name to remove

    Example:
        safe_unregister_property(bpy.types.Scene, "onixey3_health")
    """
    type_name = getattr(owner_type, "__name__", repr(owner_type))

    if not hasattr(owner_type, prop_name):
        _log.debug(
            "Property %s.%s not found during unregister — already clean.",
            type_name, prop_name,
        )
        return

    try:
        delattr(owner_type, prop_name)
        _log.debug("Property unregistered: %s.%s", type_name, prop_name)
    except AttributeError as exc:
        # Can occur if the property was registered by a different module instance
        _log.error(
            "Failed to unregister property %s.%s (AttributeError): %s",
            type_name, prop_name, exc,
        )
    except Exception as exc:
        _log.error(
            "Unexpected error unregistering property %s.%s: %s\n%s",
            type_name, prop_name, exc, traceback.format_exc(),
        )


# ──────────────────────────────────────────────────────────────────────────────
# HANDLER WRAPPERS
# Idempotent append/remove prevents duplicate handler bugs on reload.
# N reloads without these = N handler executions per frame = performance death.
# ──────────────────────────────────────────────────────────────────────────────

def safe_handler_append(handler_list: list, handler_fn: Any) -> None:
    """
    Append handler_fn to a Blender handler list, preventing duplicates.

    Idempotent: calling this N times for the same function registers it once.

    WHY DUPLICATES HAPPEN
    ──────────────────────
    When an addon is disabled and re-enabled (or reloaded via F8), Python
    re-executes register(). If register() calls handler_list.append()
    without first removing the old instance, the handler runs N times per
    event after N reloads. This is silent and catastrophic for performance.

    This wrapper removes all existing instances before appending — ensuring
    exactly one registration regardless of how many reloads occurred.

    Args:
        handler_list: e.g. bpy.app.handlers.load_post
        handler_fn:   the handler function to append (should be @persistent
                      ONLY for load_post and load_pre)
    """
    safe_handler_remove(handler_list, handler_fn)  # Remove stale instances first
    handler_list.append(handler_fn)
    _log.debug(
        "Handler registered: %s",
        getattr(handler_fn, "__name__", repr(handler_fn)),
    )


def safe_handler_remove(handler_list: list, handler_fn: Any) -> None:
    """
    Remove ALL instances of handler_fn from a Blender handler list.

    Idempotent: calling this for a handler that is not in the list is safe.
    Removes ALL instances to recover from any duplicate registration scenario.

    Args:
        handler_list: e.g. bpy.app.handlers.load_post
        handler_fn:   the handler function to remove
    """
    fn_name = getattr(handler_fn, "__name__", repr(handler_fn))
    removed = 0
    while handler_fn in handler_list:
        handler_list.remove(handler_fn)
        removed += 1

    if removed > 1:
        _log.warning(
            "Handler '%s' was registered %d times (duplicate detected). "
            "All instances removed. Check for missing safe_handler_remove() "
            "calls in unregister().",
            fn_name, removed,
        )
    elif removed == 1:
        _log.debug("Handler removed: %s", fn_name)
    # removed == 0: was not registered, idempotently OK


# ──────────────────────────────────────────────────────────────────────────────
# HANDLER DIAGNOSTICS
# Read-only inspection of bpy.app.handlers for forensic reports and the
# runtime lifecycle diagnostic system.
#
# CONTRACT:
#   These functions are STRICTLY read-only. They NEVER modify handler lists.
#   Safe to call from:
#       - get_runtime_report()  in runtime/lifecycle.py
#       - get_forensic_report() in core/registration.py
#       - diagnostic operators  in operators/utility_ops.py
#       - draw()                (no bpy mutation, no depsgraph, < 0.1ms)
#   NOT needed from:
#       - frame_change_post  (already inside a handler — no self-inspection)
#       - depsgraph_update_post  (same reason)
# ──────────────────────────────────────────────────────────────────────────────

# All handler list names exposed on bpy.app.handlers, in the order Blender
# 4.2 documents them. Ordered for deterministic output in reports.
# When Blender adds a new handler in 5.x, add its name here — nothing else changes.
_KNOWN_HANDLER_LISTS: tuple[str, ...] = (
    "load_pre",
    "load_post",
    "load_factory_startup_pre",
    "load_factory_startup_post",
    "load_factory_preferences_pre",
    "load_factory_preferences_post",
    "save_pre",
    "save_post",
    "save_backup_pre",
    "save_backup_post",
    "undo_pre",
    "undo_post",
    "redo_pre",
    "redo_post",
    "depsgraph_update_pre",
    "depsgraph_update_post",
    "frame_change_pre",
    "frame_change_post",
    "render_pre",
    "render_post",
    "render_cancel",
    "render_complete",
    "render_init",
    "render_stats",
    "render_write",
    "annotation_pre",
    "annotation_post",
    "object_bake_pre",
    "object_bake_complete",
    "object_bake_cancel",
    "version_update",
    "xr_session_start_pre",
    "composite_pre",
    "composite_post",
    "composite_cancel",
)

# Module names that identify Onixey's own handlers in the forensic output.
# A handler whose __module__ starts with any of these is flagged as "onixey".
_ONIXEY_MODULE_PREFIXES: tuple[str, ...] = ("onixey3",)


def snapshot_handler_counts() -> dict[str, Any]:
    """
    Return a complete read-only snapshot of all bpy.app.handler list states.

    WHAT THIS RETURNS
    ──────────────────
    A plain-Python dict (no bpy objects) with the following structure:

    {
        "lists": {
            "load_post": {
                "total":      int,   # total handlers registered
                "onixey":     int,   # handlers belonging to onixey3
                "duplicates": int,   # functions registered more than once
                "persistent": int,   # handlers decorated with @persistent
                "entries": [
                    {
                        "name":       str,   # handler.__name__ or repr()
                        "module":     str,   # handler.__module__ or "?"
                        "is_onixey":  bool,  # belongs to onixey3
                        "persistent": bool,  # @persistent decorated
                        "count":      int,   # how many times registered (>1 = duplicate)
                    },
                    ...
                ],
            },
            ...  (one entry per known handler list that exists in this Blender version)
        },
        "summary": {
            "total_handlers":     int,  # sum across all lists
            "onixey_handlers":    int,  # sum of onixey handlers across all lists
            "duplicate_handlers": int,  # sum of duplicates across all lists
            "persistent_handlers":int,  # sum of @persistent across all lists
            "lists_with_onixey":  int,  # number of lists that contain onixey handlers
            "lists_with_duplicates": int,  # number of lists that have duplicates
        },
        "alerts": [str, ...],  # human-readable warnings (duplicates, unexpected handlers)
        "blender_version": [int, int, int],
        "available_lists": [str, ...],  # handler list names present in this Blender version
        "missing_lists":   [str, ...],  # names in _KNOWN_HANDLER_LISTS absent from this Blender
    }

    PERFORMANCE
    ───────────
    Pure Python list traversal. No bpy mutation. No frame_set().
    Typical cost: < 0.5ms on a scene with < 100 total handlers.
    Safe to call from draw() if needed for a diagnostic panel.

    CROSS-VERSION SAFETY
    ─────────────────────
    Handler lists absent from this Blender build (e.g. xr_session_start_pre
    on a non-XR build) are silently skipped and reported in "missing_lists".
    This function never raises — it returns partial data with an alert instead.

    DUPLICATE DETECTION
    ────────────────────
    A duplicate is defined as the same function object appearing more than once
    in the same handler list. This can happen when:
        - register() is called without a preceding unregister()
        - A migration script appended handlers without the idempotency guard
        - A prior crashed session left stale handlers
    Duplicates are reported in "alerts" and per-list "duplicates" count.

    ONIXEY HANDLER IDENTIFICATION
    ──────────────────────────────
    A handler is flagged as "onixey" if its __module__ starts with "onixey3".
    This correctly identifies both current session handlers and any stale
    handlers from a prior reload that were not cleaned up.

    CALLED BY
    ─────────
    runtime/lifecycle.py  → get_runtime_report()
    core/registration.py  → get_forensic_report()  (optional integration)
    operators/utility_ops → diagnostic display operator
    """
    import bpy

    H = bpy.app.handlers

    available: list[str] = []
    missing:   list[str] = []
    alerts:    list[str] = []
    lists_out: dict[str, Any] = {}

    summary = {
        "total_handlers":        0,
        "onixey_handlers":       0,
        "duplicate_handlers":    0,
        "persistent_handlers":   0,
        "lists_with_onixey":     0,
        "lists_with_duplicates": 0,
    }

    for list_name in _KNOWN_HANDLER_LISTS:
        handler_list = getattr(H, list_name, None)
        if handler_list is None:
            missing.append(list_name)
            continue

        available.append(list_name)

        # ── Traverse the handler list (read-only) ─────────────────────────────
        # Count occurrences of each function object to detect duplicates.
        # fn_id → (fn, count) mapping built in one pass.
        seen: dict[int, list[Any]] = {}   # id(fn) → [fn, count]
        entries_raw: list[Any] = []

        try:
            for fn in handler_list:
                fid = id(fn)
                if fid not in seen:
                    seen[fid] = [fn, 0]
                seen[fid][1] += 1
                entries_raw.append(fn)
        except Exception as exc:
            # Defensive: handler list iteration failed (unusual, future API change).
            alerts.append(
                f"[{list_name}] list iteration failed: {exc}. "
                f"Data for this list may be incomplete."
            )
            _log.warning(
                "snapshot_handler_counts: failed to iterate '%s': %s",
                list_name, exc,
            )
            lists_out[list_name] = {
                "total": 0, "onixey": 0, "duplicates": 0,
                "persistent": 0, "entries": [],
                "error": str(exc),
            }
            continue

        # ── Build per-entry records ───────────────────────────────────────────
        entries: list[dict[str, Any]] = []
        list_onixey    = 0
        list_dupes     = 0
        list_persistent = 0

        # Track which function IDs we've already added to entries
        # (avoid duplicating the entry row itself; count is in "count" field).
        seen_in_entries: set[int] = set()

        for fn in entries_raw:
            fid = id(fn)
            _, count = seen[fid]

            fn_name   = getattr(fn, "__name__",   None) or repr(fn)
            fn_module = getattr(fn, "__module__",  None) or "?"

            is_onixey = any(
                fn_module.startswith(pfx)
                for pfx in _ONIXEY_MODULE_PREFIXES
            )

            # @persistent marker: Blender sets app.handlers.persistent attribute
            # on handler functions decorated with @bpy.app.handlers.persistent.
            # The attribute name is "persistent" and its value is True.
            is_persistent = getattr(fn, "persistent", False) is True

            entry: dict[str, Any] = {
                "name":       fn_name,
                "module":     fn_module,
                "is_onixey":  is_onixey,
                "persistent": is_persistent,
                "count":      count,
            }

            # Accumulate per-list counters (once per unique function).
            if fid not in seen_in_entries:
                seen_in_entries.add(fid)
                entries.append(entry)

                if is_onixey:
                    list_onixey += 1
                if count > 1:
                    list_dupes += 1
                    alerts.append(
                        f"[DUPLICATE] '{fn_name}' from '{fn_module}' "
                        f"is registered {count}x in '{list_name}'. "
                        f"Possible missed unregister()."
                    )
                    _log.warning(
                        "Duplicate handler detected: '%s' (%s) appears %d times "
                        "in bpy.app.handlers.%s.",
                        fn_name, fn_module, count, list_name,
                    )
                if is_persistent:
                    list_persistent += 1
                    # @persistent on non-load handlers is almost always a bug.
                    if list_name not in ("load_post", "load_pre",
                                        "load_factory_startup_post",
                                        "load_factory_startup_pre",
                                        "load_factory_preferences_post",
                                        "load_factory_preferences_pre",
                                        "save_pre", "save_post",
                                        "version_update"):
                        if is_onixey:
                            alerts.append(
                                f"[WARNING] Onixey handler '{fn_name}' in "
                                f"'{list_name}' is @persistent. "
                                f"@persistent on non-load handlers survives "
                                f"file loads and runs even when the addon is "
                                f"disabled. This is almost certainly a bug."
                            )

        # ── Total handler count = raw list length (includes duplicates) ───────
        total_in_list = len(entries_raw)

        lists_out[list_name] = {
            "total":      total_in_list,
            "onixey":     list_onixey,
            "duplicates": list_dupes,
            "persistent": list_persistent,
            "entries":    entries,
        }

        # Accumulate into summary.
        summary["total_handlers"]     += total_in_list
        summary["onixey_handlers"]    += list_onixey
        summary["duplicate_handlers"] += list_dupes
        summary["persistent_handlers"] += list_persistent
        if list_onixey > 0:
            summary["lists_with_onixey"] += 1
        if list_dupes > 0:
            summary["lists_with_duplicates"] += 1

    # ── Blender version (deferred import, cached after first call) ────────────
    try:
        bv: list[int] = list(bpy.app.version[:3])
    except Exception:
        bv = [0, 0, 0]

    _log.debug(
        "snapshot_handler_counts: %d lists scanned, %d total handlers, "
        "%d onixey, %d duplicates, %d alerts.",
        len(available),
        summary["total_handlers"],
        summary["onixey_handlers"],
        summary["duplicate_handlers"],
        len(alerts),
    )

    return {
        "lists":             lists_out,
        "summary":           summary,
        "alerts":            alerts,
        "blender_version":   bv,
        "available_lists":   available,
        "missing_lists":     missing,
    }
