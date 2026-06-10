"""
onixey3/runtime/cache.py

Multi-Tier Analysis Cache with Undo-Safe Invalidation Contracts.

SINGLE RESPONSIBILITY
─────────────────────
Provide fast, invalidatable storage for pre-computed analysis results
so that overlays and UI can read data at O(1) per frame instead of
re-running analysis (which requires expensive frame_set() calls).

CACHE TIER DESIGN
─────────────────
Three tiers with independent TTLs, capacities, and invalidation triggers:

  L1 — Frame tier  (fastest, most volatile):
       Stores evaluated world-space data for the CURRENT frame only.
       Invalidated: on every frame_change_post OR depsgraph_update_post.
       Purpose: avoid redundant evaluated_get() calls within one frame
                when multiple overlay passes or operator checks need the
                same bone position in the same frame.
       TTL: 1.0s (safety net; effectively frame-scoped)
       Capacity: 64 entries
       Key format: "L1:{obj_name}:{bone_name}:pos"
                   "L1:{obj_name}:matrix"

  L2 — Range tier  (analysis results):
       Stores computed metrics (arc, spacing, energy, IK/FK) for a frame range.
       Invalidated: when ANIMATION DATA CHANGES (depsgraph_update_post) or
                    when undo/redo restores a different animation state.
       Purpose: expensive world-space analysis runs once, overlays read cheap.
       TTL: 120.0s (2 minutes)
       Capacity: 256 entries
       Key format: "L2:{obj_name}:{analysis_type}:{frame_start}:{frame_end}"
       Example:    "L2:Armature:arc:1:120"

  L3 — Session tier  (metadata, rarely changes):
       Stores rig topology, bone chain maps, IK chain detection.
       Invalidated: when armature structure changes (exit edit mode with bone
                    add/remove, apply modifier, etc.).
       Purpose: topology scans are O(N bones); cache for the session.
       TTL: 600.0s (10 minutes)
       Capacity: 32 entries
       Key format: "L3:{obj_name}:topology"
                   "L3:{obj_name}:ik_chains"

INVALIDATION CONTRACTS
──────────────────────
The following callers are CONTRACTUALLY REQUIRED to call these functions:

  Handler: frame_change_post
      → invalidate_l1()
      Cost: < 0.05ms

  Handler: depsgraph_update_post
      → invalidate_l1()
      → invalidate_l2_for(obj_name) for each changed obj in depsgraph.updates
      Cost: < 0.1ms

  Handler: undo_post / redo_post
      → invalidate_all()
      Reason: Ctrl+Z restores .blend state for any/all objects. We cannot know
              which objects changed. Full invalidation is the only safe choice.
      Cost: < 0.1ms

  Handler: load_post
      → invalidate_all()
      Reason: New scene loaded — all cached data refers to a different file.

  Operator: any operator with bl_options={'UNDO'} that modifies FCurves
      → invalidate_all()  (safe default before execute())
      OR
      → invalidate_l2_for(obj_name)  (targeted, if only one object is modified)

STRONG REFERENCE POLICY
────────────────────────
  DO NOT store bpy objects (Object, Armature, Action, PoseBone, etc.) as
  cache values. They can become invalid after undo, and stale refs leak memory.

  STORE INSTEAD:
      - dict/list of plain Python types (float, int, str, tuple)
      - mathutils.Vector.copy() — copies, not references
      - Custom dataclass instances with only primitive fields

  BAD:   cache_set(key, context.active_object, "L1")
  GOOD:  cache_set(key, {"name": obj.name, "pos": obj.location.copy()}, "L1")

CONCURRENCY
────────────
  Blender is single-threaded. This cache is NOT thread-safe by design.
  Do not use from Python threading.Thread — bpy is not thread-safe either.

DEPENDENCY CONTRACT
───────────────────
  Imports from: core/ (constants only, optional)
  Must NOT import: session, lifecycle, operators, ui, analysis, properties

CHANGELOG
─────────
  3.1.0 — Iteration 2. AAA rewrite with 3-tier architecture, strict invalidation
           contracts, generation counter, diagnostics, and key builders.
  3.2.0 — Two targeted hardening fixes:
           (1) cache_get() deepcopy failure: now raises CacheDeepCopyError and
               evicts the entry instead of silently returning a live reference.
               Eliminates the reference-contamination risk entirely.
           (2) _mark_animation_related_invalid() robustness: added
               _ANIMATION_KEY_PREFIXES (structural prefix match) and
               _ANIMATION_SAFE_SUFFIXES (protected-key allowlist) alongside
               the existing _ANIMATION_KEYWORDS. Pass logic updated to 3-pass
               (prefix → safe-suffix allowlist → keyword scan). New analysis
               types added via key_l2_analysis() are caught automatically by
               Pass 1 without touching this file.
"""

