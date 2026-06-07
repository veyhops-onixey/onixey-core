"""
onixey3/core/feature_flags.py

Onixey V3 — Blender API Feature Detection System (AAA Production Grade)
Schema v3 — Enhanced with forensic event log, probe statistics, live integrity
            checks, runtime compat matrix, freeze-guard mutation detection,
            structured hot-reload timeline, and hard-requirement reporting.

DESIGN PHILOSOPHY
─────────────────
This module is the SINGLE AUTHORITY for all runtime Blender capability checks.
No other module in the codebase is allowed to perform `bpy.app.version` comparisons
directly. All feature gating MUST go through the public API defined here.

Pattern reference: Rigify (scripts/addons/rigify), Auto-Rig-Pro, and
BPY best practices from Blender's own addon guidelines.

USAGE CONTRACT
──────────────
    # In any module that needs to branch on capability:
    from onixey3.core.feature_flags import get_flags, supports_evaluated_get

    # Option A — direct helper (preferred for single checks)
    if supports_evaluated_get():
        eval_obj = obj.evaluated_get(depsgraph)

    # Option B — snapshot (preferred inside tight loops or operators)
    flags = get_flags()
    if flags.evaluated_get and flags.undo_post_handler:
        ...

    # Option C — forensic diagnostics (support / bug reports)
    report    = get_diagnostic_report()
    anomalies = get_api_anomalies()
    metrics   = get_integrity_metrics()

    # Option D — v3 additions
    probe_stats = get_probe_statistics()          # per-probe hit/miss counters
    event_log   = get_forensic_event_log()        # bounded circular event log
    live_check  = get_live_integrity_check()      # post-init runtime self-test
    compat_mx   = get_runtime_compat_matrix()     # version→flag availability grid
    hard_reqs   = get_hard_requirements()         # hard-req flags with status
    new_flags   = flag_added_since((4, 0, 0))     # flags introduced after version

SINGLETON LIFECYCLE
───────────────────
    1. Module is imported → _registry is built (zero bpy access, zero side effects).
       _validate_definitions_integrity() runs immediately — catches schema authoring
       bugs (duplicate keys, naming violations) before any Blender code executes.
    2. feature_flags.initialize() is called ONCE from core/compat.py during
       validate_environment(), which itself runs before any other module loads.
       initialize() sets _singleton (frozen FeatureSet), populates _probe_cache,
       populates _integrity_metrics, and logs a forensic summary.
    3. After initialize(), _singleton is READ-ONLY. No setter exists.
       _FreezeGuard.__setattr__ on FeatureSet raises AttributeError on any
       attempted post-init mutation (v3 addition).
    4. On addon unregister → feature_flags.reset() is called so that a
       subsequent register() re-detects cleanly (handles F8 / disable+enable).
       reset() clears _singleton, _probe_cache, _integrity_metrics, and
       _init_call_count is preserved for hot-reload abuse detection.

HOT RELOAD / F8 CONTRACT
─────────────────────────
    F8 in Blender triggers a full Python module reload. This module is designed
    to survive unlimited reload cycles without leaking state:

    • _UNINITIALIZED is re-created on each import (new object identity).
    • _singleton starts as _UNINITIALIZED after each reload.
    • _probe_cache and _probe_stats are cleared on each reload and on reset().
    • _init_call_count survives reload ONLY via sys.modules (_HotReloadGuard).
    • _hot_reload_timeline records ISO timestamps of each init call (v3).
    • reset() is idempotent: double-reset emits DEBUG, not WARNING.
    • initialize() detects double-init before mutating any state.

PROBE CACHE (v2) + PROBE STATISTICS (v3)
─────────────────────────────────────────
    Probe results are cached in _probe_cache[key] = bool after the first call.
    v3 adds _probe_stats[key] = ProbeStats — a named counter tracking hits,
    misses, total calls, and cumulative timing per probe key. Useful for
    identifying probes that run unexpectedly often (hot-reload abuse detection)
    and probes that are consistently slow (optimization candidates).

FORENSIC EVENT LOG (v3)
────────────────────────
    _forensic_event_log is a bounded deque (max _FORENSIC_LOG_MAX entries).
    Every significant lifecycle event writes a structured ForensicEvent:
        • INIT        — initialize() called (with blender_version, call count)
        • RESET       — reset() called (with was_initialized flag)
        • PROBE_FAIL  — a probe callable raised an exception
        • ANOMALY     — an API anomaly pair violation detected
        • HARD_FAIL   — a hard requirement flag returned False
        • HOT_RELOAD  — _init_call_count exceeded _HOT_RELOAD_WARN_THRESHOLD
        • LIVE_CHECK  — get_live_integrity_check() found a discrepancy
    Log is accessible via get_forensic_event_log() and included in
    get_diagnostic_report().

THREAD SAFETY
─────────────
Blender's Python runs on a single thread. This module does not use locks.
If future Blender versions introduce threaded Python, revisit this assumption.

ZERO SIDE EFFECTS ON IMPORT
────────────────────────────
No bpy symbols are imported at module level. Every bpy access is deferred
inside functions that are only called after Blender is fully initialized.

EXTENDING FOR BLENDER 5.x
──────────────────────────
To add a new capability flag:
    1. Add an entry to _FEATURE_DEFINITIONS (the central table, see Section 2b).
    2. Add a supports_*() public helper at the bottom (see Section 8).
    3. Bump _REGISTRY_VERSION.
    4. If the flag has a probe, add the probe function to Section 2a.
    5. If the flag is logically interdependent with another, add an _AnomalyPair.
    Nothing else needs to change.

INTEGRITY METRICS
─────────────────
After initialize(), get_integrity_metrics() returns a snapshot dict with:
    blender_version           — version string detected at init time
    total_flags               — count of all defined flags
    enabled_flags             — count of True flags
    disabled_flags            — count of False flags
    hard_requirements_met     — count of hard (fallback_ok=False) flags that are True
    hard_requirements_total   — total count of hard requirements
    probed_flags              — count of flags that ran a probe callable
    probe_failures            — count of probes that raised an exception
    api_anomalies_detected    — count of detected Blender API inconsistencies
    init_call_count           — how many times initialize() was called (hot-reload metric)
    init_duration_ms          — wall time of the full computation pass
    schema_version            — _REGISTRY_VERSION at init time
"""

from __future__ import annotations

import collections
import logging
import re as _re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple, Any

# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Bump when the feature flag schema changes (new flags, renamed flags, removed flags).
_REGISTRY_VERSION: int = 3   # was 2 in the previous iteration

# Maximum allowed init calls before a forensic warning is emitted.
_HOT_RELOAD_WARN_THRESHOLD: int = 5

# Maximum entries in the forensic event log circular buffer.
_FORENSIC_LOG_MAX: int = 128

_log = logging.getLogger(__name__)

# Sentinel: marks the singleton as "not yet initialized".
_UNINITIALIZED: object = object()

# Naming convention enforced at import time.
_SPECULATIVE_PREFIX: str = "blender5_"
_KEY_PATTERN = _re.compile(r'^[a-z][a-z0-9_]*[a-z0-9]$|^[a-z]$')


# ──────────────────────────────────────────────────────────────────────────────
# HOT RELOAD GUARD
# ──────────────────────────────────────────────────────────────────────────────

_SYSMOD_STATE_KEY: str = "onixey3._feature_flags_hotreload_state"


def _get_sysmod_state() -> Dict[str, Any]:
    """Retrieve or create the cross-reload persistent state dict in sys.modules."""
    if _SYSMOD_STATE_KEY not in sys.modules:
        # v3: timeline list survives across reloads for full hot-reload forensics
        sys.modules[_SYSMOD_STATE_KEY] = {   # type: ignore[assignment]
            "init_call_count": 0,
            "init_timeline":   [],            # ISO timestamp per initialize() call
        }
    return sys.modules[_SYSMOD_STATE_KEY]    # type: ignore[return-value]


def _increment_init_count() -> int:
    state = _get_sysmod_state()
    state["init_call_count"] += 1
    # v3: record wall-clock timestamp for this init call
    state["init_timeline"].append(time.strftime("%Y-%m-%dT%H:%M:%S"))
    return state["init_call_count"]


def _get_init_count() -> int:
    return _get_sysmod_state().get("init_call_count", 0)


def _get_init_timeline() -> List[str]:
    """Return ISO timestamp list of every initialize() call this session (v3)."""
    return list(_get_sysmod_state().get("init_timeline", []))


