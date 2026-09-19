"""Provider-neutral handoff between the audit and an agent's web-search tools.

The collector emits bounded verification requests.  The invoking agent searches
and inspects source pages, then this module validates the returned citations and
turns them into ordinary CORROBORATION/CLAIM_CORROBORATION observations.  It
never performs network requests itself.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent
MARKETPLACE_ROOT = SCRIPTS_DIR.parents[2]
for _path in (
    MARKETPLACE_ROOT,
    MARKETPLACE_ROOT / "skills" / "trust-freshness-audit" / "scripts",
    MARKETPLACE_ROOT / "skills" / "entity-semantic-audit" / "scripts",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from lib.common.observations import make_observation  # noqa: E402
from lib.common.schema import load_schema, validate_document  # noqa: E402

SCHEMA_VERSION = "1.0"
CLAIM_ASSESSMENTS = {"supports", "contradicts", "mentions", "unrelated"}
ENTITY_ASSESSMENTS = {"distinct_entity", "same_entity", "unrelated"}


def _audit_id(store: Dict[str, Any]) -> str:
    target = store.get("target", {}) or {}
    material = "|".join(
        str(value)
        for value in (
            store.get("store_version", ""),
            store.get("collected_at", ""),
            target.get("audited_host", ""),
            target.get("normalized_url", ""),
        )
    )
    return "AUDIT-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _request_id(kind: str, query: str) -> str:
    digest = hashlib.sha256(f"{kind}|{query}".encode("utf-8")).hexdigest()[:16]
    return "VERIFY-" + digest


def _audited_domain(store: Dict[str, Any]) -> str:
    target = store.get("target", {}) or {}
    return (
        urlparse(target.get("origin", "")).hostname
        or urlparse("//" + target.get("audited_host", "")).hostname
        or target.get("audited_host", "")
    ).lower().rstrip(".")


def _external_url(url: str, audited_domain: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    audited = audited_domain.lower().rstrip(".")
    if audited.startswith("www."):
        audited = audited[4:]
    bare = host[4:] if host.startswith("www.") else host
    return parsed.scheme in {"http", "https"} and bool(host) and not (
        bare == audited or bare.endswith("." + audited)
    )


def build_verification_requests(store: Dict[str, Any], max_queries: int = 3) -> Dict[str, Any]:
    """Select a bounded request set from the saved observation store."""
    import build_claim_table
    import build_entity_profile
    import corroborate
    from _trust_util import is_corroboration_worthy

    claim_table = build_claim_table.build_claim_table(store)
    profile = build_entity_profile.build_entity_profile(store)
    canonical_name = ((profile.get("fields", {}).get("canonical_name") or {}).get("value") or "").strip()
    distinguishing = profile.get("fields", {}).get("distinguishing_attributes", {}) or {}
    collision_applicable = bool(canonical_name) and not distinguishing.get("present", False)
    audited_domain = _audited_domain(store)
    requests: List[Dict[str, Any]] = []

    if collision_applicable and len(requests) < max_queries:
        query = (
            f'Find genuinely distinct public entities using the exact name "{canonical_name}". '
            f'Exclude the audited entity and all pages on {audited_domain}. Inspect each source page.'
        )
        requests.append(
            {
                "id": _request_id("entity_collision", query),
                "kind": "entity_collision",
                "query": query,
                "excluded_domain": audited_domain,
                "canonical_name": canonical_name,
            }
        )

    eligible = [claim for claim in claim_table.get("claims", []) if is_corroboration_worthy(claim)]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for claim in eligible:
        query = corroborate.build_corroboration_query(claim, profile)
        grouped.setdefault(query, []).append(claim)

    for query, claims in grouped.items():
        if len(requests) >= max_queries:
            break
        requests.append(
            {
                "id": _request_id("claim_corroboration", query),
                "kind": "claim_corroboration",
                "query": query,
                "excluded_domain": audited_domain,
                "canonical_name": canonical_name,
                "claims": [
                    {
                        "id": claim["id"],
                        "text": claim["text"],
                        "source_url": claim["source_url"],
                        "observation_id": claim["observation_id"],
                    }
                    for claim in claims
                ],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": _audit_id(store),
        "audited_domain": audited_domain,
        "generated_at": store.get("collected_at"),
        "limits": {"max_queries": max_queries, "max_sources_per_request": 5},
        "selection": {
            "eligible_claims": len(eligible),
            "requested_claims": sum(len(item.get("claims", [])) for item in requests),
            "entity_collision_applicable": collision_applicable,
        },
        "requests": requests,
    }


def build_results_template(requests: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_id": requests["audit_id"],
        "results": [
            {
                "request_id": request["id"],
                "status": "pending",
                "searched_at": None,
                "sources": [],
            }
            for request in requests.get("requests", [])
        ],
    }


def _validate_or_raise(document: Dict[str, Any], schema_name: str, label: str) -> None:
    valid, errors = validate_document(document, load_schema(schema_name))
    if not valid:
        raise ValueError(f"Invalid {label}: " + "; ".join(errors))


def validate_verification_requests(document: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate a request artifact against the public handoff contract."""
    return validate_document(document, load_schema("verification-requests.schema.json"))