from __future__ import annotations

import copy
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# EXCEPTION TYPES
# ──────────────────────────────────────────────────────────────────────────────

class CacheDeepCopyError(RuntimeError):
    """
    Raised by cache_get() when deepcopy of the cached value fails.

    This is a protective error: instead of returning a live reference that
    a caller might mutate (corrupting the cache for all subsequent readers),
    cache_get() evicts the problematic entry and raises this exception.

    CAUSE
    ──────
    The cached value contains an object that cannot be deep-copied:
        - A bpy object stored in violation of the STRONG REFERENCE POLICY.
        - A generator, coroutine, or lock object.
        - An object whose __deepcopy__ raises intentionally.

    RECOVERY
    ─────────
    The entry is already evicted when this is raised — the next cache_set()
    with the same key will repopulate normally.

    Callers that cannot afford to re-run the analysis should catch this and
    proceed as if cache_get() returned None:

        try:
            result = cache_get(key)
        except CacheDeepCopyError:
            result = None   # will be recomputed by the caller

    ROOT FIX
    ─────────
    Inspect the cache_set() call for this key. Values must be:
        - Plain Python types: dict, list, int, float, str, tuple.
        - Copied mathutils types: Vector.copy(), Matrix.copy().
        - Custom dataclasses with only primitive fields.
    NEVER store bpy objects (Object, Armature, PoseBone, Action, etc.).
    """


# ──────────────────────────────────────────────────────────────────────────────
# TIER CONFIGURATION
# Centralized constants. Change here only — not scattered across callers.
# ──────────────────────────────────────────────────────────────────────────────

# (TTL_seconds, max_entries)
_TIER_CONFIG: Dict[str, Tuple[float, int]] = {
    "L1": (1.0,   64),
    "L2": (120.0, 256),
    "L3": (600.0, 32),
}

_VALID_TIERS = frozenset(_TIER_CONFIG.keys())


# ──────────────────────────────────────────────────────────────────────────────
# ANIMATION-RELATED INVALIDATION
# Keywords used to identify cache keys that store animation-derived data.
# Any key containing one of these terms is considered animation-related
# and will be cleared by _mark_animation_related_invalid().
#
# SCOPE: Runtime Iteration 1 — minimal pattern matching on key strings.
# The keys are already structured by the key builder functions:
#   key_l2_analysis(obj, analysis_type, frame_start, frame_end)
# analysis_type values like "arc", "spacing", "energy", "ikfk", "euler",
# "overlap", "noise", "momentum" — all of these are animation-derived.
# Rather than enumerate every analysis type, we match on the L2 prefix
# plus the specific non-animation keys to EXCLUDE (only L3 topology data
# is safe to keep when animation changes).
# ──────────────────────────────────────────────────────────────────────────────

