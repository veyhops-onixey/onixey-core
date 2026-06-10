"""
onixey3/validation/report.py

Structured Report Formatting — Onixey V3 AAA Validation.

PURPOSE
───────
The single formatting layer for all validation output. All other
validation modules produce data (dicts, lists). This module turns
that data into human-readable text and structured diagnostics.

DESIGN CONTRACT
───────────────
- Zero bpy imports at module level.
- Zero side effects on import.
- All functions are pure: input data → output string/dict.
- No state stored between calls.
- Compatible with Blender 4.2, 4.5, 5.x (uses only stdlib).

OUTPUT FORMATS
──────────────
    as_text()       — Multi-line string for console / Text Editor output.
    as_lines()      — List[str] for programmatic processing.
    as_dict()       — Plain dict for JSON serialization or operator props.
    summary_line()  — Single line for panel labels or log prefixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum
import datetime


# ──────────────────────────────────────────────────────────────────────────────
# SEVERITY LEVELS
# Mirrors the AAA architecture color/icon scheme.
# ──────────────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    """
    Maps directly to the AAA architecture visual scheme:
        CRITICAL → RED   (#F44336) — addon must not continue
        WARNING  → AMBER (#FFC107) — degraded but functional
        INFO     → BLUE  (#2196F3) — neutral, no action needed
        OK       → GREEN (#4CAF50) — check passed cleanly
    """
    CRITICAL = "CRITICAL"
    WARNING  = "WARNING"
    INFO     = "INFO"
    OK       = "OK"


# Blender icon mapping (used by future debug operator panel draw())
SEVERITY_ICON: Dict[Severity, str] = {
    Severity.CRITICAL: "CANCEL",
    Severity.WARNING:  "ERROR",
    Severity.INFO:     "INFO",
    Severity.OK:       "CHECKMARK",
}

# Short prefix for console output
SEVERITY_PREFIX: Dict[Severity, str] = {
    Severity.CRITICAL: "✖ CRITICAL",
    Severity.WARNING:  "⚠ WARNING ",
    Severity.INFO:     "· INFO    ",
    Severity.OK:       "✔ OK      ",
}


# ──────────────────────────────────────────────────────────────────────────────
# FINDING — A single discrete finding from a check
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    """
    One discrete problem or observation from a validation check.

    Attributes:
        severity:  CRITICAL | WARNING | INFO | OK
        code:      Short machine-readable identifier (e.g. "HANDLER_DUPLICATE").
                   Use SCREAMING_SNAKE_CASE. Stable across versions.
        message:   Human-readable description of the finding.
        detail:    Optional extra context (values, counts, names).
        fix:       Optional recommended action for the developer.
    """
    severity: Severity
    code:     str
    message:  str
    detail:   Optional[str] = None
    fix:      Optional[str] = None

    def as_line(self) -> str:
        """Single formatted line for console output."""
        prefix = SEVERITY_PREFIX[self.severity]
        line = f"    {prefix}  [{self.code}]  {self.message}"
        if self.detail:
            line += f"\n               Detail: {self.detail}"
        if self.fix:
            line += f"\n               Fix:    {self.fix}"
        return line

    def as_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code":     self.code,
            "message":  self.message,
            "detail":   self.detail,
            "fix":      self.fix,
        }


# ──────────────────────────────────────────────────────────────────────────────
# CHECK RESULT — Output of one validation function
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """
    Result of a single validation check.

    A check is a named, bounded unit of validation (e.g. "Handler Duplicates").
    It produces zero or more Findings.

    Attributes:
        check_name:  Display name of the check, e.g. "Handler Duplication".
        passed:      True if no CRITICAL or WARNING findings were produced.
        findings:    List of Finding objects from this check.
        duration_ms: Optional wall-clock time of the check in milliseconds.
        metadata:    Arbitrary extra data for diagnostics (plain dict).
    """
    check_name:  str
    passed:      bool
    findings:    List[Finding] = field(default_factory=list)
    duration_ms: Optional[float] = None
    metadata:    Dict[str, Any] = field(default_factory=dict)

    # ── Convenience constructors ──────────────────────────────────────────────

    @classmethod
    def ok(cls, name: str, message: str = "All checks passed.",
           duration_ms: Optional[float] = None,
           **metadata: Any) -> "CheckResult":
        """Construct a fully-passed result with a single OK finding."""
        return cls(
            check_name=name,
            passed=True,
            findings=[Finding(Severity.OK, "CHECK_PASSED", message)],
            duration_ms=duration_ms,
            metadata=dict(metadata),
        )

    @classmethod
    def skipped(cls, name: str, reason: str) -> "CheckResult":
        """Construct a skipped result (bpy unavailable, context missing, etc.)."""
        return cls(
            check_name=name,
            passed=True,   # Skipped ≠ failed — caller decides semantics
            findings=[Finding(Severity.INFO, "CHECK_SKIPPED", reason)],
        )

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def highest_severity(self) -> Optional[Severity]:
        """Return the most severe finding's severity, or None if no findings."""
        order = [Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.OK]
        for sev in order:
            if any(f.severity == sev for f in self.findings):
                return sev
        return None

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    # ── Output methods ────────────────────────────────────────────────────────

    def as_lines(self) -> List[str]:
        """Return list of formatted strings, one per finding."""
        status = "PASS" if self.passed else "FAIL"
        timing = f"  ({self.duration_ms:.1f}ms)" if self.duration_ms is not None else ""
        lines = [f"  ── {self.check_name} [{status}]{timing}"]
        for finding in self.findings:
            lines.append(finding.as_line())
        if self.metadata:
            for k, v in self.metadata.items():
                if v not in (None, {}, [], ""):
                    lines.append(f"    {'':15s} {k}: {v}")
        return lines

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check_name":   self.check_name,
            "passed":       self.passed,
            "findings":     [f.as_dict() for f in self.findings],
            "duration_ms":  self.duration_ms,
            "metadata":     self.metadata,
            "critical":     self.critical_count,
            "warnings":     self.warning_count,
        }

    def summary_line(self) -> str:
        """One-line summary suitable for a panel label or log entry."""
        status = "✔" if self.passed else "✖"
        c = self.critical_count
        w = self.warning_count
        issues = f"{c}C/{w}W" if (c or w) else "clean"
        timing = f" {self.duration_ms:.1f}ms" if self.duration_ms is not None else ""
        return f"{status} {self.check_name}: {issues}{timing}"


# ──────────────────────────────────────────────────────────────────────────────
# VALIDATION REPORT — Aggregates multiple CheckResults
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    """
    Top-level container aggregating all CheckResults from a validation run.

    Produced by:
        stress.run_all()        → ValidationReport
        healthcheck.run_startup_checks() → ValidationReport

    Consumed by:
        report.print_report(vr)     — console output
        vr.as_text()                — string for Text Editor
        vr.as_dict()                — serialization
    """
    title:     str
    results:   List[CheckResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    context:   Dict[str, Any] = field(default_factory=dict)

    # ── Derived stats ─────────────────────────────────────────────────────────

    @property
    def total_checks(self) -> int:
        return len(self.results)

    @property
    def passed_checks(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_checks(self) -> int:
        return self.total_checks - self.passed_checks

    @property
    def overall_passed(self) -> bool:
        return self.failed_checks == 0

    @property
    def total_criticals(self) -> int:
        return sum(r.critical_count for r in self.results)

    @property
    def total_warnings(self) -> int:
        return sum(r.warning_count for r in self.results)

    # ── Output methods ────────────────────────────────────────────────────────

    def as_text(self) -> str:
        """Full multi-line text report."""
        width = 70
        bar   = "═" * width
        lines: List[str] = []

        lines.append(f"\n{bar}")
        lines.append(f"  {self.title}")
        lines.append(f"  {self.timestamp}")
        lines.append(bar)

        # Context block
        if self.context:
            lines.append("")
            for k, v in self.context.items():
                lines.append(f"  {k:<28} {v}")

        lines.append("")
        lines.append(f"  Checks: {self.passed_checks}/{self.total_checks} passed"
                     f"  |  Criticals: {self.total_criticals}"
                     f"  |  Warnings: {self.total_warnings}")
        lines.append("")

        for result in self.results:
            lines.extend(result.as_lines())
            lines.append("")

        # Footer
        if self.overall_passed:
            lines.append(f"  ✔  ALL CHECKS PASSED")
        else:
            lines.append(f"  ✖  {self.failed_checks} CHECK(S) FAILED")
        lines.append(bar + "\n")

        return "\n".join(lines)

    def as_lines(self) -> List[str]:
        """Flat list of summary lines — one per CheckResult."""
        return [r.summary_line() for r in self.results]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title":          self.title,
            "timestamp":      self.timestamp,
            "context":        self.context,
            "overall_passed": self.overall_passed,
            "total_checks":   self.total_checks,
            "passed_checks":  self.passed_checks,
            "failed_checks":  self.failed_checks,
            "total_criticals": self.total_criticals,
            "total_warnings": self.total_warnings,
            "results":        [r.as_dict() for r in self.results],
        }

    def summary_line(self) -> str:
        """Single line — suitable for a panel header or log prefix."""
        icon = "✔" if self.overall_passed else "✖"
        return (
            f"{icon} {self.title}: "
            f"{self.passed_checks}/{self.total_checks} checks  "
            f"{self.total_criticals}C/{self.total_warnings}W"
        )


# ──────────────────────────────────────────────────────────────────────────────
# BUILDER HELPERS — Convenience functions used by stress.py and healthcheck.py
# ──────────────────────────────────────────────────────────────────────────────

def make_report(title: str, results: List[CheckResult],
                **context_kv: Any) -> ValidationReport:
    """
    Construct a ValidationReport from a list of CheckResults.

    Args:
        title:      Display title for the report.
        results:    List of CheckResult objects.
        **context_kv: Key-value pairs added to the context block
                      (e.g. blender_version="4.2.0", addon_version="3.1.0").

    Returns:
        ValidationReport ready for as_text() or as_dict().
    """
    return ValidationReport(
        title=title,
        results=results,
        context=dict(context_kv),
    )


def print_report(report: ValidationReport) -> None:
    """
    Print a ValidationReport to stdout.

    Blender's console (System Console on Windows, terminal on Linux/macOS)
    will show this output. Safe to call from any context.
    """
    print(report.as_text())


# ──────────────────────────────────────────────────────────────────────────────
# FINDING FACTORIES — Typed constructors for common finding patterns
# Used by stress.py and healthcheck.py for consistency.
# ──────────────────────────────────────────────────────────────────────────────

def finding_ok(code: str, message: str) -> Finding:
    return Finding(Severity.OK, code, message)

def finding_info(code: str, message: str, detail: Optional[str] = None) -> Finding:
    return Finding(Severity.INFO, code, message, detail=detail)

def finding_warning(code: str, message: str,
                    detail: Optional[str] = None,
                    fix: Optional[str] = None) -> Finding:
    return Finding(Severity.WARNING, code, message, detail=detail, fix=fix)

def finding_critical(code: str, message: str,
                     detail: Optional[str] = None,
                     fix: Optional[str] = None) -> Finding:
    return Finding(Severity.CRITICAL, code, message, detail=detail, fix=fix)
