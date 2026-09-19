"""Entrypoint executable: lifecycle, capability detection, collection,
detector invocation, scoring, evidence binding, emission. Contains no check
logic and assigns no severity -- see SKILL.md's "Explicitly NOT this skill's
job".

Inputs:  url, options
Outputs: report.json, report.md
Nature:  deterministic orchestration

This is the ONLY entrypoint in the marketplace (marketplace.json marks it
"entrypoint": true; the other five skills are invoked only from here, never
directly). `run_audit()` composes them in a fixed order and never re-fetches:
one collection pass (lib/site_observer.collect), then the four detector
skills, then evidence-prioritization, then evidence-binding + schema
validation, then the proactive layer, then emission.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import importlib
import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent
MARKETPLACE_ROOT = SCRIPTS_DIR.parents[2]
for _path in (str(MARKETPLACE_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from lib.common.budget import Budget  # noqa: E402
from lib.common.network_policy import MAX_REQUESTS_PER_AUDIT  # noqa: E402
from lib.common.observations import make_observation  # noqa: E402
from lib.common.extract import parse_cache, with_parse_cache  # noqa: E402
from lib.common.schema import (  # noqa: E402
    validate_observation as validate_observation_store,
    validate_report as validate_report_schema,
)
from lib.site_observer.collect import collect  # noqa: E402
from lib.site_observer.search import search_linkup, search_linkup_entity_collisions  # noqa: E402

import agent_search as agent_search_mod  # noqa: E402
import proactive as proactive_mod  # noqa: E402
import validate_report as validate_report_mod  # noqa: E402

DETECTOR_SCRIPT_DIRS = ["crawl-render-audit", "entity-semantic-audit", "trust-freshness-audit", "engagement-audit"]
SEVERITY_TIERS = ["critical", "high", "medium", "low"]


def _register_detector_paths() -> None:
    for skill_dir in DETECTOR_SCRIPT_DIRS:
        path = str(MARKETPLACE_ROOT / "skills" / skill_dir / "scripts")
        if path not in sys.path:
            sys.path.insert(0, path)


def _run_detector(name: str, fn: Callable[..., List[Dict[str, Any]]], *args: Any,
                  failures: List[Dict[str, Any]], coverage: List[Dict[str, Any]],
                  timeout_s: Optional[float] = None) -> List[Dict[str, Any]]:
    """Never lets one detector's exception stop the others, or the audit,
    from completing -- graceful skill-failure handling. The failure is
    recorded twice: once for operators (`run.skill_failures`) and once as a
    coverage gap so the report itself explains the missing checks rather
    than silently having fewer findings than expected."""
    try:
        if timeout_s is None:
            return fn(*args) or []
        if timeout_s <= 0:
            raise TimeoutError("no detector budget remained")
        outcome: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)
        def invoke_bounded() -> None:
            try:
                # A timed-out detector may continue briefly in its daemon
                # thread, so it must not share the outer audit's mutable cache.
                # The detector still gets its own cache for repeated parsing.
                with parse_cache():
                    outcome.put(("ok", fn(*args)))
            except Exception as exc:  # noqa: BLE001
                outcome.put(("error", exc))

        threading.Thread(target=invoke_bounded, name=f"audit-{name}", daemon=True).start()
        try:
            status, value = outcome.get(timeout=max(0.001, timeout_s))
        except queue.Empty as exc:
            raise TimeoutError(f"exceeded {timeout_s:.1f}s detector allocation") from exc
        if status == "error":
            raise value
        return value or []
    except Exception as exc:  # noqa: BLE001 - a misbehaving detector must never take the audit down
        failures.append({"skill": name, "error": f"{type(exc).__name__}: {exc}"})
        coverage.append(
            {
                "check_id": "X-COV-01",
                "status": "failed",
                "reason": "SKILL_TIMEOUT" if isinstance(exc, TimeoutError) else "SKILL_FAILED",
                "detail": f"{name} raised {type(exc).__name__}: {exc}",
                "scope": name,
            }
        )
        return []


def _invoke_detectors(store: Dict[str, Any], budget: Optional[Budget] = None,
                      on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    _register_detector_paths()

    pooled: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []

    entity_profile: Optional[Dict[str, Any]] = None
    try:
        import build_entity_profile

        entity_profile = build_entity_profile.build_entity_profile(store)
    except Exception as exc:  # noqa: BLE001
        failures.append({"skill": "entity-semantic-audit", "stage": "build_entity_profile", "error": f"{type(exc).__name__}: {exc}"})
        coverage.append(
            {
                "check_id": "X-COV-01",
                "status": "failed",
                "reason": "SKILL_FAILED",
                "detail": f"build_entity_profile raised {type(exc).__name__}: {exc}",
                "scope": "entity-semantic-audit, engagement-audit (shared entity profile)",
            }
        )

    on_progress = on_progress or (lambda _stage: None)
    detector_specs = [
        ("detect_crawl", "crawl-render-audit", (store,)),
        ("detect_render", "crawl-render-audit", (store,)),
        ("detect_extract", "crawl-render-audit", (store,)),
        ("detect_entity", "entity-semantic-audit", (store, entity_profile)),
        ("detect_trust", "trust-freshness-audit", (store,)),
        ("detect_engagement", "engagement-audit", (store, entity_profile)),
    ]
    if budget is not None:
        budget.start_stage("detectors_scoring")
    for index, (module_name, skill, args) in enumerate(detector_specs):
        on_progress(f"detector:{module_name}")
        def invoke():
            return getattr(importlib.import_module(module_name), module_name)(*args)
        timeout_s = None
        if budget is not None:
            remaining = budget.stage_time_left_s("detectors_scoring", "detectors_scoring_s")
            # Heavy DOM detectors need a usable slice; fast detectors return
            # unused time immediately, so this remains within the shared stage
            # budget without forcing every detector into an equal 10s slot.
            timeout_s = min(remaining, max(15.0, remaining / max(1, len(detector_specs) - index)))
        pooled.extend(_run_detector(
            f"{skill}/{module_name}", invoke,
            failures=failures, coverage=coverage, timeout_s=timeout_s,
        ))
    proactive_timeout = None
    if budget is not None:
        proactive_timeout = budget.stage_time_left_s("detectors_scoring", "detectors_scoring_s")
    if proactive_timeout is not None and proactive_timeout <= 0:
        opportunities = []
        coverage.append(
            {
                "check_id": "X-COV-01",
                "status": "partial",
                "reason": "BUDGET_EXHAUSTED",
                "detail": "Detector budget ended before optional proactive opportunity extraction.",
                "scope": "crawl-render-audit/proactive opportunities",
            }
        )
    else:
        opportunities = _run_detector(
            "crawl-render-audit/proactive",
            lambda: importlib.import_module("detect_extract").proactive_opportunities(store),
            failures=failures,
            coverage=coverage,
            timeout_s=proactive_timeout,
        )
    return {"pooled_findings": pooled, "skill_failures": failures, "coverage": coverage, "proactive_opportunities": opportunities}


def _prioritize(pooled_findings: List[Dict[str, Any]], store: Dict[str, Any], failures: List[Dict[str, Any]], coverage: List[Dict[str, Any]]) -> Dict[str, Any]:
    path = str(MARKETPLACE_ROOT / "skills" / "evidence-prioritization" / "scripts")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import score as score_mod
        return score_mod.prioritize_findings(pooled_findings, store)
    except Exception as exc:  # noqa: BLE001
        failures.append({"skill": "evidence-prioritization", "error": f"{type(exc).__name__}: {exc}"})
        coverage.append(
            {
                "check_id": "X-COV-01",
                "status": "failed",
                "reason": "SKILL_FAILED",
                "detail": f"evidence-prioritization raised {type(exc).__name__}: {exc}",
                "scope": "all findings for this audit",
            }
        )
        return {"findings": [], "demoted": [], "rejected": pooled_findings, "merge_log": [], "calibration_log": []}


# Checks whose evidence can only come from an instrument this deployment does
# not have. A check like this is in coverage-policy.md's third state: not
# "evaluated and passed" and not "evaluated and failed", but *not evaluable*.
# Silence from one of them must never read as a clean bill of health, so the
# report names them rather than leaving the reader to infer the checks ran.
#
# Availability is derived from the observations actually collected, not from a
# hardcoded flag: wire a real probe or corroboration instrument later and these
# entries disappear on their own, with no list to remember to update.
INSTRUMENT_DEPENDENT_CHECKS = (
    (
        "PROBE",
        "per-page semantic extraction instrument",
        ("D-EXTRACT-01", "D-EXTRACT-03", "D-EXTRACT-06", "D-EXTRACT-07", "D-RENDER-02", "E-ANSWER-04"),
        "whether a specific fact a user would ask for is actually extractable from this page's text",
    ),
    (
        "CORROBORATION",
        "entity-collision classification instrument",
        ("D-ENTITY-03",),
        "whether other distinct entities use the site's canonical name",
    ),
    (
        "CLAIM_CORROBORATION",
        "outbound claim-corroboration search instrument",
        ("D-TRUST-05",),
        "whether the site's selected factual claims are supported beyond the site itself",
    ),
    (
        "SITEMAP",
        "successful sitemap retrieval",
        ("D-CRAWL-07",),
        "sitemap quality; the orphan-page half of D-CRAWL-07 is still evaluated from the link graph",
    ),
)

# Gated on crawl telemetry the collector does not currently record, rather than
# on a missing observation type.
BUDGET_TELEMETRY_CHECKS = ("D-CRAWL-14",)


def _unavailable_instrument_coverage(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Coverage entries naming the checks that could not be evaluated at all."""
    observations = store.get("observations", []) or []
    entries: List[Dict[str, Any]] = []

    for observation_type, instrument, checks, scope in INSTRUMENT_DEPENDENT_CHECKS:
        matching = [observation for observation in observations if observation.get("type") == observation_type]
        # The local deterministic probe records explicit spans, but it cannot
        # answer the model-dependent absence and extraction questions covered
        # by this entry. A future full probe has no such marker.
        available = bool(matching)
        if observation_type == "PROBE":
            available = any(
                (observation.get("value", {}) or {}).get("method") != "deterministic_explicit_spans"
                for observation in matching
            )
        if observation_type == "CLAIM_CORROBORATION":
            available = any(
                bool((observation.get("value", {}) or {}).get("performed"))
                for observation in matching
            )
            capability = (store.get("capabilities", {}) or {}).get("corroboration", {}) or {}
            if capability.get("available") and capability.get("eligible_claims") == 0:
                available = True
        if observation_type == "CORROBORATION":
            capability = (store.get("capabilities", {}) or {}).get("corroboration", {}) or {}
            if capability.get("entity_collision_applicable") is False:
                available = True
        if available:
            continue
        entries.append(
            {
                "check_id": "X-COV-01",
                "status": "skipped",
                "reason": "UNAVAILABLE_INSTRUMENT",
                "detail": (
                    f"{', '.join(checks)} could not be evaluated: "
                    f"{'they require' if len(checks) > 1 else 'it requires'} a "
                    f"{observation_type} observation from the {instrument}, which this run did "
                    "not have. "
                    f"{'These checks did' if len(checks) > 1 else 'This check did'} not pass; "
                    f"{'they' if len(checks) > 1 else 'it'} did not run."
                ),
                "scope": scope,
            }
        )

    crawl_capability = (store.get("capabilities", {}) or {}).get("crawl", {}) or {}
    if not crawl_capability.get("telemetry_available", False):
        entries.append(
            {
                "check_id": "X-COV-01",
                "status": "skipped",
                "reason": "UNAVAILABLE_INSTRUMENT",
                "detail": (
                    f"{', '.join(BUDGET_TELEMETRY_CHECKS)} could not be evaluated: it requires "
                    "crawl frontier telemetry, which is unavailable because the raw crawl did not run. This "
                    "check did not pass; it did not run."
                ),
                "scope": "whether the crawl was cut short by budget rather than by the site's own size",
            }
        )
    return entries