# Key fragments that identify animation-dependent cache entries.
#
# _ANIMATION_KEYWORDS  — substring match against the key (lowercased).
#   A key is animation-related if it contains any of these strings.
#   Used as the FALLBACK pass for L1/L3 custom keys that fall outside
#   the structured "L2:" prefix (Pass 1 already handles all L2 entries).
#
# _ANIMATION_KEY_PREFIXES  — prefix match (structural, fast, O(1) per prefix).
#   Added in v3.2 to make the detection layer more robust for future
#   analysis types added to key_l2_analysis() without touching this file.
#   Currently mirrors "L2:" (L2 = all frame-range analysis), but future
#   tiers or namespaces can be added here independently of keyword sprawl.
#
# _ANIMATION_SAFE_SUFFIXES — suffix match against the FULL key (lowercased).
#   Keys whose suffix appears in this set are NEVER invalidated, regardless
#   of other matches. Provides a stable allowlist for L3 topology data
#   that must survive animation changes:
#       "L3:{obj}:topology"    → bone chain structure (not animation data)
#       "L3:{obj}:ik_chains"   → IK chain detection (not animation data)
#   Adding a new L3 key that must survive animation changes: append its
#   suffix to _ANIMATION_SAFE_SUFFIXES. No other code needs to change.
#
# EXTENSION GUIDE (for future analysis types):
#   New L2 key via key_l2_analysis()? → Pass 1 catches it automatically.
#   New keyword-based L1/L3 key?      → Add keyword to _ANIMATION_KEYWORDS.
#   New structural prefix?             → Add prefix to _ANIMATION_KEY_PREFIXES.
#   New L3 key safe from animation?    → Add suffix to _ANIMATION_SAFE_SUFFIXES.
_ANIMATION_KEYWORDS: frozenset = frozenset({
    "animation", "fcurve", "keyframe", "action",
    "spacing", "timing", "arc", "energy", "ikfk",
    "euler", "overlap", "noise", "momentum",
})

_ANIMATION_KEY_PREFIXES: frozenset = frozenset({
    "L2:",   # All L2 entries are frame-range analysis — always animation-derived.
    # Add future animation-tier prefixes here, e.g. "L4:" if a fourth tier
    # is introduced for another class of animation-derived data.
})

_ANIMATION_SAFE_SUFFIXES: frozenset = frozenset({
    ":topology",   # L3 bone chain maps — structure, not animation data.
    ":ik_chains",  # L3 IK chain detection — structure, not animation data.
    # Add new L3 structural keys here when introduced. Format: ":<suffix>"
    # where suffix is the last segment of the key returned by key_l3_*().
})


# ──────────────────────────────────────────────────────────────────────────────
# CACHE ENTRY
# Lightweight dataclass. slots=True saves ~50 bytes/entry vs dict.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class _CacheEntry:
    value:     Any    # The cached payload. Must be plain Python types (no bpy refs).
    stored_at: float  # time.monotonic() at insertion. Used for TTL eviction.
    tier:      str    # "L1" | "L2" | "L3"

    def age(self) -> float:
        """Seconds since this entry was stored."""
        return time.monotonic() - self.stored_at

    def is_expired(self, ttl: float) -> bool:
        return self.age() > ttl


# ──────────────────────────────────────────────────────────────────────────────
# CACHE STATE
# Module-level state. Created in _init(). Destroyed in _destroy().
# Never written to between _init() and _destroy() except via public API.
# ──────────────────────────────────────────────────────────────────────────────

_store: Dict[str, _CacheEntry] = {}
_generation: int = 0     # Increments on every full invalidate_all(). Diagnostic only.
_hit_count: int = 0      # Total cache hits since startup.
_miss_count: int = 0     # Total cache misses since startup.
_initialized: bool = False


def _assert_initialized() -> None:
    """Guard: fail loudly if accessed before lifecycle.startup()."""
    if not _initialized:
        raise RuntimeError(
            "onixey3.runtime.cache: Accessed before startup(). "
            "Ensure lifecycle.startup() was called in register()."
        )


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC KEY BUILDERS
# All cache keys must use these builders. No ad-hoc f-strings at call sites.
# This centralizes the key format in one place; a format change only touches here.
# ──────────────────────────────────────────────────────────────────────────────

def key_l1_bone_pos(obj_name: str, bone_name: str) -> str:
    """
    L1 key: evaluated world-space position of a bone on the current frame.
    Used to avoid redundant evaluated_get() calls within one frame.
    """
    return f"L1:{obj_name}:{bone_name}:pos"


