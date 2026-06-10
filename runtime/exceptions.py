"""
onixey3/runtime/exceptions.py

Centralized Runtime Exception Hierarchy — Onixey V3 / Blender 4.2+

RESPONSIBILITY
──────────────
Define every exception type that can be raised by the Onixey V3 runtime
subsystems (state, cache, session, lifecycle, handlers, reload).

Centralizing exceptions in one module with zero dependencies means any
other module in the codebase can import from here without risk of circular
imports, import-time side effects, or transitive bpy loading.

WHAT THIS MODULE DOES NOT DO
─────────────────────────────
    - Does NOT import bpy (at any scope).
    - Does NOT register handlers or bpy classes.
    - Does NOT access the filesystem.
    - Does NOT produce any side effects on import.
    - Does NOT log anything automatically (logging is the caller's responsibility).

EXCEPTION HIERARCHY
───────────────────
    Exception
    └── OnixeyRuntimeError                     Base for all Onixey runtime errors.
        ├── OnixeyRuntimeStateError            FSM / phase violations.
        │   ├── OnixeyInvalidPhaseTransition   Illegal LifecyclePhase edge.
        │   └── OnixeyPhaseConflict            Operation not allowed in current phase.
        ├── OnixeyReloadError                  Module reload failures.
        │   ├── OnixeyReloadTransactionError   Atomic transaction failed.
        │   ├── OnixeyRollbackError            Rollback itself failed.
        │   └── OnixeyCircularDependencyError  Circular dep in module graph.
        ├── OnixeySessionError                 Session state / weakref issues.
        │   ├── OnixeySessionNotInitialized    get_or_raise() before startup().
        │   └── OnixeySessionObjectInvalid     Weakref to bpy object is dead.
        ├── OnixeyCacheError                   Cache subsystem failures.
        │   ├── OnixeyCacheNotInitialized      cache_get/set before _init().
        │   └── OnixeyCacheKeyError            Malformed or unknown cache key.
        ├── OnixeyHandlerError                 bpy.app.handlers registration issues.
        │   ├── OnixeyHandlerDuplicateError    Handler registered more than once.
        │   └── OnixeyHandlerLeakError         Handler leaked across reload cycle.
        ├── OnixeyLifecycleError               Addon lifecycle contract violations.
        │   ├── OnixeyDoubleInitError          initialize() called twice.
        │   └── OnixeyPrematureAccessError     Module accessed before initialization.
        └── OnixeyIntegrityError               Runtime self-check failures.
            ├── OnixeyHardRequirementError     fallback_ok=False flag not available.
            └── OnixeyStateCorruptionError     Internal state invariant violated.

USAGE
─────
    Raise:
        from onixey3.runtime.exceptions import OnixeyReloadTransactionError
        raise OnixeyReloadTransactionError(
            "Transaction failed at 'onixey3.core.feature_flags'.",
            module="onixey3.core.feature_flags",
            generation=3,
        )

    Catch (broad):
        from onixey3.runtime.exceptions import OnixeyRuntimeError
        try:
            ...
        except OnixeyRuntimeError as exc:
            log.error("Onixey runtime error: %s", exc.formatted())

    Catch (specific):
        from onixey3.runtime.exceptions import OnixeySessionNotInitialized
        try:
            state = session.get_or_raise()
        except OnixeySessionNotInitialized:
            self.report({'ERROR'}, "Onixey session is not active.")
            return {'CANCELLED'}

    Inspect context:
        exc.context          # dict of structured payload
        exc.subsystem        # e.g. "reload", "cache", "session"
        exc.formatted()      # multi-line human-readable string
        exc.as_dict()        # JSON-serializable dict for diagnostics

DESIGN PRINCIPLES
─────────────────
    1. Every exception carries a structured `context` dict so callers can
       log or display precise details without string parsing.

    2. `formatted()` produces a consistent multi-line string suitable for
       Blender's Info header, the system console, or a diagnostic report.

    3. `as_dict()` returns a JSON-serializable snapshot — useful for the
       forensic event log and crash reporting.

    4. Subclass `__init__` signatures are explicit (no **kwargs catch-alls)
       so IDEs and type checkers surface missing fields at call sites.

    5. The hierarchy is designed to be extended: adding a new subsystem
       requires only subclassing OnixeyRuntimeError or an appropriate
       mid-level class. No registry, no metaclass magic.

CHANGELOG
─────────
    3.1.0 — Initial implementation.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional


# ══════════════════════════════════════════════════════════════════════════════
# BASE
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyRuntimeError(Exception):
    """
    Base class for all Onixey V3 runtime exceptions.

    Every exception in this hierarchy:
        - Carries a `subsystem` tag identifying which part of the runtime raised it.
        - Carries a `context` dict of structured, machine-readable detail.
        - Provides `formatted()` for human-readable multi-line output.
        - Provides `as_dict()` for JSON-serializable diagnostic snapshots.
        - Records a monotonic timestamp at construction time.

    Attributes:
        message (str):         Primary human-readable description.
        subsystem (str):       Runtime subsystem tag (e.g. "reload", "cache").
        context (Dict):        Structured payload — varies by subclass.
        timestamp (float):     time.monotonic() at construction.
        timestamp_str (str):   ISO wall-clock at construction (for logs/display).
    """

    def __init__(
        self,
        message:   str,
        subsystem: str = "runtime",
        **context: Any,
    ) -> None:
        """
        Args:
            message:   Human-readable error description.
            subsystem: Tag identifying the originating subsystem.
                       Convention: lowercase, no spaces. E.g. "reload", "cache".
            **context: Arbitrary key-value pairs with structured detail.
                       All values must be JSON-serializable (str, int, float, bool, None).
        """
        super().__init__(message)
        self.message:       str         = message
        self.subsystem:     str         = subsystem
        self.context:       Dict[str, Any] = dict(context)
        self.timestamp:     float       = time.monotonic()
        self.timestamp_str: str         = time.strftime("%Y-%m-%dT%H:%M:%S")

    def formatted(self) -> str:
        """
        Return a multi-line human-readable string describing the exception.

        Suitable for:
            - Blender's system console (bpy.ops.wm.call_menu not available everywhere).
            - The Onixey diagnostic panel.
            - Log files via logging.error(exc.formatted()).

        Example output:
            [OnixeyReloadTransactionError] reload
            Message : Transaction failed at 'onixey3.core.feature_flags'.
            module  : onixey3.core.feature_flags
            generation : 3
            ts      : 2025-01-15T14:32:01
        """
        lines = [
            f"[{type(self).__name__}]  subsystem={self.subsystem}",
            f"  Message : {self.message}",
        ]
        for k, v in self.context.items():
            lines.append(f"  {k:<12}: {v}")
        lines.append(f"  {'ts':<12}: {self.timestamp_str}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        """
        Return a JSON-serializable snapshot of this exception.

        Suitable for:
            - The forensic event log (feature_flags._log_forensic).
            - The reload manager's audit trail.
            - Crash reports.

        Returns:
            {
                "exception_type": "OnixeyReloadTransactionError",
                "subsystem":      "reload",
                "message":        "Transaction failed ...",
                "context":        {"module": "...", "generation": 3},
                "ts":             "2025-01-15T14:32:01",
            }
        """
        return {
            "exception_type": type(self).__name__,
            "subsystem":      self.subsystem,
            "message":        self.message,
            "context":        dict(self.context),
            "ts":             self.timestamp_str,
        }

    def __str__(self) -> str:
        """
        Compact single-line representation for logging and exception chains.

        Format: [ClassName/subsystem] message  (key=value ...)
        """
        ctx_str = "  ".join(f"{k}={v}" for k, v in self.context.items())
        base = f"[{type(self).__name__}/{self.subsystem}] {self.message}"
        return f"{base}  ({ctx_str})" if ctx_str else base

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"message={self.message!r}, "
            f"subsystem={self.subsystem!r}, "
            f"**{self.context!r}"
            f")"
        )


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME STATE ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyRuntimeStateError(OnixeyRuntimeError):
    """
    Base for errors related to RuntimeStateManager / LifecyclePhase violations.

    Raised when code attempts an operation that is incompatible with the
    current lifecycle phase or runtime flag state.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="state", **context)


