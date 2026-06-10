"""
onixey3/validation/__init__.py

Validation Package — Onixey V3 AAA Internal QA Infrastructure.

PURPOSE
───────
Internal debugging and QA tooling. This package has zero side effects
on Blender state. It only reads — never registers classes, never touches
operators, never calls bpy.ops, never modifies scene properties.

MODULES
───────
    stress.py      — Register/unregister cycle testing, handler duplication
                     detection, scene property cleanup verification,
                     memory cleanup validation.

    healthcheck.py — Startup validation, Blender compatibility reporting,
                     module compatibility reporting, runtime diagnostics.

    report.py      — Structured output formatting for all check results.
                     Warnings, structured diagnostics, technical reports.

DEPENDENCY CONTRACT (from AAA Architecture doc)
───────────────────────────────────────────────
This package sits OUTSIDE the normal module dependency graph.
It may import from: core/, runtime/, properties/ (read-only).
It MUST NOT import from: operators/, ui/, analysis/.
It MUST NOT register any bpy.types.
It MUST NOT produce side effects when imported.

USAGE PATTERN
─────────────
From Blender Text Editor (development only):

    import importlib
    import onixey3.validation.healthcheck as hc
    importlib.reload(hc)
    report = hc.run_startup_checks()
    print(report.as_text())

    import onixey3.validation.stress as st
    importlib.reload(st)
    results = st.run_all()
    for r in results:
        print(r)

Or from a future debug operator (ONIXEY3_OT_run_diagnostics).
"""

from __future__ import annotations

# Intentionally empty — do not re-export anything at package level.
# Consumers import from the specific sub-module they need.
# This prevents accidental coupling and keeps import graphs explicit.