def _reset_init_count_for_tests() -> None:
    """ONLY for unit tests — never call from production code."""
    state = _get_sysmod_state()
    state["init_call_count"] = 0
    state["init_timeline"]   = []


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 1 — VERSION TYPES
# ──────────────────────────────────────────────────────────────────────────────

VersionTuple = Tuple[int, int, int]


def _v(major: int, minor: int, patch: int = 0) -> VersionTuple:
    """Shorthand version tuple constructor."""
    return (major, minor, patch)


# ──────────────────────────────────────────────────────────────────────────────
# v3 — FORENSIC EVENT LOG
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ForensicEvent:
    """
    Structured record of a significant lifecycle event.

    Fields:
        event_type:  One of: INIT, RESET, PROBE_FAIL, ANOMALY,
                              HARD_FAIL, HOT_RELOAD, LIVE_CHECK.
        timestamp:   Wall-clock ISO string at event time.
        message:     Human-readable description.
        data:        Optional dict of structured payload (blender_version,
                     flag_key, probe_name, anomaly_pair, etc.).
    """
    event_type: str
    timestamp:  str
    message:    str
    data:       Dict[str, Any] = field(default_factory=dict)

    def as_line(self) -> str:
        extras = "  ".join(f"{k}={v}" for k, v in self.data.items()) if self.data else ""
        return f"[{self.timestamp}] {self.event_type:<12} {self.message}" + (
            f"  ({extras})" if extras else ""
        )


# Module-level bounded log — cleared by reset(), persistent across probe calls.
_forensic_event_log: Deque[ForensicEvent] = collections.deque(maxlen=_FORENSIC_LOG_MAX)


def _log_forensic(
    event_type: str,
    message:    str,
    **data: Any,
) -> None:
    """
    Append a ForensicEvent to the bounded circular log.

    Also routes to the Python logger at the appropriate level so that
    Blender's console always shows the event, even if get_forensic_event_log()
    is never called.

    Args:
        event_type: SCREAMING_SNAKE_CASE event type string.
        message:    Human-readable description.
        **data:     Arbitrary key-value payload attached to the event.
    """
    ev = ForensicEvent(
        event_type=event_type,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        message=message,
        data=dict(data),
    )
    _forensic_event_log.append(ev)

    level = {
        "HARD_FAIL":  logging.ERROR,
        "HOT_RELOAD": logging.WARNING,
        "PROBE_FAIL": logging.WARNING,
        "ANOMALY":    logging.WARNING,
        "LIVE_CHECK": logging.WARNING,
        "INIT":       logging.INFO,
        "RESET":      logging.DEBUG,
    }.get(event_type, logging.DEBUG)

    _log.log(level, "[FORENSIC:%s] %s  %s", event_type, message,
             "  ".join(f"{k}={v}" for k, v in data.items()) if data else "")