def validate_verification_results(document: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate a result artifact against the public handoff contract."""
    return validate_document(document, load_schema("verification-results.schema.json"))


def apply_verification_results(
    store: Dict[str, Any], requests: Dict[str, Any], results: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate agent results and append evidence observations.

    Returns ``(observations, coverage)``. Structural errors raise ValueError so
    a previous valid report is never overwritten with cross-run or malformed
    evidence.
    """
    _validate_or_raise(requests, "verification-requests.schema.json", "verification requests")
    _validate_or_raise(results, "verification-results.schema.json", "verification results")
    expected_audit_id = _audit_id(store)
    if requests.get("audit_id") != expected_audit_id or results.get("audit_id") != expected_audit_id:
        raise ValueError("Verification audit_id does not match the observation store")
    expected_requests = build_verification_requests(
        store, max_queries=requests.get("limits", {}).get("max_queries", 3)
    )
    if requests != expected_requests:
        raise ValueError("Verification requests do not match the request set issued for this observation store")

    request_map = {item["id"]: item for item in requests.get("requests", [])}
    result_map: Dict[str, Dict[str, Any]] = {}
    for result in results.get("results", []):
        request_id = result["request_id"]
        if request_id not in request_map:
            raise ValueError(f"Unknown verification request_id: {request_id}")
        if request_id in result_map:
            raise ValueError(f"Duplicate verification result for request_id: {request_id}")
        result_map[request_id] = result

    audited_domain = requests["audited_domain"]
    observations: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []
    completed_requests = 0
    verified_claims = 0
    collision_performed = False

    for request_id, request in request_map.items():
        result = result_map.get(request_id)
        status = result.get("status") if result else "missing"
        if status != "completed":
            if result and (result.get("searched_at") is not None or result.get("sources")):
                raise ValueError(
                    f"Non-completed verification result must not contain inspected evidence: {request_id}"
                )
            reason = "SEARCH_FAILED" if status == "failed" else "SEARCH_UNAVAILABLE"
            detail = (result or {}).get("error") or f"No completed agent-search result for {request_id}."
            coverage.append(
                {
                    "check_id": "X-COV-01",
                    "status": "failed" if status == "failed" else "skipped",
                    "reason": reason,
                    "detail": detail,
                    "scope": request["kind"].replace("_", " ") + " request",
                }
            )
            continue

        if not result.get("searched_at"):
            raise ValueError(f"Completed verification result lacks searched_at: {request_id}")
        completed_requests += 1
        sources = result.get("sources", [])
        if len(sources) > requests.get("limits", {}).get("max_sources_per_request", 5):
            raise ValueError(f"Too many sources for request_id: {request_id}")
        seen_urls = set()
        normalized_sources = []
        allowed = CLAIM_ASSESSMENTS if request["kind"] == "claim_corroboration" else ENTITY_ASSESSMENTS
        for source in sources:
            url = source["url"].strip()
            title = source["title"].strip()
            supporting_text = source["supporting_text"].strip()
            if not _external_url(url, audited_domain):
                raise ValueError(f"Source is not independent of {audited_domain}: {url}")
            if not title or not supporting_text:
                raise ValueError(f"Source title and supporting_text must contain non-whitespace text: {url}")
            if url in seen_urls:
                raise ValueError(f"Duplicate source URL for request_id {request_id}: {url}")
            seen_urls.add(url)
            assessment = source["assessment"]
            if assessment not in allowed:
                raise ValueError(f"Assessment {assessment!r} is invalid for {request['kind']}")
            entity_match = bool(source.get("entity_match", False))
            if request["kind"] == "claim_corroboration" and assessment in {"supports", "contradicts"} and not entity_match:
                raise ValueError(f"Claim assessment {assessment!r} requires entity_match for source: {url}")
            if request["kind"] == "entity_collision" and assessment == "same_entity" and not entity_match:
                raise ValueError(f"same_entity requires entity_match for source: {url}")
            if request["kind"] == "entity_collision" and assessment == "distinct_entity" and entity_match:
                raise ValueError(f"distinct_entity must not set entity_match for source: {url}")
            if source.get("entity_match") and not source.get("entity_evidence", "").strip():
                raise ValueError(f"entity_match requires entity_evidence for source: {url}")
            normalized_sources.append(
                {
                    "url": url,
                    "title": title,
                    "snippet": supporting_text,
                    "supporting_text": supporting_text,
                    "assessment": assessment,
                    "relation": assessment,
                    "entity_match": entity_match,
                    "entity_evidence": source.get("entity_evidence", "").strip(),
                    "source_type": source.get("source_type", "other"),
                }
            )

        timestamp = result["searched_at"]
        if request["kind"] == "claim_corroboration":
            for claim in request.get("claims", []):
                observations.append(
                    make_observation(
                        "CLAIM_CORROBORATION",
                        claim["source_url"],
                        {
                            "claim_id": claim["id"],
                            "claim_text": claim["text"],
                            "performed": True,
                            "query": request["query"],
                            "method": "agent_web_search_page_inspection",
                            "timestamp": timestamp,
                            "sources": normalized_sources,
                        },
                    )
                )
                verified_claims += 1
        else:
            normalized_name = re.sub(r"\W+", "", request["canonical_name"]).casefold()
            matches = []
            for source in normalized_sources:
                original = next(item for item in sources if item["url"].strip() == source["url"])
                entity_name = re.sub(r"\s+", " ", original.get("entity_name", "")).strip()
                category = re.sub(r"\s+", " ", original.get("entity_category", "")).strip()
                if source["assessment"] != "distinct_entity":
                    continue
                if re.sub(r"\W+", "", entity_name).casefold() != normalized_name or not category:
                    raise ValueError("Distinct-entity evidence requires the exact canonical name and a category")
                matches.append(
                    {
                        "name": entity_name,
                        "source_url": source["url"],
                        "category": category,
                        "supporting_text": source["supporting_text"],
                    }
                )
            observations.append(
                make_observation(
                    "CORROBORATION",
                    (store.get("target", {}) or {}).get("origin", ""),
                    {
                        "performed": True,
                        "query": request["query"],
                        "method": "agent_web_search_page_inspection",
                        "matches": matches,
                        "sources": normalized_sources,
                        "timestamp": timestamp,
                    },
                )
            )
            collision_performed = True

    # Re-finalization is idempotent: replace earlier agent-search observations,
    # while preserving raw collection and any explicitly injected adapter data.
    store["observations"] = [
        item
        for item in store.get("observations", [])
        if (item.get("value", {}) or {}).get("method") != "agent_web_search_page_inspection"
    ] + observations
    capability = store.setdefault("capabilities", {}).setdefault("corroboration", {})
    selection = requests.get("selection", {}) or {}
    capability.update(
        {
            "available": completed_requests > 0 or not request_map,
            "reason": None if completed_requests > 0 or not request_map else "AGENT_SEARCH_INCOMPLETE",
            "provider": "invoking_agent_web_search",
            "eligible_claims": selection.get("eligible_claims", 0),
            "verified_claims": verified_claims,
            "requested_queries": len(request_map),
            "performed_queries": completed_requests,
            "entity_collision_applicable": selection.get("entity_collision_applicable", False),
            "entity_collision_performed": collision_performed,
        }
    )
    return observations, coverage


def external_verification_summary(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Public, citation-ready projection for report.json/report.md."""
    summary = []
    for observation in store.get("observations", []):
        if observation.get("type") not in {"CORROBORATION", "CLAIM_CORROBORATION"}:
            continue
        value = observation.get("value", {}) or {}
        if value.get("method") != "agent_web_search_page_inspection" or not value.get("performed"):
            continue
        summary.append(
            {
                "type": "claim" if observation["type"] == "CLAIM_CORROBORATION" else "entity_collision",
                "claim_id": value.get("claim_id"),
                "claim_text": value.get("claim_text"),
                "query": value.get("query", ""),
                "searched_at": value.get("timestamp"),
                "sources": [
                    {
                        "url": source.get("url", ""),
                        "title": source.get("title", ""),
                        "supporting_text": source.get("supporting_text") or source.get("snippet", ""),
                        "assessment": source.get("assessment") or source.get("relation", "mentions"),
                        "entity_match": bool(source.get("entity_match")),
                    }
                    for source in value.get("sources", [])
                ],
            }
        )
    return summary
