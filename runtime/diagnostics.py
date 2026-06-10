"""
onixey3/runtime/diagnostics.py

Central Runtime Diagnostic System — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Read-only inspection layer over RuntimeStateManager (state.py).

Provides structured, formatted, and aggregated views of the complete
runtime state — suitable for operator reports, the Blender console,
support ticket attachments, and in-addon diagnostic panels.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT write to any bpy data (scenes, objects, meshes).
    - Does NOT register bpy handlers, classes, or properties.
    - Does NOT mutate RuntimeStateManager state.
    - Does NOT call state.get().set_flag() or any mutating method.
    - Does NOT import bpy at module level.
    - Does NOT raise exceptions to callers (all public functions are
      internally guarded — errors are embedded in the returned report).

ARCHITECTURE
────────────
Every public function in this module follows the same contract:

    Input  : optional filter / verbosity parameters
    Process: read state.get() snapshot (read-only)
    Output : typed plain-Python result (dict, list, str, bool)
             — never bpy objects, never mutable internal state

The module is deliberately stateless: it holds no caches and no
globals beyond the logger. Every call re-reads from state.get().

PUBLIC API SUMMARY
──────────────────
    runtime_snapshot()          → dict   Full machine-readable state dump.
    runtime_health_report()     → HealthReport (dict)  Pass/warn/fail assessment.
    handler_integrity_check()   → HandlerCheckResult (dict)
    cache_integrity_check()     → CacheCheckResult (dict)
    generate_diagnostic_report()→ str   Full human-readable formatted report.
    format_flags_block()        → str   Flags section only (for UI panels).
    format_locks_block()        → str   Locks section only.
    format_signals_block()      → str   Integrity signals section only.

DEPENDENCY CONTRACT
───────────────────
    Imports from:    onixey3.runtime.state
    Must NOT import: cache, session, lifecycle, operators, ui, analysis,
                     properties, migration, core, registration, bpy (top-level)

USAGE
──────
    from onixey3.runtime import diagnostics

    # In an operator or panel:
    report = diagnostics.generate_diagnostic_report()
    print(report)

    # Health gate before a risky operation:
    health = diagnostics.runtime_health_report()
    if health["overall"] == "FAIL":
        self.report({'ERROR'}, health["summary"])
        return {'CANCELLED'}

    # Machine-readable snapshot for a diagnostic operator:
    snap = diagnostics.runtime_snapshot()

CHANGELOG
─────────
    3.1.0 — Initial implementation. Full AAA diagnostic suite aligned to
             RuntimeStateManager v3.1.0 public API.
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Lock held duration (seconds) beyond which we report WARN in health checks.
# Mirrors state._LOCK_WARN_SECONDS for cross-module consistency.
_LOCK_WARN_SECONDS: float = 30.0

# Minimum seconds since last successful reload before we warn about staleness.
_RELOAD_STALENESS_WARN_SECONDS: float = 3600.0  # 1 hour

# Report section separator — 68 chars wide (fits 80-col terminals with indent).
_SEP_HEAVY = "═" * 68
_SEP_LIGHT = "─" * 68
_SEP_MID   = "┄" * 68

# Health level constants used across all returned dicts.
_HEALTH_PASS = "PASS"
_HEALTH_WARN = "WARN"
_HEALTH_FAIL = "FAIL"
_HEALTH_SKIP = "SKIP"   # Section not applicable (e.g., no reload has occurred).

# IntegritySignal names that are FAIL-grade (vs WARN-grade).
# Single source of truth — add new critical signal names here only.
# Referenced by: runtime_health_report(), generate_diagnostic_report(),
#                format_signals_block()
_FAIL_GRADE_SIGNALS: frozenset = frozenset({
    "UNEXPECTED_PHASE_TRANSITION",
    "RELOAD_REENTRY",
    "HANDLER_REENTRY_DETECTED",
    "INIT_WITHOUT_FEATURE_FLAGS",
})


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _get_state_safe() -> Optional[Any]:
    """
    Return the RuntimeStateManager singleton without raising.

    Returns None if state module is unavailable or manager is uninitialized.
    All public functions call this and handle None gracefully.
    """
    try:
        from onixey3.runtime import state  # deferred — no top-level bpy risk
        mgr = state.get()
        return mgr
    except Exception as exc:
        _log.error("diagnostics: could not access state.get(): %s", exc)
        return None


def _blender_version_str() -> str:
    """Return Blender version string. Never raises."""
    try:
        import bpy
        v = bpy.app.version
        return f"{v[0]}.{v[1]}.{v[2]}"
    except Exception:
        return "unknown"


def _blender_version_tuple() -> Tuple[int, ...]:
    """Return Blender version as (major, minor, patch). Never raises."""
    try:
        import bpy
        return tuple(bpy.app.version)[:3]
    except Exception:
        return (0, 0, 0)


def _timestamp_now() -> str:
    """ISO-8601 timestamp with milliseconds. Never raises."""
    try:
        ms = int(time.monotonic() * 1000) % 1000
        return time.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}"
    except Exception:
        return "???"


def _flag_indicator(value: bool) -> str:
    """Visual indicator for a boolean flag."""
    return "✔" if value else "·"


def _health_indicator(level: str) -> str:
    """Visual indicator for a health level string."""
    return {"PASS": "✔", "WARN": "⚠", "FAIL": "✖", "SKIP": "○"}.get(level, "?")


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — runtime_snapshot()
# ──────────────────────────────────────────────────────────────────────────────

def runtime_snapshot() -> Dict[str, Any]:
    """
    Return a complete, machine-readable snapshot of the current runtime state.

    Aggregates all subsections of RuntimeStateManager into a single plain-Python
    dict. Safe to log, serialize to JSON, or display in a diagnostic operator.

    Never raises. On error, returns a dict with an "error" key.

    Returns dict with keys
    ──────────────────────
    generated_at       : str    ISO-8601 timestamp of snapshot generation.
    blender_version    : str    Blender version string (e.g. "4.2.0").
    state_available    : bool   False if RuntimeStateManager is unreachable.
    instance_id        : int    Manager instance number (resets on F8).
    phase              : str    Current LifecyclePhase name.
    phase_is_active    : bool   True iff phase == ACTIVE.
    age_s              : float  Seconds since this manager instance was created.
    any_critical_lock  : bool   True if flags indicate unsafe state.
    flags              : dict   {flag_name: bool} — all RuntimeFlags.
    active_locks       : dict   {lock_name: {held_s, instance_id, acquired_at}}.
    active_guards      : list   Currently active reentry guard names.
    integrity_signals  : list   [(signal_name, detail_message), ...]
    reload_state       : dict   ReloadState snapshot.
    audit_log_size     : int    Total entries in the internal audit log.
    """
    result: Dict[str, Any] = {
        "generated_at":    _timestamp_now(),
        "blender_version": _blender_version_str(),
        "state_available": False,
    }

    try:
        mgr = _get_state_safe()
        if mgr is None:
            result["error"] = "RuntimeStateManager is not initialized."
            return result

        snap = mgr.get_diagnostic_snapshot()

        result.update({
            "state_available":  True,
            "instance_id":      snap.get("instance_id", -1),
            "phase":            snap.get("phase", "UNKNOWN"),
            "phase_is_active":  snap.get("phase", "") == "ACTIVE",
            "age_s":            snap.get("age_s", 0.0),
            "any_critical_lock": snap.get("any_critical_lock", False),
            "flags":            snap.get("flags", {}),
            "active_locks":     snap.get("active_locks", {}),
            "active_guards":    snap.get("active_guards", []),
            "integrity_signals": snap.get("integrity_signals", []),
            "reload_state":     snap.get("reload_state", {}),
            "audit_log_size":   snap.get("audit_log_size", 0),
        })

    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        _log.error("diagnostics.runtime_snapshot() error: %s\n%s", exc, traceback.format_exc())

    return result


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — runtime_health_report()
# ──────────────────────────────────────────────────────────────────────────────

def runtime_health_report() -> Dict[str, Any]:
    """
    Evaluate the current runtime state and return a structured health report.

    Assesses each major subsystem (lifecycle, flags, locks, signals, reload)
    individually and derives an overall PASS / WARN / FAIL verdict.

    Designed for use by:
        - Diagnostic operators that need a fast go/no-go check.
        - Pre-operation gates before risky actions (batch export, reload).
        - Addon preferences panels that display a health indicator.

    Never raises. On error, overall is "FAIL" with an error key.

    Returns dict with keys
    ──────────────────────
    generated_at   : str    Snapshot timestamp.
    overall        : str    "PASS" | "WARN" | "FAIL"
    summary        : str    One-line human-readable verdict.
    checks         : dict   {check_name: {"level": str, "detail": str}}
        lifecycle      : Phase is ACTIVE?
        flags          : Any critical flag combo active?
        locks          : Stale locks held?
        signals        : Any integrity signals recorded?
        reload         : Last reload healthy? Staleness?
        feature_flags  : Hard requirements met?
    """
    result: Dict[str, Any] = {
        "generated_at": _timestamp_now(),
        "overall":      _HEALTH_FAIL,
        "summary":      "State manager unavailable.",
        "checks":       {},
    }

    try:
        mgr = _get_state_safe()
        if mgr is None:
            result["checks"]["lifecycle"] = {
                "level":  _HEALTH_FAIL,
                "detail": "RuntimeStateManager is not initialized. "
                          "Addon may not be registered.",
            }
            return result

        snap = mgr.get_diagnostic_snapshot()
        checks: Dict[str, Dict[str, str]] = {}

        # ── Lifecycle check ───────────────────────────────────────────────────
        phase = snap.get("phase", "UNKNOWN")
        if phase == "ACTIVE":
            checks["lifecycle"] = {
                "level":  _HEALTH_PASS,
                "detail": f"Phase is ACTIVE. Addon is fully operational.",
            }
        elif phase in ("INITIALIZING", "SHUTTING_DOWN"):
            checks["lifecycle"] = {
                "level":  _HEALTH_WARN,
                "detail": f"Phase is {phase}. Addon is in transition.",
            }
        else:
            checks["lifecycle"] = {
                "level":  _HEALTH_FAIL,
                "detail": f"Phase is {phase}. Addon is not operational.",
            }

        # ── Flags check ───────────────────────────────────────────────────────
        flags = snap.get("flags", {})
        critical_active = snap.get("any_critical_lock", False)
        critical_flag_names = [
            k for k in ("analysis_running", "analysis_batch_active",
                        "reload_in_progress", "migration_running")
            if flags.get(k, False)
        ]
        post_reload_pending = flags.get("post_reload_validation", False)
        migration_pending   = flags.get("migration_pending", False)

        if critical_active:
            checks["flags"] = {
                "level":  _HEALTH_WARN,
                "detail": f"Critical flags active: {critical_flag_names}. "
                          "Unsafe operations should wait.",
            }
        elif post_reload_pending:
            checks["flags"] = {
                "level":  _HEALTH_WARN,
                "detail": "post_reload_validation=True. "
                          "Integrity check pending after reload.",
            }
        elif migration_pending:
            checks["flags"] = {
                "level":  _HEALTH_WARN,
                "detail": "migration_pending=True. "
                          "Loaded .blend has pre-V3 data requiring upgrade.",
            }
        else:
            checks["flags"] = {
                "level":  _HEALTH_PASS,
                "detail": "No critical flags active.",
            }

        # ── Locks check ───────────────────────────────────────────────────────
        active_locks = snap.get("active_locks", {})
        if active_locks:
            stale = {
                name: info for name, info in active_locks.items()
                if float(info.get("held_s", 0)) > _LOCK_WARN_SECONDS
            }
            if stale:
                stale_detail = "; ".join(
                    f"'{n}' held {info['held_s']}s"
                    for n, info in stale.items()
                )
                checks["locks"] = {
                    "level":  _HEALTH_WARN,
                    "detail": f"Stale lock(s) detected: {stale_detail}. "
                              "Possible operator that crashed without releasing.",
                }
            else:
                lock_detail = ", ".join(
                    f"'{n}' ({info['held_s']}s)"
                    for n, info in active_locks.items()
                )
                checks["locks"] = {
                    "level":  _HEALTH_WARN,
                    "detail": f"Active modal lock(s): {lock_detail}.",
                }
        else:
            checks["locks"] = {
                "level":  _HEALTH_PASS,
                "detail": "No modal locks held.",
            }

        # ── Integrity signals check ───────────────────────────────────────────
        signals = snap.get("integrity_signals", [])
        if signals:
            signal_names = [s[0] for s in signals]
            has_fail = any(n in _FAIL_GRADE_SIGNALS for n in signal_names)
            level = _HEALTH_FAIL if has_fail else _HEALTH_WARN
            checks["signals"] = {
                "level":  level,
                "detail": f"{len(signals)} signal(s) recorded: "
                          f"{', '.join(dict.fromkeys(signal_names))}.",
            }
        else:
            checks["signals"] = {
                "level":  _HEALTH_PASS,
                "detail": "No integrity signals recorded.",
            }

        # ── Reload state check ────────────────────────────────────────────────
        rs = snap.get("reload_state", {})
        last_ago = rs.get("last_reload_ago_s")
        failed   = rs.get("failed_count", 0)
        gen      = rs.get("generation", 0)

        if gen == 0 or last_ago is None:
            checks["reload"] = {
                "level":  _HEALTH_SKIP,
                "detail": "No reload has occurred in this session.",
            }
        elif failed > 0:
            checks["reload"] = {
                "level":  _HEALTH_WARN,
                "detail": f"Last reload gen={gen}: {failed} module(s) failed. "
                          "post_reload_validation should have been triggered.",
            }
        elif last_ago is not None and last_ago > _RELOAD_STALENESS_WARN_SECONDS:
            checks["reload"] = {
                "level":  _HEALTH_WARN,
                "detail": f"Last reload was {last_ago:.0f}s ago "
                          f"(threshold {_RELOAD_STALENESS_WARN_SECONDS:.0f}s). "
                          "Consider a fresh reload if issues are observed.",
            }
        else:
            checks["reload"] = {
                "level":  _HEALTH_PASS,
                "detail": f"Last reload gen={gen} was {last_ago}s ago, "
                          f"no failures.",
            }

        # ── Feature flags / hard requirements check ───────────────────────────
        feature_sealed = flags.get("feature_flags_sealed", False)
        hard_met       = flags.get("hard_requirements_met", False)

        if not feature_sealed:
            checks["feature_flags"] = {
                "level":  _HEALTH_FAIL,
                "detail": "feature_flags_sealed=False. "
                          "core.compat.validate_environment() may not have run.",
            }
        elif not hard_met:
            checks["feature_flags"] = {
                "level":  _HEALTH_WARN,
                "detail": "feature_flags_sealed=True but hard_requirements_met=False. "
                          "Some required capabilities are unavailable.",
            }
        else:
            checks["feature_flags"] = {
                "level":  _HEALTH_PASS,
                "detail": "Feature flags sealed. All hard requirements met.",
            }

        # ── Overall verdict ───────────────────────────────────────────────────
        levels = [v["level"] for v in checks.values() if v["level"] != _HEALTH_SKIP]
        if _HEALTH_FAIL in levels:
            overall = _HEALTH_FAIL
        elif _HEALTH_WARN in levels:
            overall = _HEALTH_WARN
        else:
            overall = _HEALTH_PASS

        fail_checks = [k for k, v in checks.items() if v["level"] == _HEALTH_FAIL]
        warn_checks = [k for k, v in checks.items() if v["level"] == _HEALTH_WARN]

        if overall == _HEALTH_PASS:
            summary = f"All systems nominal. Phase: {phase}."
        elif overall == _HEALTH_WARN:
            summary = (
                f"Warnings in: {', '.join(warn_checks)}. "
                f"Phase: {phase}."
            )
        else:
            summary = (
                f"Failures in: {', '.join(fail_checks)}. "
                f"Phase: {phase}. Addon may not be functional."
            )

        result.update({
            "overall": overall,
            "summary": summary,
            "checks":  checks,
        })

    except Exception as exc:
        result["overall"] = _HEALTH_FAIL
        result["summary"] = f"Health report error: {exc}"
        result["error"]   = traceback.format_exc()
        _log.error(
            "diagnostics.runtime_health_report() error: %s\n%s",
            exc, traceback.format_exc(),
        )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — handler_integrity_check()
# ──────────────────────────────────────────────────────────────────────────────

def handler_integrity_check() -> Dict[str, Any]:
    """
    Inspect reentry guard state and flag any handler-related anomalies.

    Reads the active reentry guards and integrity signals from RuntimeStateManager
    and classifies each guard as ACTIVE (normal), STUCK (active too long), or
    cross-references against HANDLER_REENTRY_DETECTED signals.

    This function does NOT inspect bpy.app.handlers directly (that would
    require bpy and could be unsafe). It reads only the state layer.

    Returns dict with keys
    ──────────────────────
    generated_at    : str   Timestamp.
    overall         : str   "PASS" | "WARN" | "FAIL"
    active_guards   : list  Currently active reentry guard names.
    guard_count     : int   Number of active guards.
    reentry_signals : list  HANDLER_REENTRY_DETECTED signal details.
    detail          : str   One-line summary.
    """
    result: Dict[str, Any] = {
        "generated_at":    _timestamp_now(),
        "overall":         _HEALTH_FAIL,
        "active_guards":   [],
        "guard_count":     0,
        "reentry_signals": [],
        "detail":          "State manager unavailable.",
    }

    try:
        mgr = _get_state_safe()
        if mgr is None:
            return result

        snap = mgr.get_diagnostic_snapshot()
        guards  = list(snap.get("active_guards", []))
        signals = snap.get("integrity_signals", [])

        reentry_sigs = [
            detail for sig_name, detail in signals
            if sig_name == "HANDLER_REENTRY_DETECTED"
        ]

        result["active_guards"]   = guards
        result["guard_count"]     = len(guards)
        result["reentry_signals"] = reentry_sigs

        if reentry_sigs:
            result["overall"] = _HEALTH_FAIL
            result["detail"]  = (
                f"{len(reentry_sigs)} reentry event(s) recorded. "
                "A handler executed itself recursively. "
                "Check depsgraph_update_post and frame_change_post handlers."
            )
        elif guards:
            result["overall"] = _HEALTH_WARN
            result["detail"]  = (
                f"{len(guards)} reentry guard(s) currently active: "
                f"{guards}. Normal if inside a handler call."
            )
        else:
            result["overall"] = _HEALTH_PASS
            result["detail"]  = "No active reentry guards. No reentry events recorded."

    except Exception as exc:
        result["overall"] = _HEALTH_FAIL
        result["detail"]  = f"Error: {exc}"
        result["error"]   = traceback.format_exc()
        _log.error(
            "diagnostics.handler_integrity_check() error: %s\n%s",
            exc, traceback.format_exc(),
        )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — cache_integrity_check()
# ──────────────────────────────────────────────────────────────────────────────

def cache_integrity_check() -> Dict[str, Any]:
    """
    Evaluate cache-related runtime flags for consistency anomalies.

    Reads cache_valid, cache_warming, analysis_results_valid, and
    analysis_running from RuntimeFlags and checks for contradictory
    flag combinations that indicate cache inconsistency.

    Does NOT touch the actual cache data structures (no import of cache.py).
    Reads only the state layer — safe, read-only.

    Returns dict with keys
    ──────────────────────
    generated_at          : str   Timestamp.
    overall               : str   "PASS" | "WARN" | "FAIL"
    cache_valid           : bool  Current flag value.
    cache_warming         : bool  Current flag value.
    analysis_results_valid: bool  Current flag value.
    analysis_running      : bool  Current flag value.
    anomalies             : list  Human-readable anomaly descriptions.
    detail                : str   One-line summary.
    """
    result: Dict[str, Any] = {
        "generated_at":           _timestamp_now(),
        "overall":                _HEALTH_FAIL,
        "cache_valid":            False,
        "cache_warming":          False,
        "analysis_results_valid": False,
        "analysis_running":       False,
        "anomalies":              [],
        "detail":                 "State manager unavailable.",
    }

    try:
        mgr = _get_state_safe()
        if mgr is None:
            return result

        flags = mgr.get_flags_snapshot()
        cache_valid     = flags.get("cache_valid", False)
        cache_warming   = flags.get("cache_warming", False)
        results_valid   = flags.get("analysis_results_valid", False)
        analysis_run    = flags.get("analysis_running", False)
        reload_in_prog  = flags.get("reload_in_progress", False)
        migration_run   = flags.get("migration_running", False)

        result.update({
            "cache_valid":            cache_valid,
            "cache_warming":          cache_warming,
            "analysis_results_valid": results_valid,
            "analysis_running":       analysis_run,
        })

        anomalies: List[str] = []

        # Anomaly 1: analysis results valid but cache is not.
        if results_valid and not cache_valid:
            anomalies.append(
                "analysis_results_valid=True but cache_valid=False. "
                "Analysis results may be stale — cache was invalidated after analysis."
            )

        # Anomaly 2: analysis running and reload in progress simultaneously.
        if analysis_run and reload_in_prog:
            anomalies.append(
                "analysis_running=True AND reload_in_progress=True simultaneously. "
                "This combination is unsafe and may cause cache corruption."
            )

        # Anomaly 3: cache warming and migration running simultaneously.
        if cache_warming and migration_run:
            anomalies.append(
                "cache_warming=True AND migration_running=True simultaneously. "
                "Cache pre-population should not run during data migration."
            )

        # Anomaly 4: analysis results valid while analysis is still running.
        if results_valid and analysis_run:
            anomalies.append(
                "analysis_results_valid=True while analysis_running=True. "
                "Results flag should be cleared at analysis start."
            )

        result["anomalies"] = anomalies

        if anomalies:
            result["overall"] = _HEALTH_WARN
            result["detail"]  = (
                f"{len(anomalies)} cache flag anomaly(ies) detected. "
                f"Review cache and analysis subsystems."
            )
        elif not cache_valid and not cache_warming:
            # Cache is simply cold — expected after a fresh register or blend load.
            result["overall"] = _HEALTH_PASS
            result["detail"]  = (
                "Cache is cold (cache_valid=False, cache_warming=False). "
                "Normal on startup or after scene change. No anomalies."
            )
        elif cache_warming:
            result["overall"] = _HEALTH_PASS
            result["detail"]  = "Cache warming is in progress. No anomalies."
        else:
            result["overall"] = _HEALTH_PASS
            result["detail"]  = "Cache flags consistent. No anomalies detected."

    except Exception as exc:
        result["overall"] = _HEALTH_FAIL
        result["detail"]  = f"Error: {exc}"
        result["error"]   = traceback.format_exc()
        _log.error(
            "diagnostics.cache_integrity_check() error: %s\n%s",
            exc, traceback.format_exc(),
        )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — generate_diagnostic_report()
# ──────────────────────────────────────────────────────────────────────────────

def generate_diagnostic_report(
    *,
    include_audit_log: bool = False,
    audit_log_entries: int = 20,
    include_blender_info: bool = True,
) -> str:
    """
    Generate a complete, human-readable diagnostic report as a string.

    Aggregates runtime_snapshot(), runtime_health_report(),
    handler_integrity_check(), and cache_integrity_check() into a
    single formatted document suitable for:
        - Pasting into a support ticket.
        - Printing via the Blender System Console.
        - Displaying in a modal operator dialog.

    Args:
        include_audit_log:   Include the last N audit log entries.
                             Default False (keeps report concise).
        audit_log_entries:   Number of audit log entries to include
                             if include_audit_log is True. Default 20.
        include_blender_info: Include Blender version and build info.
                              Default True.

    Returns:
        Formatted string. Never raises — errors are embedded in the report.
    """
    lines: List[str] = []

    def _h1(title: str) -> None:
        lines.append("")
        lines.append(_SEP_HEAVY)
        lines.append(f"  {title}")
        lines.append(_SEP_HEAVY)

    def _h2(title: str) -> None:
        lines.append("")
        lines.append(f"  ── {title} " + "─" * max(0, 60 - len(title)))

    def _row(label: str, value: Any, indent: int = 4) -> None:
        pad = " " * indent
        lines.append(f"{pad}{label:<28} {value}")

    def _bullet(text: str, indent: int = 4, marker: str = "•") -> None:
        lines.append(f"{' ' * indent}{marker} {text}")

    try:
        ts  = _timestamp_now()
        snap   = runtime_snapshot()
        health = runtime_health_report()
        handler_chk = handler_integrity_check()
        cache_chk   = cache_integrity_check()

        # ── Report header ─────────────────────────────────────────────────────
        _h1("ONIXEY V3  ·  RUNTIME DIAGNOSTIC REPORT")
        _row("Generated at",   ts)
        _row("State available", snap.get("state_available", False))

        if include_blender_info:
            _row("Blender version", snap.get("blender_version", "unknown"))
            try:
                import bpy
                _row("Blender build hash", getattr(bpy.app, "build_hash", b"?").decode("utf-8", "replace"))
                _row("Blender branch",    getattr(bpy.app, "build_branch", b"?").decode("utf-8", "replace"))
            except Exception:
                pass

        # ── Overall health banner ─────────────────────────────────────────────
        overall = health.get("overall", "FAIL")
        indicator = _health_indicator(overall)
        _h2(f"OVERALL HEALTH: {indicator} {overall}")
        lines.append(f"    {health.get('summary', '')}")

        # ── Lifecycle / phase ─────────────────────────────────────────────────
        _h2("LIFECYCLE")
        _row("Phase",           snap.get("phase", "UNKNOWN"))
        _row("Instance ID",     snap.get("instance_id", "N/A"))
        _row("Manager age",     f"{snap.get('age_s', 0.0):.1f}s")
        _row("Any critical lock", snap.get("any_critical_lock", False))

        # ── Health check results ──────────────────────────────────────────────
        _h2("HEALTH CHECKS")
        for check_name, check in health.get("checks", {}).items():
            lvl    = check.get("level", "?")
            detail = check.get("detail", "")
            ind    = _health_indicator(lvl)
            lines.append(f"    {ind} [{lvl:<4}] {check_name:<16} {detail}")

        # ── Runtime flags ─────────────────────────────────────────────────────
        _h2("RUNTIME FLAGS")
        flags = snap.get("flags", {})
        if flags:
            # Group flags by prefix category.
            categories: Dict[str, List[Tuple[str, bool]]] = {}
            for fname, fval in sorted(flags.items()):
                prefix = fname.split("_")[0]
                categories.setdefault(prefix, []).append((fname, fval))

            for cat, items in sorted(categories.items()):
                lines.append(f"    [{cat.upper()}]")
                for fname, fval in items:
                    ind = _flag_indicator(fval)
                    active_tag = " ← ACTIVE" if fval else ""
                    lines.append(f"      [{ind}] {fname}{active_tag}")
        else:
            lines.append("    (no flags available)")

        # ── Modal locks ───────────────────────────────────────────────────────
        _h2("MODAL LOCKS")
        locks = snap.get("active_locks", {})
        if locks:
            for lname, linfo in locks.items():
                held   = linfo.get("held_s", 0.0)
                iid    = linfo.get("instance_id", "?")
                stale  = " ⚠ STALE" if float(held) > _LOCK_WARN_SECONDS else ""
                lines.append(f"    • '{lname}'  held={held}s  id={iid}{stale}")
        else:
            lines.append("    (none)")

        # ── Reentry guards ────────────────────────────────────────────────────
        _h2("REENTRY GUARDS")
        guards = snap.get("active_guards", [])
        if guards:
            for g in guards:
                lines.append(f"    • '{g}'")
        else:
            lines.append("    (none)")

        # ── Handler integrity ─────────────────────────────────────────────────
        _h2("HANDLER INTEGRITY")
        h_overall = handler_chk.get("overall", "FAIL")
        h_ind     = _health_indicator(h_overall)
        lines.append(f"    {h_ind} {h_overall}: {handler_chk.get('detail', '')}")
        reentry_sigs = handler_chk.get("reentry_signals", [])
        if reentry_sigs:
            for sig_detail in reentry_sigs:
                _bullet(sig_detail, marker="  ⚠")

        # ── Cache integrity ───────────────────────────────────────────────────
        _h2("CACHE INTEGRITY")
        c_overall = cache_chk.get("overall", "FAIL")
        c_ind     = _health_indicator(c_overall)
        lines.append(f"    {c_ind} {c_overall}: {cache_chk.get('detail', '')}")

        _row("cache_valid",            cache_chk.get("cache_valid", False))
        _row("cache_warming",          cache_chk.get("cache_warming", False))
        _row("analysis_results_valid", cache_chk.get("analysis_results_valid", False))
        _row("analysis_running",       cache_chk.get("analysis_running", False))

        anomalies = cache_chk.get("anomalies", [])
        if anomalies:
            lines.append("    Anomalies:")
            for a in anomalies:
                _bullet(a, indent=6, marker="⚠")

        # ── Integrity signals ─────────────────────────────────────────────────
        _h2("INTEGRITY SIGNALS")
        signals = snap.get("integrity_signals", [])
        if signals:
            for sig_name, detail in signals:
                marker = "✖" if sig_name in _FAIL_GRADE_SIGNALS else "⚠"
                lines.append(f"    {marker} {sig_name}")
                lines.append(f"        {detail}")
        else:
            lines.append("    (none — environment is clean)")

        # ── Reload state ──────────────────────────────────────────────────────
        _h2("RELOAD STATE")
        rs = snap.get("reload_state", {})
        if rs:
            for k, v in rs.items():
                _row(k, v)
        else:
            lines.append("    (no reload data available)")

        # ── Audit log (optional) ──────────────────────────────────────────────
        if include_audit_log:
            _h2(f"AUDIT LOG (last {audit_log_entries} entries)")
            mgr = _get_state_safe()
            if mgr is not None:
                entries = mgr.get_audit_log(last_n=audit_log_entries)
                if entries:
                    for entry in entries:
                        ts_e  = entry.get("ts", "")
                        etype = entry.get("event_type", "")
                        msg   = entry.get("message", "")
                        phase = entry.get("phase", "")
                        lines.append(f"    {ts_e}  [{phase:<13}] {etype:<22} {msg}")
                else:
                    lines.append("    (audit log is empty)")
            else:
                lines.append("    (state unavailable)")

        # ── Footer ────────────────────────────────────────────────────────────
        lines.append("")
        lines.append(_SEP_HEAVY)
        lines.append(f"  Onixey V3  ·  diagnostics.py  ·  {ts}")
        lines.append(_SEP_HEAVY)
        lines.append("")

    except Exception as exc:
        lines.append("")
        lines.append("═" * 68)
        lines.append("  DIAGNOSTIC REPORT GENERATION ERROR")
        lines.append("═" * 68)
        lines.append(f"  {exc}")
        lines.append(traceback.format_exc())
        _log.error(
            "diagnostics.generate_diagnostic_report() error: %s\n%s",
            exc, traceback.format_exc(),
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — SECTION FORMATTERS
# Lightweight helpers for UI panels that display individual sections.
# ──────────────────────────────────────────────────────────────────────────────

def format_flags_block(*, active_only: bool = False) -> str:
    """
    Return a formatted string of all RuntimeFlags.

    Args:
        active_only: If True, only show flags that are True.

    Returns:
        Multi-line string. Empty string on error.
    """
    try:
        mgr = _get_state_safe()
        if mgr is None:
            return "  (state unavailable)"
        flags = mgr.get_flags_snapshot()
        lines = []
        for fname, fval in sorted(flags.items()):
            if active_only and not fval:
                continue
            ind = _flag_indicator(fval)
            lines.append(f"  [{ind}] {fname}")
        return "\n".join(lines) if lines else "  (no active flags)"
    except Exception as exc:
        _log.error("diagnostics.format_flags_block() error: %s", exc)
        return f"  (error: {exc})"


def format_locks_block() -> str:
    """
    Return a formatted string of all active ModalLocks.

    Returns:
        Multi-line string. Empty string on error.
    """
    try:
        mgr = _get_state_safe()
        if mgr is None:
            return "  (state unavailable)"
        locks = mgr.get_active_locks()
        if not locks:
            return "  (no active locks)"
        lines = []
        for lname, linfo in locks.items():
            held  = linfo.get("held_s", 0.0)
            iid   = linfo.get("instance_id", "?")
            stale = "  ⚠ STALE" if float(held) > _LOCK_WARN_SECONDS else ""
            lines.append(f"  • '{lname}'  held={held}s  id={iid}{stale}")
        return "\n".join(lines)
    except Exception as exc:
        _log.error("diagnostics.format_locks_block() error: %s", exc)
        return f"  (error: {exc})"


def format_signals_block() -> str:
    """
    Return a formatted string of all recorded IntegritySignals.

    Returns:
        Multi-line string. Never raises.
    """
    try:
        mgr = _get_state_safe()
        if mgr is None:
            return "  (state unavailable)"
        signals = mgr.get_integrity_signals()
        if not signals:
            return "  (no signals — environment is clean)"
        lines = []
        for sig_name, detail in signals:
            marker = "✖" if sig_name in _FAIL_GRADE_SIGNALS else "⚠"
            lines.append(f"  {marker} {sig_name}")
            lines.append(f"      {detail}")
        return "\n".join(lines)
    except Exception as exc:
        _log.error("diagnostics.format_signals_block() error: %s", exc)
        return f"  (error: {exc})"


def format_phase_line() -> str:
    """
    Return a compact one-line phase/health summary for status bars.

    Example: "● ACTIVE  ✔ PASS  locks=0  signals=0"

    Never raises.
    """
    try:
        mgr = _get_state_safe()
        if mgr is None:
            return "● UNKNOWN  ✖ FAIL  (state unavailable)"
        snap    = mgr.get_diagnostic_snapshot()
        phase   = snap.get("phase", "UNKNOWN")
        n_locks = len(snap.get("active_locks", {}))
        n_sigs  = len(snap.get("integrity_signals", []))
        crit    = snap.get("any_critical_lock", False)

        health  = runtime_health_report()
        overall = health.get("overall", "FAIL")
        h_ind   = _health_indicator(overall)
        c_tag   = "  ⚠ CRITICAL" if crit else ""

        return (
            f"● {phase}  {h_ind} {overall}  "
            f"locks={n_locks}  signals={n_sigs}{c_tag}"
        )
    except Exception as exc:
        return f"● ERROR  ✖ FAIL  ({exc})"