def key_l1_object_matrix(obj_name: str) -> str:
    """
    L1 key: evaluated world matrix of a whole object on the current frame.
    """
    return f"L1:{obj_name}:matrix"


def key_l2_analysis(
    obj_name: str,
    analysis_type: str,
    frame_start: int,
    frame_end: int,
) -> str:
    """
    L2 key: computed analysis result over a frame range.

    analysis_type: one of "arc", "spacing", "energy", "ikfk", "euler",
                   "overlap", "noise", "momentum".

    Example: "L2:Armature:arc:1:120"
    """
    return f"L2:{obj_name}:{analysis_type}:{frame_start}:{frame_end}"


def key_l3_topology(obj_name: str) -> str:
    """L3 key: bone chain topology map for a rig."""
    return f"L3:{obj_name}:topology"


def key_l3_ik_chains(obj_name: str) -> str:
    """L3 key: detected IK chains for a rig."""
    return f"L3:{obj_name}:ik_chains"


# ──────────────────────────────────────────────────────────────────────────────
# CORE API: GET / SET
# ──────────────────────────────────────────────────────────────────────────────

def cache_get(key: str) -> Optional[Any]:
    """
    Retrieve a cached value by key.

    Returns:
        A deep copy of the cached payload, or None if the key is absent
        or the entry has expired.

    REFERENCE ISOLATION:
        Returns a deep copy of the stored value. Mutations to the returned
        object do NOT affect the cached entry.

        Example:
            result = cache_get(key)
            if result:
                result["score"] = 99   # does NOT corrupt the cache entry
                                       # next cache_get() still returns original

    Side effect:
        Expired entries are evicted on access (lazy eviction). This keeps the
        hot path fast: no background thread, no periodic sweep.

    Performance:
        O(1) dict lookup + deepcopy of result. Expected < 0.1ms for typical
        analysis result payloads (small dicts with float values).
    """
    global _hit_count, _miss_count
    _assert_initialized()

    entry = _store.get(key)
    if entry is None:
        _miss_count += 1
        return None

    ttl, _ = _TIER_CONFIG.get(entry.tier, (60.0, 0))
    if entry.is_expired(ttl):
        del _store[key]
        _miss_count += 1
        _log.debug("Cache MISS (expired TTL): %s", key)
        return None

    _hit_count += 1

    # Return a deep copy so the caller cannot accidentally mutate the
    # stored entry through the returned reference.
    try:
        safe_return = copy.deepcopy(entry.value)
        _log.debug("[Runtime] Returning safe cache copy: %s", key)
        return safe_return
    except Exception as exc:
        # Evict the entry immediately: it contains a value that cannot be
        # safely isolated. Keeping it would risk returning a live reference
        # that a caller could mutate, silently corrupting future reads.
        del _store[key]
        _hit_count -= 1     # This hit did not yield a usable value.
        _miss_count += 1
        raise CacheDeepCopyError(
            f"cache_get: deepcopy failed for key {key!r}. "
            f"Entry evicted to prevent reference contamination. "
            f"Root cause: {type(exc).__name__}: {exc}. "
            f"Fix: ensure values stored via cache_set() contain only plain "
            f"Python types (dict, list, int, float, str, tuple) and copies "
            f"of mathutils types (Vector.copy(), Matrix.copy()). "
            f"Do NOT store bpy objects, generators, or objects with __deepcopy__ "
            f"that raise."
        ) from exc