def _run_external_corroboration(
    store: Dict[str, Any],
    budget: Budget,
    now: Callable[[], datetime.datetime],
    *,
    search_client: Any = None,
) -> List[Dict[str, Any]]:
    """Search a small, selected claim set and append honest observations."""
    _register_detector_paths()
    coverage: List[Dict[str, Any]] = []
    capability = store.setdefault("capabilities", {}).setdefault("corroboration", {})

    if search_client is None:
        raise ValueError("A search_client must be explicitly injected; CLI agent search uses the file handoff")

    try:
        import build_claim_table
        import build_entity_profile
        import corroborate
        from _trust_util import is_corroboration_worthy

        claim_table = build_claim_table.build_claim_table(store)
        entity_profile = build_entity_profile.build_entity_profile(store)
    except Exception as exc:  # noqa: BLE001
        capability.update({"available": False, "reason": "CORROBORATION_INPUT_FAILED"})
        return [{
            "check_id": "X-COV-01", "status": "failed", "reason": "SEARCH_FAILED",
            "detail": f"Corroboration inputs could not be built ({type(exc).__name__}); no claim search ran.",
            "scope": "D-TRUST-05 claim corroboration checks",
        }]

    eligible = [claim for claim in claim_table.get("claims", []) if is_corroboration_worthy(claim)]
    canonical_name = ((entity_profile.get("fields", {}).get("canonical_name") or {}).get("value") or "").strip()
    distinguishing = entity_profile.get("fields", {}).get("distinguishing_attributes", {}) or {}
    entity_collision_applicable = bool(canonical_name) and not distinguishing.get("present", False)
    capability.update({
        "provider": "linkup", "available": True, "reason": None,
        "eligible_claims": len(eligible), "verified_claims": 0, "performed_queries": 0,
        "entity_collision_applicable": entity_collision_applicable,
        "entity_collision_performed": False,
    })
    if not eligible and not entity_collision_applicable:
        return coverage

    target = store.get("target", {}) or {}
    audited_host = (
        urlparse(target.get("origin", "")).hostname
        or urlparse("//" + target.get("audited_host", "")).hostname
        or target.get("audited_host", "")
    )
    budget.start_stage("corroboration")
    failures = 0
    if entity_collision_applicable:
        if not budget.can_search() or budget.stage_exceeded("corroboration", "corroboration_s"):
            coverage.append({
                "check_id": "X-COV-01", "status": "partial", "reason": "BUDGET_EXHAUSTED",
                "detail": "No budget remained for entity-collision corroboration.",
                "scope": "D-ENTITY-03 entity-collision check",
            })
        else:
            try:
                collision = search_linkup_entity_collisions(
                    search_client, canonical_name, excluded_host=audited_host,
                    timeout_s=budget.stage_time_left_s("corroboration", "corroboration_s"),
                )
                budget.record_search()
                store["observations"].append(make_observation(
                    "CORROBORATION", target.get("origin") or target.get("audited_host", ""),
                    {
                        "performed": True,
                        "query": collision["query"],
                        "method": "linkup_standard_structured_sourced",
                        "matches": collision["matches"],
                        "timestamp": now().isoformat(),
                    },
                ))
                capability["performed_queries"] += 1
                capability["entity_collision_performed"] = True
            except Exception as exc:  # noqa: BLE001
                budget.record_search()
                failures += 1
                coverage.append({
                    "check_id": "X-COV-01", "status": "partial", "reason": "SEARCH_FAILED",
                    "detail": f"Linkup entity-collision query failed ({type(exc).__name__}); identity collision remains unknown.",
                    "scope": "D-ENTITY-03 entity-collision check",
                })

    result_cache: Dict[str, List[Dict[str, str]]] = {}
    for index, claim in enumerate(eligible):
        query = corroborate.build_corroboration_query(claim, entity_profile)
        if query in result_cache:
            store["observations"].append(corroborate.record_corroboration(
                claim, search_results=result_cache[query], entity_profile=entity_profile,
                method="linkup_standard_search_results", timestamp=now().isoformat(),
            ))
            capability["verified_claims"] += 1
            continue
        if not budget.can_search() or budget.stage_exceeded("corroboration", "corroboration_s"):
            coverage.append({
                "check_id": "X-COV-01", "status": "partial", "reason": "BUDGET_EXHAUSTED",
                "detail": f"Claim corroboration stopped after {budget.search_queries_made} query(s).",
                "scope": f"{len(eligible) - index} remaining eligible claim(s)",
            })
            break
        try:
            results = search_linkup(
                search_client, query, excluded_host=audited_host,
                timeout_s=budget.stage_time_left_s("corroboration", "corroboration_s"),
            )
            budget.record_search()
            result_cache[query] = results
            store["observations"].append(corroborate.record_corroboration(
                claim, search_results=results, entity_profile=entity_profile,
                method="linkup_standard_search_results", timestamp=now().isoformat(),
            ))
            capability["performed_queries"] += 1
            capability["verified_claims"] += 1
        except Exception as exc:  # noqa: BLE001
            budget.record_search()
            failures += 1
            coverage.append({
                "check_id": "X-COV-01", "status": "partial", "reason": "SEARCH_FAILED",
                "detail": f"Linkup query failed ({type(exc).__name__}); that claim remains unverified.",
                "scope": claim.get("source_url", "selected claim"),
            })

    if capability["performed_queries"] == 0 and failures:
        capability.update({"available": False, "reason": "SEARCH_PROVIDER_REQUEST_FAILED"})
    return coverage