class OnixeyInvalidPhaseTransition(OnixeyRuntimeStateError):
    """
    Raised when a LifecyclePhase FSM transition is attempted that is not
    listed in the valid-transition table.

    Example:
        Attempting begin_shutdown() while phase is INITIALIZING.

    Context keys:
        current_phase (str):  The phase at the time of the attempt.
        target_phase  (str):  The phase that was requested.
        caller        (str):  The method or function that triggered the transition.
        valid_targets (list): The phases that ARE valid from current_phase.
    """

    def __init__(
        self,
        current_phase: str,
        target_phase:  str,
        caller:        str         = "",
        valid_targets: Optional[list] = None,
    ) -> None:
        super().__init__(
            f"Invalid lifecycle transition: {current_phase} → {target_phase}"
            + (f" (caller={caller})" if caller else ""),
            current_phase = current_phase,
            target_phase  = target_phase,
            caller        = caller,
            valid_targets = str(valid_targets or []),
        )


class OnixeyPhaseConflict(OnixeyRuntimeStateError):
    """
    Raised when an operation requires a specific lifecycle phase but the
    current phase does not satisfy the requirement.

    Example:
        An analysis operator calling execute() while phase is SHUTTING_DOWN.

    Context keys:
        required_phase (str):  The phase the operation requires.
        current_phase  (str):  The actual phase at time of the call.
        operation      (str):  The operation that was denied.
    """

    def __init__(
        self,
        required_phase: str,
        current_phase:  str,
        operation:      str = "",
    ) -> None:
        super().__init__(
            f"Operation '{operation}' requires phase={required_phase}, "
            f"but current phase is {current_phase}.",
            required_phase = required_phase,
            current_phase  = current_phase,
            operation      = operation,
        )


