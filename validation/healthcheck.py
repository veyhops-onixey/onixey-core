"""
onixey3/validation/healthcheck.py

Health Check — Onixey V3 AAA Startup & Runtime Diagnostics.

PURPOSE
───────
Three classes of checks, callable at any time:

    run_startup_checks()
        All checks that must pass before the addon registers:
        Blender version, Python version, required bpy APIs,
        module compatibility declarations.

    run_blender_compatibility_report()
        Detailed feature-flag report: which Blender APIs are available,
        which are degraded, which are missing. Structured for logging
        and for display in a future debug panel.

    run_module_compatibility_report()
        Verify that all loaded Onixey sub-modules declare a MODULE_COMPAT
        value compatible with the current core/version.py contract.

    run_runtime_diagnostics()
        Snapshot of the full runtime state: handlers registered, cache
        stats, session state, sys.modules, feature flags. No pass/fail —
        purely informational for debugging and bug reports.

DESIGN CONTRACT
───────────────
- Zero bpy imports at module level.
- All functions return CheckResult or ValidationReport (from report.py).
- No side effects. READ-ONLY throughout.
- All bpy access is inside try/except — if bpy is unavailable, the check
  returns CheckResult.skipped() instead of raising.
- Compatible with Blender 4.2, 4.5, 5.x.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from .report import (
    CheckResult, Finding, Severity, ValidationReport,
    make_report, finding_ok, finding_info, finding_warning, finding_critical,
)


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL CONSTANTS
# These replicate the constraints from core/version.py to avoid a circular
# import. Validation must be runnable even when core/ has import errors.
# ──────────────────────────────────────────────────────────────────────────────

_BLENDER_MIN:     Tuple[int, int, int] = (4, 2, 0)
_BLENDER_SOFT_MAX: Tuple[int, int, int] = (5, 9, 99)
_PYTHON_MIN:      Tuple[int, int] = (3, 11)
_ADDON_MODULE:    str = "onixey3"
_MODULE_COMPAT_REQUIRED: int = 3

# APIs whose presence we verify at startup.
# Format: (description, bpy_attribute_path_as_dotted_string)
_REQUIRED_BPY_APIS: Tuple[Tuple[str, str], ...] = (
    ("bpy.app.handlers.load_post",         "app.handlers.load_post"),
    ("bpy.app.handlers.undo_post",         "app.handlers.undo_post"),
    ("bpy.app.handlers.depsgraph_update_post", "app.handlers.depsgraph_update_post"),
    ("bpy.utils.register_class",           "utils.register_class"),
    ("bpy.utils.unregister_class",         "utils.unregister_class"),
    ("bpy.types.Scene",                    "types.Scene"),
    ("bpy.types.Armature",                 "types.Armature"),
    ("bpy.types.PoseBone",                 "types.PoseBone"),
    ("bpy.props.IntProperty",              "props.IntProperty"),
    ("bpy.props.FloatProperty",            "props.FloatProperty"),
    ("bpy.props.StringProperty",           "props.StringProperty"),
    ("bpy.props.PointerProperty",          "props.PointerProperty"),
    ("bpy.props.CollectionProperty",       "props.CollectionProperty"),
)

# Sub-modules that MUST be loadable when the addon is registered.
# Format: (import_path, human_name)
_CORE_MODULES: Tuple[Tuple[str, str], ...] = (
    (f"{_ADDON_MODULE}.core.version", "core.version"),
    (f"{_ADDON_MODULE}.core.compat",  "core.compat"),
)


# ──────────────────────────────────────────────────────────────────────────────
# STARTUP CHECKS
# ──────────────────────────────────────────────────────────────────────────────

def run_startup_checks(addon_module_name: str = _ADDON_MODULE) -> ValidationReport:
    """
    Run all checks that validate the environment before or during register().

    These checks mirror the validation already done in core/compat_checks.py
    but produce structured CheckResult output for debugging and QA logging.
    They do NOT call validate_environment() — they are independent observers.

    Returns:
        ValidationReport with one CheckResult per check domain.
    """
    context = _build_context()
    results = [
        _check_blender_version(),
        _check_python_version(),
        _check_required_bpy_apis(),
        _check_core_modules_importable(addon_module_name),
    ]
    return make_report("Onixey V3 — Startup Checks", results, **context)


def _check_blender_version() -> CheckResult:
    """Verify Blender version is within the supported range."""
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
        v = bpy.app.version
        v_str = ".".join(str(x) for x in v[:3])
    except ImportError:
        return CheckResult.skipped("Blender Version", "bpy unavailable.")

    if v[:3] < _BLENDER_MIN:
        findings.append(finding_critical(
            "BLENDER_TOO_OLD",
            f"Blender {v_str} is below the minimum {'.'.join(str(x) for x in _BLENDER_MIN)}.",
            detail="Addon will not register. APIs required by Onixey V3 are absent.",
            fix=f"Upgrade to Blender {'.'.join(str(x) for x in _BLENDER_MIN)} or newer.",
        ))
    elif v[:3] > _BLENDER_SOFT_MAX:
        findings.append(finding_warning(
            "BLENDER_ABOVE_TESTED",
            f"Blender {v_str} is above the tested ceiling "
            f"{'.'.join(str(x) for x in _BLENDER_SOFT_MAX)}.",
            detail="Addon will attempt to load. Some features may misbehave on untested builds.",
            fix="Report issues at the project repository if behavior is unexpected.",
        ))
    else:
        findings.append(finding_ok(
            "BLENDER_VERSION_OK",
            f"Blender {v_str} is within the supported range "
            f"[{'.'.join(str(x) for x in _BLENDER_MIN)}, "
            f"{'.'.join(str(x) for x in _BLENDER_SOFT_MAX)}].",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Blender Version",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"version": tuple(bpy.app.version[:3])},
    )


def _check_python_version() -> CheckResult:
    """Verify the embedded Python version meets the minimum."""
    start = time.perf_counter()
    vi = sys.version_info
    py_str = f"{vi.major}.{vi.minor}.{vi.micro}"
    findings: List[Finding] = []

    if (vi.major, vi.minor) < _PYTHON_MIN:
        findings.append(finding_warning(
            "PYTHON_TOO_OLD",
            f"Python {py_str} is below the tested minimum "
            f"{_PYTHON_MIN[0]}.{_PYTHON_MIN[1]}.",
            detail="Some syntax or stdlib features used by Onixey V3 may not be available.",
            fix="Use a Blender build that ships Python 3.11 or newer.",
        ))
    else:
        findings.append(finding_ok(
            "PYTHON_VERSION_OK",
            f"Python {py_str} meets the minimum "
            f"{_PYTHON_MIN[0]}.{_PYTHON_MIN[1]}+.",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Python Version",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"version": py_str},
    )


def _check_required_bpy_apis() -> CheckResult:
    """Verify all required bpy APIs exist in the running Blender."""
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped("Required bpy APIs", "bpy unavailable.")

    missing: List[str] = []
    present: List[str] = []

    for label, dotted_path in _REQUIRED_BPY_APIS:
        parts = dotted_path.split(".")
        obj: Any = bpy
        found = True
        for part in parts:
            try:
                obj = getattr(obj, part)
            except AttributeError:
                found = False
                break
        if found:
            present.append(label)
        else:
            missing.append(label)

    if missing:
        for api in missing:
            findings.append(finding_critical(
                "BPY_API_MISSING",
                f"Required API not found: {api}",
                detail="This API is used by Onixey V3 and its absence will cause errors.",
                fix=(
                    "This Blender build does not have this API. "
                    "Update Blender or add a compatibility wrapper in core/api_wrappers.py."
                ),
            ))
    else:
        findings.append(finding_ok(
            "BPY_APIS_OK",
            f"All {len(_REQUIRED_BPY_APIS)} required bpy APIs are present.",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = len(missing) == 0
    return CheckResult(
        check_name="Required bpy APIs",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"checked": len(_REQUIRED_BPY_APIS), "missing": missing},
    )


def _check_core_modules_importable(addon_module_name: str) -> CheckResult:
    """Verify core Onixey modules can be imported without raising."""
    start = time.perf_counter()
    findings: List[Finding] = []
    import importlib

    for mod_path, label in _CORE_MODULES:
        try:
            importlib.import_module(mod_path)
            findings.append(finding_ok(
                "MODULE_IMPORTABLE",
                f"{label} imports cleanly.",
            ))
        except ImportError as exc:
            findings.append(finding_critical(
                "MODULE_IMPORT_ERROR",
                f"{label} failed to import: {exc}",
                detail=traceback.format_exc(limit=4),
                fix="Check the module for syntax errors or missing dependencies.",
            ))
        except Exception as exc:
            findings.append(finding_critical(
                "MODULE_INIT_ERROR",
                f"{label} raised during import: {type(exc).__name__}: {exc}",
                detail=traceback.format_exc(limit=4),
                fix="The module raises on import — remove all side effects from module level.",
            ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Core Module Imports",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
    )


# ──────────────────────────────────────────────────────────────────────────────
# BLENDER COMPATIBILITY REPORT
# ──────────────────────────────────────────────────────────────────────────────

def run_blender_compatibility_report(
    addon_module_name: str = _ADDON_MODULE,
) -> ValidationReport:
    """
    Detailed feature-flag report — which Blender APIs are available.

    Reads the feature flags from core/feature_flags.py if the module is
    loaded. Otherwise, computes them independently based on bpy.app.version.

    Returns:
        ValidationReport with one CheckResult per API category.
    """
    context = _build_context()
    results = [
        _check_feature_flags(addon_module_name),
        _check_depsgraph_api(),
        _check_handler_api(),
        _check_undo_api(),
    ]
    return make_report("Onixey V3 — Blender Compatibility", results, **context)


def _check_feature_flags(addon_module_name: str) -> CheckResult:
    """Report the current state of all feature flags."""
    start = time.perf_counter()
    findings: List[Finding] = []

    flags: Optional[Dict[str, bool]] = None
    source = "computed"

    # Try to read from the live feature_flags module first
    mod_name = f"{addon_module_name}.core.feature_flags"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
        if hasattr(mod, "get_all_flags"):
            flags = mod.get_all_flags()
            source = "live (from registered module)"

    # Fall back to independent computation
    if not flags:
        try:
            import bpy
            v = bpy.app.version[:3]
            flags = _compute_flags_standalone(v)
            source = f"standalone (Blender {'.'.join(str(x) for x in v)})"
        except ImportError:
            return CheckResult.skipped("Feature Flags", "bpy unavailable.")

    enabled  = [k for k, v in flags.items() if v]
    disabled = [k for k, v in flags.items() if not v]

    findings.append(finding_info(
        "FLAGS_SOURCE",
        f"Feature flags source: {source}",
    ))

    for flag in enabled:
        findings.append(finding_ok(f"FLAG_{flag.upper()}", f"{flag}: available"))

    for flag in disabled:
        findings.append(finding_warning(
            f"FLAG_{flag.upper()}_MISSING",
            f"{flag}: NOT available in this Blender build",
            fix=(
                f"Check the fallback path in api_wrappers.py for {flag}. "
                "Calls that depend on this feature must use the degraded path."
            ),
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = len(disabled) == 0
    return CheckResult(
        check_name="Feature Flags",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"enabled": enabled, "disabled": disabled},
    )


def _check_depsgraph_api() -> CheckResult:
    """Verify depsgraph-specific APIs with a live probe."""
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped("Depsgraph API", "bpy unavailable.")

    # Probe: can we call evaluated_depsgraph_get on a context?
    # We use bpy.context, which is available even outside operators.
    ctx = bpy.context
    if ctx is None:
        findings.append(finding_info(
            "DEPSGRAPH_NO_CONTEXT",
            "bpy.context is None — cannot probe depsgraph outside active context.",
            detail="This is normal when running outside a UI context. No action needed.",
        ))
    else:
        has_method = hasattr(ctx, "evaluated_depsgraph_get")
        if has_method:
            findings.append(finding_ok(
                "DEPSGRAPH_METHOD_PRESENT",
                "context.evaluated_depsgraph_get() is available.",
            ))
        else:
            findings.append(finding_critical(
                "DEPSGRAPH_METHOD_MISSING",
                "context.evaluated_depsgraph_get() not found on bpy.context.",
                fix=(
                    "This is a hard requirement (AAA Rule 2). "
                    "Onixey cannot compute world-space positions without it."
                ),
            ))

    # Verify obj.evaluated_get exists on Object type
    has_eval_get = hasattr(bpy.types.Object, "evaluated_get")
    if has_eval_get:
        findings.append(finding_ok(
            "EVALUATED_GET_PRESENT",
            "bpy.types.Object.evaluated_get is available.",
        ))
    else:
        findings.append(finding_critical(
            "EVALUATED_GET_MISSING",
            "bpy.types.Object.evaluated_get not found.",
            fix="Update get_evaluated_object_safe() in api_wrappers.py with a fallback.",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Depsgraph API",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
    )


def _check_handler_api() -> CheckResult:
    """Verify handler lists exist and @persistent decorator works."""
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped("Handler API", "bpy unavailable.")

    required_lists = ["load_post", "load_pre", "undo_post", "redo_post",
                      "depsgraph_update_post", "frame_change_post"]

    for list_name in required_lists:
        hlist = getattr(bpy.app.handlers, list_name, None)
        if hlist is None:
            findings.append(finding_warning(
                f"HANDLER_LIST_MISSING_{list_name.upper()}",
                f"bpy.app.handlers.{list_name} does not exist.",
                fix=(
                    f"Guard registration of handlers on this list with "
                    f"hasattr(bpy.app.handlers, '{list_name}')."
                ),
            ))
        else:
            findings.append(finding_ok(
                f"HANDLER_LIST_{list_name.upper()}",
                f"bpy.app.handlers.{list_name} is present.",
            ))

    # Verify @persistent decorator exists
    if hasattr(bpy.app.handlers, "persistent"):
        findings.append(finding_ok(
            "HANDLER_PERSISTENT_DECORATOR",
            "bpy.app.handlers.persistent decorator is available.",
        ))
    else:
        findings.append(finding_critical(
            "HANDLER_PERSISTENT_MISSING",
            "bpy.app.handlers.persistent not found.",
            fix="The @persistent decorator is required for load_post handlers (AAA Rule 9).",
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Handler API",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
    )


def _check_undo_api() -> CheckResult:
    """Verify undo-related APIs used for cache invalidation."""
    start = time.perf_counter()
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped("Undo API", "bpy unavailable.")

    # undo_post handler (required for cache invalidation — AAA Rule 8)
    if hasattr(bpy.app.handlers, "undo_post"):
        findings.append(finding_ok(
            "UNDO_POST_HANDLER",
            "bpy.app.handlers.undo_post is available — cache can be invalidated on Ctrl+Z.",
        ))
    else:
        findings.append(finding_warning(
            "UNDO_POST_MISSING",
            "bpy.app.handlers.undo_post not available in this Blender build.",
            detail="Cache may serve stale data after Ctrl+Z (AAA Rule 8 violation risk).",
            fix=(
                "In session.py register(), skip undo_post registration if not available. "
                "Consider using depsgraph_update_post as a degraded fallback (max 0.5ms)."
            ),
        ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Undo API",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
    )


# ──────────────────────────────────────────────────────────────────────────────
# MODULE COMPATIBILITY REPORT
# ──────────────────────────────────────────────────────────────────────────────

def run_module_compatibility_report(
    addon_module_name: str = _ADDON_MODULE,
) -> ValidationReport:
    """
    Verify all loaded Onixey sub-modules declare compatible MODULE_COMPAT values.

    Per the AAA architecture (Section 3), each sub-module that exposes
    internal APIs should declare a MODULE_COMPAT integer. This check
    verifies:
        - The constant exists in each loaded module.
        - Its value meets MODULE_COMPAT_REQUIRED from core/version.py.

    Modules without MODULE_COMPAT are noted as informational (not required
    for all modules — only for those that expose cross-module APIs).

    Returns:
        ValidationReport with one CheckResult.
    """
    context = _build_context()
    results = [_check_all_module_compat(addon_module_name)]
    return make_report("Onixey V3 — Module Compatibility", results, **context)


def _check_all_module_compat(addon_module_name: str) -> CheckResult:
    """Scan all loaded Onixey modules for MODULE_COMPAT declarations."""
    start = time.perf_counter()
    findings: List[Finding] = []

    # Read the required compat level from core/version.py if available
    required_compat = _MODULE_COMPAT_REQUIRED
    mod_name = f"{addon_module_name}.core.version"
    if mod_name in sys.modules:
        version_mod = sys.modules[mod_name]
        required_compat = getattr(version_mod, "MODULE_COMPAT_REQUIRED", required_compat)

    addon_mods = {
        name: mod
        for name, mod in sys.modules.items()
        if name == addon_module_name or name.startswith(f"{addon_module_name}.")
    }

    if not addon_mods:
        return CheckResult.skipped(
            "Module Compatibility",
            f"No {addon_module_name}.* modules found in sys.modules — addon not registered.",
        )

    findings.append(finding_info(
        "MODULES_LOADED",
        f"{len(addon_mods)} {addon_module_name}.* module(s) in sys.modules",
    ))

    for mod_path, mod in sorted(addon_mods.items()):
        compat = getattr(mod, "MODULE_COMPAT", None)

        if compat is None:
            # Only warn for non-__init__ modules (packages don't need it)
            if not mod_path.endswith("__init__"):
                findings.append(finding_info(
                    "MODULE_NO_COMPAT",
                    f"{mod_path}: MODULE_COMPAT not declared.",
                    detail="Optional for leaf modules without cross-module API contracts.",
                ))
            continue

        if not isinstance(compat, int):
            findings.append(finding_warning(
                "MODULE_COMPAT_INVALID_TYPE",
                f"{mod_path}: MODULE_COMPAT={compat!r} is not an int.",
                fix="MODULE_COMPAT must be an integer (e.g. MODULE_COMPAT = 3).",
            ))
            continue

        if compat < required_compat:
            findings.append(finding_critical(
                "MODULE_COMPAT_TOO_LOW",
                f"{mod_path}: MODULE_COMPAT={compat} < required {required_compat}.",
                detail=(
                    "This module declares an API contract below the required level. "
                    "Loading may cause silent corruption or AttributeError at runtime."
                ),
                fix=(
                    f"Update MODULE_COMPAT to {required_compat} in {mod_path} "
                    f"and ensure the module's public API is up to date."
                ),
            ))
        else:
            findings.append(finding_ok(
                "MODULE_COMPAT_OK",
                f"{mod_path}: MODULE_COMPAT={compat} ≥ required {required_compat}.",
            ))

    duration_ms = (time.perf_counter() - start) * 1000
    passed = not any(f.severity == Severity.CRITICAL for f in findings)
    return CheckResult(
        check_name="Module Compatibility",
        passed=passed,
        findings=findings,
        duration_ms=duration_ms,
        metadata={"modules_scanned": len(addon_mods), "required_compat": required_compat},
    )


# ──────────────────────────────────────────────────────────────────────────────
# RUNTIME DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def run_runtime_diagnostics(
    addon_module_name: str = _ADDON_MODULE,
) -> ValidationReport:
    """
    Full runtime state snapshot — no pass/fail, purely informational.

    Collects everything useful for a bug report or QA session:
    Blender version, Python version, sys.modules list, handler summary,
    feature flags, cache stats, session state, compat validation status.

    Returns:
        ValidationReport — always "passed" (diagnostic, not assertive).
    """
    context = _build_context()
    results = [
        _diag_environment(),
        _diag_handlers(addon_module_name),
        _diag_cache(addon_module_name),
        _diag_session(addon_module_name),
        _diag_sys_modules(addon_module_name),
    ]
    return make_report("Onixey V3 — Runtime Diagnostics", results, **context)


def _diag_environment() -> CheckResult:
    """Snapshot: Blender version, Python version, platform."""
    import platform
    findings: List[Finding] = []

    py_vi = sys.version_info
    py_str = f"{py_vi.major}.{py_vi.minor}.{py_vi.micro}"

    try:
        import bpy
        bl_str = ".".join(str(x) for x in bpy.app.version[:3])
        bl_hash = getattr(bpy.app, "build_hash", b"?")
        if isinstance(bl_hash, bytes):
            bl_hash = bl_hash.decode("utf-8", errors="replace")
    except ImportError:
        bl_str = "unavailable"
        bl_hash = "unavailable"

    findings += [
        finding_info("BLENDER_VERSION",  f"Blender {bl_str} (hash: {bl_hash})"),
        finding_info("PYTHON_VERSION",   f"Python {py_str}"),
        finding_info("PLATFORM",         platform.platform()),
        finding_info("SYS_EXEC",         sys.executable),
    ]

    return CheckResult(
        check_name="Environment",
        passed=True,
        findings=findings,
        metadata={"blender": bl_str, "python": py_str},
    )


def _diag_handlers(addon_module_name: str) -> CheckResult:
    """Snapshot: all handlers currently registered by Onixey."""
    findings: List[Finding] = []

    try:
        import bpy
    except ImportError:
        return CheckResult.skipped("Handler State", "bpy unavailable.")

    total = 0
    for list_name in (
        "load_post", "load_pre", "undo_post", "redo_post",
        "depsgraph_update_post", "frame_change_post", "save_pre", "save_post",
    ):
        hlist = getattr(bpy.app.handlers, list_name, None)
        if hlist is None:
            continue

        all_fns = list(hlist)
        onixey_fns = [
            fn for fn in all_fns
            if getattr(fn, "__module__", "").startswith(addon_module_name)
        ]

        if onixey_fns:
            total += len(onixey_fns)
            for fn in onixey_fns:
                fn_name = getattr(fn, "__name__", repr(fn))
                fn_mod  = getattr(fn, "__module__", "?")
                is_pers = getattr(fn, "_bpy_persistent", False)
                findings.append(finding_info(
                    f"HANDLER_{list_name.upper()}",
                    f"handlers.{list_name}: {fn_name}",
                    detail=f"module={fn_mod}  persistent={is_pers}",
                ))

    if total == 0:
        findings.append(finding_info(
            "HANDLERS_NONE",
            f"No {addon_module_name}.* handlers currently registered.",
        ))

    return CheckResult(
        check_name="Handler State",
        passed=True,
        findings=findings,
        metadata={"total_onixey_handlers": total},
    )


def _diag_cache(addon_module_name: str) -> CheckResult:
    """Snapshot: runtime cache statistics."""
    findings: List[Finding] = []
    mod_name = f"{addon_module_name}.runtime.cache"

    if mod_name not in sys.modules:
        findings.append(finding_info(
            "CACHE_NOT_LOADED",
            "runtime.cache not in sys.modules — not registered yet.",
        ))
    else:
        cache_mod = sys.modules[mod_name]
        if hasattr(cache_mod, "get_stats"):
            stats = cache_mod.get_stats()
            for k, v in stats.items():
                findings.append(finding_info(f"CACHE_{k.upper()}", f"{k}: {v}"))
        else:
            findings.append(finding_info(
                "CACHE_NO_STATS",
                "runtime.cache loaded but get_stats() not found.",
            ))

    return CheckResult(check_name="Cache State", passed=True, findings=findings)


def _diag_session(addon_module_name: str) -> CheckResult:
    """Snapshot: session state content."""
    findings: List[Finding] = []
    mod_name = f"{addon_module_name}.runtime.session"

    if mod_name not in sys.modules:
        findings.append(finding_info(
            "SESSION_NOT_LOADED",
            "runtime.session not in sys.modules — not registered yet.",
        ))
    else:
        session_mod = sys.modules[mod_name]
        if hasattr(session_mod, "get_state"):
            try:
                state = session_mod.get_state()
                if hasattr(state, "to_dict"):
                    d = state.to_dict()
                    for k, v in d.items():
                        findings.append(finding_info(
                            f"SESSION_{k.upper()}", f"{k}: {v!r}"
                        ))
                else:
                    findings.append(finding_info("SESSION_STATE", repr(state)))
            except Exception as exc:
                findings.append(finding_warning(
                    "SESSION_READ_ERROR",
                    f"get_state() raised: {exc}",
                ))
        else:
            findings.append(finding_info(
                "SESSION_NO_GET_STATE",
                "runtime.session loaded but get_state() not found.",
            ))

    return CheckResult(check_name="Session State", passed=True, findings=findings)


def _diag_sys_modules(addon_module_name: str) -> CheckResult:
    """Snapshot: all Onixey modules in sys.modules."""
    findings: List[Finding] = []

    addon_mods = sorted(
        k for k in sys.modules
        if k == addon_module_name or k.startswith(f"{addon_module_name}.")
    )

    findings.append(finding_info(
        "SYS_MODULES_COUNT",
        f"{len(addon_mods)} {addon_module_name}.* module(s) in sys.modules",
    ))
    for mod_path in addon_mods:
        findings.append(finding_info("SYS_MODULE", mod_path))

    return CheckResult(
        check_name="sys.modules State",
        passed=True,
        findings=findings,
        metadata={"count": len(addon_mods)},
    )


# ──────────────────────────────────────────────────────────────────────────────
# PRIVATE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _build_context() -> Dict[str, Any]:
    """Build the context block for ValidationReport."""
    ctx: Dict[str, Any] = {}
    try:
        import bpy
        ctx["blender_version"] = ".".join(str(x) for x in bpy.app.version[:3])
    except ImportError:
        ctx["blender_version"] = "unavailable"

    vi = sys.version_info
    ctx["python_version"] = f"{vi.major}.{vi.minor}.{vi.micro}"
    ctx["blender_min_required"] = ".".join(str(x) for x in _BLENDER_MIN)

    # Read addon version from core/version.py if available
    mod_name = f"{_ADDON_MODULE}.core.version"
    if mod_name in sys.modules:
        ver_mod = sys.modules[mod_name]
        v = getattr(ver_mod, "ONIXEY_API_VERSION", None)
        if v:
            ctx["addon_version"] = ".".join(str(x) for x in v)

    return ctx


def _compute_flags_standalone(blender_version: Tuple[int, int, int]) -> Dict[str, bool]:
    """
    Compute feature flags without importing core/feature_flags.py.
    Used when the addon is not yet registered (standalone mode).
    Mirrors the logic in core/feature_flags.py._compute().
    """
    v = blender_version

    def at_least(minimum: Tuple[int, int, int]) -> bool:
        return v >= minimum

    return {
        "depsgraph_object_instances":  at_least((2, 80, 0)),
        "evaluated_get":               at_least((2, 80, 0)),
        "msgbus":                      at_least((2, 80, 0)),
        "gpu_shader":                  at_least((2, 83, 0)),
        "handler_depsgraph_arg":       at_least((2, 80, 0)),
        "undo_post_handler":           at_least((2, 81, 0)),
        "pose_bone_matrix_channel":    at_least((3, 0, 0)),
        "nla_track_mute":              at_least((2, 80, 0)),
        "undo_grouped":                at_least((3, 0, 0)),
    }
