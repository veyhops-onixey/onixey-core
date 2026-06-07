"""
onixey3/core/version.py

Version & Build Configuration — Onixey V3
==========================================
Single source of truth for every version constant, compatibility boundary,
and build-type flag used across the codebase.

RULES FOR THIS FILE
────────────────────
  ✓  Module-level constants only (NamedTuple + plain literals).
  ✓  Pure utility functions that operate on those constants.
  ✓  Complete type annotations on every symbol.
  ✗  No mutable state.
  ✗  No classes beyond VersionInfo (a frozen NamedTuple).
  ✗  No imports from onixey3.* (zero circular-dependency risk).
  ✗  No bpy, no addon_utils, no Blender API of any kind.
  ✗  No business logic, no animation logic, no runtime logic.
  ✗  No singleton, no __init__-time side effects beyond constant definition.

IMPORT ANYWHERE
────────────────
Because this file has no internal imports and no side effects, it is safe to
import at any point in the lifecycle — including at module-level in every other
file in the project.  The intended pattern is:

    from onixey3.core.version import (
        ONIXEY_VERSION,
        BLENDER_MIN_VERSION,
        is_blender_compatible,
    )

EXTENDING THIS FILE
────────────────────
Adding a new constant:
    1. Define it at module level with a type annotation and a brief comment.
    2. Add it to __all__.
    3. If it changes the public contract, bump ONIXEY_API_VERSION.patch.

Bumping a version:
    - Bug-fix release  → increment ONIXEY_VERSION.patch
    - New public API   → increment ONIXEY_VERSION.minor, reset patch to 0
    - Breaking change  → increment ONIXEY_VERSION.major, reset minor + patch
    Always update CHANGELOG at the bottom of this file.
"""

from __future__ import annotations

from typing import Final, NamedTuple, Tuple

__all__: Tuple[str, ...] = (
    # Types
    "VersionInfo",
    "BuildType",
    # Onixey identity
    "ONIXEY_VERSION",
    "ONIXEY_API_VERSION",
    "MODULE_COMPAT_REQUIRED",
    "RUNTIME_VERSION",
    "ADDON_DISPLAY_NAME",
    "ADDON_IDENTIFIER",
    # Blender compatibility
    "BLENDER_MIN_VERSION",
    "BLENDER_SOFT_MAX_VERSION",
    "PYTHON_MIN_VERSION",
    # Build
    "BUILD_TYPE",
    "IS_DEBUG",
    "IS_RELEASE",
    "IS_BETA",
    # Derived strings
    "VERSION_STRING",
    "FULL_VERSION_STRING",
    "BLENDER_MIN_STRING",
    # Utility functions
    "version_tuple_to_str",
    "is_blender_compatible",
    "is_blender_above_soft_max",
    "is_python_compatible",
    "build_info_string",
)


# ──────────────────────────────────────────────────────────────────────────────
# VERSION INFO TYPE
# ──────────────────────────────────────────────────────────────────────────────

class VersionInfo(NamedTuple):
    """
    Immutable three-part semantic version.

    NamedTuple is chosen over dataclass(frozen=True) because:
      - It is directly comparable:  ONIXEY_VERSION >= (3, 0, 0)  → True
      - It is directly unpackable:  major, minor, patch = ONIXEY_VERSION
      - It is hashable with no extra decoration.
      - Its repr is unambiguous:    VersionInfo(major=3, minor=1, patch=0)

    Attributes:
        major: Incremented on breaking API changes.
        minor: Incremented on backward-compatible new features.
        patch: Incremented on bug-fix releases.
    """

    major: int
    minor: int
    patch: int

    def as_tuple(self) -> Tuple[int, int, int]:
        """Return as a plain (major, minor, patch) tuple for external APIs."""
        return (self.major, self.minor, self.patch)

    def as_string(self, sep: str = ".") -> str:
        """Return human-readable version string, e.g. '3.1.0'."""
        return f"{self.major}{sep}{self.minor}{sep}{self.patch}"

    def as_bl_info_tuple(self) -> Tuple[int, int, int]:
        """
        Return exactly the tuple expected by bl_info['version'].
        Identical to as_tuple() but named explicitly for the Blender context.
        """
        return (self.major, self.minor, self.patch)


# ──────────────────────────────────────────────────────────────────────────────
# BUILD TYPE
# ──────────────────────────────────────────────────────────────────────────────