def cache_set(key: str, value: Any, tier: str) -> None:
    """
    Store a value in the cache.

    Args:
        key:   Built via key_l*_*() builders above. No raw f-strings at call sites.
        value: Plain Python types ONLY. Do NOT store bpy objects — they become
               invalid after undo and create memory leaks.
               Acceptable: dict, list, int, float, str, tuple,
                           mathutils.Vector.copy() (copied, not referenced).
        tier:  "L1", "L2", or "L3".

    REFERENCE ISOLATION:
        The value is deep-copied before storage. The caller retains ownership
        of the original object; mutations to the caller's object after cache_set()
        do NOT affect the stored entry.

        Example:
            result = {"score": 0.95}
            cache_set(key, result, "L2")
            result["score"] = 0.0        # does NOT affect the cached entry

    Side effect:
        If tier is at capacity, the oldest entry in that tier is evicted first.
        This is an approximate LRU: we evict one entry per insertion, not a sweep.
    """
    _assert_initialized()

    if tier not in _VALID_TIERS:
        _log.error(
            "cache_set: Unknown tier '%s'. Must be one of %s. Key: %s",
            tier, sorted(_VALID_TIERS), key,
        )
        return

    # Enforce tier capacity before inserting. Evict oldest if over limit.
    _, max_entries = _TIER_CONFIG[tier]
    tier_prefix = f"{tier}:"
    tier_keys = [k for k in _store if k.startswith(tier_prefix)]

    if len(tier_keys) >= max_entries:
        # Evict the single oldest entry in this tier.
        oldest_key = min(tier_keys, key=lambda k: _store[k].stored_at, default=None)
        if oldest_key:
            del _store[oldest_key]
            _log.debug("Cache EVICT (capacity %d): %s", max_entries, oldest_key)

    # Deep-copy the value before storing to prevent shared references.
    # This ensures that mutations to the caller's original object after
    # cache_set() do not silently corrupt the cached entry.
    try:
        safe_value = copy.deepcopy(value)
    except Exception as exc:
        _log.error(
            "[Runtime] cache_set: deepcopy failed for key '%s' (%s). "
            "Storing original reference as fallback — mutation safety not guaranteed.",
            key, exc,
        )
        safe_value = value

    _store[key] = _CacheEntry(value=safe_value, stored_at=time.monotonic(), tier=tier)
    _log.debug("Cache SET [%s]: %s", tier, key)


# ──────────────────────────────────────────────────────────────────────────────
# INVALIDATION API
# Each function corresponds to one or more callers described in the module
# docstring's INVALIDATION CONTRACTS section.
# ──────────────────────────────────────────────────────────────────────────────

def invalidate_l1() -> None:
    """
    Invalidate ALL L1 (frame-tier) entries.

    CALLED BY:
        - frame_change_post handler (mandatory, every frame during playback)
        - depsgraph_update_post handler (mandatory, when any data changes)

    Performance: O(n) where n = total L1 entries. Typical: < 0.05ms.
    This MUST be fast enough to run every frame without affecting playback.
    """
    _assert_initialized()
    keys = [k for k in _store if k.startswith("L1:")]
    for k in keys:
        del _store[k]
    if keys:
        _log.debug("invalidate_l1: cleared %d entries.", len(keys))


def invalidate_l2_for(obj_name: str) -> None:
    """
    Invalidate all L2 (analysis-tier) entries for one specific object.

    CALLED BY:
        - depsgraph_update_post handler when that specific object's animation changed.
        - Operators that modify FCurves for a single object (targeted invalidation).

    Performance: O(n) where n = total L2 entries. Typical: < 0.05ms.
    """
    _assert_initialized()
    prefix = f"L2:{obj_name}:"
    keys = [k for k in _store if k.startswith(prefix)]
    for k in keys:
        del _store[k]
    if keys:
        _log.debug("invalidate_l2_for '%s': cleared %d entries.", obj_name, len(keys))


def invalidate_l3_for(obj_name: str) -> None:
    """
    Invalidate all L3 (topology-tier) entries for one specific object.

    CALLED BY:
        - When an armature exits Edit Mode with bone structure changes.
        - When Apply Modifier changes the armature topology.

    Performance: O(n) where n = total L3 entries (typically < 10). < 0.01ms.
    """
    _assert_initialized()
    prefix = f"L3:{obj_name}:"
    keys = [k for k in _store if k.startswith(prefix)]
    for k in keys:
        del _store[k]
    if keys:
        _log.debug("invalidate_l3_for '%s': cleared %d entries.", obj_name, len(keys))