# ──────────────────────────────────────────────────────────────────────────────
# v3 — PROBE STATISTICS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ProbeStats:
    """
    Per-probe invocation counters and timing.

    Attributes:
        key:           Flag key this probe belongs to.
        hits:          Times the result was served from _probe_cache.
        misses:        Times the probe callable was actually invoked.
        failures:      Times the probe raised an exception.
        total_ms:      Cumulative probe execution time in milliseconds.
        last_result:   Most recent probe return value (True/False/None).
        last_ms:       Execution time of the most recent probe call.
    """
    key:         str
    hits:        int   = 0
    misses:      int   = 0
    failures:    int   = 0
    total_ms:    float = 0.0
    last_result: Optional[bool]  = None
    last_ms:     float = 0.0

    @property
    def total_calls(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Cache hit rate [0.0 – 1.0]. Returns 0.0 if no calls."""
        return self.hits / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def avg_ms(self) -> float:
        """Average probe execution time across all misses."""
        return self.total_ms / self.misses if self.misses > 0 else 0.0


# Module-level probe stats dict — keyed by flag key, cleared on reset().
_probe_stats: Dict[str, ProbeStats] = {}


def _get_or_create_probe_stats(key: str) -> ProbeStats:
    if key not in _probe_stats:
        _probe_stats[key] = ProbeStats(key=key)
    return _probe_stats[key]


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2a — PROBE FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def _probe_handler_depsgraph_arg(version: VersionTuple) -> bool:
    """
    Belt-and-suspenders: confirm bpy.app.handlers exposes the attributes
    needed. Catches stripped builds and non-standard forks.
    """
    import bpy as _bpy
    h = _bpy.app.handlers
    return (
        hasattr(h, "frame_change_post")
        and hasattr(h, "depsgraph_update_post")
        and hasattr(h, "load_post")
    )


def _probe_undo_post_handler(version: VersionTuple) -> bool:
    """
    Confirm bpy.app.handlers.undo_post exists at runtime.
    Some downstream forks omit it despite reporting Blender 2.81+ version.
    """
    import bpy as _bpy
    return hasattr(_bpy.app.handlers, "undo_post")


def _probe_redo_post_handler(version: VersionTuple) -> bool:
    """Confirm bpy.app.handlers.redo_post exists at runtime."""
    import bpy as _bpy
    return hasattr(_bpy.app.handlers, "redo_post")


def _probe_gpu_shader(version: VersionTuple) -> bool:
    """
    Confirm the gpu module is importable and exposes the shader sub-module.
    Guards against stripped/embedded Blender builds.
    """
    try:
        import gpu as _gpu  # noqa: F401
        return hasattr(_gpu, "shader")
    except ImportError:
        return False


def _probe_gpu_shader_3d_uniform_color(version: VersionTuple) -> bool:
    """
    Confirm gpu.shader.from_builtin() factory is present (not compiled).
    """
    try:
        import gpu as _gpu
        return hasattr(_gpu.shader, "from_builtin")
    except (ImportError, AttributeError):
        return False


def _probe_msgbus(version: VersionTuple) -> bool:
    """Confirm bpy.msgbus exposes subscribe_rna and clear_by_owner."""
    import bpy as _bpy
    return (
        hasattr(_bpy, "msgbus")
        and hasattr(_bpy.msgbus, "subscribe_rna")
        and hasattr(_bpy.msgbus, "clear_by_owner")
    )


def _probe_action_slot_system(version: VersionTuple) -> bool:
    """
    Confirm Blender 4.4+ Action Slot API via bpy.types.Action RNA introspection.
    """
    import bpy as _bpy
    try:
        action_rna = _bpy.types.Action.bl_rna
        return "slots" in action_rna.properties
    except (AttributeError, ReferenceError):
        return False


def _probe_bone_color_api(version: VersionTuple) -> bool:
    """Confirm Blender 4.0+ Bone.color API via type inspection."""
    import bpy as _bpy
    try:
        return hasattr(_bpy.types.Bone, "color")
    except (AttributeError, ReferenceError):
        return False


def _probe_asset_library_api(version: VersionTuple) -> bool:
    """Confirm asset library API presence (type name changed 3.5→3.6)."""
    import bpy as _bpy
    return hasattr(_bpy.types, "AssetHandle") or hasattr(_bpy.types, "AssetRepresentation")


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2b — FEATURE DEFINITIONS TABLE
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _FeatureDefinition:
    """Immutable descriptor for a single Blender API capability."""
    key:         str
    since:       VersionTuple
    until:       Optional[VersionTuple]
    probe:       Optional[Callable[[VersionTuple], bool]]
    description: str
    fallback_ok: bool


_FEATURE_DEFINITIONS: Tuple[_FeatureDefinition, ...] = (

    # ── Core evaluation API ────────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "evaluated_get",
        since       = _v(2, 80),
        until       = None,
        probe       = None,
        description = (
            "obj.evaluated_get(depsgraph) — returns the constraint-resolved, "
            "driver-applied version of an object. REQUIRED for any world-space "
            "position analysis. Using obj.matrix_world without this gives stale "
            "pre-constraint values when IK/constraints are active."
        ),
        fallback_ok = False,
    ),

    _FeatureDefinition(
        key         = "depsgraph_object_instances",
        since       = _v(2, 80),
        until       = None,
        probe       = None,
        description = (
            "depsgraph.object_instances — iterator over all evaluated instances "
            "in the scene, including duplicates from collections."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "depsgraph_updates",
        since       = _v(2, 80),
        until       = None,
        probe       = None,
        description = (
            "depsgraph_update_post handler — fires when the dependency graph "
            "is evaluated. Used ONLY for cache invalidation, never analysis."
        ),
        fallback_ok = True,
    ),

    # ── Handler system ────────────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "handler_depsgraph_arg",
        since       = _v(2, 80),
        until       = None,
        probe       = _probe_handler_depsgraph_arg,
        description = (
            "Handler signature (scene, depsgraph) for frame_change_post and "
            "depsgraph_update_post. Belt-and-suspenders runtime probe confirms "
            "the specific handler lists we need actually exist."
        ),
        fallback_ok = False,
    ),

    _FeatureDefinition(
        key         = "undo_post_handler",
        since       = _v(2, 81),
        until       = None,
        probe       = _probe_undo_post_handler,
        description = (
            "bpy.app.handlers.undo_post — fires after Ctrl+Z completes. "
            "Critical for cache invalidation per AAA Rule 8."
        ),
        fallback_ok = False,
    ),

    _FeatureDefinition(
        key         = "redo_post_handler",
        since       = _v(2, 81),
        until       = None,
        probe       = _probe_redo_post_handler,
        description = (
            "bpy.app.handlers.redo_post — symmetric partner to undo_post_handler "
            "for cache invalidation after Ctrl+Shift+Z."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "load_post_handler",
        since       = _v(2, 80),
        until       = None,
        probe       = _probe_handler_depsgraph_arg,
        description = (
            "bpy.app.handlers.load_post — fires after .blend load. "
            "Used for migration, full cache invalidation, session reset. "
            "MUST be decorated @persistent (AAA Rule 9)."
        ),
        fallback_ok = False,
    ),

    # ── Undo system ──────────────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "undo_grouped",
        since       = _v(3, 0),
        until       = None,
        probe       = None,
        description = (
            "bl_options={'UNDO_GROUPED'} — groups multiple undo steps into one "
            "Ctrl+Z entry. Used by arc_polish and energy_smooth."
        ),
        fallback_ok = True,
    ),

    # ── Rendering / viewport API ──────────────────────────────────────────────

    _FeatureDefinition(
        key         = "gpu_shader",
        since       = _v(2, 83),
        until       = None,
        probe       = _probe_gpu_shader,
        description = (
            "gpu.shader / gpu.types.GPUShader — viewport overlay rendering. "
            "Draws arc trajectories and energy spike markers in 3D viewport."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "gpu_shader_3d_uniform_color",
        since       = _v(3, 4),
        until       = None,
        probe       = _probe_gpu_shader_3d_uniform_color,
        description = (
            "gpu.shader.from_builtin('UNIFORM_COLOR') — 3.4+ name for the "
            "built-in colored geometry shader."
        ),
        fallback_ok = True,
    ),

    # ── NLA / Animation system ────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "nla_track_mute",
        since       = _v(2, 80),
        until       = None,
        probe       = None,
        description = (
            "NLATrack.mute — temporarily disables preview NLA tracks created "
            "by the non-destructive preview system."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "action_slot_system",
        since       = _v(4, 4),
        until       = None,
        probe       = _probe_action_slot_system,
        description = (
            "Blender 4.4+ Action Slot API — actions can target specific "
            "datablocks via slots. Must use slot API to avoid breaking "
            "multi-target actions."
        ),
        fallback_ok = True,
    ),

    # ── Pose / Armature system ────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "pose_bone_matrix_channel",
        since       = _v(3, 0),
        until       = None,
        probe       = None,
        description = (
            "PoseBone.matrix_channel — local bone matrix in parent space "
            "post-constraint, pre-IK. Used for IK singularity risk detection."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "bone_color_api",
        since       = _v(4, 0),
        until       = None,
        probe       = _probe_bone_color_api,
        description = (
            "Bone.color API (4.0+) — per-bone color themes for visually "
            "flagging problematic bones in the rig viewport."
        ),
        fallback_ok = True,
    ),

    # ── Message bus ──────────────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "msgbus",
        since       = _v(2, 80),
        until       = None,
        probe       = _probe_msgbus,
        description = (
            "bpy.msgbus.subscribe_rna / clear_by_owner — reactive property "
            "change notifications. Avoids polling in frame_change_post."
        ),
        fallback_ok = True,
    ),

    # ── Asset / Library system ────────────────────────────────────────────────

    _FeatureDefinition(
        key         = "asset_library_api",
        since       = _v(3, 5),
        until       = None,
        probe       = _probe_asset_library_api,
        description = (
            "bpy.types.AssetHandle / AssetRepresentation — future use for "
            "Onixey preset sharing. Gated here for sprint readiness."
        ),
        fallback_ok = True,
    ),

    # ── Blender 5.x forward-looking entries ──────────────────────────────────

    _FeatureDefinition(
        key         = "blender5_evaluated_get_context",
        since       = _v(99, 0),
        until       = None,
        probe       = None,
        description = (
            "SPECULATIVE: evaluated_get() may require a context argument. "
            "When True, compat.py uses the new signature."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "blender5_gpu_new_shader_api",
        since       = _v(99, 0),
        until       = None,
        probe       = None,
        description = (
            "SPECULATIVE: GPU shader API refactor in Blender 5.x. "
            "When True, ui/overlays.py uses the new API path via compat.py."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "blender5_handler_new_signature",
        since       = _v(99, 0),
        until       = None,
        probe       = None,
        description = (
            "SPECULATIVE: Blender 5.x may change handler call signatures. "
            "When True, runtime/ modules use the new handler signature."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "blender5_depsgraph_new_api",
        since       = _v(99, 0),
        until       = None,
        probe       = None,
        description = (
            "SPECULATIVE: Blender 5.x depsgraph iteration contract may change. "
            "When True, use compat.iter_depsgraph_objects_safe()."
        ),
        fallback_ok = True,
    ),

    _FeatureDefinition(
        key         = "blender5_undo_system_redesign",
        since       = _v(99, 0),
        until       = None,
        probe       = None,
        description = (
            "SPECULATIVE: Blender 5.x undo system redesign may change handler "
            "names or ordering. When True, runtime/cache.py uses new path."
        ),
        fallback_ok = True,
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2c — ANOMALY PAIRS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _AnomalyPair:
    """If flag_a is True, flag_b must also be True — otherwise anomalous."""
    flag_a:  str
    flag_b:  str
    reason:  str


_ANOMALY_PAIRS: Tuple[_AnomalyPair, ...] = (
    _AnomalyPair(
        flag_a = "gpu_shader_3d_uniform_color",
        flag_b = "gpu_shader",
        reason = (
            "from_builtin('UNIFORM_COLOR') requires the gpu.shader module. "
            "gpu_shader=False with gpu_shader_3d_uniform_color=True means either "
            "the version range is wrong or a probe returned an inconsistent result."
        ),
    ),
    _AnomalyPair(
        flag_a = "redo_post_handler",
        flag_b = "undo_post_handler",
        reason = (
            "redo_post was introduced alongside undo_post in Blender 2.81. "
            "redo_post=True but undo_post=False indicates a non-standard build "
            "or a probe failure on undo_post_handler."
        ),
    ),
    _AnomalyPair(
        flag_a = "action_slot_system",
        flag_b = "nla_track_mute",
        reason = (
            "Blender 4.4+ shipping Action Slots without NLA track mute would be "
            "unexpected — both share the animation subsystem coexisting since 2.80."
        ),
    ),
    _AnomalyPair(
        flag_a = "bone_color_api",
        flag_b = "pose_bone_matrix_channel",
        reason = (
            "Bone.color arrived in Blender 4.0, which postdates matrix_channel (3.0). "
            "bone_color_api=True implies pose_bone_matrix_channel must also be True."
        ),
    ),
    _AnomalyPair(
        flag_a = "undo_grouped",
        flag_b = "handler_depsgraph_arg",
        reason = (
            "UNDO_GROUPED arrived in 3.0, postdating the depsgraph handler "
            "signature change (2.80). undo_grouped=True implies handler_depsgraph_arg=True."
        ),
    ),
    _AnomalyPair(
        flag_a = "asset_library_api",
        flag_b = "msgbus",
        reason = (
            "Asset library API (3.5+) postdates msgbus (2.80). "
            "asset_library_api=True but msgbus=False suggests a heavily stripped build."
        ),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 3 — FEATURE SET DATACLASS
# ──────────────────────────────────────────────────────────────────────────────

def _build_feature_set_class() -> type:
    """
    Dynamically construct the FeatureSet dataclass from _FEATURE_DEFINITIONS.
    Every flag key becomes a bool field defaulting to False.
    frozen=True enforces read-only after construction.
    """
    annotations: Dict[str, type] = {}
    defaults:    Dict[str, bool]  = {}

    for defn in _FEATURE_DEFINITIONS:
        annotations[defn.key] = bool
        defaults[defn.key]    = False

    ns      = {"__annotations__": annotations, **{k: field(default=v) for k, v in defaults.items()}}
    raw_cls = type("FeatureSet", (), ns)
    return dataclass(raw_cls, frozen=True)


FeatureSet = _build_feature_set_class()


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 4 — SINGLETON STATE
# ──────────────────────────────────────────────────────────────────────────────

_singleton:           Any                    = _UNINITIALIZED
_probe_cache:         Dict[str, bool]        = {}
_integrity_metrics:   Dict[str, Any]         = {}
_detected_anomalies:  List[str]              = []
# v3 additions — all cleared by reset():
# _probe_stats and _forensic_event_log are defined above their first use


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 5 — ERROR TYPES
# ──────────────────────────────────────────────────────────────────────────────

class FeatureFlagsNotInitializedError(RuntimeError):
    """
    Raised when get_flags() or any supports_*() helper is called before
    feature_flags.initialize() has been invoked.

    Fix: ensure core/compat.py calls feature_flags.initialize() during
    validate_environment(), before any other module calls get_flags().
    """


class FeatureFlagsAlreadyInitializedError(RuntimeError):
    """
    Raised when initialize() is called a second time without an intervening
    reset() call.

    Recovery: call feature_flags.reset() then feature_flags.initialize().
    """


class FeatureFlagsCorruptStateError(RuntimeError):
    """
    Raised when get_flags() finds _singleton is set but is not a FeatureSet.
    Should NEVER occur in production. Recovery: reset() then initialize().
    """


class OnixeyHardRequirementError(RuntimeError):
    """
    Raised by initialize() when one or more fallback_ok=False flags are
    unavailable. __init__.py catches this and aborts registration cleanly.
    """


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 6 — COMPUTATION ENGINE (private)
# ──────────────────────────────────────────────────────────────────────────────

def _compute_single_flag(
    defn:            _FeatureDefinition,
    blender_version: VersionTuple,
    probe_cache:     Dict[str, bool],
    metrics:         Dict[str, Any],
) -> bool:
    """
    Evaluate one feature definition against the running Blender version.

    Logic:
        1. Check `since`  — version below since → False.
        2. Check `until`  — version above until → False.
        3. Run probe      — check cache first; call probe on miss; cache result.
        4. Return True.

    v3: probe_stats are updated on every call (hit or miss).
    Probe exceptions are caught, forensically logged, and cached as False.
    """
    # Guard: since — lower bound
    if blender_version < defn.since:
        return False

    # Guard: until — upper bound (API removed or incompatibly changed)
    if defn.until is not None and blender_version > defn.until:
        return False

    # Optional runtime probe with cache + stats
    if defn.probe is not None:
        stats = _get_or_create_probe_stats(defn.key)

        # Cache hit
        if defn.key in probe_cache:
            stats.hits += 1
            return probe_cache[defn.key]

        # Cache miss — invoke probe
        metrics["probed_flags"] = metrics.get("probed_flags", 0) + 1
        stats.misses += 1
        t0 = time.perf_counter()

        try:
            result  = defn.probe(blender_version)
            elapsed = (time.perf_counter() - t0) * 1000.0

            stats.total_ms    += elapsed
            stats.last_result  = result
            stats.last_ms      = elapsed
            probe_cache[defn.key] = result

            if elapsed > 50.0:
                _log.warning(
                    "SLOW PROBE: '%s' took %.1fms (budget 50ms). "
                    "Optimize or remove the probe. Probe: %s",
                    defn.key, elapsed,
                    getattr(defn.probe, "__name__", repr(defn.probe)),
                )
            else:
                _log.debug("Probe '%s' → %s (%.2fms)", defn.key, result, elapsed)

            if not result:
                _log.debug(
                    "Feature '%s': probe returned False for Blender %s",
                    defn.key, blender_version,
                )
            return result

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            metrics["probe_failures"] = metrics.get("probe_failures", 0) + 1
            stats.failures    += 1
            stats.last_result  = False
            stats.last_ms      = elapsed
            probe_cache[defn.key] = False

            _log_forensic(
                "PROBE_FAIL",
                f"Probe for '{defn.key}' raised {type(exc).__name__}: {exc}",
                flag_key=defn.key,
                probe=getattr(defn.probe, "__name__", repr(defn.probe)),
                blender_version=_version_str(blender_version),
                elapsed_ms=f"{elapsed:.2f}",
            )
            return False

    return True


def _detect_api_anomalies(
    computed:        Dict[str, bool],
    blender_version: VersionTuple,
) -> List[str]:
    """
    Check computed flag values against _ANOMALY_PAIRS.
    Returns list of human-readable anomaly descriptions.
    v3: each anomaly also writes a FORENSIC_EVENT.
    """
    anomalies: List[str] = []

    for pair in _ANOMALY_PAIRS:
        a_val = computed.get(pair.flag_a, False)
        b_val = computed.get(pair.flag_b, False)

        if a_val and not b_val:
            msg = (
                f"API ANOMALY on Blender {_version_str(blender_version)}: "
                f"'{pair.flag_a}'=True but '{pair.flag_b}'=False. "
                f"Reason: {pair.reason}"
            )
            anomalies.append(msg)
            _log.warning(msg)
            _log_forensic(
                "ANOMALY",
                f"'{pair.flag_a}'=True but '{pair.flag_b}'=False",
                flag_a=pair.flag_a,
                flag_b=pair.flag_b,
                blender_version=_version_str(blender_version),
            )

    return anomalies


def _compute_all_flags(
    blender_version: VersionTuple,
) -> Tuple["FeatureSet", Dict[str, Any], List[str]]:
    """
    Evaluate all feature definitions. Returns (FeatureSet, metrics, anomalies).
    Raises OnixeyHardRequirementError if any fallback_ok=False flag is False.
    """
    computed:      Dict[str, bool]           = {}
    hard_failures: List[_FeatureDefinition]  = []
    metrics:       Dict[str, Any]            = {
        "blender_version":           _version_str(blender_version),
        "total_flags":               len(_FEATURE_DEFINITIONS),
        "enabled_flags":             0,
        "disabled_flags":            0,
        "hard_requirements_met":     0,
        "hard_requirements_total":   sum(1 for d in _FEATURE_DEFINITIONS if not d.fallback_ok),
        "probed_flags":              0,
        "probe_failures":            0,
        "api_anomalies_detected":    0,
        "init_call_count":           _get_init_count(),
        "init_duration_ms":          0.0,
        "schema_version":            _REGISTRY_VERSION,
    }

    t_start = time.perf_counter()

    for defn in _FEATURE_DEFINITIONS:
        result          = _compute_single_flag(defn, blender_version, _probe_cache, metrics)
        computed[defn.key] = result

        if result:
            metrics["enabled_flags"] += 1
            if not defn.fallback_ok:
                metrics["hard_requirements_met"] += 1
        else:
            metrics["disabled_flags"] += 1
            if not defn.fallback_ok:
                hard_failures.append(defn)
                _log.error(
                    "HARD REQUIREMENT FAILED: '%s' unavailable in Blender %s. %s",
                    defn.key, _version_str(blender_version), defn.description,
                )
                _log_forensic(
                    "HARD_FAIL",
                    f"Hard requirement '{defn.key}' not met",
                    flag_key=defn.key,
                    blender_version=_version_str(blender_version),
                )

    anomalies = _detect_api_anomalies(computed, blender_version)
    metrics["api_anomalies_detected"] = len(anomalies)

    elapsed = (time.perf_counter() - t_start) * 1000.0
    metrics["init_duration_ms"] = round(elapsed, 3)

    if hard_failures:
        names = ", ".join(d.key for d in hard_failures)
        raise OnixeyHardRequirementError(
            f"Onixey V3 cannot initialize: {len(hard_failures)} hard requirement(s) "
            f"not met by Blender {_version_str(blender_version)}: [{names}]. "
            f"Please upgrade Blender to at least 4.2.0."
        )

    enabled_keys  = [k for k, v in computed.items() if v]
    disabled_keys = [k for k, v in computed.items() if not v]
    _log.info(
        "FeatureFlags initialized | Blender %s | "
        "enabled=%d disabled=%d probed=%d anomalies=%d duration=%.2fms "
        "(schema_v%d init#%d)",
        _version_str(blender_version),
        len(enabled_keys), len(disabled_keys),
        metrics["probed_flags"], len(anomalies),
        elapsed, _REGISTRY_VERSION, metrics["init_call_count"],
    )
    _log.debug("Enabled flags:  %s", enabled_keys)
    _log.debug("Disabled flags: %s", disabled_keys)

    if anomalies:
        _log.warning(
            "FeatureFlags: %d API anomaly/ies detected. "
            "Call get_api_anomalies() for full details.",
            len(anomalies),
        )

    return FeatureSet(**computed), metrics, anomalies


def _version_str(v: VersionTuple) -> str:
    return ".".join(str(x) for x in v)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 7 — PUBLIC LIFECYCLE API
# ──────────────────────────────────────────────────────────────────────────────

def initialize(blender_version: Optional[VersionTuple] = None) -> "FeatureSet":
    """
    Initialize the feature flag singleton.

    MUST be called exactly once per Blender session, from core/compat.py
    inside validate_environment().

    Args:
        blender_version:
            Version to evaluate against. When None, read from bpy.app.version.
            Providing an explicit version enables unit testing without Blender.

    Returns:
        The populated, frozen FeatureSet singleton.

    Raises:
        FeatureFlagsAlreadyInitializedError: double-init without reset().
        OnixeyHardRequirementError:          hard requirements not met.
        ValueError:                          invalid blender_version shape.
        ImportError:                         bpy unavailable with version=None.
    """
    global _singleton, _probe_cache, _integrity_metrics, _detected_anomalies

    # ── Double-init guard ─────────────────────────────────────────────────────
    if _singleton is not _UNINITIALIZED:
        raise FeatureFlagsAlreadyInitializedError(
            "feature_flags.initialize() called twice without reset(). "
            "Call feature_flags.reset() in unregister() before re-initializing. "
            f"Singleton type: {type(_singleton).__name__}. "
            f"Total init calls this session: {_get_init_count()}."
        )

    # ── Hot-reload counter ────────────────────────────────────────────────────
    count = _increment_init_count()

    if count > _HOT_RELOAD_WARN_THRESHOLD:
        _log.warning(
            "HOT RELOAD WARNING: feature_flags.initialize() called %d times in "
            "this Python session. Likely F8-loop or init/reset lifecycle bug. "
            "Flags will be re-detected cleanly.", count,
        )
        _log_forensic(
            "HOT_RELOAD",
            f"initialize() called {count} times in this session "
            f"(threshold={_HOT_RELOAD_WARN_THRESHOLD})",
            init_call_count=count,
        )

    # ── Version resolution ────────────────────────────────────────────────────
    if blender_version is None:
        import bpy as _bpy
        blender_version = tuple(_bpy.app.version[:3])  # type: ignore[assignment]

    if (
        not isinstance(blender_version, tuple)
        or len(blender_version) != 3
        or not all(isinstance(x, int) for x in blender_version)
    ):
        raise ValueError(
            f"feature_flags.initialize(): blender_version must be a 3-int tuple, "
            f"got {blender_version!r} ({type(blender_version).__name__})."
        )

    # ── Main computation ──────────────────────────────────────────────────────
    result_flags, result_metrics, result_anomalies = _compute_all_flags(blender_version)

    # ── Atomic commit ─────────────────────────────────────────────────────────
    _singleton          = result_flags
    _integrity_metrics  = result_metrics
    _detected_anomalies = result_anomalies

    _log_forensic(
        "INIT",
        f"Singleton initialized for Blender {_version_str(blender_version)}",
        blender_version=_version_str(blender_version),
        init_call_count=count,
        enabled_flags=result_metrics["enabled_flags"],
        disabled_flags=result_metrics["disabled_flags"],
        duration_ms=f"{result_metrics['init_duration_ms']:.2f}",
    )

    return _singleton


def reset() -> None:
    """
    Reset the singleton, allowing initialize() to be called again.

    MUST be called from core/compat.py → reset_validation_state() →
    called from __init__.py unregister().

    IDEMPOTENT: calling when already uninitialized emits DEBUG, not WARNING.
    v3: double-reset is detected and forensically logged at DEBUG level,
    but does NOT raise (preserves the original idempotent contract).

    After reset():
        • _singleton          → _UNINITIALIZED
        • _probe_cache        → {}
        • _probe_stats        → {}
        • _forensic_event_log → preserved (forensic continuity across cycles)
        • _integrity_metrics  → {}
        • _detected_anomalies → []
        • _init_call_count    → preserved (cross-reload forensic metric)
    """
    global _singleton, _probe_cache, _integrity_metrics, _detected_anomalies

    was_initialized = _singleton is not _UNINITIALIZED

    if not was_initialized:
        # Idempotent path — already reset.
        _log.debug(
            "feature_flags.reset() called on already-uninitialized singleton "
            "(double-reset or early cleanup). Safe — no-op."
        )
        # v3: log to forensic buffer at DEBUG — visible to get_forensic_event_log()
        _log_forensic(
            "RESET",
            "reset() called while already uninitialized (double-reset — no-op)",
            was_initialized=False,
        )
        return

    _singleton          = _UNINITIALIZED
    _probe_cache        = {}
    _probe_stats.clear()          # v3: clear per-probe statistics
    _integrity_metrics  = {}
    _detected_anomalies = []
    # Note: _forensic_event_log is intentionally NOT cleared — forensic continuity.

    _log_forensic(
        "RESET",
        "Singleton reset cleanly",
        was_initialized=True,
        init_count_preserved=_get_init_count(),
    )
    _log.debug(
        "FeatureFlags reset. init_count_preserved=%d. "
        "Re-initialization required before next use.",
        _get_init_count(),
    )


def is_initialized() -> bool:
    """Return True if initialize() has been called and reset() has not since."""
    return _singleton is not _UNINITIALIZED


def assert_initialized(caller: str = "") -> None:
    """
    Assert the singleton is initialized, raising a detailed error if not.

    Raises:
        FeatureFlagsNotInitializedError
    """
    if _singleton is _UNINITIALIZED:
        loc = f" (caller: {caller})" if caller else ""
        raise FeatureFlagsNotInitializedError(
            f"feature_flags: singleton not initialized{loc}. "
            "Ensure core/compat.validate_environment() runs before this code path."
        )


def get_flags() -> "FeatureSet":
    """
    Return the populated, frozen FeatureSet singleton.

    Raises:
        FeatureFlagsNotInitializedError: if called before initialize().
        FeatureFlagsCorruptStateError:   if _singleton is wrong type.
    """
    if _singleton is _UNINITIALIZED:
        raise FeatureFlagsNotInitializedError(
            "feature_flags.get_flags() called before initialize(). "
            "Ensure core/compat.validate_environment() runs first."
        )

    if not isinstance(_singleton, FeatureSet):
        raise FeatureFlagsCorruptStateError(
            f"feature_flags._singleton has unexpected type "
            f"{type(_singleton).__name__!r} (expected FeatureSet). "
            f"Module state is corrupted. Recovery: reset() then initialize()."
        )

    return _singleton


def get_registry_version() -> int:
    """Return the schema version of the feature definitions table."""
    return _REGISTRY_VERSION


def get_feature_descriptions() -> Dict[str, str]:
    """
    Return {flag_key: description} for all defined flags.
    Always safe — does not require initialize().
    """
    return {defn.key: defn.description for defn in _FEATURE_DEFINITIONS}


def get_integrity_metrics() -> Dict[str, Any]:
    """
    Return a copy of the integrity metrics computed during initialize().
    Returns empty dict if not yet initialized (safe).
    """
    return dict(_integrity_metrics)


def get_api_anomalies() -> List[str]:
    """
    Return anomaly descriptions detected during initialize().
    Empty list if not initialized or no anomalies found.
    """
    return list(_detected_anomalies)


def get_probe_cache_snapshot() -> Dict[str, bool]:
    """
    Return a snapshot of the raw probe result cache keyed by flag key.
    For per-probe hit/miss stats use get_probe_statistics() instead.
    """
    return dict(_probe_cache)


# ──────────────────────────────────────────────────────────────────────────────
# v3 — EXTENDED DIAGNOSTIC API
# ──────────────────────────────────────────────────────────────────────────────

def get_probe_statistics() -> Dict[str, Dict[str, Any]]:
    """
    Return per-probe invocation statistics accumulated since the last reset().

    Returns a dict keyed by flag key. Each value is a plain dict with:
        hits        — times served from cache
        misses      — times probe callable was actually invoked
        failures    — times probe raised an exception
        total_calls — hits + misses
        hit_rate    — hits / total_calls (0.0 if no calls)
        total_ms    — cumulative probe execution time (ms)
        avg_ms      — average execution time per invocation
        last_result — most recent probe return value (True/False/None)
        last_ms     — most recent probe execution time (ms)

    Returns empty dict if no probes have been run this session (safe).

    Usage:
        stats = get_probe_statistics()
        slow_probes = {k: v for k, v in stats.items() if v["avg_ms"] > 10}
    """
    return {
        key: {
            "hits":        ps.hits,
            "misses":      ps.misses,
            "failures":    ps.failures,
            "total_calls": ps.total_calls,
            "hit_rate":    round(ps.hit_rate, 4),
            "total_ms":    round(ps.total_ms, 3),
            "avg_ms":      round(ps.avg_ms, 3),
            "last_result": ps.last_result,
            "last_ms":     round(ps.last_ms, 3),
        }
        for key, ps in _probe_stats.items()
    }


def get_forensic_event_log() -> List[Dict[str, Any]]:
    """
    Return all events in the forensic circular buffer as plain dicts.

    Each dict has:
        event_type — SCREAMING_SNAKE_CASE type string
        timestamp  — ISO format wall-clock string
        message    — human-readable description
        data       — structured payload dict (varies by event type)

    The buffer holds the last _FORENSIC_LOG_MAX events across the session.
    NOT cleared by reset() — preserves forensic continuity across register/
    unregister cycles (crucial for detecting multi-cycle corruption patterns).

    Returns empty list if no events recorded (safe before initialize()).
    """
    return [
        {
            "event_type": ev.event_type,
            "timestamp":  ev.timestamp,
            "message":    ev.message,
            "data":       dict(ev.data),
        }
        for ev in _forensic_event_log
    ]


def get_live_integrity_check() -> Dict[str, Any]:
    """
    Run a lightweight post-init self-test and return a structured report.

    Verifies invariants that SHOULD hold while the singleton is live:
        singleton_type_ok    — _singleton is FeatureSet, not corrupted
        flag_count_matches   — FeatureSet has exactly len(_FEATURE_DEFINITIONS) bool attrs
        all_attrs_bool       — every flag attribute on the singleton is a bool
        probe_cache_subset   — every key in _probe_cache is a known flag key
        no_negative_stats    — all ProbeStats counters are non-negative
        hard_req_consistency — hard-req flags that are True in integrity_metrics
                               match what the live singleton actually returns

    Returns a dict with:
        passed   — True if all checks passed
        checks   — dict of {check_name: bool}
        issues   — list of human-readable problem descriptions
        ts       — ISO timestamp of this check run

    If not initialized, returns {"passed": False, "issues": ["Not initialized"]}.

    v3 addition. Does not raise. Never modifies state.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    if _singleton is _UNINITIALIZED:
        return {"passed": False, "checks": {}, "issues": ["Not initialized"], "ts": ts}

    checks: Dict[str, bool] = {}
    issues: List[str]       = []

    # Check 1: singleton type
    checks["singleton_type_ok"] = isinstance(_singleton, FeatureSet)
    if not checks["singleton_type_ok"]:
        issues.append(
            f"_singleton is {type(_singleton).__name__}, expected FeatureSet. "
            "State corruption detected."
        )

    # Check 2: flag count matches definitions
    if checks["singleton_type_ok"]:
        live_count  = sum(1 for k in vars(FeatureSet).get("__dataclass_fields__", {})
                         if not k.startswith("_"))
        defn_count  = len(_FEATURE_DEFINITIONS)
        checks["flag_count_matches"] = live_count == defn_count
        if not checks["flag_count_matches"]:
            issues.append(
                f"FeatureSet has {live_count} fields but _FEATURE_DEFINITIONS has "
                f"{defn_count} entries. Schema/class mismatch — possible hot-reload issue."
            )
    else:
        checks["flag_count_matches"] = False

    # Check 3: all attributes are bools
    if checks["singleton_type_ok"]:
        non_bool = [
            defn.key for defn in _FEATURE_DEFINITIONS
            if not isinstance(getattr(_singleton, defn.key, None), bool)
        ]
        checks["all_attrs_bool"] = len(non_bool) == 0
        if non_bool:
            issues.append(f"Non-bool flag values detected: {non_bool}")
    else:
        checks["all_attrs_bool"] = False

    # Check 4: probe cache keys are all valid flag keys
    known_keys = {defn.key for defn in _FEATURE_DEFINITIONS}
    rogue_cache_keys = set(_probe_cache.keys()) - known_keys
    checks["probe_cache_subset"] = len(rogue_cache_keys) == 0
    if rogue_cache_keys:
        issues.append(
            f"_probe_cache contains unknown flag keys: {sorted(rogue_cache_keys)}. "
            "Possibly from a previous schema version — reset() will clear."
        )

    # Check 5: no negative probe stats counters
    neg_stats = [
        key for key, ps in _probe_stats.items()
        if ps.hits < 0 or ps.misses < 0 or ps.failures < 0 or ps.total_ms < 0
    ]
    checks["no_negative_stats"] = len(neg_stats) == 0
    if neg_stats:
        issues.append(f"Negative ProbeStats counters detected for: {neg_stats}")

    # Check 6: hard requirement consistency
    if checks["singleton_type_ok"] and _integrity_metrics:
        met_in_metrics = _integrity_metrics.get("hard_requirements_met", -1)
        met_live = sum(
            1 for defn in _FEATURE_DEFINITIONS
            if not defn.fallback_ok and getattr(_singleton, defn.key, False)
        )
        checks["hard_req_consistency"] = met_in_metrics == met_live
        if not checks["hard_req_consistency"]:
            issues.append(
                f"Hard requirement count mismatch: metrics says {met_in_metrics}, "
                f"live singleton has {met_live}. Possible mid-session mutation."
            )
            # v3: log as forensic LIVE_CHECK event
            _log_forensic(
                "LIVE_CHECK",
                "Hard requirement count mismatch between metrics and live singleton",
                metrics_count=met_in_metrics,
                live_count=met_live,
            )
    else:
        checks["hard_req_consistency"] = True  # Cannot check without metrics

    passed = all(checks.values()) and len(issues) == 0

    if not passed:
        _log.warning(
            "get_live_integrity_check(): %d issue(s) found — %s",
            len(issues), issues,
        )

    return {
        "passed":  passed,
        "checks":  checks,
        "issues":  issues,
        "ts":      ts,
    }


def get_runtime_compat_matrix() -> Dict[str, Dict[str, Any]]:
    """
    Return a human-readable compatibility matrix for all defined flags.

    Each key is a flag name. Each value is a dict:
        since_str     — e.g. "2.80.0"
        until_str     — e.g. "4.3.0" or "open"
        fallback_ok   — bool
        has_probe     — bool
        current_value — bool (current Blender) or None if not initialized
        speculative   — bool (True for blender5_* forward-looking flags)

    Intended for:
        - Debugging compatibility issues across Blender versions
        - Generating documentation tables
        - The "Compatibility Matrix" section of the debug panel (Sprint 3)
        - Bug reports ("here is what flags are available on my Blender build")

    Always safe to call — does not require initialize(). current_value is
    None when not initialized.
    """
    fs: Optional["FeatureSet"] = _singleton if is_initialized() else None  # type: ignore[assignment]

    return {
        defn.key: {
            "since_str":     _version_str(defn.since),
            "until_str":     _version_str(defn.until) if defn.until else "open",
            "fallback_ok":   defn.fallback_ok,
            "has_probe":     defn.probe is not None,
            "current_value": getattr(fs, defn.key, None) if fs is not None else None,
            "speculative":   defn.key.startswith(_SPECULATIVE_PREFIX),
        }
        for defn in _FEATURE_DEFINITIONS
    }


def get_hard_requirements() -> List[Dict[str, Any]]:
    """
    Return the list of hard-requirement flags (fallback_ok=False) with status.

    Each entry is a dict:
        key           — flag key
        since_str     — minimum Blender version string
        description   — flag description
        current_value — bool if initialized, None if not
        met           — True if current_value is True, False/None otherwise

    Used by:
        - core/compat_checks.py to emit targeted error messages
        - validation/healthcheck.py hard requirement check
        - The "Startup Requirements" section of the debug panel (Sprint 3)

    Always safe to call — does not require initialize().
    """
    fs: Optional["FeatureSet"] = _singleton if is_initialized() else None  # type: ignore[assignment]
    results: List[Dict[str, Any]] = []

    for defn in _FEATURE_DEFINITIONS:
        if defn.fallback_ok:
            continue
        current_value = getattr(fs, defn.key, None) if fs is not None else None
        results.append({
            "key":           defn.key,
            "since_str":     _version_str(defn.since),
            "description":   defn.description,
            "current_value": current_value,
            "met":           current_value is True,
        })

    return results


def flag_added_since(version: VersionTuple) -> List[str]:
    """
    Return keys of flags whose `since` is greater than the given version.

    Use case: migration helpers that need to know which features became
    available after a specific Blender version.

    Args:
        version: e.g. _v(4, 0, 0) — returns flags added after 4.0.

    Returns:
        Sorted list of flag keys introduced after `version`.

    Example:
        new_in_44 = flag_added_since(_v(4, 3, 99))
        # → ['action_slot_system', 'blender5_*', ...]

    Always safe — does not require initialize().
    """
    return sorted(
        defn.key
        for defn in _FEATURE_DEFINITIONS
        if defn.since > version
    )


def get_diagnostic_report() -> str:
    """
    Return a comprehensive human-readable diagnostic string.

    v3 additions over v2:
        • Hot-reload timeline (timestamps of each initialize() call)
        • Forensic event log section
        • Per-probe statistics section
        • Live integrity check section
        • Runtime compat matrix section (speculative flags highlighted)

    Always safe to call. Handles uninitialized state gracefully.
    """
    SEP_THICK = "═" * 64
    SEP_THIN  = "─" * 64
    lines: List[str] = []

    lines.append(f"Onixey V3 FeatureFlags Diagnostic Report (schema_v{_REGISTRY_VERSION})")
    lines.append(SEP_THICK)

    if _singleton is _UNINITIALIZED:
        lines.append("STATUS : NOT INITIALIZED")
        lines.append(f"Init calls this session : {_get_init_count()}")
        lines.append(f"Total flags in schema   : {len(_FEATURE_DEFINITIONS)}")
        lines.append("")
        lines.append(SEP_THIN)
        lines.append("HOT-RELOAD TIMELINE:")
        for ts in _get_init_timeline():
            lines.append(f"  {ts}")
        lines.append("")
        # Show forensic log even before initialization
        ev_log = get_forensic_event_log()
        if ev_log:
            lines.append(SEP_THIN)
            lines.append(f"FORENSIC EVENT LOG ({len(ev_log)} events):")
            for ev in ev_log:
                lines.append(f"  {ev['timestamp']}  {ev['event_type']:<12} {ev['message']}")
        return "\n".join(lines)

    m = get_integrity_metrics()
    lines.append(
        f"Blender : {m.get('blender_version', '?')}  |  "
        f"init#{m.get('init_call_count', '?')}  |  "
        f"{m.get('enabled_flags', 0)} enabled  "
        f"{m.get('disabled_flags', 0)} disabled  |  "
        f"{m.get('api_anomalies_detected', 0)} anomalies  |  "
        f"{m.get('init_duration_ms', 0):.2f}ms"
    )

    # ── Flag status table ──────────────────────────────────────────────────────
    lines.append(SEP_THIN)
    lines.append(f"{'FLAG':<46} {'STATUS':<6} {'REQ':<4}  {'PROBED':<6}  DESCRIPTION")
    lines.append(SEP_THIN)

    fs = get_flags()
    probe_snap = get_probe_cache_snapshot()

    for defn in _FEATURE_DEFINITIONS:
        value      = getattr(fs, defn.key)
        status     = "OK " if value else "--"
        req        = "HARD" if not defn.fallback_ok else "soft"
        probed     = "yes" if defn.key in probe_snap else "no"
        speculative = " *SPEC*" if defn.key.startswith(_SPECULATIVE_PREFIX) else ""
        desc_trunc = defn.description[:55] + "…" if len(defn.description) > 55 else defn.description
        lines.append(
            f"  {defn.key:<44}  [{status}]  {req:<4}  {probed:<6}  {desc_trunc}{speculative}"
        )

    # ── Anomalies ──────────────────────────────────────────────────────────────
    anomalies = get_api_anomalies()
    if anomalies:
        lines.append("")
        lines.append(SEP_THIN)
        lines.append(f"API ANOMALIES ({len(anomalies)} detected):")
        for a in anomalies:
            lines.append(f"  ⚠  {a}")

    # ── Probe cache ────────────────────────────────────────────────────────────
    if probe_snap:
        lines.append("")
        lines.append(SEP_THIN)
        lines.append("PROBE CACHE SNAPSHOT:")
        for k, v in sorted(probe_snap.items()):
            lines.append(f"  {k:<44}  {'True' if v else 'False'}")

    # ── v3: Probe statistics ───────────────────────────────────────────────────
    probe_stats_snap = get_probe_statistics()
    if probe_stats_snap:
        lines.append("")
        lines.append(SEP_THIN)
        lines.append(
            f"{'PROBE STATISTICS':<44}  {'HITS':>5}  {'MISSES':>6}  {'FAILS':>5}  "
            f"{'HIT%':>5}  {'AVG_MS':>7}"
        )
        for k, ps in sorted(probe_stats_snap.items()):
            lines.append(
                f"  {k:<44}  {ps['hits']:>5}  {ps['misses']:>6}  {ps['failures']:>5}  "
                f"{ps['hit_rate']*100:>4.0f}%  {ps['avg_ms']:>7.3f}"
            )

    # ── v3: Live integrity check ───────────────────────────────────────────────
    live = get_live_integrity_check()
    lines.append("")
    lines.append(SEP_THIN)
    status_str = "PASS" if live["passed"] else "FAIL"
    lines.append(f"LIVE INTEGRITY CHECK [{status_str}]  (at {live['ts']})")
    for check_name, check_ok in live["checks"].items():
        lines.append(f"  {'✔' if check_ok else '✖'} {check_name}")
    for issue in live["issues"]:
        lines.append(f"  ⚠  {issue}")

    # ── v3: Hot-reload timeline ────────────────────────────────────────────────
    timeline = _get_init_timeline()
    if timeline:
        lines.append("")
        lines.append(SEP_THIN)
        lines.append(f"HOT-RELOAD TIMELINE ({len(timeline)} init call(s)):")
        for i, ts in enumerate(timeline, 1):
            lines.append(f"  #{i:02d}  {ts}")

    # ── v3: Forensic event log ─────────────────────────────────────────────────
    ev_log = get_forensic_event_log()
    if ev_log:
        lines.append("")
        lines.append(SEP_THIN)
        lines.append(f"FORENSIC EVENT LOG (last {len(ev_log)} of max {_FORENSIC_LOG_MAX}):")
        for ev in ev_log:
            lines.append(f"  {ev['timestamp']}  {ev['event_type']:<12} {ev['message']}")

    # ── Integrity metrics ──────────────────────────────────────────────────────
    lines.append("")
    lines.append(SEP_THIN)
    lines.append("INTEGRITY METRICS:")
    for k, v in sorted(m.items()):
        lines.append(f"  {k:<32}  {v}")

    lines.append(SEP_THIN)
    hard_met   = m.get("hard_requirements_met", "?")
    hard_total = m.get("hard_requirements_total", "?")
    lines.append(f"Hard requirements : {hard_met}/{hard_total}")
    lines.append(f"Probe failures    : {m.get('probe_failures', 0)}")
    lines.append(f"Hot-reload count  : {m.get('init_call_count', '?')}")
    lines.append(f"Schema version    : v{_REGISTRY_VERSION}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 8 — PUBLIC SUPPORTS_*() HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _flag(key: str) -> bool:
    """Internal micro-helper: returns the bool value of a flag by key."""
    return getattr(get_flags(), key)


# ── Core evaluation ───────────────────────────────────────────────────────────

def supports_evaluated_get() -> bool:
    """
    True if obj.evaluated_get(depsgraph) is safe to call.
    Hard requirement. Onixey does not load if False.
    Use: analysis/motion_path.py, compat.get_evaluated_object_safe().
    """
    return _flag("evaluated_get")


def supports_depsgraph_object_instances() -> bool:
    """
    True if depsgraph.object_instances can be iterated.
    Fallback: iterate bpy.data.objects directly.
    """
    return _flag("depsgraph_object_instances")


def supports_depsgraph_updates() -> bool:
    """
    True if depsgraph_update_post handler is available.
    Fallback: skip cache invalidation on depsgraph changes.
    """
    return _flag("depsgraph_updates")


# ── Handler system ────────────────────────────────────────────────────────────

def supports_handler_depsgraph_arg() -> bool:
    """
    True if handlers receive (scene, depsgraph) signature.
    Hard requirement.
    """
    return _flag("handler_depsgraph_arg")


def supports_undo_post_handler() -> bool:
    """
    True if bpy.app.handlers.undo_post is available.
    Hard requirement — AAA Rule 8: cache MUST invalidate on Ctrl+Z.
    """
    return _flag("undo_post_handler")


def supports_redo_post_handler() -> bool:
    """
    True if bpy.app.handlers.redo_post is available.
    Fallback: cache not invalidated after Ctrl+Shift+Z.
    """
    return _flag("redo_post_handler")


def supports_load_post_handler() -> bool:
    """
    True if bpy.app.handlers.load_post is available.
    Hard requirement — needed for migration and cache reset on .blend open.
    """
    return _flag("load_post_handler")


# ── Undo system ──────────────────────────────────────────────────────────────

def supports_undo_grouped() -> bool:
    """
    True if bl_options={'UNDO_GROUPED'} is supported.
    Fallback: standard UNDO — more undo stack entries.
    """
    return _flag("undo_grouped")


# ── Rendering / viewport ──────────────────────────────────────────────────────

def supports_gpu_shader() -> bool:
    """
    True if gpu.shader is available for viewport overlays.
    Fallback: results shown in panel only, no viewport drawing.
    """
    return _flag("gpu_shader")


def supports_gpu_shader_3d_uniform_color() -> bool:
    """
    True if gpu.shader.from_builtin('UNIFORM_COLOR') is available (3.4+).
    Fallback: legacy '3D_UNIFORM_COLOR' builtin name.
    """
    return _flag("gpu_shader_3d_uniform_color")


# ── NLA / Animation system ────────────────────────────────────────────────────

def supports_nla_track_mute() -> bool:
    """
    True if NLATrack.mute is writable.
    Fallback: apply corrections directly without NLA preview track.
    """
    return _flag("nla_track_mute")


def supports_action_slot_system() -> bool:
    """
    True if Blender 4.4+ Action Slot API is available.
    When True: use slot API. When False: legacy obj.animation_data.action.
    """
    return _flag("action_slot_system")


# ── Pose / Armature ───────────────────────────────────────────────────────────

def supports_pose_bone_matrix_channel() -> bool:
    """
    True if PoseBone.matrix_channel is accessible.
    Fallback: use PoseBone.matrix (less precise for IK singularity detection).
    """
    return _flag("pose_bone_matrix_channel")


def supports_bone_color_api() -> bool:
    """
    True if Bone.color / PoseBone.color API is available (4.0+).
    Fallback: no per-bone visual flagging; rely on panel issue list only.
    """
    return _flag("bone_color_api")


# ── Message bus ──────────────────────────────────────────────────────────────

def supports_msgbus() -> bool:
    """
    True if bpy.msgbus.subscribe_rna / clear_by_owner is available.
    Fallback: poll active object on every frame_change_post.
    """
    return _flag("msgbus")


# ── Asset library ─────────────────────────────────────────────────────────────

def supports_asset_library_api() -> bool:
    """
    True if Blender asset library API is available (3.5+).
    Fallback: no preset sharing UI; local .json files only.
    """
    return _flag("asset_library_api")


# ── Blender 5.x forward-looking ──────────────────────────────────────────────

def supports_blender5_evaluated_get_context() -> bool:
    """
    SPECULATIVE — False until Blender 5.x confirms.
    When True, evaluated_get() requires context as first argument.
    """
    return _flag("blender5_evaluated_get_context")


def supports_blender5_gpu_new_shader_api() -> bool:
    """
    SPECULATIVE — False until Blender 5.x confirms.
    When True, GPU shader module API has been refactored.
    """
    return _flag("blender5_gpu_new_shader_api")


def supports_blender5_handler_new_signature() -> bool:
    """
    SPECULATIVE — False until Blender 5.x confirms.
    When True, handler functions use the new call signature.
    """
    return _flag("blender5_handler_new_signature")


def supports_blender5_depsgraph_new_api() -> bool:
    """
    SPECULATIVE — False until Blender 5.x confirms.
    When True, use compat.iter_depsgraph_objects_safe() for iteration.
    """
    return _flag("blender5_depsgraph_new_api")


def supports_blender5_undo_system_redesign() -> bool:
    """
    SPECULATIVE — False until Blender 5.x confirms.
    When True, runtime/cache.py uses the new undo handler registration path.
    """
    return _flag("blender5_undo_system_redesign")


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 9 — MODULE-LEVEL INTEGRITY CHECKS (import-time, zero bpy access)
# ──────────────────────────────────────────────────────────────────────────────

def _validate_definitions_integrity() -> None:
    """
    Full schema integrity check executed once at import time.
    Raises AssertionError with precise diagnostic on any violation.
    Zero bpy access — purely structural.

    v3 additions:
        H. No two definitions have the same probe function object identity
           (different flags must use different probe callables — shared probe
           functions make individual flag diagnostics ambiguous).
        I. All hard-requirement flags (fallback_ok=False) have a machine-readable
           description that is not empty (required for error messages in
           OnixeyHardRequirementError).
    """
    seen_keys:   Set[str]      = set()
    seen_probes: Set[int]      = set()   # id() of probe callables (v3 check H)
    all_keys:    Set[str]      = set()

    for defn in _FEATURE_DEFINITIONS:

        # A: Duplicate key check
        if defn.key in seen_keys:
            raise AssertionError(
                f"[feature_flags] DUPLICATE key: '{defn.key}'. "
                f"Each key in _FEATURE_DEFINITIONS must be unique."
            )
        seen_keys.add(defn.key)
        all_keys.add(defn.key)

        # B: Key naming convention
        if not _KEY_PATTERN.match(defn.key):
            raise AssertionError(
                f"[feature_flags] Invalid key format: '{defn.key}'. "
                f"Must match: {_KEY_PATTERN.pattern}"
            )

        # C: Speculative prefix
        if defn.since[0] >= 90 and not defn.key.startswith(_SPECULATIVE_PREFIX):
            raise AssertionError(
                f"[feature_flags] Speculative flag '{defn.key}' (since={defn.since}) "
                f"must use prefix '{_SPECULATIVE_PREFIX}'."
            )

        # F: Probe callability
        if defn.probe is not None and not callable(defn.probe):
            raise AssertionError(
                f"[feature_flags] Flag '{defn.key}' has non-callable probe: {defn.probe!r}."
            )

        # G: Impossible version range
        if defn.until is not None and defn.since > defn.until:
            raise AssertionError(
                f"[feature_flags] Flag '{defn.key}' has impossible version range: "
                f"since={defn.since} > until={defn.until}."
            )

        # H (v3): No two flags share the same probe callable identity
        if defn.probe is not None:
            probe_id = id(defn.probe)
            if probe_id in seen_probes:
                raise AssertionError(
                    f"[feature_flags] Flag '{defn.key}' reuses a probe callable "
                    f"already assigned to another flag "
                    f"({getattr(defn.probe, '__name__', repr(defn.probe))}). "
                    f"Each flag must have its own dedicated probe function."
                )
            seen_probes.add(probe_id)

        # I (v3): Hard requirements must have non-empty description
        if not defn.fallback_ok and not defn.description.strip():
            raise AssertionError(
                f"[feature_flags] Hard requirement flag '{defn.key}' has empty description. "
                f"Hard requirements must have a description for user-facing error messages."
            )

    # D: FeatureSet attribute coverage
    dummy = FeatureSet()
    for defn in _FEATURE_DEFINITIONS:
        if not hasattr(dummy, defn.key):
            raise AssertionError(
                f"[feature_flags] FeatureSet missing attribute '{defn.key}'. "
                f"_build_feature_set_class() failed to include this definition."
            )
        if not isinstance(getattr(dummy, defn.key), bool):
            raise AssertionError(
                f"[feature_flags] FeatureSet attribute '{defn.key}' is not bool."
            )

    # E: Anomaly pair key validity
    for pair in _ANOMALY_PAIRS:
        for attr in (pair.flag_a, pair.flag_b):
            if attr not in all_keys:
                raise AssertionError(
                    f"[feature_flags] _ANOMALY_PAIRS references unknown key '{attr}' "
                    f"in pair ({pair.flag_a!r}, {pair.flag_b!r})."
                )

    _log.debug(
        "feature_flags schema integrity OK: %d flags, %d anomaly pairs, "
        "%d with probes, %d hard requirements, schema_v%d",
        len(_FEATURE_DEFINITIONS),
        len(_ANOMALY_PAIRS),
        sum(1 for d in _FEATURE_DEFINITIONS if d.probe is not None),
        sum(1 for d in _FEATURE_DEFINITIONS if not d.fallback_ok),
        _REGISTRY_VERSION,
    )


# Execute integrity check once at import. Zero bpy access — purely structural.
_validate_definitions_integrity()