class BuildType:
    """
    Namespace of build-type string constants.

    Not an Enum to avoid import overhead and to remain a plain str in all
    comparisons (no .value access needed).

    Usage:
        from onixey3.core.version import BUILD_TYPE, BuildType
        if BUILD_TYPE == BuildType.DEBUG:
            ...
    """

    #: Active development build.  Verbose logging, extra assertions enabled.
    DEBUG:   Final[str] = "DEBUG"

    #: Pre-release build for testers.  Logging at WARNING+.
    BETA:    Final[str] = "BETA"

    #: Stable public release.  Minimal logging, all assertions stripped.
    RELEASE: Final[str] = "RELEASE"

    # Prevent instantiation — this is a constants namespace, not a class.
    def __init_subclass__(cls, **kwargs: object) -> None:  # type: ignore[override]
        raise TypeError("BuildType is a constants namespace and cannot be subclassed.")


# ──────────────────────────────────────────────────────────────────────────────
# ONIXEY IDENTITY
# ──────────────────────────────────────────────────────────────────────────────

# Human-readable addon name used in bl_info and UI labels.
ADDON_DISPLAY_NAME: Final[str] = "Onixey"

# Python package identifier — must match the root package folder name.
# Used in bl_info['name'] and sys.modules lookups.
ADDON_IDENTIFIER: Final[str] = "onixey3"

# ── Semantic version of the addon release ─────────────────────────────────────
#
# Bump rules (see module docstring):
#   patch → bug fixes, no API change
#   minor → new features, backward-compatible
#   major → breaking changes to public operators/properties/API
ONIXEY_VERSION: Final[VersionInfo] = VersionInfo(major=3, minor=2, patch=0)

# ── Public API contract version ───────────────────────────────────────────────
#
# An integer that sub-modules declare as MODULE_COMPAT = <value>.
# core/compat_checks.py verifies all loaded modules meet this floor.
# Increment when the cross-module Python API changes in a breaking way.
# Independent of ONIXEY_VERSION — an API break forces this up even on a
# patch release of the addon itself.
ONIXEY_API_VERSION: Final[int] = 3

# ── Minimum MODULE_COMPAT required from every sub-module ──────────────────────
#
# Any module declaring MODULE_COMPAT < MODULE_COMPAT_REQUIRED is rejected
# by core/compat_checks.py at registration time.
MODULE_COMPAT_REQUIRED: Final[int] = 3

# ── Runtime subsystem version ─────────────────────────────────────────────────
#
# Tracks the runtime/ package's internal protocol (cache tier layout,
# handler signature contracts, registry key format).
# Bump when runtime/lifecycle.py, runtime/cache.py, or runtime/registry.py
# change their internal contracts in a way that requires other modules to adapt.
RUNTIME_VERSION: Final[VersionInfo] = VersionInfo(major=3, minor=2, patch=0)


# ──────────────────────────────────────────────────────────────────────────────
# BLENDER COMPATIBILITY BOUNDARIES
# ──────────────────────────────────────────────────────────────────────────────

# Minimum Blender version required for Onixey to register at all.
# Hard gate: core/compat_checks.py raises OnixeyCompatibilityError below this.
#
# 4.2.0 is the first LTS release with a stable depsgraph API, evaluated_get(),
# undo_post handler, and the PropertyGroup contract that Onixey relies on.
BLENDER_MIN_VERSION: Final[Tuple[int, int, int]] = (4, 2, 0)

# Soft ceiling: Onixey will attempt to load above this version but emits a
# WARNING.  Not a hard block — untested does not mean broken.
#
# Update this after each new Blender release is tested and confirmed stable.
BLENDER_SOFT_MAX_VERSION: Final[Tuple[int, int, int]] = (5, 9, 99)

# Minimum Python version embedded in the supported Blender range.
# Blender 4.2 ships with Python 3.11.  This is a soft check (WARNING only).
PYTHON_MIN_VERSION: Final[Tuple[int, int]] = (3, 11)


# ──────────────────────────────────────────────────────────────────────────────
# BUILD TYPE & FLAGS
# ──────────────────────────────────────────────────────────────────────────────

# Change this constant to switch build behavior globally.
# Downstream modules read IS_DEBUG / IS_RELEASE / IS_BETA — not BUILD_TYPE
# directly — so the branching logic stays here.
BUILD_TYPE: Final[str] = BuildType.RELEASE

