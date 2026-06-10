"""
onixey3/runtime/guards.py

Runtime Protection Guards — Onixey V3
======================================
A collection of reusable, composable guard primitives that wrap
``runtime/state.py`` primitives in safe, leak-proof execution patterns.

POSITION IN THE ARCHITECTURE
─────────────────────────────
    guards.py reads from:  runtime/state.py  (sole dependency inside runtime/)
    guards.py is used by:  operators/, analysis/, ui/ (overlays only)

    guards.py MUST NOT import from:
        cache.py, session.py, lifecycle.py  (within runtime/)
        operators/, ui/, analysis/          (would create import cycles)

    guards.py has ZERO bpy imports — not even at module level.
    It never touches bpy.types, bpy.app, frame_set(), or Scene properties.

WHY THIS MODULE EXISTS
───────────────────────
``state.py`` owns the primitive operations (acquire/release on the singleton).
``guards.py`` owns the *usage patterns* — the try/finally discipline,
the contextmanager sugar, the decorator wrappers, and the structured result
types that make guards composable without boilerplate in every operator.

Without guards.py, every operator would need to:
    1. Call state.get_or_raise()
    2. Check is_runtime_active()
    3. Call acquire_reentry_guard(key)
    4. Wrap body in try/finally
    5. Call release_reentry_guard_safe(key) in finally
    6. Handle ModalLockError, ReentryError, RuntimeStateError distinctly
    7. Log the right level for each failure path

That's 7 steps before any business logic. guards.py reduces this to one
decorator or one ``with`` block.

THE FIVE GUARDS
───────────────
    safe_execute()     — Functional wrapper: runs fn(*args, **kwargs) inside a
                         full protection shell. Returns ExecutionResult.
                         Used when composing guards programmatically.

    operator_guard()   — Context manager for execute() methods. Checks runtime
                         active, optionally acquires a reentry guard, and
                         always releases in finally. Yields GuardContext.

    reentry_guard()    — Lightweight context manager: only a reentry guard,
                         no operator-level state checks. For analysis functions
                         and internal helpers called from handlers.

    context_guard()    — Validates that a bpy context is in the expected mode
                         and has the expected active object type before
                         proceeding. Returns ContextValidation. Not a CM —
                         call it imperatively at the top of execute() after
                         guard acquisition.

    lock_guard()       — Context manager for modal operators. Acquires the
                         modal lock on enter, releases on exit. Raises
                         ModalLockError if the lock is not available.

EXECUTION RESULT
─────────────────
    All guards that wrap execution return an ExecutionResult:
        .ok         — True if the body ran without raising
        .cancelled  — True if a guard condition blocked execution
        .result     — The body's return value if ok, else None
        .error      — The exception if not ok and not cancelled, else None
        .guard_ms   — Time spent in guard setup/teardown (not body)
        .body_ms    — Time spent in the body callable
        .blender_set — {'FINISHED'} if ok, {'CANCELLED'} otherwise
                       Ready to return directly from operator execute().

DESIGN DECISIONS
─────────────────
    • try/finally throughout — every acquisition has a guaranteed release path,
      even if the body raises or if guard setup itself partially succeeds.

    • No bare except — every except clause names its exception type(s).
      Unexpected exceptions propagate upward unless safe_execute() is explicitly
      asked to capture them (capture_exceptions=True).

    • contextmanager over __enter__/__exit__ — cleaner tracebacks and easier
      to test. The with-block body's traceback is preserved unchanged.

    • logging at the right level:
        DEBUG    — normal guard acquire/release
        INFO     — guard blocked execution (reentry / lock busy)
        WARNING  — stale guard auto-expired during acquire
        ERROR    — unexpected exception in body (with traceback)

    • GuardContext.report_blender() — a helper that calls self.operator.report()
      with the right severity and message, without guards.py depending on
      bpy.types.Operator.

USAGE EXAMPLES
──────────────
    # 1. operator_guard() — the standard pattern for execute()
    class ONIXEY3_OT_analyze(bpy.types.Operator):
        bl_idname = "onixey3.analyze"

        @classmethod
        def poll(cls, context):
            rs = runtime_state.get()
            return rs is not None and rs.is_runtime_active()

        def execute(self, context):
            guard_key = f"{self.bl_idname}:{id(context.active_object)}"
            with operator_guard(self.bl_idname, reentry_key=guard_key) as gctx:
                if gctx.cancelled:
                    self.report({'WARNING'}, gctx.cancel_reason)
                    return {'CANCELLED'}
                # do work
                result = run_analysis(context)
            return {'FINISHED'}

    # 2. lock_guard() — for modal operators
    def invoke(self, context, event):
        try:
            with lock_guard(self.bl_idname, context_hint="Arc Polish"):
                # This block sets up the modal, lock released when block exits
                context.window_manager.modal_handler_add(self)
                return {'RUNNING_MODAL'}
        except ModalLockError as exc:
            self.report({'WARNING'}, f"Another operator is running: {exc}")
            return {'CANCELLED'}

    # 3. reentry_guard() — for internal analysis helpers
    def _compute_spacing(obj_name, frame_range):
        key = f"spacing:{obj_name}:{frame_range}"
        with reentry_guard(key, timeout_s=5.0):
            return _do_spacing_math(obj_name, frame_range)

    # 4. safe_execute() — for functional / fire-and-forget patterns
    result = safe_execute(
        run_euler_filter,
        args=(armature_name, bone_names),
        caller="ONIXEY3_OT_euler_fix",
        capture_exceptions=True,
    )
    if not result.ok:
        log.error("Euler filter failed: %s", result.error)
    return result.blender_set

    # 5. context_guard() — validate Blender context before heavy work
    cv = context_guard(context, expected_mode="POSE", expected_type="ARMATURE")
    if not cv.valid:
        self.report({'WARNING'}, cv.reason)
        return {'CANCELLED'}
"""