def _deduplicate_coverage(coverage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per (reason, scope); first occurrence wins."""
    seen = set()
    unique = []
    for entry in coverage:
        key = (entry.get("reason"), entry.get("scope"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _normalize_proactive_opportunity(item: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich legacy detector opportunities for the current report contract."""
    normalized = dict(item)
    normalized.setdefault("source", str(item.get("check_id") or "detector_opportunity"))
    normalized.setdefault("evidence", str(item.get("observed_signal") or item.get("opportunity") or ""))
    normalized.setdefault("why_it_matters", str(item.get("expected_mechanism") or ""))
    normalized.setdefault("suggested_action", str(item.get("opportunity") or item.get("suggestion") or ""))
    normalized.setdefault("validation", str(item.get("expected_effect") or "Re-run the audit after applying the change."))
    normalized.setdefault("confidence", "medium")
    return normalized


def _summarize(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {tier: 0 for tier in SEVERITY_TIERS}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return {"total_findings": len(findings), **counts}


def _compose_report(
    store: Dict[str, Any],
    collected: Dict[str, Any],
    budget: Budget,
    options: Dict[str, Any],
    on_progress: Callable[[str], None],
) -> Dict[str, Any]:
    """Run local detectors/scoring over one already-collected store."""
    coverage: List[Dict[str, Any]] = list(collected.get("coverage", []))
    skill_failures: List[Dict[str, Any]] = []
    collected.setdefault("budget", {}).update(budget.snapshot())

    detector_result = _invoke_detectors(store, budget=budget, on_progress=on_progress)
    skill_failures.extend(detector_result["skill_failures"])
    coverage.extend(detector_result["coverage"])

    on_progress("evidence_prioritization")
    prioritized = _prioritize(detector_result["pooled_findings"], store, skill_failures, coverage)

    on_progress("evidence_binding_and_report")
    kept_findings, dropped = validate_report_mod.bind_evidence(prioritized["findings"], store)
    kept_ids = {finding["id"] for finding in kept_findings}
    for finding in kept_findings:
        if "related_findings" in finding:
            finding["related_findings"] = [ref for ref in finding["related_findings"] if ref in kept_ids and ref != finding["id"]]
    if dropped:
        coverage.append(
            {
                "check_id": "X-COV-01",
                "status": "partial",
                "reason": "EVIDENCE_UNRESOLVED",
                "detail": f"{len(dropped)} finding(s) dropped: evidence did not resolve against the store",
                "scope": "the specific dropped findings, logged in run.evidence_binding_drops",
            }
        )

    coverage.extend(_unavailable_instrument_coverage(store))
    coverage = _deduplicate_coverage(coverage)
    proactive_opportunities = _run_detector(
        "audit-orchestrator/proactive",
        proactive_mod.generate_proactive_opportunities,
        store,
        prioritized["demoted"],
        kept_findings,
        failures=skill_failures,
        coverage=coverage,
    )
    proactive_opportunities.extend(
        _normalize_proactive_opportunity(item)
        for item in detector_result.get("proactive_opportunities", [])
    )

    report: Dict[str, Any] = {
        "site": store["target"]["audited_host"],
        "audited_at": store["collected_at"],
        "summary": _summarize(kept_findings),
        "findings": kept_findings,
        "scope": collected.get("scope", {}),
        "coverage": coverage,
        "proactive_opportunities": proactive_opportunities,
        "external_verification": agent_search_mod.external_verification_summary(store),
        "demoted": prioritized["demoted"],
        "run": {
            "capabilities": store["capabilities"],
            "budget": collected.get("budget", {}),
            "skill_failures": skill_failures,
            "evidence_binding_drops": dropped,
            "merge_log": prioritized.get("merge_log", []),
            "calibration_log": prioritized.get("calibration_log", []),
            "rejected_findings": prioritized.get("rejected", []),
        },
    }
    if options.get("include_evidence"):
        report["evidence_store"] = store
    is_valid, errors = validate_report_schema(report)
    report["run"]["schema_valid"] = is_valid
    if not is_valid:
        report["run"]["schema_errors"] = errors
    return report


@with_parse_cache
def _run_audit(
    url: str,
    options: Optional[Dict[str, Any]] = None,
    *,
    fetch: Optional[Callable[[str], Dict[str, Any]]] = None,
    robots_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    render_capability: Optional[Dict[str, Any]] = None,
    now: Optional[Callable[[], datetime.datetime]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    search_client: Any = None,
) -> Dict[str, Any]:
    """Run one full audit end to end and return a schema-shaped report dict.

    Never raises: every stage from collection onward is wrapped so a single
    failure degrades to a coverage gap rather than taking the whole audit
    down (references/degraded-mode.md: a crash scores zero on every rubric
    line simultaneously). `fetch`/`robots_fetcher`/`render_capability`/`now`
    are injectable seams for tests; a real invocation leaves them unset and
    gets the real network client.
    """
    options = options or {}
    on_progress = options.get("_progress") or (lambda _stage: None)
    now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
    budget = Budget(options.get("budget"))

    collected = collect(
        url,
        budget=budget,
        fetch=fetch,
        robots_fetcher=robots_fetcher,
        render_capability=render_capability,
        now=now,
        sleep=sleep,
        on_progress=on_progress,
    )
    store = collected["store"]
    on_progress("external_corroboration")
    requests_artifact = None
    if search_client is not None:
        collected["coverage"].extend(
            _run_external_corroboration(store, budget, now, search_client=search_client)
        )
    else:
        requests_artifact = agent_search_mod.build_verification_requests(
            store, max_queries=budget.config["corroboration_max_queries"]
        )
        selection = requests_artifact.get("selection", {}) or {}
        request_count = len(requests_artifact.get("requests", []))
        capability = store.setdefault("capabilities", {}).setdefault("corroboration", {})
        capability.update(
            {
                "available": request_count == 0,
                "reason": None if request_count == 0 else "AGENT_SEARCH_PENDING",
                "provider": "invoking_agent_web_search",
                "eligible_claims": selection.get("eligible_claims", 0),
                "verified_claims": 0,
                "requested_queries": request_count,
                "performed_queries": 0,
                "entity_collision_applicable": selection.get("entity_collision_applicable", False),
                "entity_collision_performed": False,
            }
        )
        if request_count:
            collected["coverage"].append(
                {
                    "check_id": "X-COV-01",
                    "status": "skipped",
                    "reason": "SEARCH_UNAVAILABLE",
                    "detail": (
                        f"{request_count} verification request(s) await the invoking agent's "
                        "web search and source-page inspection."
                    ),
                    "scope": "D-TRUST-05 and applicable D-ENTITY-03 checks",
                }
            )
    report = _compose_report(store, collected, budget, options, on_progress)
    if requests_artifact is not None:
        report["verification_requests_artifact"] = requests_artifact
    return report


@with_parse_cache
def finalize_agent_search(
    store: Dict[str, Any],
    requests: Dict[str, Any],
    results: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Finalize a saved collection with agent-inspected search evidence."""
    options = options or {}
    valid_store, store_errors = validate_observation_store(store)
    if not valid_store:
        raise ValueError("Invalid observation store: " + "; ".join(store_errors))
    working_store = copy.deepcopy(store)
    _, verification_coverage = agent_search_mod.apply_verification_results(
        working_store, requests, results
    )
    context = working_store.get("audit_context")
    if not isinstance(context, dict):
        raise ValueError("Observation store lacks audit_context; collection must be prepared by this version")

    collected = {
        "store": working_store,
        "coverage": copy.deepcopy(context.get("coverage", [])) + verification_coverage,
        "budget": copy.deepcopy(context.get("budget", {})),
        "scope": copy.deepcopy(context.get("scope", {})),
    }
    budget = Budget(options.get("budget"))
    previous = collected["budget"]
    budget.pages_fetched = int(previous.get("pages_fetched", 0) or 0)
    budget.requests_made = int(previous.get("requests_made", 0) or 0)
    budget.render_pages_done = int(previous.get("render_pages_done", 0) or 0)
    budget.search_queries_made = int(
        working_store.get("capabilities", {}).get("corroboration", {}).get("performed_queries", 0) or 0
    )
    on_progress = options.get("_progress") or (lambda _stage: None)
    report = _compose_report(working_store, collected, budget, options, on_progress)
    report["run"]["budget"]["agent_search_requests"] = len(requests.get("requests", []))
    report["run"]["budget"]["agent_search_completed"] = budget.search_queries_made
    return report


def _failure_report(url: str, reason: str, detail: str) -> Dict[str, Any]:
    return {"site": str(url) or "unknown", "audited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "summary": _summarize([]), "findings": [], "scope": {}, "proactive_opportunities": [],
            "coverage": [{"check_id": "X-COV-01", "status": "failed", "reason": reason, "detail": detail,
                          "scope": "audit incomplete; no conclusion about site quality"}],
            "run": {"schema_valid": True, "skill_failures": [{"skill": "audit-lifecycle", "error": detail}]}}


def run_audit(url: str, options=None, **kwargs) -> Dict[str, Any]:
    """Library API: unexpected lifecycle failures become explicit coverage gaps."""
    import time
    started = time.monotonic()
    try:
        report = _run_audit(url, options, **kwargs)
    except Exception as exc:
        report = _failure_report(url, "AUDIT_FAILED", f"{type(exc).__name__}: {exc}")
    report["run"]["elapsed_s"] = round(time.monotonic() - started, 3)
    return report


def _worker(connection, url, options):
    import os
    if os.name == "posix":
        os.setsid()  # descendants can be reclaimed when the process deadline expires
    try:
        worker_options = dict(options or {})
        worker_options["_progress"] = lambda stage: connection.send(("progress", stage))
        connection.send(("report", run_audit(url, worker_options)))
    finally:
        connection.close()


def run_with_deadline(url, options, timeout_s=280):
    """A separate process bounds DNS, browser startup and detector CPU time."""
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, url, options))
    process.start()
    sender.close()
    last_stage = "worker_startup"
    import time
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _failure_report(
                    url,
                    "BUDGET_EXHAUSTED",
                    f"Audit exceeded {timeout_s}s process deadline during {last_stage}.",
                )
            if not receiver.poll(remaining):
                continue
            try:
                kind, payload = receiver.recv()
            except EOFError:
                return _failure_report(url, "AUDIT_FAILED", f"Audit worker exited without a report during {last_stage}.")
            if kind == "progress":
                last_stage = payload
                continue
            if kind == "report":
                return payload
    finally:
        import os
        import signal
        if os.name == "posix":
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        receiver.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a website for AI-discoverability and on-site-engagement failures.")
    parser.add_argument("url", nargs="?", help="The URL or domain to audit.")
    parser.add_argument("--out-dir", default=".", help="Directory to write report.json and report.md into.")
    parser.add_argument("--max-pages", type=int, help="Override the raw-crawl page budget.")
    parser.add_argument("--deadline-s", type=int, default=280, help="Hard worker deadline in seconds.")
    parser.add_argument("--save-evidence", action="store_true", help="Also write observations.json for evidence replay.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-agent-search", action="store_true",
        help="Collect once and emit observations plus bounded agent-search requests.",
    )
    mode.add_argument(
        "--finalize-agent-search", metavar="RESULTS_JSON",
        help="Validate agent search results and finalize from saved evidence without recrawling.",
    )
    parser.add_argument(
        "--verification-requests", metavar="REQUESTS_JSON",
        help="Requests file for finalization; defaults to <out-dir>/verification_requests.json.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if args.finalize_agent_search:
        if args.url:
            parser.error("URL is not accepted with --finalize-agent-search; the saved store fixes audit scope")
        store_path = out_dir / "observations.json"
        requests_path = Path(args.verification_requests) if args.verification_requests else out_dir / "verification_requests.json"
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
            requests = json.loads(requests_path.read_text(encoding="utf-8"))
            results = json.loads(Path(args.finalize_agent_search).read_text(encoding="utf-8"))
            import time
            started = time.monotonic()
            report = finalize_agent_search(
                store, requests, results, {"include_evidence": True}
            )
            report["run"]["elapsed_s"] = round(time.monotonic() - started, 3)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.exit(2, f"Agent-search finalization failed: {exc}\n")
        evidence_store = report.pop("evidence_store", None)
        requests_artifact = None
    else:
        if not args.url:
            parser.error("URL is required unless --finalize-agent-search is used")
        if args.verification_requests:
            parser.error("--verification-requests applies only to --finalize-agent-search")
        options: Dict[str, Any] = {}
        if args.max_pages is not None:
            if not 1 <= args.max_pages <= MAX_REQUESTS_PER_AUDIT:
                parser.error(f"--max-pages must be between 1 and {MAX_REQUESTS_PER_AUDIT}")
            options["budget"] = {"raw_crawl_max_pages": args.max_pages}
        options["include_evidence"] = args.save_evidence or args.prepare_agent_search
        report = run_with_deadline(args.url, options, timeout_s=args.deadline_s)
        evidence_store = report.pop("evidence_store", None)
        requests_artifact = report.pop("verification_requests_artifact", None)

    valid, errors = validate_report_schema(report)
    if not valid:
        parser.exit(2, "Report validation failed; no report written: " + "; ".join(errors) + "\n")

    results_template = None
    if args.prepare_agent_search:
        if evidence_store is None or requests_artifact is None:
            parser.exit(2, "Preparation failed to produce saved evidence and verification requests.\n")
        valid_requests, request_errors = agent_search_mod.validate_verification_requests(requests_artifact)
        if not valid_requests:
            parser.exit(2, "Verification request validation failed: " + "; ".join(request_errors) + "\n")
        results_template = agent_search_mod.build_results_template(requests_artifact)
        valid_template, template_errors = agent_search_mod.validate_verification_results(results_template)
        if not valid_template:
            parser.exit(2, "Verification template validation failed: " + "; ".join(template_errors) + "\n")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    evidence_path = out_dir / "observations.json"
    if evidence_store is not None:
        evidence_path.write_text(json.dumps(evidence_store, indent=2), encoding="utf-8")
    elif evidence_path.exists():
        # The directory is explicitly the destination for this run. Keeping
        # evidence from an older run beside a newer report is more dangerous
        # than omitting it: timestamps and findings no longer correspond.
        evidence_path.unlink()

    if args.prepare_agent_search:
        (out_dir / "verification_requests.json").write_text(
            json.dumps(requests_artifact, indent=2), encoding="utf-8"
        )
        (out_dir / "verification_results.template.json").write_text(
            json.dumps(results_template, indent=2), encoding="utf-8"
        )

    import render_report

    (out_dir / "report.md").write_text(render_report.render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
