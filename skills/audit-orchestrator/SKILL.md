---
name: audit-orchestrator
description: Entrypoint for the brand-ai-readiness-audit marketplace. Takes a URL, establishes audit scope and budget, drives a single site-observation pass, invokes the four detector skills and the scoring skill, validates that every finding's evidence resolves to a real observation, enforces the report schema, and emits one audit report of findings plus prioritized suggested actions. Use when asked to audit a website for AI discoverability (whether AI assistants can reach, read, extract, trust and cite it) or on-site engagement (whether visitors arriving from AI answers can orient, get their answer, and continue). Read-only; never modifies the audited site.
license: MIT
allowed-tools: Bash Read Write WebSearch WebFetch
---

# Audit Orchestrator (entrypoint)

## When to use

Invoked with a single URL or domain. This is the only skill in the marketplace that
should be called directly. It composes the others; they are not standalone entrypoints.


## Tool requirements

Read [shared runtime and safety requirements](../../references/skill-runtime.md)
before execution. Keep the full marketplace and shared library together. Commands
below run from the marketplace root; use absolute input/output paths when needed.
Only the orchestrator collects remotely; other skills read local evidence. Do not
repeat stages already run by the orchestrator. Local `--out` files may be overwritten.

## Inputs

- `url` (required)
- Optional CLI `--max-pages N`, `--out-dir DIR`, and `--save-evidence`.
- The Python API also accepts `options.budget`. Rendering is capability-detected.
- External verification uses the invoking agent's web-search and page-reading tools
  through [references/agent-search-handoff.md](references/agent-search-handoff.md).
  It requires no provider API key inside this package.

## Responsibility

Lifecycle only. Scope, capability detection, budget, invocation, evidence-binding
validation, schema enforcement, proactive layer, emission.

## Explicitly NOT this skill's job

- Detecting any site defect. All check logic lives in the four detector skills.
- Scoring. Severity, confidence, aggregation and ranking belong to
  `evidence-prioritization`. This skill never assigns a severity.

## Procedure

Follow [the agent-search handoff](references/agent-search-handoff.md). Preparation
performs the remote collection exactly once; finalization reads saved evidence.

1. Resolve target; record requested vs audited host if redirected.
2. Detect runtime capabilities (renderer and invoking-agent search). Record them in the capability matrix.
3. Fetch robots before pages. Check each target path, including allowed deep exceptions;
   unreachable policy or excluded paths produce partial coverage, not permission to bypass.
4. Drive [../../lib/site_observer](../../lib/site_observer) for exactly one collection pass into an immutable store.
5. Emit at most three verification requests. Use available web-search tools, inspect
   independent source pages, and save cited results using the supplied template.
6. Validate and import only results tied to this audit and its issued request IDs.
7. Invoke `crawl-render-audit`, `entity-semantic-audit`, `trust-freshness-audit`,
   `engagement-audit`. Each returns unscored findings.
8. Invoke `evidence-prioritization` over the pooled findings.
9. Bind `observation_ids` against the store. Materialize IDs from known source
   observations for absence findings. Drop unresolvable evidence anchors.
10. Generate proactive opportunities from gaps, not defects.
11. Validate against [../../schemas/report.schema.json](../../schemas/report.schema.json) and emit `report.json` and `report.md`.
12. Inspect `run.schema_valid`, `run.skill_failures` and coverage. Exit zero alone
    does not prove a complete or valid audit; report unexpected emission failures.

## Evidence rules

Every cited observation ID must resolve. Absence findings without IDs instead
require known source URLs. Resolution establishes provenance, not interpretation accuracy.

## Outputs

`report.json` and `report.md` in the selected output directory; `--save-evidence`
also writes `observations.json`. Partial runs carry coverage entries for unexpected
lifecycle failures. Budgeted audit work stops at 250 seconds by default, reserving
30 seconds inside the 280-second CLI worker deadline to compose and write a partial report.
Filesystem errors or missing dependencies may still prevent emission. Never describe
an incomplete run or zero observed pages as a clean audit.

## Scripts

| Script | Role | CLI |
|---|---|---|
| [scripts/run_audit.py](scripts/run_audit.py) | Prepares bounded agent-search requests and finalizes supplied results without recrawling. | `python skills/audit-orchestrator/scripts/run_audit.py <url> --out-dir DIR --prepare-agent-search`; then `python skills/audit-orchestrator/scripts/run_audit.py --out-dir DIR --finalize-agent-search DIR/verification_results.json` |
| [scripts/agent_search.py](scripts/agent_search.py) | Builds and validates the provider-neutral search handoff; performs no network requests. | library only |
| [scripts/validate_report.py](scripts/validate_report.py) | Evidence-binding CLI (`bind_evidence`); `validate_final_report(report)` is a Python API, not the CLI. | `python scripts/validate_report.py --findings <path> --store <path> [--out <path>]` |
| [scripts/proactive.py](scripts/proactive.py) | Generates `proactive_opportunities[]` from gaps per [references/proactive-layer.md](references/proactive-layer.md). | (library only, no CLI) |
| [scripts/render_report.py](scripts/render_report.py) | Renders `report.json` to `report.md`. | `python scripts/render_report.py --report <path> [--out <path>]` |

Collection itself lives in [../../lib/site_observer/](../../lib/site_observer/) (`collect.py`
drives `crawl.py`, `render.py`, `classify.py`, `probe.py`), not in this skill's
own `scripts/` — see the package's single-collection contract.

## References

[references/orchestration-rules.md](references/orchestration-rules.md), [references/coverage-policy.md](references/coverage-policy.md),
[references/degraded-mode.md](references/degraded-mode.md), [references/proactive-layer.md](references/proactive-layer.md),
and [references/agent-search-handoff.md](references/agent-search-handoff.md)

## Dependencies

- [../../lib/site_observer](../../lib/site_observer) — the single collection pass this skill drives
- [../../schemas/report.schema.json](../../schemas/report.schema.json) — emission contract
- [../../references/](../../references/) — shared failure taxonomy and archetype matrix
- The five sibling skills listed in [marketplace manifest](../../marketplace.json)

Composition links (invoke these through the single workflow, not as new audits):

- [Crawl/render/extract](../crawl-render-audit/SKILL.md)
- [Entity identity](../entity-semantic-audit/SKILL.md)
- [Trust/freshness](../trust-freshness-audit/SKILL.md)
- [Visitor engagement](../engagement-audit/SKILL.md)
- [Evidence prioritization](../evidence-prioritization/SKILL.md)

Shared output semantics: [finding contract](../../references/finding-contract.md).
