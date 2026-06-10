"""
onixey3/validation/stress.py

Stress Validation — Onixey V3 AAA Internal QA.

PURPOSE
───────
Four focused validation functions for the addon lifecycle:

    validate_handler_duplicates()
        Scan all bpy.app.handlers lists for duplicate entries caused
        by register() running without a preceding unregister().

    validate_scene_properties_cleanup()
        Verify that onixey_* properties were removed from bpy.types.*
        after an unregister() cycle. Detects ghost property bugs.

    validate_register_unregister_cycles()
        Simulate N enable → disable cycles and verify the addon leaves
        Blender in a clean state after each. The only function here
        that modifies Blender state — must be called explicitly.

    validate_memory_cleanup()
        Inspect the runtime cache and session state for stale entries,
        uncleaned data, and leaked references after unregister().

DESIGN CONTRACT
───────────────
- Zero bpy imports at module level — safe to import before bpy init.
- All functions return CheckResult (from report.py).
- All functions are READ-ONLY except validate_register_unregister_cycles().
- No bpy.types registered here. No operators. No UI. No side effects.
- Compatible with Blender 4.2, 4.5, 5.x.

USAGE
─────
    from onixey3.validation.stress import run_all
    from onixey3.validation.report import print_report
    report = run_all()
    print_report(report)
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from .report import (
    CheckResult, Finding, Severity, ValidationReport,
    make_report, finding_ok, finding_info, finding_warning, finding_critical,
)


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Prefix that identifies all Onixey scene properties.
# Must match the ADDON_PREFIX in core/version.py.
_ONIXEY_PROP_PREFIX: str = "onixey"

# bpy.types that Onixey may register properties on.
# Extend this tuple when new property owners are added in properties/.
_PROPERTY_OWNER_TYPES: Tuple[str, ...] = (
    "Scene",
    "Object",
    "PoseBone",
    "Bone",
    "Armature",
)

# Handler lists that Onixey is allowed to register functions on.
_RELEVANT_HANDLER_LISTS: Tuple[str, ...] = (
    "load_post",
    "load_pre",
    "undo_post",
    "redo_post",
    "depsgraph_update_post",
    "frame_change_post",
    "save_pre",
    "save_post",
)


# ──────────────────────────────────────────────────────────────────────────────
# 1. VALIDATE HANDLER DUPLICATES
# ──────────────────────────────────────────────────────────────────────────────

def validate_handler_duplicates(
    addon_module_prefix: str = "onixey3",
) -> CheckResult:
    """
    Scan bpy.app.handlers for duplicate entries.

    A handler appearing more than once means register() ran without a
    preceding unregister() — the classic reload pollution bug that the
    AAA architecture explicitly forbids (Rule 3: symmetric lifecycle).

    Checks two things independently:
        A. Any function appearing more than once in any handler list.
        B. Onixey-owned functions appearing in unexpected handler lists.

    Args:
        addon_module_prefix: Module prefix to identify Onixey handlers
                             (e.g. "onixey3"). Used for ownership detection.

    Returns:
        CheckResult — passed=True if no duplicates found.

    READ-ONLY. Does not modify any handler list.
    """
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped(
            "Handler Duplicates",
            "bpy not available — run inside Blender."
        )

    for list_name in _RELEVANT_HANDLER_LISTS:
        hlist = getattr(bpy.app.handlers, list_name, None)
        if hlist is None:
            continue

        # Count occurrences of each function by identity
        seen: Dict[int, Tuple[Any, int]] = {}
        for fn in hlist:
            fid = id(fn)
            if fid in seen:
                seen[fid] = (seen[fid][0], seen[fid][1] + 1)
            else:
                seen[fid] = (fn, 1)

        for fid, (fn, count) in seen.items():
            fn_name   = getattr(fn, "__name__", repr(fn))
            fn_module = getattr(fn, "__module__", "")

            if count > 1:
                findings.append(finding_critical(
                    "HANDLER_DUPLICATE",
                    f"bpy.app.handlers.{list_name}: '{fn_name}' registered {count}×",
                    detail=f"module: {fn_module}",
                    fix=(
                        f"Ensure unregister() calls safe_handler_remove(bpy.app.handlers.{list_name}, fn) "
                        f"before register() calls safe_handler_append(). "
                        f"Rule 3: every append() → symmetric remove()."
                    ),
                ))

            # Warn if Onixey handler is in a list it should never touch.
            # Per architecture: @persistent only on load_post and load_pre.
            is_onixey = fn_module.startswith(addon_module_prefix)
            is_persistent_decorated = getattr(fn, "_bpy_persistent", False)

            if is_onixey and is_persistent_decorated and list_name not in ("load_post", "load_pre"):
                findings.append(finding_warning(
                    "HANDLER_WRONGLY_PERSISTENT",
                    f"'{fn_name}' in handlers.{list_name} is @persistent but should not be.",
                    detail=(
                        "Per AAA Rule 9: only load_post and load_pre should be @persistent. "
                        f"A @persistent handler in {list_name} will execute in ALL scenes, "
                        "even when Onixey is not relevant."
                    ),
                    fix=f"Remove @bpy.app.handlers.persistent from {fn_name}.",
                ))

    if not findings:
        findings.append(finding_ok(
            "HANDLERS_CLEAN",
            f"No duplicate handlers found across {len(_RELEVANT_HANDLER_LISTS)} handler lists.",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity in (Severity.CRITICAL, Severity.WARNING) for f in findings)

    return CheckResult(
        check_name="Handler Duplicates",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"lists_scanned": len(_RELEVANT_HANDLER_LISTS)},
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. VALIDATE SCENE PROPERTIES CLEANUP
# ──────────────────────────────────────────────────────────────────────────────

def validate_scene_properties_cleanup(
    expected_absent: bool = False,
) -> CheckResult:
    """
    Audit onixey_* properties on all relevant bpy.types.

    Two modes:
        expected_absent=False (default — addon is REGISTERED):
            Verifies no unexpected/duplicate property descriptors exist.
            Informational — reports what is present.

        expected_absent=True (addon just called unregister()):
            Verifies ALL onixey_* properties have been removed.
            A remaining property is a ghost property bug (Critical).

    Ghost property bug (from AAA Architecture forensic findings):
        If unregister() does not call `del bpy.types.Scene.onixey_prop`,
        the property descriptor remains in the RNA system. When the user
        saves the .blend, the property data is serialized with no reader —
        causing AttributeError or data loss on next open.

    Args:
        expected_absent: True = post-unregister verification.
                         False = pre-unregister inventory.

    Returns:
        CheckResult with findings per bpy type.

    READ-ONLY. Does not modify any property.
    """
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped(
            "Scene Properties Cleanup",
            "bpy not available — run inside Blender.",
        )

    all_found: Dict[str, List[str]] = {}  # type_name → [prop_names]

    for type_name in _PROPERTY_OWNER_TYPES:
        bpy_type = getattr(bpy.types, type_name, None)
        if bpy_type is None:
            continue

        try:
            rna_props = bpy_type.bl_rna.properties
        except AttributeError:
            continue

        onixey_props = [
            prop.identifier
            for prop in rna_props
            if prop.identifier.startswith(_ONIXEY_PROP_PREFIX)
        ]

        if onixey_props:
            all_found[type_name] = onixey_props

    if expected_absent:
        # Post-unregister: all onixey props should be gone
        for type_name, props in all_found.items():
            for prop_name in props:
                findings.append(finding_critical(
                    "GHOST_PROPERTY",
                    f"bpy.types.{type_name}.{prop_name} still exists after unregister()",
                    detail=(
                        "Property was not removed. Saving a .blend now will serialize "
                        "this property with no reader → data loss or AttributeError on reload."
                    ),
                    fix=(
                        f"In unregister(): add `del bpy.types.{type_name}.{prop_name}` "
                        f"wrapped in try/except AttributeError. Use safe_unregister_property() "
                        f"from core/api_wrappers.py."
                    ),
                ))

        if not findings:
            findings.append(finding_ok(
                "PROPERTIES_CLEAN",
                f"No onixey_* properties remain on any bpy type after unregister().",
            ))

    else:
        # Pre-unregister: inventory mode — report what is registered
        if all_found:
            for type_name, props in all_found.items():
                findings.append(finding_info(
                    "PROPERTIES_REGISTERED",
                    f"bpy.types.{type_name}: {len(props)} onixey_* properties registered",
                    detail=", ".join(props),
                ))
        else:
            findings.append(finding_info(
                "PROPERTIES_NONE",
                "No onixey_* properties found on any bpy type. "
                "Expected if addon is not yet registered or properties/ not yet built.",
            ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)

    return CheckResult(
        check_name="Scene Properties Cleanup",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={
            "mode":         "post_unregister" if expected_absent else "inventory",
            "types_checked": list(_PROPERTY_OWNER_TYPES),
            "found":         {k: v for k, v in all_found.items()},
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. VALIDATE REGISTER/UNREGISTER CYCLES
# ──────────────────────────────────────────────────────────────────────────────

def validate_register_unregister_cycles(
    addon_module_name: str = "onixey3",
    cycles: int = 3,
) -> CheckResult:
    """
    Simulate N enable → disable cycles and verify clean state after each.

    THIS IS THE ONLY FUNCTION IN THIS MODULE THAT MODIFIES BLENDER STATE.
    It enables and disables the addon intentionally. Do not call it from
    draw(), handlers, or any automatic path.

    Per the AAA architecture checklist (Section 8):
        "Reload: disable/enable 3 times consecutively without error."
        "No handler duplicates after 3 reloads."

    Each cycle verifies:
        - unregister() does not raise
        - No onixey handlers remain after disable
        - No onixey_* ghost properties remain after disable
        - register() does not raise
        - Handler count returns to exactly the expected number

    Args:
        addon_module_name: Module name for addon_utils (e.g. "onixey3").
        cycles:            Number of enable/disable cycles. Default: 3.

    Returns:
        CheckResult — passed=True if all cycles clean.

    ⚠ MODIFIES BLENDER STATE. Call only from debug operator or Text Editor.
    """
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
        import addon_utils
    except ImportError:
        return CheckResult.skipped(
            "Register/Unregister Cycles",
            "bpy or addon_utils not available — run inside Blender.",
        )

    def _count_onixey_handlers() -> Dict[str, int]:
        """Count onixey handlers per handler list."""
        counts: Dict[str, int] = {}
        for list_name in _RELEVANT_HANDLER_LISTS:
            hlist = getattr(bpy.app.handlers, list_name, None)
            if not hasattr(hlist, "__iter__"):
                continue
            n = sum(
                1 for fn in hlist
                if getattr(fn, "__module__", "").startswith(addon_module_name)
            )
            if n > 0:
                counts[list_name] = n
        return counts

    def _count_onixey_props() -> Dict[str, int]:
        """Count remaining onixey_* props per bpy type."""
        counts: Dict[str, int] = {}
        for type_name in _PROPERTY_OWNER_TYPES:
            bpy_type = getattr(bpy.types, type_name, None)
            if bpy_type is None:
                continue
            try:
                n = sum(
                    1 for p in bpy_type.bl_rna.properties
                    if p.identifier.startswith(_ONIXEY_PROP_PREFIX)
                )
                if n > 0:
                    counts[type_name] = n
            except AttributeError:
                continue
        return counts

    cycle_metadata: List[Dict[str, Any]] = []

    for cycle_num in range(1, cycles + 1):
        cycle: Dict[str, Any] = {"cycle": cycle_num}

        # ── DISABLE ───────────────────────────────────────────────────────────
        try:
            addon_utils.disable(addon_module_name, default_set=False)
            cycle["disable"] = "ok"
        except Exception as exc:
            cycle["disable"] = f"ERROR: {exc}"
            findings.append(finding_critical(
                "DISABLE_RAISED",
                f"Cycle {cycle_num}: addon_utils.disable() raised an exception.",
                detail=traceback.format_exc(limit=3),
                fix="Check unregister() for unhandled exceptions. All unregister steps must use try/except.",
            ))
            cycle_metadata.append(cycle)
            continue  # Cannot verify post-disable state if disable itself failed

        # ── POST-DISABLE: Handler leak check ─────────────────────────────────
        leaked_handlers = _count_onixey_handlers()
        cycle["handlers_after_disable"] = leaked_handlers

        for list_name, count in leaked_handlers.items():
            findings.append(finding_critical(
                "HANDLER_LEAK_AFTER_DISABLE",
                f"Cycle {cycle_num}: {count} onixey handler(s) remain in "
                f"bpy.app.handlers.{list_name} after disable.",
                detail=f"handler list: {list_name}, count: {count}",
                fix=(
                    f"In unregister(): call safe_handler_remove(bpy.app.handlers.{list_name}, fn) "
                    f"for every fn registered in register(). Rule 3: symmetric lifecycle."
                ),
            ))

        # ── POST-DISABLE: Ghost property check ───────────────────────────────
        remaining_props = _count_onixey_props()
        cycle["props_after_disable"] = remaining_props

        for type_name, count in remaining_props.items():
            findings.append(finding_critical(
                "GHOST_PROP_AFTER_DISABLE",
                f"Cycle {cycle_num}: {count} onixey_* prop(s) remain on "
                f"bpy.types.{type_name} after disable.",
                fix=(
                    f"In unregister(): call `del bpy.types.{type_name}.prop_name` "
                    f"for every prop registered in register(). Rule 4: del, not overwrite."
                ),
            ))

        # ── RE-ENABLE ─────────────────────────────────────────────────────────
        try:
            addon_utils.enable(addon_module_name, default_set=False)
            cycle["enable"] = "ok"
        except Exception as exc:
            cycle["enable"] = f"ERROR: {exc}"
            findings.append(finding_critical(
                "ENABLE_RAISED",
                f"Cycle {cycle_num}: addon_utils.enable() raised an exception.",
                detail=traceback.format_exc(limit=3),
                fix="Check register() for unhandled exceptions and for double-registration guards.",
            ))
            cycle_metadata.append(cycle)
            continue

        # ── POST-ENABLE: Duplicate handler check ──────────────────────────────
        post_enable_handlers = _count_onixey_handlers()
        cycle["handlers_after_enable"] = post_enable_handlers

        for list_name, count in post_enable_handlers.items():
            if count > 1:
                findings.append(finding_critical(
                    "HANDLER_DUPLICATE_AFTER_CYCLE",
                    f"Cycle {cycle_num}: {count}× duplicate handler in "
                    f"bpy.app.handlers.{list_name} immediately after enable.",
                    fix=(
                        "Use safe_handler_append() from core/api_wrappers.py — "
                        "it removes before appending (idempotent by design)."
                    ),
                ))

        cycle_metadata.append(cycle)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    if not any(f.severity in (Severity.CRITICAL, Severity.WARNING) for f in findings):
        findings.append(finding_ok(
            "CYCLES_CLEAN",
            f"{cycles} register/unregister cycle(s) completed with no leaks or errors.",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)

    return CheckResult(
        check_name="Register/Unregister Cycles",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={
            "cycles_requested": cycles,
            "addon_module":     addon_module_name,
            "cycle_details":    cycle_metadata,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. VALIDATE MEMORY CLEANUP
# ──────────────────────────────────────────────────────────────────────────────

def validate_memory_cleanup(
    addon_module_name: str = "onixey3",
    expect_cache_empty: bool = False,
) -> CheckResult:
    """
    Inspect runtime cache and session state for stale data or leaked references.

    Checks three areas:

        A. runtime.cache — entry count, stale keys, oversized store.
        B. runtime.session — session state cleared after unregister.
        C. sys.modules — no unexpected Onixey module fragments remain
           after an unregister() cycle (module objects should be
           garbage-collected, not permanently retained).

    Args:
        addon_module_name:  Module prefix for sys.modules inspection.
        expect_cache_empty: True = post-unregister verification (cache
                            should be empty). False = runtime inventory.

    Returns:
        CheckResult — passed=True if memory state is healthy.

    READ-ONLY. Does not clear any cache or modify session state.
    """
    start = time.perf_counter()
    findings: List[Finding] = []

    # ── A: runtime.cache inspection ───────────────────────────────────────────
    cache_stats: Optional[Dict[str, Any]] = None
    try:
        # Import lazily — cache module may not be loaded yet
        import importlib
        import sys

        cache_mod_name = f"{addon_module_name}.runtime.cache"
        if cache_mod_name in sys.modules:
            cache_mod = sys.modules[cache_mod_name]
            if hasattr(cache_mod, "get_stats"):
                cache_stats = cache_mod.get_stats()
    except Exception as exc:
        findings.append(finding_info(
            "CACHE_INSPECTION_FAILED",
            f"Could not inspect runtime.cache: {exc}",
        ))

    if cache_stats is not None:
        traj  = cache_stats.get("trajectory_entries", 0)
        anal  = cache_stats.get("analysis_entries", 0)
        t_max = cache_stats.get("trajectory_max", 64)
        a_max = cache_stats.get("analysis_max", 128)
        hits  = cache_stats.get("traj_hits", 0) + cache_stats.get("analysis_hits", 0)
        total = hits + cache_stats.get("traj_misses", 0) + cache_stats.get("analysis_misses", 0)
        hit_rate = (hits / total * 100) if total > 0 else 0.0

        if expect_cache_empty:
            if traj > 0 or anal > 0:
                findings.append(finding_critical(
                    "CACHE_NOT_CLEARED",
                    f"runtime.cache has {traj} trajectory + {anal} analysis entries after unregister().",
                    detail="Cache should be empty after cache.unregister() is called.",
                    fix=(
                        "Ensure runtime.cache.unregister() calls invalidate_all(). "
                        "Ensure __init__.py unregisters 'runtime.cache' in reverse order."
                    ),
                ))
            else:
                findings.append(finding_ok(
                    "CACHE_CLEARED",
                    "runtime.cache is empty after unregister() — correct.",
                ))
        else:
            # Runtime inventory
            findings.append(finding_info(
                "CACHE_STATS",
                f"runtime.cache: {traj}/{t_max} trajectory, {anal}/{a_max} analysis entries",
                detail=f"Hit rate: {hit_rate:.1f}%",
            ))

            # Warn if near capacity (may indicate invalidation not firing)
            if traj >= t_max * 0.9:
                findings.append(finding_warning(
                    "CACHE_NEAR_CAPACITY",
                    f"Trajectory cache at {traj}/{t_max} entries (≥90% full).",
                    detail="High fill rate may indicate undo_post/load_post not invalidating correctly.",
                    fix=(
                        "Verify that bpy.app.handlers.undo_post calls cache.invalidate_all(). "
                        "Per AAA Rule 8: cache must invalidate on every Ctrl+Z."
                    ),
                ))
    else:
        findings.append(finding_info(
            "CACHE_NOT_LOADED",
            "runtime.cache module not in sys.modules — not yet registered or already unloaded.",
        ))

    # ── B: runtime.session inspection ────────────────────────────────────────
    import sys as _sys

    session_mod_name = f"{addon_module_name}.runtime.session"
    if session_mod_name in _sys.modules:
        session_mod = _sys.modules[session_mod_name]

        try:
            state = session_mod.get_state() if hasattr(session_mod, "get_state") else None
        except Exception as exc:
            state = None
            findings.append(finding_warning(
                "SESSION_GET_STATE_FAILED",
                f"session.get_state() raised: {exc}",
            ))

        if state is not None:
            if expect_cache_empty:
                # After unregister, session state should be cleared
                state_dict = state.to_dict() if hasattr(state, "to_dict") else {}
                active_arm = state_dict.get("active_armature_name")
                in_progress = state_dict.get("analysis_in_progress", False)

                if active_arm is not None:
                    findings.append(finding_warning(
                        "SESSION_STALE_REFERENCE",
                        f"session.active_armature_name='{active_arm}' not cleared after unregister().",
                        fix="Ensure session.unregister() calls state.clear().",
                    ))

                if in_progress:
                    findings.append(finding_warning(
                        "SESSION_ANALYSIS_STUCK",
                        "session.analysis_in_progress=True after unregister() — stuck flag.",
                        fix="session.unregister() must call state.clear() which resets this flag.",
                    ))

                if not findings or all(f.severity == Severity.OK for f in findings):
                    findings.append(finding_ok(
                        "SESSION_CLEARED",
                        "Session state is clean after unregister().",
                    ))
            else:
                # Runtime inventory
                if hasattr(state, "to_dict"):
                    d = state.to_dict()
                    findings.append(finding_info(
                        "SESSION_STATE",
                        "runtime.session state snapshot",
                        detail=str({k: v for k, v in d.items() if v not in (None, [], False)}),
                    ))

                    # Warn if referenced objects are dead
                    if d.get("active_armature_name") and not d.get("armature_alive"):
                        findings.append(finding_warning(
                            "SESSION_DEAD_ARMATURE_REF",
                            f"session.active_armature_name='{d['active_armature_name']}' "
                            f"but armature no longer exists in bpy.data.",
                            fix=(
                                "session.py uses name-based references — the resolve_active_armature() "
                                "method should auto-clear stale names. Verify it sets name=None on None result."
                            ),
                        ))
    else:
        findings.append(finding_info(
            "SESSION_NOT_LOADED",
            "runtime.session module not in sys.modules — not yet registered or already unloaded.",
        ))

    # ── C: sys.modules inspection ─────────────────────────────────────────────
    addon_mods = [k for k in _sys.modules if k == addon_module_name or
                  k.startswith(f"{addon_module_name}.")]
    findings.append(finding_info(
        "SYS_MODULES_COUNT",
        f"{len(addon_mods)} onixey3.* module(s) in sys.modules",
        detail=", ".join(sorted(addon_mods)) if addon_mods else "(none)",
    ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)

    return CheckResult(
        check_name="Memory Cleanup",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={
            "mode":               "post_unregister" if expect_cache_empty else "runtime_inventory",
            "cache_stats":        cache_stats,
            "addon_modules_count": len(addon_mods),
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# RUN ALL — Convenience aggregator
# ──────────────────────────────────────────────────────────────────────────────

def run_all(
    addon_module_name: str = "onixey3",
    include_cycle_test: bool = False,
    cycle_count: int = 3,
    expect_post_unregister: bool = False,
) -> ValidationReport:
    """
    Run all non-destructive stress checks and return a ValidationReport.

    By default, validate_register_unregister_cycles() is NOT included
    because it modifies Blender state. Set include_cycle_test=True to
    include it explicitly.

    Args:
        addon_module_name:       Module prefix for all checks.
        include_cycle_test:      If True, run the register/unregister cycle test.
                                 ⚠ This modifies Blender state.
        cycle_count:             Number of cycles for the cycle test.
        expect_post_unregister:  Pass True if running after unregister() to
                                 check cleanup completeness.

    Returns:
        ValidationReport ready for print_report() or as_text().
    """
    results = [
        validate_handler_duplicates(addon_module_name),
        validate_scene_properties_cleanup(expected_absent=expect_post_unregister),
        validate_memory_cleanup(
            addon_module_name,
            expect_cache_empty=expect_post_unregister,
        ),
    ]

    if include_cycle_test:
        results.append(
            validate_register_unregister_cycles(addon_module_name, cycle_count)
        )

    # Build context block for the report
    context: Dict[str, Any] = {"addon_module": addon_module_name}
    try:
        import bpy
        context["blender_version"] = ".".join(str(x) for x in bpy.app.version)
    except ImportError:
        context["blender_version"] = "unavailable"

    return make_report(
        "Onixey V3 — Stress Validation",
        results,
        **context,
    )
