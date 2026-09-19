"""Optional legacy Linkup adapter for explicitly injected Python clients.

The submission's default workflow uses the invoking agent's provider-neutral JSON
handoff in ``skills/audit-orchestrator/scripts/agent_search.py`` and requires no
Linkup setup. This adapter remains only for callers that explicitly inject a client;
provider objects and credentials never enter the evidence store.
"""

from __future__ import annotations

import queue
import re
import threading
from typing import Any, Dict, List
from urllib.parse import urlparse


def create_linkup_client(api_key: str) -> Any:
    """Create the official Linkup SDK client without making a request."""
    from linkup import LinkupClient

    return LinkupClient(api_key=api_key)


def _field(item: Any, *names: str) -> Any:
    if isinstance(item, dict):
        for name in names:
            if item.get(name) is not None:
                return item[name]
        return None
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _external_url(url: str, excluded_host: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    excluded = (urlparse("//" + excluded_host).hostname or excluded_host).lower().rstrip(".")
    if excluded.startswith("www."):
        excluded = excluded[4:]
    bare_host = host[4:] if host.startswith("www.") else host
    return parsed.scheme in {"http", "https"} and bool(host) and not (
        bare_host == excluded or bare_host.endswith("." + excluded)
    )


def normalize_linkup_results(response: Any, *, excluded_host: str, limit: int = 5) -> List[Dict[str, str]]:
    """Normalize SDK response models (or equivalent dicts) and drop site-owned hits."""
    items = _field(response, "results") or []
    normalized: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        # searchResults can include images when requested by another caller;
        # this audit accepts only textual evidence.
        if str(_field(item, "type") or "text").lower() != "text":
            continue
        url = str(_field(item, "url") or "").strip()
        if not _external_url(url, excluded_host) or url in seen:
            continue
        seen.add(url)
        normalized.append(
            {
                "url": url,
                "title": str(_field(item, "name", "title") or "").strip(),
                "snippet": str(_field(item, "content", "snippet") or "").strip(),
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _bounded_call(call: Any, timeout_s: float, thread_name: str) -> Any:
    outcome: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcome.put(("ok", call()))
        except Exception as exc:  # noqa: BLE001 - translated by the orchestrator
            outcome.put(("error", exc))

    threading.Thread(target=invoke, name=thread_name, daemon=True).start()
    try:
        status, value = outcome.get(timeout=max(0.001, timeout_s))
    except queue.Empty as exc:
        raise TimeoutError("Linkup search exceeded the corroboration deadline") from exc
    if status == "error":
        raise value
    return value


def search_linkup(
    client: Any,
    query: str,
    *,
    excluded_host: str,
    timeout_s: float,
    max_results: int = 5,
) -> List[Dict[str, str]]:
    """Run one bounded Linkup lookup and return provider-neutral results.

    The SDK receives the remaining stage timeout and the call is also placed in
    a daemon thread so the audit does not depend solely on transport behavior
    to honor its stage deadline.
    """
    response = _bounded_call(
        lambda: client.search(
                query=query,
                depth="standard",
                output_type="searchResults",
                include_images=False,
                exclude_domains=[excluded_host],
                max_results=max_results,
                timeout=timeout_s,
        ),
        timeout_s,
        "linkup-claim-search",
    )
    return normalize_linkup_results(response, excluded_host=excluded_host, limit=max_results)


def search_linkup_entity_collisions(
    client: Any,
    canonical_name: str,
    *,
    excluded_host: str,
    timeout_s: float,
    max_results: int = 8,
) -> Dict[str, Any]:
    """Ask Linkup for sourced, distinct same-name entities.

    Structured candidates are retained only when their URL also appears in
    Linkup's returned source list. This prevents an unsourced generated item
    from becoming collision evidence.
    """
    clean_name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(canonical_name or ""))
    clean_name = re.sub(r"\s+", " ", clean_name).strip()[:120]
    query = (
        f'Find public entities genuinely distinct from the entity at {excluded_host} '
        f'that use the same canonical name "{clean_name}". Exclude profiles, articles, '
        "directories, and social pages about the audited entity itself. Return only "
        "a match supported by its source URL; return an empty matches list when uncertain."
    )
    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "source_url": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["name", "source_url", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["matches"],
        "additionalProperties": False,
    }
    response = _bounded_call(
        lambda: client.search(
            query=query,
            depth="standard",
            output_type="structured",
            structured_output_schema=schema,
            include_images=False,
            include_sources=True,
            exclude_domains=[excluded_host],
            max_results=max_results,
            timeout=timeout_s,
        ),
        timeout_s,
        "linkup-entity-search",
    )
    data = _field(response, "data") or (response if isinstance(response, dict) else {})
    raw_matches = data.get("matches", []) if isinstance(data, dict) else []
    raw_sources = _field(response, "sources") or []
    sourced_urls = {str(_field(source, "url") or "").strip() for source in raw_sources}
    matches: List[Dict[str, str]] = []
    seen = set()
    normalized_name = re.sub(r"\W+", "", clean_name).casefold()
    for item in raw_matches if isinstance(raw_matches, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url") or "").strip()
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
        category = re.sub(r"\s+", " ", str(item.get("category") or "")).strip()
        if (
            not url
            or url not in sourced_urls
            or not _external_url(url, excluded_host)
            or url in seen
            or re.sub(r"\W+", "", name).casefold() != normalized_name
            or not category
        ):
            continue
        seen.add(url)
        matches.append({"name": name, "source_url": url, "category": category})
    return {"query": query, "matches": matches}