def invalidate_all() -> None:
    """
    Invalidate the ENTIRE cache. All tiers, all objects.

    CALLED BY (mandatory):
        - undo_post handler   — Ctrl+Z restores unknown objects' state
        - redo_post handler   — Ctrl+Y same reason
        - load_post handler   — New .blend; all prior data is foreign
        - unregister()        — Session ends; cache must die

    CALLED BY (explicit operator path):
        - Any operator with UNDO that modifies multiple objects

    Performance: O(n). Increments generation counter for diagnostic use.
    """
    _assert_initialized()
    global _generation
    count = len(_store)
    _store.clear()
    _generation += 1
    _log.info("[Runtime] Cache invalidated: gen=%d, cleared %d entries.", _generation, count)
    _log.debug("invalidate_all: gen=%d, cleared %d entries.", _generation, count)


def invalidate_by_prefix(prefix: str) -> int:
    """
    Invalidate all entries whose key starts with the given prefix.

    This is the flexible escape hatch for callers that need to invalidate
    a custom subset — e.g., all analysis types for a specific object+tier:

        invalidate_by_prefix("L2:MyArmature:")
        invalidate_by_prefix("L3:")            # all topology

    Returns:
        Number of entries removed.

    Performance: O(n). Use targeted invalidation (l2_for, l3_for) when possible.
    """
    _assert_initialized()
    keys = [k for k in _store if k.startswith(prefix)]
    for k in keys:
        del _store[k]
    if keys:
        _log.debug("invalidate_by_prefix '%s': cleared %d entries.", prefix, len(keys))
    return len(keys)


