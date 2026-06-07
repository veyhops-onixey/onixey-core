"""
onixey3/core/compat.py

Compatibility Layer — Onixey V3.

SINGLE RESPONSIBILITY
─────────────────────
This module owns exactly one concern: determining whether the current
Blender environment is compatible with Onixey V3, and exposing that
determination to the rest of the codebase through a stable public API.

It is the ONLY module allowed to call bpy.app.version.
All other modules must use the supports_*() helpers re-exported here.
This means: when Blender 5.x changes an API, only this file and
feature_flags.py need updating. The rest of the addon remains untouched.

WHAT THIS FILE DOES
────────────────────
  1. validate_environment()     — hard/soft Blender version checks +
                                  Python version check +
                                  feature flag population.
  2. reset_validation_state()   — called by unregister() so the next
                                  register() re-validates from scratch.
  3. register() / unregister()  — thin hooks for __init__.py module system.
  4. Re-exports                 — every public name from the sub-modules
                                  (feature_flags, api_wrappers, registration)
                                  so callers have a single stable import path.

WHAT THIS FILE DOES NOT DO
───────────────────────────
  - It does not contain feature detection logic   → feature_flags.py
  - It does not contain bpy API wrappers          → api_wrappers.py
  - It does not contain class registration logic  → registration.py
  - It does not import bpy at module level
  - It does not have side effects on import

IMPORT STABILITY CONTRACT
──────────────────────────
Every name listed in __all__ is guaranteed stable across Onixey V3 minor
versions. Removing or renaming a name here requires a major version bump.

RELOAD SAFETY (F8 / disable-enable)
──────────────────────────────────────
_ENVIRONMENT_VALIDATED is a module-level bool. On reload Blender re-executes
the module, resetting it to False. This is intentional: the next register()
call re-validates the environment against the (possibly new) Blender version.
reset_validation_state() also resets feature_flags._registry explicitly,
ensuring no stale flags survive a reload.
"""

from __future__ import annotations
from typing import Any, List, Optional, Tuple, Type
import logging

from .version import (
    BLENDER_MIN,
    BLENDER_SOFT_MAX,
    PYTHON_MIN,
    ADDON_DISPLAY_NAME,
    is_version_at_least,
    is_version_above,
    is_python_supported,
    get_python_version,
    version_string,
    build_info_string,
    VersionTuple,
)

# ── Sub-module imports (no bpy, no side effects) ──────────────────────────────

from .feature_flags import (
    # Internal hooks used only by validate_environment / reset_validation_state
    _populate               as _ff_populate,
    _reset                  as _ff_reset,
    _as_dict                as _ff_as_dict,
    # Public supports_*() API — re-exported for callers that import from compat
    supports_evaluated_get,
    supports_msgbus,
    supports_gpu_shader,
    supports_undo_post_handler,
    supports_redo_post_handler,
    supports_undo_grouped,
    supports_depsgraph_object_instances,
    supports_pose_bone_matrix_channel,
    supports_handler_depsgraph_arg,
    supports_nla_track_mute,
)

from .api_wrappers import (
    get_evaluated_object_safe,
    get_depsgraph_safe,
    get_active_armature_safe,
    get_active_action_safe,
    get_pose_bone_safe,
    safe_register_property,
    safe_unregister_property,
    safe_handler_append,
    safe_handler_remove,
)

from .registration import (
    safe_register_class,
    safe_unregister_class,
    register_classes,
    unregister_classes,
    register_module_classes,
    unregister_module_classes,
)


_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API SURFACE
# Declaring __all__ makes the contract explicit and enables IDE navigation.
# ──────────────────────────────────────────────────────────────────────────────