from __future__ import annotations

import logging
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, FrozenSet, Generator, Optional, Set, Tuple, TYPE_CHECKING
)

# state.py is the only import from within the runtime package.
# Import specific names — never ``from . import state as _state_mod``
# because that would bind to the module object and survive F8 reloads
# with a stale reference.
from onixey3.runtime.state import (
    # Names mapped to the real state.py (Onixey V3 / state.py 3.1.0)
    RuntimeStateManager  as RuntimeState,       # public alias for type hints
    LifecyclePhase,                             # FSM enum — same name
    ModalLock            as _ModalLock,         # dataclass, not an error type
    IntegritySignal      as _IntegritySignal,   # enum — used for signal checks
    get                  as _state_get,         # module-level singleton accessor
    reset                as _state_reset,       # module-level reset (reload)
)

# ── Compatibility shims ───────────────────────────────────────────────────────
# These names are used throughout guards.py. They are thin wrappers so that
# the rest of the file needs zero changes when state.py evolves.

class RuntimeFlag:
    """
    Namespace shim for RuntimeFlags field names used as string keys.
    guards.py calls rs.get_flag(RuntimeFlag.INTEGRITY_FAULT) which maps to
    RuntimeStateManager.get_flag('integrity_fault') — the actual field name
    in RuntimeFlags dataclass.
    """
    INTEGRITY_FAULT = "integrity_fault"         # RuntimeFlags.hard_requirements_met == False
    ANALYSIS_RUNNING = "analysis_running"
    RELOAD_IN_PROGRESS = "reload_in_progress"


class RuntimeStateError(RuntimeError):
    """
    Raised by guards.py when the runtime state blocks execution.
    Distinct from OnixeyRuntimeError (exceptions.py) — this is a guard-level
    lightweight error, not a full forensic exception.
    """
    pass


class ModalLockError(RuntimeError):
    """
    Raised by lock_guard() when the modal lock is already held.
    Callers catch this and return {'CANCELLED'} with a user-facing message.
    """
    pass


class ReentryError(RuntimeError):
    """
    Raised by reentry_guard() when raise_on_busy=True and the key is busy.
    """
    pass


def _state_get_or_raise() -> RuntimeState:
    """
    Return the active RuntimeStateManager or raise RuntimeStateError.
    Wraps state.get() with a clear error for call sites that require
    an initialized runtime.
    """
    rs = _state_get()
    if rs is None:
        raise RuntimeStateError(
            "RuntimeStateManager is None. "
            "Ensure lifecycle.startup() was called before accessing runtime guards."
        )
    return rs

