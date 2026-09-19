---
name: trust-freshness-audit
description: Detects credibility and freshness failures that make AI systems reluctant to repeat a claim. Checks whether time-sensitive claims carry visible or machine-readable dates, whether dated signals have gone stale, whether pages contradict each other on founding year or employee/customer counts, whether falsifiable claims carry attribution, whether organizational and authorship information exists, and whether a claim is corroborated anywhere beyond the site itself. Never reports corroboration status that was not actually verified. Use as part of a website AI-readiness audit.
license: MIT
allowed-tools: Bash Read WebSearch WebFetch
---

# Trust / Freshness Audit

## When to use

Invoked by `audit-orchestrator` against a completed observation store.
Answers one question: would a machine believe this claim and repeat it.

## Tool requirements

Read [shared runtime and safety requirements](../../references/skill-runtime.md)
before execution. Keep the full marketplace and shared library together. Commands
below run from this skill directory; use absolute input/output paths when needed.
Only the orchestrator collects remotely; other skills read local evidence. Do not
repeat stages already run by the orchestrator. Local `--out` files may be overwritten.

## Inputs

Observation store and optional claim table. The corroboration recorder accepts
supplied search results and an optional entity profile for its identity-confusion
guard. The orchestrator imports results gathered by the invoking agent; this detector
never performs outbound requests itself. Exact observation shapes: [references/store-contract.md](references/store-contract.md).

## Scripts

Deterministic extraction and comparison throughout — no model call is required
for the implemented checks (see [references/store-contract.md](references/store-contract.md)'s note
on where an LLM-instrument upgrade is a documented, not required, extension
point):

| Script | Role | CLI |
|---|---|---|
| [scripts/build_claim_table.py](scripts/build_claim_table.py) | Builds the claim table ([references/claim-table-contract.md](references/claim-table-contract.md)) | `python scripts/build_claim_table.py --store <path> [--out <path>]` |
| [scripts/corroborate.py](scripts/corroborate.py) | Records one `CLAIM_CORROBORATION` observation from search results (or an honest absent-capability record if none supplied) | `python scripts/corroborate.py --claim <path> [--results <path>] [--entity-profile <path>] [--timestamp ISO_TIME] [--method METHOD] [--out <path>]` |
| [scripts/detect_trust.py](scripts/detect_trust.py) | D-TRUST-01..06 over the claim table | `python scripts/detect_trust.py --store <path> [--claim-table <path>] [--out <path>]` |

[scripts/_trust_util.py](scripts/_trust_util.py) is shared, not a public entrypoint. If `--claim-table`
is omitted, `detect_trust.py` builds one itself from `--store`.

## Explicitly NOT this skill's job

- Identity facts -> `entity-semantic-audit`
- Missing date *markup* as a metadata defect -> `crawl-render-audit`. This skill
  fires only when a time-sensitive claim lacks a date, a much narrower trigger.

## Procedure

1. Build the claim table: claim text, page, claim type, date signal, attribution.
2. Run [references/trust-checks.md](references/trust-checks.md) in ID order, applicability precondition first.
3. Follow [references/corroboration-honesty.md](references/corroboration-honesty.md) only for genuine, already-obtained
   results. The recorder never searches: the invoking agent supplies inspected results
   through the orchestrator handoff. Omitted results mean not performed; `[]` means a
   performed search found no results. Never invent lookup evidence.

## Evidence rules — hard constraint

The corroboration finding class cannot be emitted unless a corroboration record with a
real timestamp, query and method exists in the store. Absent that record the check is
neither passed nor failed: it is logged as coverage gap X-COV-01.
See [references/corroboration-honesty.md](references/corroboration-honesty.md).

> **Capability note.** D-TRUST-05 consumes the invoking agent's cited web-search
> results. No provider key is required by the marketplace. If agent search is unavailable
> or incomplete, the affected request is coverage, never a silent pass. Selection is
> limited to eligible claims and the shared three-request budget.

## Outputs

Unscored findings built with the common finding contract ([../../lib/common/findings.py](../../lib/common/findings.py)
— `check_id, category, title, severity, confidence, mechanism, impact,
observed_signal, evidence, observation_ids, source_urls, affected,
suggested_action`). A proposed base severity and bound evidence only — never a
final `id` or final severity.

## Governing principles

Never claim external corroboration unless it was actually checked
([references/corroboration-honesty.md](references/corroboration-honesty.md)). Distinguish observed facts from
inference — every finding states what was mechanically found, never a
subjective read on whether a claim "seems" current or trustworthy
([references/fp-guardrails.md](references/fp-guardrails.md)).

## Dependencies

- [../../lib/](../../lib/) — shared instrumentation, including the common finding
  contract ([../../lib/common/findings.py](../../lib/common/findings.py)). This skill reads the observation store; it
  never fetches the target site. See the package's single-collection contract.
- [../../references/failure-taxonomy.md](../../references/failure-taxonomy.md) — check ID registry
- [../../references/archetype-applicability.md](../../references/archetype-applicability.md) — check gating by site archetype

Shared output semantics: [finding contract](../../references/finding-contract.md).