__all__: List[str] = [
    # Lifecycle
    "OnixeyCompatibilityError",
    "validate_environment",
    "reset_validation_state",
    "register",
    "unregister",
    # Feature flags
    "supports_evaluated_get",
    "supports_msgbus",
    "supports_gpu_shader",
    "supports_undo_post_handler",
    "supports_redo_post_handler",
    "supports_undo_grouped",
    "supports_depsgraph_object_instances",
    "supports_pose_bone_matrix_channel",
    "supports_handler_depsgraph_arg",
    "supports_nla_track_mute",
    # API wrappers
    "get_evaluated_object_safe",
    "get_depsgraph_safe",
    "get_active_armature_safe",
    "get_active_action_safe",
    "get_pose_bone_safe",
    "safe_register_property",
    "safe_unregister_property",
    "safe_handler_append",
    "safe_handler_remove",
    # Registration
    "safe_register_class",
    "safe_unregister_class",
    "register_classes",
    "unregister_classes",
    "register_module_classes",
    "unregister_module_classes",
]


# ──────────────────────────────────────────────────────────────────────────────
# EXCEPTION
# ──────────────────────────────────────────────────────────────────────────────

class OnixeyCompatibilityError(Exception):
    """
    Raised by validate_environment() when the Blender environment is
    incompatible with this version of Onixey.

    __init__.py.register() catches this and aborts registration cleanly,
    leaving Blender in a consistent state.

    Attributes:
        message (str): Human-readable explanation shown to the user.
    """
    pass


# ──────────────────────────────────────────────────────────────────────────────
# VALIDATION STATE
# Module-level flag. Intentionally reset to False on every module reload
# (F8 / disable-enable) so register() always re-validates after a reload.
# reset_validation_state() provides an explicit reset path for unregister().
# ──────────────────────────────────────────────────────────────────────────────

_ENVIRONMENT_VALIDATED: bool = False


def validate_environment() -> None:
    """
    Validate that the current Blender environment supports Onixey V3.

    Must be called at the very start of __init__.py.register(), before any
    other module is imported or any class is registered.

    Idempotent: if already validated in this session, returns immediately.
    After an explicit reset_validation_state() call, re-validates fully.

    Validation sequence:
        1. Hard minimum Blender version  → raises OnixeyCompatibilityError
        2. Soft maximum Blender version  → WARNING only, registration continues
        3. Python version                → WARNING only, registration continues
        4. Feature flag population       → initialises feature_flags._registry

    Side effects:
        - Sets _ENVIRONMENT_VALIDATED = True on success.
        - Populates the feature_flags registry (read-only after this point).
        - Emits one INFO log line with the full environment summary.

    Raises:
        OnixeyCompatibilityError: The Blender version is below BLENDER_MIN.
            The caller must not proceed with registration.
        Exception: Any unexpected error during bpy.app access is re-raised
            so the caller sees a real traceback, not a silent failure.
    """
    global _ENVIRONMENT_VALIDATED

    if _ENVIRONMENT_VALIDATED:
        _log.debug("validate_environment(): already validated — skipping.")
        return

    # bpy is imported locally to avoid top-level side effects.
    # This is safe: validate_environment() is always called from register(),
    # which Blender only calls after bpy is fully initialized.
    import bpy
    blender_version: VersionTuple = bpy.app.version

    # ── 1. Hard minimum ───────────────────────────────────────────────────────
    # Below BLENDER_MIN, APIs we depend on may not exist.
    # Raising here is the correct behaviour — do not register a broken addon.
    if not is_version_at_least(blender_version, BLENDER_MIN):
        raise OnixeyCompatibilityError(
            f"{ADDON_DISPLAY_NAME} requires Blender "
            f"{version_string(BLENDER_MIN)} or newer. "
            f"Detected: {version_string(blender_version)}. "
            f"Please upgrade Blender to continue."
        )

    # ── 2. Soft maximum ───────────────────────────────────────────────────────
    # Above BLENDER_SOFT_MAX, APIs may have changed in ways we haven't
    # tested yet. We warn but do not block — the addon will likely still
    # work, and blocking would hurt users on bleeding-edge builds.
    if is_version_above(blender_version, BLENDER_SOFT_MAX):
        _log.warning(
            "%s: Blender %s exceeds the tested ceiling %s. "
            "Registration will proceed, but untested API changes in Blender "
            "%s may cause misbehaviour. Please report any issues.",
            ADDON_DISPLAY_NAME,
            version_string(blender_version),
            version_string(BLENDER_SOFT_MAX),
            version_string(blender_version),
        )

    # ── 3. Python version ─────────────────────────────────────────────────────
    # Blender ships its own Python. We warn if it is older than our minimum
    # but do not block, since the relevant language features may still work.
    if not is_python_supported():
        py_current = get_python_version()
        _log.warning(
            "%s: Python %d.%d detected (minimum tested: %d.%d). "
            "Some features relying on newer Python syntax may not work.",
            ADDON_DISPLAY_NAME,
            py_current[0], py_current[1],
            PYTHON_MIN[0], PYTHON_MIN[1],
        )

    # ── 4. Feature flag population ────────────────────────────────────────────
    # Computes all supports_*() flags for blender_version.
    # After this call, _ff_as_dict() returns a frozen snapshot.
    _ff_populate(blender_version)

    # ── Mark as validated ─────────────────────────────────────────────────────
    _ENVIRONMENT_VALIDATED = True

    enabled = [k for k, v in _ff_as_dict().items() if v]
    disabled = [k for k, v in _ff_as_dict().items() if not v]

    _log.info(
        "%s | Blender %s | Python %d.%d | "
        "Features enabled: %d | disabled: %d",
        build_info_string(),
        version_string(blender_version),
        *get_python_version(),
        len(enabled),
        len(disabled),
    )
    if disabled:
        _log.debug(
            "Disabled features (fallback active): %s",
            ", ".join(disabled),
        )