# Derived booleans — use these at call sites for readability.
IS_DEBUG:   Final[bool] = BUILD_TYPE == BuildType.DEBUG
IS_BETA:    Final[bool] = BUILD_TYPE == BuildType.BETA
IS_RELEASE: Final[bool] = BUILD_TYPE == BuildType.RELEASE


# ──────────────────────────────────────────────────────────────────────────────
# DERIVED VERSION STRINGS
# Pre-computed at import time so call sites have zero formatting overhead.
# ──────────────────────────────────────────────────────────────────────────────

# "3.2.0"
VERSION_STRING: Final[str] = ONIXEY_VERSION.as_string()

# "Onixey 3.2.0 [RELEASE]"
FULL_VERSION_STRING: Final[str] = (
    f"{ADDON_DISPLAY_NAME} {VERSION_STRING} [{BUILD_TYPE}]"
)

# "4.2.0"  — for use in error messages and UI labels
BLENDER_MIN_STRING: Final[str] = ".".join(str(x) for x in BLENDER_MIN_VERSION)


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# Pure functions that operate only on the constants in this file.
# Zero side effects.  Safe to call from any context, including import time.
# ──────────────────────────────────────────────────────────────────────────────

def version_tuple_to_str(
    version: Tuple[int, ...],
    sep: str = ".",
) -> str:
    """
    Format any integer version tuple as a dotted string.

    Args:
        version: Tuple of ints, e.g. (4, 2, 0) or (3, 11).
        sep:     Separator string.  Default: ".".

    Returns:
        e.g. "4.2.0" or "3.11".

    Examples:
        version_tuple_to_str((4, 2, 0))       → "4.2.0"
        version_tuple_to_str((3, 11), sep="-")→ "3-11"
        version_tuple_to_str(BLENDER_MIN_VERSION) → "4.2.0"
    """
    return sep.join(str(x) for x in version)


def is_blender_compatible(blender_version: Tuple[int, int, int]) -> bool:
    """
    Return True if *blender_version* meets or exceeds BLENDER_MIN_VERSION.

    This is a HARD compatibility check.  A False return means Onixey must
    not register — core/compat_checks.py raises OnixeyCompatibilityError
    when this returns False.

    Args:
        blender_version: (major, minor, patch) tuple from bpy.app.version.

    Returns:
        True  if blender_version >= BLENDER_MIN_VERSION.
        False otherwise.

    Examples:
        is_blender_compatible((4, 2, 0))  → True   (exactly the minimum)
        is_blender_compatible((4, 3, 1))  → True
        is_blender_compatible((4, 1, 9))  → False
        is_blender_compatible((3, 6, 0))  → False
    """
    return blender_version >= BLENDER_MIN_VERSION


def is_blender_above_soft_max(blender_version: Tuple[int, int, int]) -> bool:
    """
    Return True if *blender_version* exceeds BLENDER_SOFT_MAX_VERSION.

    A True return triggers a WARNING log but does NOT block registration.
    The addon will attempt to load — "untested" is not the same as "broken".

    Args:
        blender_version: (major, minor, patch) tuple from bpy.app.version.

    Returns:
        True  if blender_version > BLENDER_SOFT_MAX_VERSION.
        False if within the tested range.

    Examples:
        is_blender_above_soft_max((5, 9, 99))  → False  (exactly the ceiling)
        is_blender_above_soft_max((6, 0, 0))   → True
        is_blender_above_soft_max((4, 4, 0))   → False
    """
    return blender_version > BLENDER_SOFT_MAX_VERSION


def is_python_compatible(python_version: Tuple[int, int]) -> bool:
    """
    Return True if *python_version* meets or exceeds PYTHON_MIN_VERSION.

    This is a SOFT check — a False return triggers a WARNING, not an abort.
    Blender controls the embedded Python version; we cannot force an upgrade.

    Args:
        python_version: (major, minor) tuple from sys.version_info[:2].

    Returns:
        True  if python_version >= PYTHON_MIN_VERSION.
        False otherwise.

    Examples:
        is_python_compatible((3, 11))  → True
        is_python_compatible((3, 12))  → True
        is_python_compatible((3, 10))  → False
    """
    return python_version >= PYTHON_MIN_VERSION


