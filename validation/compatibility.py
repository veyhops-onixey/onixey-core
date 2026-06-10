"""
onixey3/validation/compatibility.py

Compatibility Validator — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Verify that the current runtime environment (Blender version, Onixey version,
scene data version, build type) meets all requirements for safe addon operation.

Returns a structured result dict — never raises on incompatibility. The caller
decides whether to abort, warn, or degrade gracefully based on the result.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT import bpy at module level.
    - Does NOT modify scene data, bpy.types, or handlers.
    - Does NOT import runtime/, cache, session, state, or UI modules.
    - Does NOT perform animation analysis or FCurve access.
    - Does NOT write to disk.
    - Does NOT produce side effects on import.

ARCHITECTURE POSITION
─────────────────────
    validation/compatibility.py
        imports from: core/version.py (optional, lazy)
        imports from: stdlib only at module level
        called by:   core/compat.py, __init__.py register(), migration/

RESULT SCHEMA
─────────────
    {
        "compatible": bool,      # True only if zero errors
        "warnings":  [str, ...], # Non-blocking issues
        "errors":    [str, ...], # Blocking issues — addon should not load
        "details":   {           # Machine-readable breakdown
            "blender":  {...},
            "onixey":   {...},
            "scene":    {...},
            "build":    {...},
        }
    }

CHECKS PERFORMED
─────────────────
    1. Blender minimum version    — below minimum → error
    2. Blender maximum version    — above tested ceiling → warning
    3. ONIXEY_VERSION             — present and well-formed → validated
    4. ONIXEY_API_VERSION         — integer, matches expectations
    5. Runtime version            — aligns with core version constants
    6. Scene data version         — detects stale .blend needing migration
    7. Build type                 — RELEASE vs DEVELOPMENT vs UNKNOWN

CHANGELOG
─────────
    3.1.0 — Initial implementation.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ── Version tuple type alias ──────────────────────────────────────────────────

VersionTuple = Tuple[int, ...]

# ── Result type alias ─────────────────────────────────────────────────────────

CompatibilityResult = Dict[str, Any]

# ── Build type constants ──────────────────────────────────────────────────────

BUILD_RELEASE:     str = "RELEASE"
BUILD_DEVELOPMENT: str = "DEVELOPMENT"
BUILD_UNKNOWN:     str = "UNKNOWN"

_VALID_BUILD_TYPES: frozenset = frozenset({
    BUILD_RELEASE, BUILD_DEVELOPMENT, BUILD_UNKNOWN,
})


# ══════════════════════════════════════════════════════════════════════════════
# TYPED ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class CompatibilityError(Exception):
    """
    Base for compatibility check programming errors.

    NOT raised for incompatibility results — those go into the result dict.
    Raised only when check() receives structurally invalid arguments
    (e.g., a version tuple with non-integer elements).
    """


class InvalidVersionError(CompatibilityError):
    """Raised when a version argument is not a valid tuple of integers."""
    def __init__(self, name: str, value: Any) -> None:
        super().__init__(
            f"Invalid version for '{name}': {value!r}. "
            "Expected a tuple of integers, e.g. (4, 2, 0)."
        )


class InvalidArgumentError(CompatibilityError):
    """Raised when a required argument has an unexpected type or value."""
    def __init__(self, name: str, value: Any, expected: str) -> None:
        super().__init__(
            f"Invalid argument '{name}': {value!r}. Expected: {expected}."
        )


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _empty_result() -> CompatibilityResult:
    """Return a clean, mutable result dict."""
    return {
        "compatible": True,
        "warnings":   [],
        "errors":     [],
        "details":    {
            "blender": {},
            "onixey":  {},
            "scene":   {},
            "build":   {},
        },
    }


def _err(result: CompatibilityResult, message: str) -> None:
    """Append an error and mark result as incompatible."""
    result["errors"].append(message)
    result["compatible"] = False
    _log.error("[Onixey|Compat] ERROR: %s", message)


def _warn(result: CompatibilityResult, message: str) -> None:
    """Append a warning. Does not change compatible flag."""
    result["warnings"].append(message)
    _log.warning("[Onixey|Compat] WARNING: %s", message)


def _validate_version_tuple(name: str, value: Any) -> VersionTuple:
    """
    Validate and normalize a version argument.

    Accepts:
        tuple or list of ints, e.g. (4, 2, 0), [4, 2]

    Returns:
        Normalized tuple of ints.

    Raises:
        InvalidVersionError on invalid input.
    """
    if not isinstance(value, (tuple, list)):
        raise InvalidVersionError(name, value)
    normalized = tuple(value)
    if not normalized:
        raise InvalidVersionError(name, value)
    if not all(isinstance(x, int) for x in normalized):
        raise InvalidVersionError(name, value)
    return normalized


def _version_str(v: VersionTuple) -> str:
    """Format a version tuple as "X.Y.Z"."""
    return ".".join(str(x) for x in v)


def _version_gte(a: VersionTuple, b: VersionTuple) -> bool:
    """Return True if a >= b (padded with zeros for comparison)."""
    length = max(len(a), len(b))
    a_pad = a + (0,) * (length - len(a))
    b_pad = b + (0,) * (length - len(b))
    return a_pad >= b_pad


def _version_lte(a: VersionTuple, b: VersionTuple) -> bool:
    """Return True if a <= b."""
    return _version_gte(b, a)


def _load_version_constants() -> Optional[Any]:
    """
    Lazily import core/version.py without triggering other core imports.

    Returns the version module, or None if unavailable.
    Never raises — the caller handles a None return gracefully.
    """
    try:
        import importlib
        mod = importlib.import_module("onixey3.core.version")
        return mod
    except ImportError as exc:
        _log.debug(
            "[Onixey|Compat] core.version not importable: %s. "
            "Version constants will not be checked.",
            exc,
        )
        return None
    except Exception as exc:
        _log.error(
            "[Onixey|Compat] Unexpected error importing core.version: %s", exc,
        )
        return None


# ══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CHECKS
# Each check receives the result dict and mutates it in-place.
# All checks are independently callable for targeted validation.
# ══════════════════════════════════════════════════════════════════════════════

def check_blender_version(
    result:          CompatibilityResult,
    blender_version: VersionTuple,
    minimum:         VersionTuple,
    maximum:         Optional[VersionTuple] = None,
) -> None:
    """
    Validate the Blender version against minimum and maximum bounds.

    Rules:
        - Below minimum → ERROR (addon cannot function).
        - Above maximum (if given) → WARNING (untested, may work).
        - Within range → OK.

    Args:
        result:          The result dict to mutate.
        blender_version: Detected Blender version, e.g. (4, 2, 1).
        minimum:         Required minimum, e.g. (4, 2, 0).
        maximum:         Optional tested ceiling, e.g. (5, 0, 0).
    """
    bv_str  = _version_str(blender_version)
    min_str = _version_str(minimum)

    result["details"]["blender"]["detected"]  = bv_str
    result["details"]["blender"]["minimum"]   = min_str
    result["details"]["blender"]["maximum"]   = _version_str(maximum) if maximum else "open"

    if not _version_gte(blender_version, minimum):
        _err(
            result,
            f"Blender {bv_str} is below the required minimum {min_str}. "
            f"Upgrade Blender to {min_str} or later.",
        )
        result["details"]["blender"]["status"] = "INCOMPATIBLE"
        return

    if maximum is not None and not _version_lte(blender_version, maximum):
        _warn(
            result,
            f"Blender {bv_str} is above the tested ceiling {_version_str(maximum)}. "
            "Onixey V3 has not been validated on this version. "
            "Functionality may be degraded.",
        )
        result["details"]["blender"]["status"] = "UNTESTED"
        return

    result["details"]["blender"]["status"] = "OK"
    _log.debug("[Onixey|Compat] Blender version %s: OK", bv_str)


def check_onixey_version(
    result:             CompatibilityResult,
    onixey_version:     VersionTuple,
    onixey_api_version: int,
    expected_api:       int,
) -> None:
    """
    Validate ONIXEY_VERSION and ONIXEY_API_VERSION.

    Rules:
        - ONIXEY_VERSION must be a well-formed 3-tuple of ints.
        - ONIXEY_API_VERSION must be a positive integer.
        - If onixey_api_version < expected_api → ERROR (stale code vs new scene data).
        - If onixey_api_version > expected_api → WARNING (newer code vs old scene data).
        - Match → OK.

    Args:
        result:             Result dict to mutate.
        onixey_version:     Full version tuple, e.g. (3, 1, 0).
        onixey_api_version: Integer API version from version.py, e.g. 3.
        expected_api:       The API version this validator expects.
    """
    ov_str = _version_str(onixey_version)

    result["details"]["onixey"]["version"]          = ov_str
    result["details"]["onixey"]["api_version"]      = onixey_api_version
    result["details"]["onixey"]["expected_api"]     = expected_api

    # Well-formed version check
    if len(onixey_version) < 2:
        _err(
            result,
            f"ONIXEY_VERSION {onixey_version!r} is malformed. "
            "Expected at least (major, minor).",
        )
        result["details"]["onixey"]["status"] = "MALFORMED"
        return

    # API version type check
    if not isinstance(onixey_api_version, int) or onixey_api_version < 1:
        _err(
            result,
            f"ONIXEY_API_VERSION {onixey_api_version!r} is invalid. "
            "Expected a positive integer.",
        )
        result["details"]["onixey"]["status"] = "INVALID_API"
        return

    # API version alignment
    if onixey_api_version < expected_api:
        _err(
            result,
            f"ONIXEY_API_VERSION {onixey_api_version} is below expected {expected_api}. "
            "The installed addon is outdated. Reinstall Onixey.",
        )
        result["details"]["onixey"]["status"] = "OUTDATED"
        return

    if onixey_api_version > expected_api:
        _warn(
            result,
            f"ONIXEY_API_VERSION {onixey_api_version} is above expected {expected_api}. "
            "This validator may be outdated relative to the installed addon.",
        )
        result["details"]["onixey"]["status"] = "NEWER_THAN_EXPECTED"
        return

    result["details"]["onixey"]["status"] = "OK"
    _log.debug("[Onixey|Compat] Onixey version %s API=%d: OK", ov_str, onixey_api_version)


def check_runtime_version(
    result:           CompatibilityResult,
    runtime_version:  VersionTuple,
    core_version:     VersionTuple,
) -> None:
    """
    Verify that the runtime version aligns with the core version.

    Rules:
        - Major versions must match exactly (breaking change boundary).
        - Minor runtime < minor core → WARNING (runtime may lack new features).
        - Minor runtime > minor core → WARNING (runtime newer than core, unusual).
        - Patch differences → INFO only (no warning).

    Args:
        result:          Result dict to mutate.
        runtime_version: Version of the runtime package, e.g. (3, 1, 0).
        core_version:    Version of the core package, e.g. (3, 1, 0).
    """
    rv_str = _version_str(runtime_version)
    cv_str = _version_str(core_version)

    result["details"]["onixey"]["runtime_version"] = rv_str
    result["details"]["onixey"]["core_version"]    = cv_str

    rv_major = runtime_version[0] if runtime_version else 0
    cv_major = core_version[0]    if core_version    else 0
    rv_minor = runtime_version[1] if len(runtime_version) > 1 else 0
    cv_minor = core_version[1]    if len(core_version) > 1    else 0

    if rv_major != cv_major:
        _err(
            result,
            f"Runtime major version {rv_major} does not match "
            f"core major version {cv_major}. "
            "Breaking change — runtime and core must share the same major version.",
        )
        result["details"]["onixey"]["runtime_status"] = "MAJOR_MISMATCH"
        return

    if rv_minor < cv_minor:
        _warn(
            result,
            f"Runtime version {rv_str} is behind core version {cv_str}. "
            "Some core features may be unavailable to the runtime.",
        )
        result["details"]["onixey"]["runtime_status"] = "RUNTIME_BEHIND"
        return

    if rv_minor > cv_minor:
        _warn(
            result,
            f"Runtime version {rv_str} is ahead of core version {cv_str}. "
            "Unusual configuration — verify the installation.",
        )
        result["details"]["onixey"]["runtime_status"] = "RUNTIME_AHEAD"
        return

    result["details"]["onixey"]["runtime_status"] = "OK"
    _log.debug("[Onixey|Compat] Runtime version %s vs core %s: OK", rv_str, cv_str)


def check_scene_version(
    result:              CompatibilityResult,
    scene_data_version:  int,
    current_data_version:int,
    minimum_supported:   int = 1,
) -> None:
    """
    Verify that the scene's embedded Onixey data version is compatible.

    Rules:
        - scene_data_version < minimum_supported → ERROR (too old, migration impossible).
        - scene_data_version < current_data_version → WARNING (needs migration).
        - scene_data_version == current_data_version → OK.
        - scene_data_version > current_data_version → WARNING (future data, addon outdated).
        - scene_data_version == 0 → fresh scene, no existing data, OK.

    Args:
        result:               Result dict to mutate.
        scene_data_version:   Version stored in the .blend scene (0 if none).
        current_data_version: Version this addon writes to new scenes.
        minimum_supported:    Oldest scene version this addon can still migrate.
    """
    result["details"]["scene"]["scene_data_version"]   = scene_data_version
    result["details"]["scene"]["current_data_version"] = current_data_version
    result["details"]["scene"]["minimum_supported"]    = minimum_supported

    # Fresh scene — no existing data
    if scene_data_version == 0:
        result["details"]["scene"]["status"] = "FRESH"
        _log.debug("[Onixey|Compat] Scene: no existing Onixey data (fresh scene). OK.")
        return

    # Below minimum — too old to migrate
    if scene_data_version < minimum_supported:
        _err(
            result,
            f"Scene data version {scene_data_version} is below the minimum "
            f"supported version {minimum_supported}. "
            "Automatic migration is not possible. "
            "Open the scene with an older Onixey version to upgrade first.",
        )
        result["details"]["scene"]["status"] = "TOO_OLD"
        return

    # Needs migration
    if scene_data_version < current_data_version:
        _warn(
            result,
            f"Scene data version {scene_data_version} is below current "
            f"version {current_data_version}. "
            "Migration will run automatically on load.",
        )
        result["details"]["scene"]["status"] = "NEEDS_MIGRATION"
        return

    # Future data — addon is outdated
    if scene_data_version > current_data_version:
        _warn(
            result,
            f"Scene data version {scene_data_version} is above the current "
            f"version {current_data_version}. "
            "This scene was saved with a newer Onixey. Some data may be ignored.",
        )
        result["details"]["scene"]["status"] = "FUTURE_DATA"
        return

    result["details"]["scene"]["status"] = "OK"
    _log.debug(
        "[Onixey|Compat] Scene data version %d: OK", scene_data_version,
    )


def check_build_type(
    result:     CompatibilityResult,
    build_type: str,
) -> None:
    """
    Validate the build type tag.

    Rules:
        - RELEASE     → OK (production).
        - DEVELOPMENT → WARNING (not for end users).
        - UNKNOWN     → WARNING (unrecognized build).
        - Any other   → WARNING (non-standard).

    Args:
        result:     Result dict to mutate.
        build_type: One of BUILD_RELEASE, BUILD_DEVELOPMENT, BUILD_UNKNOWN,
                    or any custom string.
    """
    if not isinstance(build_type, str) or not build_type:
        _warn(result, f"Build type is empty or non-string: {build_type!r}.")
        result["details"]["build"]["type"]   = repr(build_type)
        result["details"]["build"]["status"] = "INVALID"
        return

    result["details"]["build"]["type"] = build_type

    if build_type == BUILD_RELEASE:
        result["details"]["build"]["status"] = "OK"
        _log.debug("[Onixey|Compat] Build type RELEASE: OK")
        return

    if build_type == BUILD_DEVELOPMENT:
        _warn(
            result,
            "Build type is DEVELOPMENT. "
            "This build is not intended for production use.",
        )
        result["details"]["build"]["status"] = "DEVELOPMENT"
        return

    if build_type == BUILD_UNKNOWN:
        _warn(
            result,
            "Build type is UNKNOWN. "
            "The build provenance cannot be verified.",
        )
        result["details"]["build"]["status"] = "UNKNOWN"
        return

    _warn(
        result,
        f"Unrecognized build type: '{build_type}'. "
        f"Expected one of: {sorted(_VALID_BUILD_TYPES)}.",
    )
    result["details"]["build"]["status"] = "UNRECOGNIZED"


def _read_attr(module: Optional[Any], attr: str, default: Any) -> Any:
    """
    Safely read an attribute from a module, returning default if unavailable.

    Args:
        module:  The imported module, or None if unavailable.
        attr:    Attribute name to read.
        default: Value to return if module is None or attr is missing.

    Returns:
        The attribute value or default.
    """
    if module is None:
        return default
    return getattr(module, attr, default)


# ══════════════════════════════════════════════════════════════════════════════
# PRIMARY ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def check(
    blender_version:      VersionTuple,
    onixey_version:       Optional[VersionTuple]  = None,
    onixey_api_version:   Optional[int]           = None,
    runtime_version:      Optional[VersionTuple]  = None,
    scene_data_version:   int                     = 0,
    build_type:           str                     = BUILD_UNKNOWN,
    blender_minimum:      Optional[VersionTuple]  = None,
    blender_maximum:      Optional[VersionTuple]  = None,
) -> CompatibilityResult:
    """
    Run all compatibility checks and return a structured result dict.

    This is the primary entry point. It is safe to call at any point in the
    addon lifecycle — it never raises on incompatibility, always returns a
    result dict regardless of what it finds.

    VERSION CONSTANTS AUTO-LOAD
    ────────────────────────────
    If onixey_version, onixey_api_version, or blender_minimum are not
    provided, they are read from core/version.py automatically (lazy import).
    If core/version.py is unavailable, the corresponding checks are skipped
    with a WARNING in the result.

    Args:
        blender_version:    Current Blender version, e.g. bpy.app.version.
                            Required — must always be passed.

        onixey_version:     Full Onixey version tuple, e.g. (3, 1, 0).
                            Optional — auto-read from core.version if None.

        onixey_api_version: Integer Onixey API version, e.g. 3.
                            Optional — auto-read from core.version if None.

        runtime_version:    Version of the runtime package, e.g. (3, 1, 0).
                            Optional — skips check if None.

        scene_data_version: Integer version stored in the .blend scene.
                            0 = fresh scene (no existing data). Default: 0.

        build_type:         Build provenance tag. Default: BUILD_UNKNOWN.

        blender_minimum:    Minimum required Blender version.
                            Optional — auto-read from core.version if None.

        blender_maximum:    Tested Blender ceiling. Optional.

    Returns:
        CompatibilityResult dict:
        {
            "compatible": bool,
            "warnings":   [str, ...],
            "errors":     [str, ...],
            "details": {
                "blender": {...},
                "onixey":  {...},
                "scene":   {...},
                "build":   {...},
            }
        }

    Raises:
        InvalidVersionError:   if blender_version is not a valid tuple.
        InvalidArgumentError:  if blender_version is missing or None.
    """
    # ── Argument validation ───────────────────────────────────────────────────
    if blender_version is None:
        raise InvalidArgumentError(
            "blender_version", None,
            "a tuple of integers, e.g. (4, 2, 0). This argument is required.",
        )
    blender_version = _validate_version_tuple("blender_version", blender_version)

    result = _empty_result()

    # ── Auto-load version constants if not provided ───────────────────────────
    version_mod = None
    if any(arg is None for arg in (onixey_version, onixey_api_version, blender_minimum)):
        version_mod = _load_version_constants()
        if version_mod is None:
            _warn(
                result,
                "core.version could not be imported. "
                "Some version checks will be skipped.",
            )

    if blender_minimum is None:
        blender_minimum = _read_attr(version_mod, "BLENDER_VERSION_MIN", (4, 2, 0))

    if onixey_version is None:
        raw = _read_attr(version_mod, "ONIXEY_VERSION", (3, 0, 0))
        onixey_version = _validate_version_tuple("ONIXEY_VERSION", raw)

    if onixey_api_version is None:
        onixey_api_version = _read_attr(version_mod, "ONIXEY_API_VERSION", 3)

    current_scene_version = _read_attr(version_mod, "SCENE_DATA_VERSION", 1)
    # SCENE_DATA_VERSION_MIN was added after the initial version.py release.
    # Fall back to SCENE_DATA_VERSION when the constant is absent so that
    # older version.py files don't generate a confusing default-value mismatch.
    minimum_scene_version = _read_attr(
        version_mod,
        "SCENE_DATA_VERSION_MIN",
        current_scene_version,   # fallback: use current as the floor
    )
    core_version          = onixey_version  # core and addon share the same version

    expected_api: int = onixey_api_version  # we expect the installed version to match

    # ── Run checks ────────────────────────────────────────────────────────────

    # 1. Blender version
    try:
        blender_min_v = _validate_version_tuple("blender_minimum", blender_minimum)
        blender_max_v = (
            _validate_version_tuple("blender_maximum", blender_maximum)
            if blender_maximum is not None else None
        )
        check_blender_version(result, blender_version, blender_min_v, blender_max_v)
    except CompatibilityError as exc:
        _err(result, f"Blender version check failed: {exc}")

    # 2. Onixey version + API version
    try:
        ov = _validate_version_tuple("onixey_version", onixey_version)
        if not isinstance(onixey_api_version, int):
            _err(result, f"ONIXEY_API_VERSION must be int, got {type(onixey_api_version).__name__}.")
        else:
            check_onixey_version(result, ov, onixey_api_version, expected_api)
    except CompatibilityError as exc:
        _err(result, f"Onixey version check failed: {exc}")

    # 3. Runtime version (optional — skip if not provided)
    if runtime_version is not None:
        try:
            rv = _validate_version_tuple("runtime_version", runtime_version)
            cv = _validate_version_tuple("core_version",    core_version)
            check_runtime_version(result, rv, cv)
        except CompatibilityError as exc:
            _err(result, f"Runtime version check failed: {exc}")
    else:
        result["details"]["onixey"]["runtime_version"] = "not_provided"
        result["details"]["onixey"]["runtime_status"]  = "SKIPPED"
        _log.debug("[Onixey|Compat] Runtime version check skipped (not provided).")

    # 4. Scene data version
    try:
        if not isinstance(scene_data_version, int):
            _err(result, f"scene_data_version must be int, got {type(scene_data_version).__name__}.")
        else:
            check_scene_version(
                result,
                scene_data_version,
                current_scene_version,
                minimum_scene_version,
            )
    except Exception as exc:
        _err(result, f"Scene version check failed unexpectedly: {exc}")

    # 5. Build type
    try:
        check_build_type(result, build_type)
    except Exception as exc:
        _warn(result, f"Build type check failed unexpectedly: {exc}")

    # ── Summary log ───────────────────────────────────────────────────────────
    if result["compatible"]:
        _log.info(
            "[Onixey|Compat] Compatibility check PASSED. "
            "Warnings: %d.", len(result["warnings"]),
        )
    else:
        _log.error(
            "[Onixey|Compat] Compatibility check FAILED. "
            "Errors: %d. Warnings: %d.",
            len(result["errors"]), len(result["warnings"]),
        )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE ENTRY POINT  (bpy-aware, for use from register())
# ══════════════════════════════════════════════════════════════════════════════

def check_from_blender(
    scene_data_version: int  = 0,
    build_type:         str  = BUILD_UNKNOWN,
    blender_maximum:    Optional[VersionTuple] = None,
) -> CompatibilityResult:
    """
    Run all compatibility checks using bpy.app.version as the Blender version.

    Convenience wrapper for use inside Blender (register(), load_post handler).
    All version constants are auto-loaded from core/version.py.

    Args:
        scene_data_version: Integer from scene custom properties. Default: 0.
        build_type:         Build tag. Default: BUILD_UNKNOWN.
        blender_maximum:    Optional tested ceiling version.

    Returns:
        CompatibilityResult dict.

    Raises:
        ImportError if bpy is not available (called outside Blender).
    """
    import bpy  # noqa: PLC0415 — intentionally deferred
    blender_version: VersionTuple = tuple(bpy.app.version[:3])

    return check(
        blender_version    = blender_version,
        scene_data_version = scene_data_version,
        build_type         = build_type,
        blender_maximum    = blender_maximum,
    )