# ══════════════════════════════════════════════════════════════════════════════
# RELOAD ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyReloadError(OnixeyRuntimeError):
    """
    Base for errors originating in the module reload system (ReloadManager).

    These are raised during or after execute_reload() transactions.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="reload", **context)


class OnixeyReloadTransactionError(OnixeyReloadError):
    """
    Raised when a reload transaction fails and cannot complete atomically.

    The transaction may have been rolled back (check ReloadResult.status).

    Context keys:
        failed_modules (list): Modules that failed during the reload pass.
        generation     (int):  Reload generation counter at time of failure.
        duration_ms    (float):Time elapsed before failure.
        rolled_back    (list): Modules restored from snapshot during rollback.
    """

    def __init__(
        self,
        message:        str,
        failed_modules: Optional[list] = None,
        generation:     int   = 0,
        duration_ms:    float = 0.0,
        rolled_back:    Optional[list] = None,
    ) -> None:
        super().__init__(
            message,
            failed_modules = failed_modules or [],
            generation     = generation,
            duration_ms    = round(duration_ms, 2),
            rolled_back    = rolled_back or [],
        )


class OnixeyRollbackError(OnixeyReloadError):
    """
    Raised when the rollback procedure itself fails after a transaction error.

    This is a critical double-failure: the reload failed AND recovery failed.
    Blender should be restarted after this error.

    Context keys:
        original_error (str):  The error that triggered the rollback attempt.
        rollback_error (str):  The error that occurred during rollback.
        partial_modules (list):Modules that may be in an inconsistent state.
    """

    def __init__(
        self,
        original_error:  str,
        rollback_error:  str,
        partial_modules: Optional[list] = None,
    ) -> None:
        super().__init__(
            f"Rollback failed after transaction error. "
            f"Blender session may be in an inconsistent state. "
            f"Restart recommended.",
            original_error  = original_error,
            rollback_error  = rollback_error,
            partial_modules = partial_modules or [],
        )


class OnixeyCircularDependencyError(OnixeyReloadError):
    """
    Raised when the topological sort of ModuleDescriptors detects a cycle.

    A cycle means two or more modules list each other as dependencies,
    making a valid reload order impossible.

    Context keys:
        cycle_nodes (list): Module dotted names involved in the cycle.
    """

    def __init__(self, cycle_nodes: list) -> None:
        super().__init__(
            f"Circular dependency detected among modules: {cycle_nodes}. "
            "Break the cycle by removing a dependency or extracting shared "
            "code into a lower-level module (e.g., core/).",
            cycle_nodes = list(cycle_nodes),
        )


# ══════════════════════════════════════════════════════════════════════════════
# SESSION ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeySessionError(OnixeyRuntimeError):
    """
    Base for errors originating in the session subsystem (runtime/session.py).

    These typically arise from access patterns that violate the session
    lifecycle contract (e.g., calling get_or_raise() before startup()).
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="session", **context)