def reset_validation_state() -> None:
    """
    Reset the validation state so the next register() re-validates fully.

    When to call:
        - At the end of __init__.py.unregister().
        - Before running integration tests that re-register the addon.
        - NOT during normal operation.

    After this call:
        - _ENVIRONMENT_VALIDATED is False.
        - The feature_flags registry is cleared.
        - The next validate_environment() call will re-check Blender version
          and re-populate all feature flags from scratch.

    This ensures that after a reload (F8 / disable-enable), the new
    Blender version (if the user upgraded between sessions) is detected.
    """
    global _ENVIRONMENT_VALIDATED
    _ENVIRONMENT_VALIDATED = False
    _ff_reset()
    _log.debug(
        "reset_validation_state(): validation state cleared. "
        "Next register() will re-validate."
    )


def is_environment_validated() -> bool:
    """
    Return True if validate_environment() has been called successfully
    in the current session and reset_validation_state() has not been called
    since then.

    Useful for defensive checks in analysis modules:
        if not is_environment_validated():
            raise RuntimeError("Onixey not initialized")
    """
    return _ENVIRONMENT_VALIDATED


# ──────────────────────────────────────────────────────────────────────────────
# MODULE LIFECYCLE HOOKS
# Called by __init__.py module system (see _ORDERED_MODULE_PATHS).
# core.compat is the first module in that list — it has no classes to
# register, but its register() triggers validate_environment() and its
# unregister() resets state for clean reloads.
# ──────────────────────────────────────────────────────────────────────────────

def register() -> None:
    """
    Module register hook — called by __init__.py during addon registration.

    Triggers validate_environment(). Raises OnixeyCompatibilityError on
    hard failure, which __init__.py catches and uses to abort registration
    with a clean rollback.

    This function intentionally has no other side effects. It does not
    register any bpy classes or properties — that is other modules' job.
    """
    validate_environment()
    _log.debug("core.compat.register() complete.")


def unregister() -> None:
    """
    Module unregister hook — called by __init__.py during addon unregistration.

    Resets validation state so that a subsequent register() (e.g. after
    the user re-enables the addon or presses F8) re-validates fully.

    This function intentionally has no other side effects. It does not
    unregister any bpy classes or properties.
    """
    reset_validation_state()
    _log.debug("core.compat.unregister() complete.")
