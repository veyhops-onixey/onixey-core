"""
onixey3/runtime/__init__.py

Runtime Package for Onixey V3.

SINGLE RESPONSIBILITY
─────────────────────
This package owns ALL volatile session state.
Nothing here persists to disk. Nothing here survives unregister().
The .blend file owns animation. Disk owns code. RAM owns runtime.

PACKAGE STRUCTURE
─────────────────
    cache.py      — AnalysisCache: multi-tier TTL cache with undo-safe invalidation.
    session.py    — SessionState: weakref-safe rig tracking and analysis state machine.
    lifecycle.py  — RuntimeLifecycle: startup/shutdown/reset orchestration.

DEPENDENCY CONTRACT
───────────────────
    runtime/ MAY import:   core/, utils/  (read-only bpy data access)
    runtime/ MUST NOT import: ui/, operators/, analysis/, properties/, migration/

    runtime/ NEVER:
        - writes to disk
        - registers bpy classes
        - calls bpy.ops.*
        - calls frame_set()
        - uses @persistent handlers (those belong to migration/)

LIFECYCLE CONTRACT
──────────────────
    Entry point:  lifecycle.startup()
    Exit point:   lifecycle.shutdown()
    Reset:        lifecycle.reset()   (called by undo/redo/load handlers)

    onixey3/__init__.py calls lifecycle.startup() inside register().
    onixey3/__init__.py calls lifecycle.shutdown() inside unregister().

    This __init__.py DOES NOT auto-import sub-modules.
    Consumers import explicitly:
        from onixey3.runtime.cache     import get as cache_get
        from onixey3.runtime.session   import get as session_get
        from onixey3.runtime.lifecycle import startup, shutdown

    This keeps dependency tracing explicit and avoids circular import risks.

IMPORT SIDE EFFECTS
───────────────────
    Importing this package has ZERO side effects.
    No global state is created at import time.
    All state is created in lifecycle.startup() and destroyed in lifecycle.shutdown().
"""