# ──────────────────────────────────────────────────────────────────────────────
# ANIMATION-RELATED INVALIDATION HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _mark_animation_related_invalid(reason: str = "") -> int:
    """
    Invalidate all cache entries that store animation-derived data.

    WHAT IS INVALIDATED
    ────────────────────
    Runtime Iteration 1 strategy — two-pass approach:

      Pass 1 — All L2 entries (always animation-derived):
        L2 stores frame-range analysis results (arc, spacing, energy, IK/FK,
        euler, overlap, noise, momentum). Every L2 entry is computed from
        evaluated bone positions, which are directly derived from FCurves,
        actions, and drivers. Any change to animation data invalidates all
        of them.

      Pass 2 — L1 and L3 entries whose key contains an animation keyword:
        L1 entries are already short-lived (TTL 1s, cleared every frame),
        but we clear matching ones explicitly for correctness.
        L3 topology entries are NOT animation-derived and are intentionally
        kept — topology (bone chains, IK chain detection) does not change
        when keyframes change.

    WHEN TO CALL
    ─────────────
    Call this from any code path that detects animation data was modified
    and the change was NOT caught by the normal handler chain:

        - A correction operator that modifies FCurves via bpy.data.actions
          (the depsgraph_update_post may not fire synchronously).
        - A migration pass that modifies keyframe values directly.
        - Any operator that calls bpy.ops.graph.* or bpy.ops.action.*.
        - Test helpers that insert keyframes without going through operators.

    This is the safety net for Runtime Iteration 1. A more targeted
    per-object invalidation path (invalidate_l2_for) should be preferred
    when the affected object is known.

    Args:
        reason: Optional human-readable description for the diagnostic log.
                E.g., "euler_correction_operator", "migration_v2_to_v3".

    Returns:
        Number of entries removed.

    Performance:
        O(n) where n = total cache entries. Expected < 0.2ms.
        Not suitable for per-frame use — call only when animation changes.
    """
    _assert_initialized()

    to_remove: List[str] = []

    for key in _store:
        # Pass 1: structural prefix match (fast, O(prefixes) per key).
        # Any key whose prefix is in _ANIMATION_KEY_PREFIXES is animation-derived.
        # Currently this means all L2 entries; future tiers added to
        # _ANIMATION_KEY_PREFIXES are caught here automatically.
        if any(key.startswith(pfx) for pfx in _ANIMATION_KEY_PREFIXES):
            to_remove.append(key)
            continue

        # Pass 2: safe-suffix allowlist check (before keyword scan).
        # Keys whose suffix appears in _ANIMATION_SAFE_SUFFIXES are structural
        # data (topology, IK chains) — they do NOT change when animation changes.
        # Checking this BEFORE the keyword scan avoids a future keyword collision
        # where a new analysis type shares a substring with a safe L3 key.
        key_lower = key.lower()
        if any(key_lower.endswith(sfx) for sfx in _ANIMATION_SAFE_SUFFIXES):
            continue   # Protected — keep this entry regardless of keywords.

        # Pass 3: keyword substring match for L1/L3 entries with custom key formats
        # that fall outside the structured tier prefixes.
        # _ANIMATION_KEYWORDS covers all known analysis type names so that
        # future key formats using these terms are caught without code changes.
        if any(kw in key_lower for kw in _ANIMATION_KEYWORDS):
            to_remove.append(key)

    for k in to_remove:
        del _store[k]

    label = f" (reason={reason})" if reason else ""
    if to_remove:
        _log.info(
            "[Runtime] Animation cache invalidated%s: cleared %d entries.",
            label, len(to_remove),
        )
    else:
        _log.debug(
            "[Runtime] Animation cache invalidated%s: no matching entries found.",
            label,
        )

    return len(to_remove)


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_stats() -> Dict[str, Any]:
    """
    Return a snapshot of cache statistics for diagnostic display.

    All values are plain Python primitives — safe to print, log, or
    display in a UI label. No bpy objects.

    Returns dict with keys:
        total_entries  — int: total live entries across all tiers
        by_tier        — dict: {tier_name: entry_count}
        generation     — int: how many full invalidations since startup
        hit_count      — int: total cache hits since startup
        miss_count     — int: total cache misses since startup
        hit_rate_pct   — float: hit / (hit + miss) * 100, or 0.0 if no accesses
        oldest_entry_s — float | None: age in seconds of the oldest live entry
        initialized    — bool: whether the cache is active
    """
    by_tier: Dict[str, int] = {t: 0 for t in _TIER_CONFIG}
    oldest: Optional[float] = None
    now = time.monotonic()

    for entry in _store.values():
        by_tier[entry.tier] = by_tier.get(entry.tier, 0) + 1
        age = now - entry.stored_at
        if oldest is None or age > oldest:
            oldest = age

    total_accesses = _hit_count + _miss_count
    hit_rate = (_hit_count / total_accesses * 100.0) if total_accesses > 0 else 0.0

    return {
        "total_entries":  len(_store),
        "by_tier":        by_tier,
        "generation":     _generation,
        "hit_count":      _hit_count,
        "miss_count":     _miss_count,
        "hit_rate_pct":   round(hit_rate, 1),
        "oldest_entry_s": round(oldest, 3) if oldest is not None else None,
        "initialized":    _initialized,
    }


def dump_keys(tier: Optional[str] = None) -> List[str]:
    """
    Return all live cache keys, optionally filtered by tier.

    For debugging only — do not call from draw() or handlers.

    Args:
        tier: "L1", "L2", "L3", or None (all tiers).
    """
    if tier is None:
        return sorted(_store.keys())
    prefix = f"{tier}:"
    return sorted(k for k in _store if k.startswith(prefix))


# ──────────────────────────────────────────────────────────────────────────────
# LIFECYCLE
# Called exclusively from runtime/lifecycle.py.
# These are NOT called by any other module.
# ──────────────────────────────────────────────────────────────────────────────

def _init() -> None:
    """
    Initialize cache state. Called ONCE by lifecycle.startup().
    Resets all state to a known-clean baseline.
    """
    global _store, _generation, _hit_count, _miss_count, _initialized
    _store = {}
    _generation = 0
    _hit_count = 0
    _miss_count = 0
    _initialized = True
    _log.debug("cache._init(): ready.")


def _destroy() -> None:
    """
    Destroy all cache state. Called ONCE by lifecycle.shutdown().
    After this, cache_get / cache_set will raise RuntimeError.
    """
    global _store, _initialized
    count = len(_store)
    _store = {}
    _initialized = False
    _log.debug("cache._destroy(): cleared %d entries.", count)