class OnixeySessionNotInitialized(OnixeySessionError):
    """
    Raised by session.get_or_raise() when the SessionState singleton is None.

    This indicates that either:
        a) lifecycle.startup() was never called (register() incomplete), or
        b) lifecycle.shutdown() was called and the session was destroyed.

    Context keys:
        caller (str): The function or operator that attempted access.

    Recovery:
        Ensure runtime/lifecycle.startup() is called from __init__.py
        register() before any operator execute() path runs.
    """

    def __init__(self, caller: str = "") -> None:
        super().__init__(
            "SessionState is None. "
            "Ensure lifecycle.startup() was called before accessing the session. "
            + (f"Caller: {caller}" if caller else ""),
            caller = caller,
        )


class OnixeySessionObjectInvalid(OnixeySessionError):
    """
    Raised when a weakref to a bpy object held by SessionState has expired.

    A weakref expires when Blender garbage-collects the referenced object —
    e.g., the armature was deleted, renamed, or the .blend was reloaded.

    Context keys:
        object_name (str):  The name of the object at the time it was tracked.
        object_type (str):  e.g. "ARMATURE", "Action".
        caller      (str):  The code path that attempted to dereference.

    Recovery:
        Callers should call session.set_flag("rig_context_valid", False),
        prompt the user to re-select the rig, and re-set the active armature.
    """

    def __init__(
        self,
        object_name: str = "",
        object_type: str = "",
        caller:      str = "",
    ) -> None:
        super().__init__(
            f"Weakref to bpy object '{object_name}' ({object_type}) has expired. "
            "The object was deleted, undone, or the .blend was reloaded. "
            "Re-select the rig to restore session context.",
            object_name = object_name,
            object_type = object_type,
            caller      = caller,
        )


# ══════════════════════════════════════════════════════════════════════════════
# CACHE ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyCacheError(OnixeyRuntimeError):
    """
    Base for errors originating in the analysis cache (runtime/cache.py).
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="cache", **context)


class OnixeyCacheNotInitialized(OnixeyCacheError):
    """
    Raised by cache_get() or cache_set() when the cache is accessed before
    _init() was called (i.e., before lifecycle.startup() completed).

    Context keys:
        operation (str): "get" or "set" — which operation was attempted.
        key       (str): The cache key that was being accessed.

    Recovery:
        Ensure lifecycle.startup() → cache._init() completes before any
        analysis module calls cache_get() or cache_set().
    """

    def __init__(self, operation: str = "", key: str = "") -> None:
        super().__init__(
            f"Cache accessed (operation='{operation}') before initialization. "
            "Ensure lifecycle.startup() was called from register().",
            operation = operation,
            key       = key,
        )


class OnixeyCacheKeyError(OnixeyCacheError):
    """
    Raised when a cache key is malformed, uses an unknown tier prefix,
    or fails the key builder contract.

    Context keys:
        key   (str): The offending key string.
        tier  (str): The tier prefix extracted from the key (if any).
        reason(str): Human-readable explanation of the violation.

    Prevention:
        Always use the key builder functions (key_l1_bone_pos, key_l2_analysis,
        key_l3_topology) rather than constructing keys with ad-hoc f-strings.
    """

    def __init__(self, key: str, tier: str = "", reason: str = "") -> None:
        super().__init__(
            f"Malformed or invalid cache key: '{key}'. {reason}",
            key    = key,
            tier   = tier,
            reason = reason,
        )


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyHandlerError(OnixeyRuntimeError):
    """
    Base for errors related to bpy.app.handlers registration and lifecycle.

    Note: These exceptions describe handler management errors (duplicate
    registration, leaks), NOT errors raised inside handler bodies.
    Errors raised inside handler bodies should be caught and logged there —
    they must not propagate to Blender's event loop.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="handler", **context)