_log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 1 — RESULT TYPES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """
    Structured result from safe_execute() and guarded callables.

    Attributes:
        ok:          True if the body ran and returned without raising.
        cancelled:   True if a guard condition blocked execution before the
                     body ran. Mutually exclusive with ok.
        result:      Return value of the body callable if ok=True, else None.
        error:       Exception instance if ok=False and cancelled=False.
                     None otherwise (ok or cancelled path).
        cancel_reason: Human-readable explanation of why execution was cancelled.
                       Non-empty only when cancelled=True.
        guard_ms:    Wall time spent acquiring/releasing guards (ms).
        body_ms:     Wall time spent in the body callable (ms). 0 if cancelled.
        caller:      Identifier of the caller (bl_idname or function name).
        ts:          ISO timestamp of the execution attempt.
    """
    ok:            bool
    cancelled:     bool
    result:        Any        = None
    error:         Optional[BaseException] = None
    cancel_reason: str        = ""
    guard_ms:      float      = 0.0
    body_ms:       float      = 0.0
    caller:        str        = ""
    ts:            str        = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(time.monotonic() * 1000) % 1000:03d}")

    @property
    def blender_set(self) -> Set[str]:
        """
        Ready-to-return Blender operator result set.

        Returns ``{'FINISHED'}`` if ok, ``{'CANCELLED'}`` otherwise.
        Use as ``return result.blender_set`` at the end of execute().
        """
        return {"FINISHED"} if self.ok else {"CANCELLED"}

    @property
    def failed(self) -> bool:
        """True if the body raised an exception (not ok, not cancelled)."""
        return not self.ok and not self.cancelled

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict snapshot. Error is represented as its string."""
        return {
            "ok":            self.ok,
            "cancelled":     self.cancelled,
            "failed":        self.failed,
            "cancel_reason": self.cancel_reason,
            "error":         repr(self.error) if self.error else None,
            "guard_ms":      round(self.guard_ms, 3),
            "body_ms":       round(self.body_ms, 3),
            "caller":        self.caller,
            "ts":            self.ts,
        }


@dataclass
class GuardContext:
    """
    Yielded by ``operator_guard()`` and ``reentry_guard()`` context managers.

    Attributes:
        cancelled:     True if a guard blocked entry. The with-block body
                       should check this FIRST and return {'CANCELLED'} if True.
        cancel_reason: Human-readable reason for cancellation (empty if not cancelled).
        guard_key:     The reentry guard key that was acquired, or "" if none.
        acquired_at:   time.monotonic() at which the guard was acquired, or 0.0.
        operator:      Opaque reference to the operator instance, stored to
                       allow guarded context to call self.report() via
                       report_blender(). May be None.
    """
    cancelled:     bool
    cancel_reason: str    = ""
    guard_key:     str    = ""
    acquired_at:   float  = 0.0
    operator:      Any    = None     # bpy.types.Operator — not typed to avoid bpy import

    def report_blender(self, severity: str, message: str) -> None:
        """
        Call self.operator.report() if an operator reference is available.

        Args:
            severity: Blender report type string: 'INFO', 'WARNING', 'ERROR'.
            message:  Human-readable message.

        If operator is None (guard used outside an operator context), logs
        instead of calling report(), so the message is never silently lost.
        """
        if self.operator is not None and hasattr(self.operator, "report"):
            try:
                self.operator.report({severity}, message)
            except Exception as exc:
                _log.error(
                    "GuardContext.report_blender: operator.report() raised %s: %s",
                    type(exc).__name__, exc,
                )
        else:
            level = {
                "INFO":    logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR":   logging.ERROR,
            }.get(severity, logging.DEBUG)
            _log.log(level, "[guard_report] %s", message)


@dataclass
class ContextValidation:
    """
    Result of ``context_guard()``.

    Attributes:
        valid:   True if all requested checks passed.
        reason:  Human-readable explanation if valid=False. Empty if valid=True.
        checks:  Dict mapping check name → bool for detailed diagnostics.
    """
    valid:   bool
    reason:  str            = ""
    checks:  Dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "valid":  self.valid,
            "reason": self.reason,
            "checks": self.checks,
        }


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 2 — INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _runtime_active_or_cancel(
    require_fully_active: bool = False,
    caller: str = "",
) -> Optional[str]:
    """
    Check runtime state and return a cancel reason string, or None if OK.

    Args:
        require_fully_active: If True, requires ACTIVE phase (not DEGRADED).
        caller:               Identifier for log messages.

    Returns:
        None if runtime state is acceptable.
        A non-empty string (the cancel reason) if execution should be blocked.

    Never raises.
    """
    try:
        rs: Optional[RuntimeState] = _state_get()
    except Exception as exc:
        _log.error(
            "guards._runtime_active_or_cancel: state.get() raised %s: %s. "
            "Caller: %r. Blocking execution.",
            type(exc).__name__, exc, caller,
        )
        return f"Runtime state unavailable: {type(exc).__name__}"

    if rs is None:
        _log.info(
            "guards: Runtime not initialized. Caller: %r. Cancelling.", caller
        )
        return "Onixey runtime is not initialized."

    if require_fully_active:
        if not rs.is_fully_active():
            _log.info(
                "guards: Runtime not ACTIVE (phase=%s). "
                "Caller %r requires fully active. Cancelling.",
                rs.phase.value, caller,
            )
            return (
                f"Onixey is not fully active (phase: {rs.phase.value}). "
                "Wait for startup to complete."
            )
    else:
        if not rs.is_runtime_active():
            _log.info(
                "guards: Runtime not operational (phase=%s). Caller: %r. Cancelling.",
                rs.phase.value, caller,
            )
            return (
                f"Onixey runtime is not operational (phase: {rs.phase.value}). "
                "The addon may be starting up or shutting down."
            )

    if rs.get_flag(RuntimeFlag.INTEGRITY_FAULT):
        _log.warning(
            "guards: RuntimeFlag.INTEGRITY_FAULT is set. Caller: %r. "
            "Execution proceeds but may produce incorrect results.",
            caller,
        )
        # INTEGRITY_FAULT is a WARNING, not a block — callers may choose to
        # proceed. We log here so the condition is always visible.

    return None   # All clear


def _acquire_reentry_safe(
    rs: RuntimeState,
    key: str,
    timeout_s: float,
    owner_hint: str,
) -> Optional[str]:
    """
    Attempt to acquire a reentry guard. Return cancel reason on failure.

    Returns:
        None if acquired successfully.
        Cancel reason string if blocked or if an unexpected error occurred.
    """
    try:
        rs.acquire_reentry_guard(key, timeout_s=timeout_s, owner_hint=owner_hint)
        return None  # Acquired
    except ReentryError as exc:
        _log.info(
            "guards: Reentry blocked for key %r (owner_hint=%r): %s",
            key, owner_hint, exc,
        )
        return f"Already running: {exc}"
    except ValueError as exc:
        _log.error(
            "guards: Invalid reentry guard key %r: %s. owner_hint=%r.",
            key, exc, owner_hint,
        )
        return f"Guard configuration error: {exc}"
    except Exception as exc:
        _log.error(
            "guards: Unexpected error acquiring reentry guard %r: %s\n%s",
            key, exc, traceback.format_exc(),
        )
        return f"Guard acquisition failed: {type(exc).__name__}"


def _release_reentry_safe(rs: RuntimeState, key: str, caller: str) -> None:
    """
    Release a reentry guard without raising.

    Uses release_reentry_guard_safe() which is idempotent — safe to call
    even if the guard was stale-expired mid-execution.
    """
    try:
        rs.release_reentry_guard_safe(key)
    except ValueError as exc:
        # Empty key — programming error at the call site.
        _log.error(
            "guards._release_reentry_safe: ValueError releasing key %r "
            "(caller=%r): %s",
            key, caller, exc,
        )
    except Exception as exc:
        _log.error(
            "guards._release_reentry_safe: Unexpected error releasing key %r "
            "(caller=%r): %s\n%s",
            key, caller, exc, traceback.format_exc(),
        )


def _release_modal_safe(rs: RuntimeState, holder: str) -> None:
    """
    Force-release the modal lock without raising.

    Calls release_modal_lock() with the real holder name. If the lock
    is not held by this holder (e.g., stale lock from a crashed operator),
    the release is idempotent — it logs a WARNING but does not raise.
    """
    try:
        rs.release_modal_lock(holder)
    except Exception as exc:
        _log.error(
            "guards._release_modal_safe: Unexpected error releasing modal lock "
            "(holder=%r): %s\n%s",
            holder, exc, traceback.format_exc(),
        )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 3 — safe_execute()
# ──────────────────────────────────────────────────────────────────────────────

def safe_execute(
    fn:                   Callable,
    args:                 Tuple                = (),
    kwargs:               Dict[str, Any]       = None,
    caller:               str                  = "",
    reentry_key:          Optional[str]        = None,
    reentry_timeout_s:    float                = 10.0,
    require_fully_active: bool                 = False,
    capture_exceptions:   bool                 = True,
) -> ExecutionResult:
    """
    Execute ``fn(*args, **kwargs)`` inside a full protection shell.

    Checks performed before calling fn:
        1. Runtime state is available and in an operational phase.
        2. If ``reentry_key`` is provided, acquires the reentry guard.

    Guarantees:
        • The reentry guard is always released in a finally block.
        • If ``capture_exceptions=True`` (default), any exception from fn is
          caught, logged with full traceback, and returned in result.error.
          The caller inspects result.ok and result.error.
        • If ``capture_exceptions=False``, exceptions propagate after the
          finally block releases the guard.

    Args:
        fn:                   Callable to execute.
        args:                 Positional arguments to fn.
        kwargs:               Keyword arguments to fn. Defaults to {}.
        caller:               Identifier for logging (bl_idname, function name).
        reentry_key:          If provided, acquires a named reentry guard
                              before calling fn. Guard is released in finally.
        reentry_timeout_s:    Timeout for the reentry guard (seconds).
        require_fully_active: If True, blocks in DEGRADED phase as well as
                              non-operational phases.
        capture_exceptions:   If True, catches all exceptions from fn and
                              returns them in result.error. If False, re-raises.

    Returns:
        ExecutionResult with full diagnostic information.

    Note:
        safe_execute() does NOT acquire the modal lock. For modal operators
        use lock_guard() instead. Mixing both on the same operation is valid:
        acquire the modal lock with lock_guard(), then call safe_execute()
        for individual steps within the modal.
    """
    if kwargs is None:
        kwargs = {}

    caller = caller or getattr(fn, "__qualname__", repr(fn))
    _ms     = int(time.monotonic() * 1000) % 1000
    ts_str  = time.strftime("%Y-%m-%dT%H:%M:%S") + f".{_ms:03d}"
    guard_t0 = time.monotonic()

    # ── Phase 1: Runtime check ────────────────────────────────────────────────
    cancel_reason = _runtime_active_or_cancel(
        require_fully_active=require_fully_active,
        caller=caller,
    )
    if cancel_reason:
        guard_ms = (time.monotonic() - guard_t0) * 1000.0
        return ExecutionResult(
            ok=False,
            cancelled=True,
            cancel_reason=cancel_reason,
            guard_ms=guard_ms,
            caller=caller,
            ts=ts_str,
        )

    # ── Phase 2: Reentry guard acquisition ───────────────────────────────────
    rs: Optional[RuntimeState] = _state_get()
    guard_acquired = False

    if reentry_key and rs is not None:
        cancel_reason = _acquire_reentry_safe(
            rs, reentry_key, reentry_timeout_s, owner_hint=caller
        )
        if cancel_reason:
            guard_ms = (time.monotonic() - guard_t0) * 1000.0
            return ExecutionResult(
                ok=False,
                cancelled=True,
                cancel_reason=cancel_reason,
                guard_ms=guard_ms,
                caller=caller,
                ts=ts_str,
            )
        guard_acquired = True

    guard_ms = (time.monotonic() - guard_t0) * 1000.0
    _log.debug(
        "safe_execute: entering body (caller=%r, reentry_key=%r, guard_ms=%.3f).",
        caller, reentry_key, guard_ms,
    )

    # ── Phase 3: Execute body with guaranteed guard release ───────────────────
    body_t0 = time.monotonic()
    try:
        return_value = fn(*args, **kwargs)
        body_ms = (time.monotonic() - body_t0) * 1000.0
        _log.debug(
            "safe_execute: body completed (caller=%r, body_ms=%.3f).",
            caller, body_ms,
        )
        return ExecutionResult(
            ok=True,
            cancelled=False,
            result=return_value,
            guard_ms=guard_ms,
            body_ms=body_ms,
            caller=caller,
            ts=ts_str,
        )

    except Exception as exc:
        body_ms = (time.monotonic() - body_t0) * 1000.0
        _log.error(
            "safe_execute: body raised %s (caller=%r, body_ms=%.3f):\n%s",
            type(exc).__name__, caller, body_ms,
            traceback.format_exc(),
        )
        if not capture_exceptions:
            raise
        return ExecutionResult(
            ok=False,
            cancelled=False,
            error=exc,
            guard_ms=guard_ms,
            body_ms=body_ms,
            caller=caller,
            ts=ts_str,
        )

    finally:
        if guard_acquired and reentry_key and rs is not None:
            _release_reentry_safe(rs, reentry_key, caller=caller)
            _log.debug(
                "safe_execute: reentry guard released (key=%r, caller=%r).",
                reentry_key, caller,
            )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 4 — operator_guard()
# ──────────────────────────────────────────────────────────────────────────────

@contextmanager
def operator_guard(
    bl_idname:            str,
    reentry_key:          Optional[str]  = None,
    reentry_timeout_s:    float          = 10.0,
    require_fully_active: bool           = False,
    operator_instance:    Any            = None,
) -> Generator[GuardContext, None, None]:
    """
    Context manager for operator ``execute()`` methods.

    Performs the standard pre-execute checks and optionally acquires a
    named reentry guard. The guard is always released in the finally block.

    Yields a ``GuardContext``. The with-block body MUST check ``gctx.cancelled``
    before doing any work:

        with operator_guard(self.bl_idname, reentry_key=key) as gctx:
            if gctx.cancelled:
                self.report({'WARNING'}, gctx.cancel_reason)
                return {'CANCELLED'}
            # safe to proceed

    The guard does NOT return from the operator — that remains the caller's
    responsibility. This preserves full control over operator return values
    (allowing partial work to still return FINISHED where appropriate).

    Args:
        bl_idname:            The operator's bl_idname. Used as the guard
                              identifier and for log messages.
        reentry_key:          Optional reentry guard key. If None, no
                              reentry guard is acquired (modal-safe).
                              Recommended: ``f"{bl_idname}:{context.active_object.name}"``
        reentry_timeout_s:    Reentry guard timeout in seconds.
        require_fully_active: If True, cancel in DEGRADED phase.
        operator_instance:    The operator's ``self`` reference. Stored in
                              GuardContext so callers can use gctx.report_blender().

    Yields:
        GuardContext with cancelled=False if all checks passed,
        or cancelled=True with cancel_reason set.

    Never raises from guard machinery — only from the with-block body.
    """
    guard_t0 = time.monotonic()

    # ── Runtime check ─────────────────────────────────────────────────────────
    cancel_reason = _runtime_active_or_cancel(
        require_fully_active=require_fully_active,
        caller=bl_idname,
    )
    if cancel_reason:
        _log.info(
            "operator_guard: Cancelled before reentry check (bl_idname=%r): %s",
            bl_idname, cancel_reason,
        )
        yield GuardContext(
            cancelled=True,
            cancel_reason=cancel_reason,
            operator=operator_instance,
        )
        return

    # ── Reentry guard ─────────────────────────────────────────────────────────
    rs: Optional[RuntimeState] = _state_get()
    guard_acquired = False
    effective_key  = reentry_key or ""

    if effective_key and rs is not None:
        cancel_reason = _acquire_reentry_safe(
            rs, effective_key, reentry_timeout_s, owner_hint=bl_idname
        )
        if cancel_reason:
            _log.info(
                "operator_guard: Reentry blocked (bl_idname=%r, key=%r): %s",
                bl_idname, effective_key, cancel_reason,
            )
            yield GuardContext(
                cancelled=True,
                cancel_reason=cancel_reason,
                guard_key=effective_key,
                operator=operator_instance,
            )
            return
        guard_acquired = True

    guard_ms = (time.monotonic() - guard_t0) * 1000.0
    _log.debug(
        "operator_guard: Entered (bl_idname=%r, key=%r, guard_ms=%.3f).",
        bl_idname, effective_key, guard_ms,
    )

    gctx = GuardContext(
        cancelled=False,
        cancel_reason="",
        guard_key=effective_key,
        acquired_at=time.monotonic(),
        operator=operator_instance,
    )

    # ── Yield to with-block body ───────────────────────────────────────────────
    try:
        yield gctx
    except Exception:
        # Do NOT swallow — re-raise after releasing the guard.
        # Operator code exceptions must propagate to Blender's error reporter.
        _log.error(
            "operator_guard: Exception in with-block body (bl_idname=%r):\n%s",
            bl_idname, traceback.format_exc(),
        )
        raise
    finally:
        if guard_acquired and effective_key and rs is not None:
            _release_reentry_safe(rs, effective_key, caller=bl_idname)
            _log.debug(
                "operator_guard: Released reentry guard (bl_idname=%r, key=%r).",
                bl_idname, effective_key,
            )


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 5 — reentry_guard()
# ──────────────────────────────────────────────────────────────────────────────

@contextmanager
def reentry_guard(
    key:          str,
    timeout_s:    float = 10.0,
    owner_hint:   str   = "",
    raise_on_busy: bool = True,
) -> Generator[bool, None, None]:
    """
    Lightweight context manager: acquires and releases a named reentry guard.

    Intended for analysis helpers and internal functions that are called
    from both operators AND handlers — contexts where operator_guard() would
    be too heavy (no operator instance, no Blender context).

    Does NOT check runtime phase — use operator_guard() if phase checking
    is required. This guard focuses solely on reentry prevention.

    Args:
        key:          Reentry guard key. Must be non-empty.
        timeout_s:    Stale guard auto-expire timeout.
        owner_hint:   Optional diagnostic string.
        raise_on_busy: If True (default), raises ReentryError when the
                       guard is already held. If False, yields False and
                       the with-block can check the result and skip work.

    Yields:
        True if the guard was acquired (with-block may proceed).
        False if the guard was NOT acquired (only when raise_on_busy=False).

    Raises:
        ReentryError: if raise_on_busy=True and the key is already held.
        ValueError:   if key is empty (always — this is a programming error).

    Usage:
        # raise_on_busy=True (strict — always raise)
        try:
            with reentry_guard("analysis:euler:ArmatureX"):
                run_euler_filter()
        except ReentryError:
            _log.info("Euler filter already running — skipping this call.")

        # raise_on_busy=False (lenient — check the yielded bool)
        with reentry_guard("analysis:spacing", raise_on_busy=False) as acquired:
            if not acquired:
                return   # Already running — safe to skip
            run_spacing_analysis()
    """
    if not key or not key.strip():
        raise ValueError(
            f"reentry_guard: key must be a non-empty string, got {key!r}."
        )

    rs: Optional[RuntimeState] = _state_get()

    # If runtime is not initialized, still allow execution — the guard's
    # purpose is reentry prevention, not runtime gating. Log and skip guard.
    if rs is None:
        _log.debug(
            "reentry_guard: RuntimeState not available (key=%r). "
            "Proceeding without guard — no reentry protection.",
            key,
        )
        yield True
        return

    # Attempt acquisition
    cancel_reason = _acquire_reentry_safe(rs, key, timeout_s, owner_hint=owner_hint)

    if cancel_reason:
        if raise_on_busy:
            raise ReentryError(
                f"Reentry guard busy for key {key!r}: {cancel_reason}"
            )
        else:
            _log.info(
                "reentry_guard: Busy, yielding False (key=%r): %s", key, cancel_reason
            )
            yield False
            return

    _log.debug("reentry_guard: Acquired (key=%r, owner_hint=%r).", key, owner_hint)

    try:
        yield True
    except Exception:
        _log.error(
            "reentry_guard: Exception in with-block (key=%r):\n%s",
            key, traceback.format_exc(),
        )
        raise
    finally:
        _release_reentry_safe(rs, key, caller=owner_hint or key)
        _log.debug("reentry_guard: Released (key=%r).", key)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 6 — context_guard()
# ──────────────────────────────────────────────────────────────────────────────

# Valid Blender context mode strings — subset used by Onixey.
_KNOWN_MODES: FrozenSet[str] = frozenset({
    "OBJECT",
    "POSE",
    "EDIT_ARMATURE",
    "EDIT_MESH",
    "WEIGHT_PAINT",
    "PAINT_WEIGHT",
})

# Valid object type strings checked by context_guard.
_KNOWN_OBJECT_TYPES: FrozenSet[str] = frozenset({
    "ARMATURE",
    "MESH",
    "EMPTY",
    "CURVE",
    "LATTICE",
})


def context_guard(
    context:                Any,
    expected_mode:          Optional[str]                = None,
    expected_type:          Optional[str]                = None,
    require_active_object:  bool                         = True,
    require_selected_bones: bool                         = False,
    custom_checks:          Optional[Dict[str, Callable[[Any], bool]]] = None,
) -> ContextValidation:
    """
    Validate a Blender context before entering heavy execution paths.

    This is an IMPERATIVE function (not a context manager) — call it at the
    top of execute() after guard acquisition, check result.valid, and return
    {'CANCELLED'} with a report if validation fails.

    Avoids crash-on-access patterns like:
        obj = context.active_object          # could be None
        bones = context.selected_pose_bones  # AttributeError in OBJECT mode

    Args:
        context:               The bpy.context passed to execute().
        expected_mode:         If provided, context.mode must equal this string.
                               Example: "POSE", "OBJECT".
        expected_type:         If provided, context.active_object.type must
                               equal this string. Example: "ARMATURE".
        require_active_object: If True, fails if context.active_object is None.
        require_selected_bones:If True (POSE mode), fails if
                               context.selected_pose_bones is empty.
        custom_checks:         Dict mapping check_name → callable(context) → bool.
                               Custom predicates are run after built-in checks.
                               Callables must not raise.

    Returns:
        ContextValidation with valid=True if all checks pass,
        or valid=False with reason and checks dict.

    Never raises. If accessing a context attribute itself raises, that
    specific check fails with a descriptive reason.

    Note:
        context_guard does NOT import bpy. It accesses context attributes
        via getattr() with defaults, so it works with real bpy contexts and
        with mock contexts in unit tests.
    """
    checks:  Dict[str, bool] = {}
    reasons: list            = []

    # ── Check: context is not None ────────────────────────────────────────────
    if context is None:
        return ContextValidation(
            valid=False,
            reason="Blender context is None.",
            checks={"context_not_none": False},
        )
    checks["context_not_none"] = True

    # ── Check: expected mode ──────────────────────────────────────────────────
    if expected_mode is not None:
        try:
            actual_mode = getattr(context, "mode", None)
            mode_ok = actual_mode == expected_mode
            checks["mode"] = mode_ok
            if not mode_ok:
                reasons.append(
                    f"Mode mismatch: expected {expected_mode!r}, "
                    f"got {actual_mode!r}. "
                    f"Switch to {expected_mode} mode before running this operator."
                )
        except Exception as exc:
            checks["mode"] = False
            reasons.append(f"Could not read context.mode: {exc}.")

    # ── Check: active object presence ────────────────────────────────────────
    active_obj = None
    if require_active_object or expected_type is not None:
        try:
            active_obj = getattr(context, "active_object", None)
            obj_present = active_obj is not None
            checks["active_object"] = obj_present
            if not obj_present:
                reasons.append(
                    "No active object. Select an object before running this operator."
                )
        except Exception as exc:
            checks["active_object"] = False
            reasons.append(f"Could not read context.active_object: {exc}.")

    # ── Check: object type ────────────────────────────────────────────────────
    if expected_type is not None and active_obj is not None:
        try:
            actual_type = getattr(active_obj, "type", None)
            type_ok = actual_type == expected_type
            checks["object_type"] = type_ok
            if not type_ok:
                reasons.append(
                    f"Object type mismatch: expected {expected_type!r}, "
                    f"got {actual_type!r}. "
                    f"Select a {expected_type} object."
                )
        except Exception as exc:
            checks["object_type"] = False
            reasons.append(f"Could not read active_object.type: {exc}.")

    # ── Check: selected pose bones ────────────────────────────────────────────
    if require_selected_bones:
        try:
            bones = getattr(context, "selected_pose_bones", None)
            bones_ok = bool(bones)
            checks["selected_pose_bones"] = bones_ok
            if not bones_ok:
                reasons.append(
                    "No pose bones selected. "
                    "Select at least one bone in Pose Mode."
                )
        except Exception as exc:
            checks["selected_pose_bones"] = False
            reasons.append(f"Could not read context.selected_pose_bones: {exc}.")

    # ── Custom checks ─────────────────────────────────────────────────────────
    if custom_checks:
        for check_name, predicate in custom_checks.items():
            try:
                result = bool(predicate(context))
                checks[check_name] = result
                if not result:
                    reasons.append(
                        f"Custom check {check_name!r} failed."
                    )
            except Exception as exc:
                checks[check_name] = False
                reasons.append(
                    f"Custom check {check_name!r} raised {type(exc).__name__}: {exc}."
                )
                _log.error(
                    "context_guard: Custom check %r raised: %s\n%s",
                    check_name, exc, traceback.format_exc(),
                )

    # ── Assemble result ───────────────────────────────────────────────────────
    all_passed = all(checks.values())
    reason_str = " | ".join(reasons) if reasons else ""

    if not all_passed:
        _log.debug(
            "context_guard: Validation failed — %s. Checks: %s",
            reason_str, checks,
        )

    return ContextValidation(valid=all_passed, reason=reason_str, checks=checks)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 7 — lock_guard()
# ──────────────────────────────────────────────────────────────────────────────

@contextmanager
def lock_guard(
    holder:           str,
    timeout_s:        float = 30.0,
    context_hint:     str   = "",
    require_active:   bool  = True,
) -> Generator[None, None, None]:
    """
    Context manager for modal operators. Acquires the global modal lock on
    entry and force-releases it on exit.

    Usage:
        def invoke(self, context, event):
            try:
                with lock_guard(self.bl_idname, context_hint="Arc Polish"):
                    context.window_manager.modal_handler_add(self)
                    self._lock_held = True
                    return {'RUNNING_MODAL'}
            except ModalLockError as exc:
                self.report({'WARNING'}, str(exc))
                return {'CANCELLED'}

        def modal(self, context, event):
            if event.type in {'ESC', 'RIGHTMOUSE'}:
                # Lock is released in cancel() via lock_guard exit
                return self.cancel(context)
            ...

        def cancel(self, context):
            # If lock_guard wraps invoke(), it only covers the setup block.
            # For persistent modal locks, acquire/release explicitly in
            # execute()/cancel() and use lock_guard only for the setup phase.
            return {'CANCELLED'}

    IMPORTANT: lock_guard() covers the with-block only. If your modal loop
    runs beyond the with-block (as is typical for RUNNING_MODAL), you need
    to acquire the lock explicitly via:
        state.acquire_modal_lock(holder)
    and release it in cancel() / finish() via:
        state.release_modal_lock(holder)

    For simple cases where the modal setup and teardown happen in the same
    code path, lock_guard() handles both automatically.

    Args:
        holder:         bl_idname of the acquiring operator.
        timeout_s:      Lock stale-expiry timeout in seconds.
        context_hint:   Optional diagnostic description of what this modal does.
        require_active: If True, check runtime is operational before acquiring.

    Yields:
        Nothing (None). The with-block proceeds after successful acquisition.

    Raises:
        ModalLockError:     if the lock is already held by a live operator.
        RuntimeStateError:  if require_active=True and runtime is not available.
        ValueError:         if holder is empty.
    """
    if not holder or not holder.strip():
        raise ValueError(
            f"lock_guard: holder must be a non-empty string, got {holder!r}."
        )

    # ── Optional runtime check ────────────────────────────────────────────────
    if require_active:
        cancel_reason = _runtime_active_or_cancel(caller=holder)
        if cancel_reason:
            raise RuntimeStateError(
                f"lock_guard: Runtime not available for {holder!r}: {cancel_reason}"
            )

    rs: Optional[RuntimeState] = _state_get()
    if rs is None:
        if require_active:
            raise RuntimeStateError(
                f"lock_guard: RuntimeState is None. "
                f"lifecycle.startup() must be called before using lock_guard. "
                f"Holder: {holder!r}."
            )
        else:
            # No state — skip lock acquisition and yield directly.
            _log.debug(
                "lock_guard: No RuntimeState, skipping lock (holder=%r).", holder
            )
            yield
            return

    # ── Acquire modal lock ────────────────────────────────────────────────────
    # May raise ModalLockError — propagates to caller without catching.
    rs.acquire_modal_lock(
        holder=holder,
        timeout_s=timeout_s,
        context_hint=context_hint,
    )
    _log.debug(
        "lock_guard: Modal lock acquired (holder=%r, hint=%r).", holder, context_hint
    )

    # ── Yield to with-block ───────────────────────────────────────────────────
    try:
        yield
    except Exception:
        _log.error(
            "lock_guard: Exception in with-block (holder=%r):\n%s",
            holder, traceback.format_exc(),
        )
        raise
    finally:
        # Force-release: if the holder crashed, the lock must still be freed.
        _release_modal_safe(rs, holder)
        _log.debug("lock_guard: Modal lock released (holder=%r).", holder)


# ──────────────────────────────────────────────────────────────────────────────
# SECTION 8 — GUARD DIAGNOSTICS
# ──────────────────────────────────────────────────────────────────────────────

def get_guard_snapshot() -> Dict[str, Any]:
    """
    Return a plain-dict snapshot of all active guards for diagnostics.

    Safe to call at any time — returns partial/empty data if runtime is
    not initialized. Never raises.

    Used by:
        lifecycle.get_runtime_report()
        validation/stress.py
        Future: ONIXEY3_OT_debug_diagnostics operator

    Returns a dict with:
        runtime_available:  bool — False if state singleton is None
        phase:              str  — current LifecyclePhase value
        modal_lock:         Optional[dict] — ModalLock.as_dict() or None
        active_guards:      dict — {key: ReentryGuard.as_dict()} for non-stale guards
        stale_guards:       list — keys of stale-but-not-expired guards
        total_guard_count:  int  — all guards including stale
    """
    try:
        rs = _state_get()
    except Exception as exc:
        _log.error(
            "get_guard_snapshot: state.get() raised %s: %s", type(exc).__name__, exc
        )
        return {
            "runtime_available": False,
            "error":             repr(exc),
        }

    if rs is None:
        return {
            "runtime_available": False,
            "phase":             "uninitialized",
            "modal_lock":        None,
            "active_guards":     {},
            "stale_guards":      [],
            "total_guard_count": 0,
        }

    try:
        modal_snap = rs.modal_lock_holder
        modal_lock_dict = None
        if rs._modal_lock is not None:
            try:
                modal_lock_dict = rs._modal_lock.as_dict()
            except Exception as exc:
                modal_lock_dict = {"error": repr(exc)}

        active_guards: Dict[str, Any] = {}
        stale_guards:  list            = []

        # Access internal guards dict safely.
        try:
            for k, g in rs._reentry_guards.items():
                if g.is_stale():
                    stale_guards.append(k)
                else:
                    active_guards[k] = g.as_dict()
        except Exception as exc:
            _log.error(
                "get_guard_snapshot: Error iterating reentry guards: %s", exc
            )

        return {
            "runtime_available": True,
            "phase":             rs.phase.value,
            "modal_lock":        modal_lock_dict,
            "active_guards":     active_guards,
            "stale_guards":      stale_guards,
            "total_guard_count": len(getattr(rs, "_reentry_guards", {})),
        }

    except Exception as exc:
        _log.error(
            "get_guard_snapshot: Unexpected error building snapshot: %s\n%s",
            exc, traceback.format_exc(),
        )
        return {
            "runtime_available": True,
            "phase":             rs.phase.value if rs else "unknown",
            "error":             repr(exc),
        }
