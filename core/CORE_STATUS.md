# CORE_STATUS — Onixey V3

```
STATUS   : LOCKED
VERSION  : Iteration1-Core-v1
DATE     : 2026-05-24
OWNER    : Onixey Architecture
```

---

## Locked Files

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker. Metadata. Zero logic. |
| `version.py` | Version constants and compatibility matrix. |
| `compat.py` | Blender environment validation. Facade. |
| `feature_flags.py` | Runtime feature detection. Singleton lifecycle. |
| `api_wrappers.py` | Safe wrappers for bpy API calls. |
| `registration.py` | Class registration with rollback. |

---

## Responsibilities

- Blender version compatibility
- Addon versioning and migration contracts
- bpy class registration and unregistration
- Feature flag detection and sealing
- Safe bpy API access patterns
- Base infrastructure for all other packages

---

## Prohibited in Core

The following must never be implemented inside `core/`:

- Animation logic (FCurves, keyframes, motion analysis)
- Session state (`session.py` belongs in `runtime/`)
- Cache systems (`cache.py` belongs in `runtime/`)
- Runtime state machines (`state.py` belongs in `runtime/`)
- Event systems and handlers (`handlers.py` belongs in `runtime/`)
- Operators, panels, UI (`operators/`, `ui/`)
- Analysis algorithms (`analysis/`)
- Circular dependencies between core files
- Unnecessary managers or singletons beyond `feature_flags`

---

## Change Policy

Changes to locked files are only permitted for:

| Allowed | Example |
|---|---|
| Real bug fixes | Crash on specific Blender build |
| Blender compatibility | New API in Blender 4.3 / 5.x |
| Security | Input validation gap |
| Critical improvements | Hard requirement check failure |

**Any other change requires a new iteration designation.**

Changes must be reviewed, logged, and reflected in `version.py`.

---

## Dependency Rules

```
core/ may import:   stdlib only (typing, logging, sys, time, os)
core/ must never import:   runtime/, analysis/, operators/, ui/, migration/
```

Each file in `core/` must be independently importable.  
Failure of one file must not prevent others from loading.

---

## Iteration History

| Iteration | Version | Status | Notes |
|---|---|---|---|
| 1 | `Iteration1-Core-v1` | **LOCKED** | Foundation complete |
