# Validation Module

Most Blender addons discover architectural problems after deployment.

Onixey attempts to discover them before deployment.

The Validation module is the quality assurance layer of Onixey. Its purpose is to verify architectural contracts, runtime assumptions, compatibility requirements, module integrity and lifecycle safety before production use.

Rather than testing features, Validation tests the foundation those features depend on.

---

## What Validation Checks

Validation currently provides five independent validation systems:

### Compatibility

Verifies environment requirements and version compatibility.

Checks include:

* Blender version support
* Runtime version compatibility
* Onixey version requirements
* Scene compatibility validation
* Build configuration checks

### Healthcheck

Performs startup diagnostics and runtime verification.

Checks include:

* Startup validation
* Runtime diagnostics
* Environment readiness
* Module compatibility reporting

### Integrity

Verifies architectural correctness.

Checks include:

* Required modules
* Required attributes
* Invalid references
* Circular imports
* Dependency violations
* Runtime contamination

### Stress

Verifies lifecycle stability under repeated execution.

Checks include:

* Register / unregister cycles
* Handler cleanup
* Ghost property detection
* Reload safety validation

### Reporting

Provides structured machine-readable and human-readable reports.

Features include:

* Severity classification
* Structured findings
* JSON export
* Validation summaries
* Aggregated reports

---

## Design Philosophy

Validation follows several engineering rules:

* Validation never mutates production state.
* Runtime integrity is more important than feature availability.
* Register and unregister operations must remain symmetrical.
* Runtime modules remain isolated.
* Validation must remain safe outside Blender whenever possible.
* Reports must be deterministic and structured.

---

## Current Status

Validation Framework

Iteration 1 Complete

Public Review Phase
