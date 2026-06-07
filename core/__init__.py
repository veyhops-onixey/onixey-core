"""
onixey3/core/__init__.py

Core Package — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Package marker only. Declares the `core` namespace and its public surface.
Contains zero logic, zero imports of sibling modules, and zero side effects.

ARCHITECTURE CONTRACT
──────────────────────
Every module in core/ is independently importable:

    from onixey3.core import compat          # compat.py only
    from onixey3.core import feature_flags   # feature_flags.py only
    from onixey3.core import registration    # registration.py only
    from onixey3.core import version         # version.py only

If one module fails to import (e.g. compat.py raises on Blender version
check), the others remain fully available. This file never forces a
transitive import of any sibling — callers import exactly what they need.

WHAT THIS FILE DOES NOT DO
───────────────────────────
    - Does NOT import compat, feature_flags, registration, or version.
    - Does NOT execute any code at import time beyond the assignments below.
    - Does NOT define __all__ entries that would force module loading.
    - Does NOT contain classes, functions, or singleton state.

HOT RELOAD / F8 SAFETY
────────────────────────
This file has no mutable state. F8 reloads it safely with no side effects.
Individual sibling modules manage their own reload lifecycle independently.

CHANGELOG
─────────
    3.1.0 — Initial implementation.
"""

# ── Package identity ──────────────────────────────────────────────────────────

__version__: str = "3.1.0"
__author__:  str = "Onixey"

# ── Public surface ────────────────────────────────────────────────────────────
# __all__ lists the modules that belong to this package.
# It does NOT import them — it only declares their names so that tooling
# (IDEs, linters, documentation generators) understands the public surface.
# Callers must import each module explicitly:
#     from onixey3.core import feature_flags
#     from onixey3.core.compat import validate_environment

__all__ = [
    "compat",
    "feature_flags",
    "registration",
    "version",
]