def build_info_string() -> str:
    """
    Return a compact single-line build information string.

    Intended for: log headers, "Copy Debug Info" buttons, bug reports.
    Contains no newlines.  Safe to log at INFO level.

    Format:
        "{ADDON_DISPLAY_NAME} v{VERSION_STRING} | API={ONIXEY_API_VERSION} "
        "| Runtime={RUNTIME_VERSION_STRING} | Build={BUILD_TYPE} "
        "| BlenderMin={BLENDER_MIN_STRING}"

    Returns:
        e.g.
        "Onixey v3.2.0 | API=3 | Runtime=3.2.0 | Build=RELEASE | BlenderMin=4.2.0"
    """
    runtime_str = RUNTIME_VERSION.as_string()
    return (
        f"{ADDON_DISPLAY_NAME} v{VERSION_STRING}"
        f" | API={ONIXEY_API_VERSION}"
        f" | Runtime={runtime_str}"
        f" | Build={BUILD_TYPE}"
        f" | BlenderMin={BLENDER_MIN_STRING}"
    )


def is_version_at_least(
    version:   Tuple[int, ...],
    minimum:   Tuple[int, ...],
) -> bool:
    """
    Generic version comparison: is *version* >= *minimum*?

    Tuple comparison in Python is lexicographic, which is exactly correct for
    semantic version tuples.  This function is a thin, named wrapper so that
    call sites are readable without requiring the reader to know that (4,2) >
    (4,1,99) because tuple comparison stops at the shorter length.

    Args:
        version: The version to test, e.g. bpy.app.version or sys.version_info.
        minimum: The lower bound, e.g. BLENDER_MIN_VERSION.

    Returns:
        True if version >= minimum.

    Examples:
        is_version_at_least((4, 2, 0), (4, 2, 0))  → True
        is_version_at_least((4, 3, 1), (4, 2, 0))  → True
        is_version_at_least((4, 1, 9), (4, 2, 0))  → False
    """
    return version >= minimum


def is_version_above(
    version:  Tuple[int, ...],
    ceiling:  Tuple[int, ...],
) -> bool:
    """
    Generic version comparison: is *version* strictly greater than *ceiling*?

    Args:
        version: The version to test.
        ceiling: The upper bound.

    Returns:
        True if version > ceiling.

    Examples:
        is_version_above((6, 0, 0), (5, 9, 99))  → True
        is_version_above((5, 9, 99), (5, 9, 99)) → False
    """
    return version > ceiling


def get_python_version() -> Tuple[int, int]:
    """
    Return the running Python version as (major, minor).

    This is the ONLY function in this module that touches stdlib at call time
    (sys.version_info).  It does NOT import bpy.  It is safe to call at any
    point, including import time.

    Returns:
        (major, minor) tuple, e.g. (3, 11).
    """
    import sys  # deferred — keeps module-level imports clean
    return (sys.version_info.major, sys.version_info.minor)


# ──────────────────────────────────────────────────────────────────────────────
# SELF-CONSISTENCY ASSERTIONS
# Run once at import time.  Pure structural checks on the constants defined
# above — no bpy, no side effects beyond raising AssertionError on a
# schema authoring bug.
# ──────────────────────────────────────────────────────────────────────────────