class OnixeyHandlerDuplicateError(OnixeyHandlerError):
    """
    Raised when handler_append() detects that a handler function is already
    present in the target handler list before registration.

    This indicates a missed unregister() call from a previous register()
    cycle — a sign of an incomplete reload or a crash during unregister().

    Context keys:
        handler_name (str):  __name__ of the duplicate handler function.
        handler_list (str):  The bpy.app.handlers.* list name (e.g. "frame_change_post").
        existing_count (int):How many instances were already present.

    Recovery:
        api_wrappers.handler_append() removes all stale instances before
        registering. This exception is raised to make the duplication
        visible in the forensic log even though recovery was automatic.
    """

    def __init__(
        self,
        handler_name:   str,
        handler_list:   str,
        existing_count: int = 1,
    ) -> None:
        super().__init__(
            f"Handler '{handler_name}' was already registered {existing_count} time(s) "
            f"in bpy.app.handlers.{handler_list}. "
            "Stale instances removed before re-registration. "
            "Check for missing unregister() in previous lifecycle.",
            handler_name   = handler_name,
            handler_list   = handler_list,
            existing_count = existing_count,
        )


class OnixeyHandlerLeakError(OnixeyHandlerError):
    """
    Raised (or logged) when a reload transaction detects that the handler
    count for a list grew during the transaction without a corresponding
    unregister() before reload.

    Context keys:
        handler_list (str): e.g. "frame_change_post".
        before       (int): Handler count before the reload transaction.
        after        (int): Handler count after the reload transaction.
        leaked       (int): after - before (the number of extra handlers).

    Severity:
        This is a WARNING by default, not an error that aborts the transaction.
        However, repeated leaks compound (N reloads = N×leaked extra handlers)
        and will degrade Blender's playback performance significantly.

    Recovery:
        Ensure every module with handlers implements a symmetric unregister()
        that calls api_wrappers.handler_remove() for every registered handler.
    """

    def __init__(
        self,
        handler_list: str,
        before:       int,
        after:        int,
    ) -> None:
        leaked = after - before
        super().__init__(
            f"Handler leak in bpy.app.handlers.{handler_list}: "
            f"{leaked} extra handler(s) added during reload "
            f"(before={before}, after={after}). "
            "Ensure all modules unregister handlers before reload.",
            handler_list = handler_list,
            before       = before,
            after        = after,
            leaked       = leaked,
        )


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyLifecycleError(OnixeyRuntimeError):
    """
    Base for errors that violate the addon's initialization / teardown contract.

    These are programming errors — they indicate that the call sequence in
    __init__.py, compat.py, or lifecycle.py is incorrect.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="lifecycle", **context)


class OnixeyDoubleInitError(OnixeyLifecycleError):
    """
    Raised by feature_flags.initialize() (and similar singletons) when
    initialize() is called a second time without an intervening reset().

    Context keys:
        singleton_type   (str): Class name of the already-initialized singleton.
        init_call_count  (int): Total initialize() calls this Python session.
        caller           (str): The function that called initialize() again.

    Recovery:
        Call reset() before initialize() in unregister() to ensure the
        singleton is destroyed at the end of each addon lifecycle.
        Pattern: register() → initialize(); unregister() → reset().
    """

    def __init__(
        self,
        singleton_type:  str = "",
        init_call_count: int = 0,
        caller:          str = "",
    ) -> None:
        super().__init__(
            f"Double initialization detected for '{singleton_type}'. "
            f"initialize() called {init_call_count} time(s) without reset(). "
            "Call reset() in unregister() to restore a clean slate.",
            singleton_type  = singleton_type,
            init_call_count = init_call_count,
            caller          = caller,
        )


class OnixeyPrematureAccessError(OnixeyLifecycleError):
    """
    Raised when a module or function is accessed before the initialization
    contract has been fulfilled.

    This is distinct from OnixeySessionNotInitialized (which is session-specific)
    — PrematureAccessError covers any module that has a startup requirement.

    Context keys:
        module        (str): The module or class being accessed prematurely.
        required_step (str): What must have happened first (e.g. "feature_flags.initialize()").
        caller        (str): The code path that triggered the premature access.

    Example:
        analysis/arc.py calling supports_evaluated_get() before
        feature_flags.initialize() has run.
    """

    def __init__(
        self,
        module:        str = "",
        required_step: str = "",
        caller:        str = "",
    ) -> None:
        super().__init__(
            f"Premature access to '{module}'. "
            + (f"Required: {required_step}. " if required_step else "")
            + "Ensure the correct initialization sequence has completed.",
            module        = module,
            required_step = required_step,
            caller        = caller,
        )


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRITY ERRORS
# ══════════════════════════════════════════════════════════════════════════════

class OnixeyIntegrityError(OnixeyRuntimeError):
    """
    Base for errors detected by runtime self-checks and invariant validation.

    These represent conditions that should NEVER occur in a correct
    implementation — they indicate either a programming error, unexpected
    Blender API change, or memory/state corruption.

    When an integrity error is caught:
        1. Log the full formatted() output at ERROR level.
        2. Attempt graceful degradation (disable the affected feature).
        3. Surface the issue in the diagnostic panel.
        4. Do NOT silently swallow it.
    """

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, subsystem="integrity", **context)


class OnixeyHardRequirementError(OnixeyIntegrityError):
    """
    Raised by feature_flags._compute_all_flags() when one or more
    fallback_ok=False flags evaluate to False.

    Onixey V3 cannot function without these capabilities. __init__.py
    catches this exception and aborts register() cleanly.

    Context keys:
        failed_flags     (list): Keys of hard-requirement flags that are False.
        blender_version  (str):  Blender version string at detection time.
        minimum_required (str):  Minimum Blender version Onixey requires.

    Recovery:
        Upgrade Blender to at least 4.2.0 LTS.
        Check for non-standard Blender builds that omit required subsystems.
    """

    def __init__(
        self,
        failed_flags:    list,
        blender_version: str = "",
        minimum_required:str = "4.2.0",
    ) -> None:
        count = len(failed_flags)
        super().__init__(
            f"Onixey V3 cannot initialize: {count} hard requirement(s) not met "
            f"by Blender {blender_version}. "
            f"Minimum required: {minimum_required}. "
            f"Failed flags: {failed_flags}.",
            failed_flags      = list(failed_flags),
            blender_version   = blender_version,
            minimum_required  = minimum_required,
            failed_count      = count,
        )


class OnixeyStateCorruptionError(OnixeyIntegrityError):
    """
    Raised when a live integrity check (e.g., get_live_integrity_check())
    detects that internal state has diverged from expected invariants.

    Examples:
        - _singleton is set but is not a FeatureSet instance.
        - Hard requirement count in metrics != count in live singleton.
        - A cache entry holds a bpy object reference (strong ref violation).

    Context keys:
        check_name    (str):  The integrity check that failed.
        expected      (str):  What the invariant requires.
        actual        (str):  What was actually found.
        recovery_hint (str):  Suggested recovery action.

    Recovery:
        Call reset() on the affected singleton and reinitialize.
        If the corruption is in a bpy-side data structure, restart Blender.
    """

    def __init__(
        self,
        check_name:    str = "",
        expected:      str = "",
        actual:        str = "",
        recovery_hint: str = "Call reset() and reinitialize the affected subsystem.",
    ) -> None:
        super().__init__(
            f"State corruption detected in check '{check_name}'. "
            f"Expected: {expected}. "
            f"Actual: {actual}. "
            f"Recovery: {recovery_hint}",
            check_name    = check_name,
            expected      = expected,
            actual        = actual,
            recovery_hint = recovery_hint,
        )