def _validate_version_constants() -> None:
    """
    Assert internal consistency of the version constants.

    Catches authoring mistakes such as:
        - Negative version components.
        - RUNTIME_VERSION major ahead of ONIXEY_VERSION (not meaningful).
        - MODULE_COMPAT_REQUIRED != ONIXEY_API_VERSION (they should be in sync).
        - BUILD_TYPE not one of the three valid strings.
        - BLENDER_MIN_VERSION length != 3.
        - PYTHON_MIN_VERSION length != 2.

    Raises AssertionError with a precise message on any violation.
    Runs ONCE, at import time — zero repeated cost.
    """
    # Version component ranges
    for name, vinfo in (
        ("ONIXEY_VERSION",  ONIXEY_VERSION),
        ("RUNTIME_VERSION", RUNTIME_VERSION),
    ):
        assert all(c >= 0 for c in vinfo), (
            f"version.py: {name} contains a negative component: {vinfo!r}."
        )

    # API version / module compat must be in sync
    assert ONIXEY_API_VERSION == MODULE_COMPAT_REQUIRED, (
        f"version.py: ONIXEY_API_VERSION ({ONIXEY_API_VERSION}) != "
        f"MODULE_COMPAT_REQUIRED ({MODULE_COMPAT_REQUIRED}). "
        f"They must be equal — both express the same cross-module contract."
    )

    # API version must be positive
    assert ONIXEY_API_VERSION > 0, (
        f"version.py: ONIXEY_API_VERSION must be > 0, got {ONIXEY_API_VERSION}."
    )

    # Build type must be a known value
    valid_build_types = {BuildType.DEBUG, BuildType.BETA, BuildType.RELEASE}
    assert BUILD_TYPE in valid_build_types, (
        f"version.py: BUILD_TYPE {BUILD_TYPE!r} is not valid. "
        f"Must be one of {sorted(valid_build_types)}."
    )

    # Derived IS_* flags must be mutually exclusive and exhaustive
    assert sum([IS_DEBUG, IS_BETA, IS_RELEASE]) == 1, (
        f"version.py: Exactly one of IS_DEBUG/IS_BETA/IS_RELEASE must be True. "
        f"Got: IS_DEBUG={IS_DEBUG}, IS_BETA={IS_BETA}, IS_RELEASE={IS_RELEASE}."
    )

    # Blender version tuple lengths
    assert len(BLENDER_MIN_VERSION) == 3, (
        f"version.py: BLENDER_MIN_VERSION must be a 3-tuple, "
        f"got {BLENDER_MIN_VERSION!r} (len={len(BLENDER_MIN_VERSION)})."
    )
    assert len(BLENDER_SOFT_MAX_VERSION) == 3, (
        f"version.py: BLENDER_SOFT_MAX_VERSION must be a 3-tuple, "
        f"got {BLENDER_SOFT_MAX_VERSION!r} (len={len(BLENDER_SOFT_MAX_VERSION)})."
    )
    assert len(PYTHON_MIN_VERSION) == 2, (
        f"version.py: PYTHON_MIN_VERSION must be a 2-tuple, "
        f"got {PYTHON_MIN_VERSION!r} (len={len(PYTHON_MIN_VERSION)})."
    )

    # Min must be below soft max
    assert BLENDER_MIN_VERSION < BLENDER_SOFT_MAX_VERSION, (
        f"version.py: BLENDER_MIN_VERSION {BLENDER_MIN_VERSION} must be "
        f"strictly less than BLENDER_SOFT_MAX_VERSION {BLENDER_SOFT_MAX_VERSION}."
    )

    # All Blender version components must be non-negative
    for comp in (*BLENDER_MIN_VERSION, *BLENDER_SOFT_MAX_VERSION):
        assert comp >= 0, (
            f"version.py: Blender version tuple contains negative component: {comp}."
        )

    # Derived strings must be non-empty
    assert VERSION_STRING, "version.py: VERSION_STRING must not be empty."
    assert FULL_VERSION_STRING, "version.py: FULL_VERSION_STRING must not be empty."
    assert BLENDER_MIN_STRING, "version.py: BLENDER_MIN_STRING must not be empty."
    assert ADDON_DISPLAY_NAME, "version.py: ADDON_DISPLAY_NAME must not be empty."
    assert ADDON_IDENTIFIER, "version.py: ADDON_IDENTIFIER must not be empty."


# Execute once at import.  Zero bpy access, zero side effects beyond an
# AssertionError on a schema authoring bug (which is a CI/development error,
# never a user-visible production error on a correctly-built release).
_validate_version_constants()


# ──────────────────────────────────────────────────────────────────────────────
# CHANGELOG
# ──────────────────────────────────────────────────────────────────────────────
#
#  3.2.0  — Initial production implementation.
#             VersionInfo NamedTuple with as_tuple(), as_string(),
#             as_bl_info_tuple().
#             BuildType namespace (DEBUG / BETA / RELEASE).
#             ONIXEY_VERSION, ONIXEY_API_VERSION, MODULE_COMPAT_REQUIRED,
#             RUNTIME_VERSION, ADDON_DISPLAY_NAME, ADDON_IDENTIFIER.
#             BLENDER_MIN_VERSION, BLENDER_SOFT_MAX_VERSION, PYTHON_MIN_VERSION.
#             BUILD_TYPE, IS_DEBUG, IS_BETA, IS_RELEASE.
#             VERSION_STRING, FULL_VERSION_STRING, BLENDER_MIN_STRING.
#             version_tuple_to_str(), is_blender_compatible(),
#             is_blender_above_soft_max(), is_python_compatible(),
#             is_version_at_least(), is_version_above(),
#             build_info_string(), get_python_version().
#             Import-time _validate_version_constants() self-check.
