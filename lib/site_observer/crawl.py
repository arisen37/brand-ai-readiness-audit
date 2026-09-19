"""Raw-lens crawl with template clustering and stratified sampling. Discovery
from the link graph only (a sitemap, if declared, is folded in by the caller
once fetched) -- never guesses URL patterns that were never actually linked.

Inputs:  origin, an injected page fetcher, robots, budget
Outputs: page records, link graph, template clusters
Nature:  deterministic

The fetcher is injected (never `requests` called directly here) so this
module has no network dependency of its own and every path is exercised in
tests without touching a socket.
"""

from __future__ import annotations

import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse, urlunsplit

from lib.common.extract import extract_links
from lib.common.robots import robots_allows_every_interpretation
from lib.common.network_policy import unsafe_target
from lib.common.url_normalize import normalize_url_for_evidence

_NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
_HEX_ID_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
_UUID_LIKE_RE = re.compile(r"^[0-9a-f-]{20,}$", re.IGNORECASE)
_SEGMENT_EXTENSION_RE = re.compile(r"^(.*)(\.[A-Za-z0-9]{1,8})$")


def registrable_host(url_or_host: str) -> str:
    """A small, local notion of "same site" (strip scheme/port/www) -- not a
    full public-suffix-list implementation, which this crawler's same-host
    scoping does not need."""
    host = urlparse(url_or_host).netloc or url_or_host
    host = host.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


# Directory-index filenames every common server maps to the containing
# directory. Deliberately short: these are the spellings that are near-universal,
# not every filename a server *could* be configured to treat as an index.
_DIRECTORY_INDEX_NAMES = ("index.html", "index.htm", "index.php", "default.html", "default.htm")


def canonical_resource_url(url: str) -> str:
    """One key per resource, so a page linked two ways is fetched and reported once.

    Applies only normalizations that URL semantics already guarantee, plus one
    near-universal server convention:

    - scheme and host are case-insensitive (RFC 3986), so they lowercase; the
      path is case-sensitive and is left exactly as written;
    - the default port for the scheme is equivalent to omitting it;
    - a fragment is a client-side pointer into a resource, never a different
      resource, so it is dropped;
    - a trailing directory-index filename resolves to its directory on every
      mainstream server, so "/docs/index.html" keys as "/docs/";
    - an empty path is "/".

    Deliberately NOT normalized, because each would merge resources that are
    genuinely allowed to differ: content-bearing query parameters (order and
    presence are server-defined, and "?page=2" is a different resource), and
    the trailing slash on a non-index path ("/about" and "/about/" are distinct
    targets that servers usually resolve with a redirect -- which this crawler
    already follows, recording the resolved URL).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return url.split("#")[0]

    netloc = host
    port = parsed.port
    if port and port != (443 if scheme == "https" else 80 if scheme == "http" else None):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    for index_name in _DIRECTORY_INDEX_NAMES:
        if path.endswith("/" + index_name):
            path = path[: -len(index_name)]
            break
    if not path:
        path = "/"

    return normalize_url_for_evidence(urlunsplit((scheme, netloc, path, parsed.query, "")))


def template_shape(path: str) -> str:
    """Collapse id-shaped path segments to '#' so /product/123 and
    /product/456 cluster as one template: /product/#. This is the raw-lens
    input to the finding unit's `(check_id, template_cluster)` grouping
    (PROJECT_CONTEXT.md D-7).

    A trailing file extension (e.g. /product/123.html) is stripped before
    the id patterns are tested and reattached after, so the numeric/hex/uuid
    match sees "123" rather than the never-matching "123.html" -- otherwise
    the most common real templated-URL shape (numeric id immediately
    followed by an extension in the same path segment) would never cluster."""
    segments = (path or "/").split("/")
    shaped = []
    for seg in segments:
        ext_match = _SEGMENT_EXTENSION_RE.match(seg)
        stem, ext = (ext_match.group(1), ext_match.group(2)) if ext_match else (seg, "")
        if _NUMERIC_SEGMENT_RE.match(stem) or _HEX_ID_RE.match(stem) or _UUID_LIKE_RE.match(stem):
            shaped.append("#" + ext)
        else:
            shaped.append(seg)
    return "/".join(shaped) or "/"


def prioritize_seed_urls(origin: str, seed_urls: List[str], limit: int) -> List[str]:
    """Return a deterministic, representative seed frontier.

    The requested URL always stays first.  The remaining sitemap seeds are
    split into clean and parameterized URLs, then selected round-robin across
    template shapes.  This keeps a large family of faceted/query URLs from
    consuming the bounded crawl before distinct content templates are seen;
    parameterized URLs remain eligible when capacity is available.
    """
    if limit <= 0:
        return []

    requested = canonical_resource_url(origin)
    unique = list(dict.fromkeys(canonical_resource_url(url) for url in [origin] + seed_urls))
    remainder = [url for url in unique if url != requested]

    def round_robin(urls: List[str]) -> List[str]:
        groups: Dict[str, deque[str]] = {}
        for url in urls:
            shape = template_shape(urlparse(url).path)
            groups.setdefault(shape, deque()).append(url)
        ordered: List[str] = []
        group_queues = list(groups.values())
        while any(group_queues):
            for group in group_queues:
                if group:
                    ordered.append(group.popleft())
        return ordered

    clean = [url for url in remainder if not urlparse(url).query]
    parameterized = [url for url in remainder if urlparse(url).query]
    return ([requested] + round_robin(clean) + round_robin(parameterized))[:limit]


def crawl(
    origin: str,
    fetch: Callable[[str], Dict[str, Any]],
    robots: Optional[Dict[str, Any]] = None,
    max_pages: int = 30,
    max_frontier: Optional[int] = None,
    time_left: Callable[[], float] = lambda: float("inf"),
    can_fetch_more: Callable[[], bool] = lambda: True,
    on_page_fetched: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    polite_delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    seed_urls: Optional[List[str]] = None,
    max_concurrency: int = 1,
) -> Dict[str, Any]:
    """Same-host BFS crawl, bounded by `max_pages`, `time_left()` (stage
    budget, polled between fetches) and `can_fetch_more()` (a page-count
    budget hook). `robots` gates every URL, including the seed, exactly the
    same way -- a disallowed origin yields zero pages, never a special-cased
    exception for "the one the operator asked about".

    `polite_delay_s`, applied between fetches (never before the first or
    after the last), is how "no rate abuse" is actually enforced rather than
    just documented -- `sleep` is injectable so tests exercise the delay
    logic without a real test suite run taking minutes."""
    host = registrable_host(origin)
    max_frontier = max_frontier if max_frontier is not None else max_pages * 6
    initial = prioritize_seed_urls(origin, seed_urls or [], max(1, max_frontier))
    requested_queue = deque(initial[:1])
    clean_queue = deque(url for url in initial[1:] if not urlparse(url).query)
    parameter_queue = deque(url for url in initial[1:] if urlparse(url).query)
    seen: Set[str] = set(initial)
    pages: Dict[str, Dict[str, Any]] = {}
    link_graph: Dict[str, Set[str]] = {}
    skipped_robots: List[str] = []
    skipped_safety: List[str] = []

    concurrency = max(1, int(max_concurrency or 1))

    def frontier_has_urls() -> bool:
        return bool(requested_queue or clean_queue or parameter_queue)

    def pop_frontier() -> str:
        if requested_queue:
            return requested_queue.popleft()
        if clean_queue:
            return clean_queue.popleft()
        return parameter_queue.popleft()

    def eligible_next_url() -> Optional[str]:
        while frontier_has_urls() and len(pages) < max_pages and time_left() > 0 and can_fetch_more():
            url = pop_frontier()
            if unsafe_target(url):
                skipped_safety.append(url)
                continue
            path = urlparse(url).path or "/"
            if urlparse(url).query:
                path += "?" + urlparse(url).query
            if robots is not None and not robots_allows_every_interpretation(robots, path):
                skipped_robots.append(url)
                continue
            return url
        return None

    def ingest_page(url: str, result: Dict[str, Any]) -> None:
        pages[url] = result
        if on_page_fetched:
            on_page_fetched(url, result)

        status = result.get("status_code", 200)
        if status is None or not 200 <= status < 300:
            return  # Do not explore authentication/error-page links.

        html = (result or {}).get("html", "")
        if not html:
            return
        for link in extract_links(html, base_url=result.get("final_url") or url):
            href = link["href"]
            if link.get("unsafe_action"):
                skipped_safety.append(href)
                continue
            parsed = urlparse(href)
            if parsed.scheme not in ("http", "https") or registrable_host(href) != host:
                continue
            normalized = canonical_resource_url(href)
            link_graph.setdefault(normalized, set()).add(url)
            if normalized not in seen and len(seen) < max_frontier:
                seen.add(normalized)
                if urlparse(normalized).query:
                    parameter_queue.append(normalized)
                else:
                    clean_queue.append(normalized)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while frontier_has_urls() and len(pages) < max_pages and time_left() > 0 and can_fetch_more():
            batch = []
            while (
                len(batch) < concurrency
                and len(pages) + len(batch) < max_pages
                and time_left() > 0
                and can_fetch_more()
            ):
                url = eligible_next_url()
                if url is None:
                    break
                if (pages or batch) and polite_delay_s > 0:
                    sleep(polite_delay_s)
                batch.append((url, executor.submit(fetch, url)))
            if not batch:
                break
            for url, future in batch:
                ingest_page(url, future.result())

    clusters: Dict[str, List[str]] = {}
    for url in pages:
        clusters.setdefault(template_shape(urlparse(url).path), []).append(url)

    return {
        "pages": pages,
        "link_graph": {url: sorted(sources) for url, sources in link_graph.items()},
        "clusters": {shape: sorted(urls) for shape, urls in clusters.items()},
        "skipped_robots": sorted(set(skipped_robots)),
        "skipped_safety": sorted(set(skipped_safety)),
        "frontier_remaining": len(requested_queue) + len(clean_queue) + len(parameter_queue),
    }


def stratified_sample(clusters: Dict[str, List[str]], sample_size: int) -> List[str]:
    """Pick up to `sample_size` URLs spreading across as many template
    clusters as possible (one per cluster first, then a second pass) rather
    than exhausting the budget on the largest cluster alone -- the rendered
    lens exists to catch cross-template gaps, so it should see every
    template at least once before it sees any template twice."""
    if sample_size <= 0:
        return []
    ordered_clusters = [sorted(urls) for _, urls in sorted(clusters.items())]
    sample: List[str] = []
    round_index = 0
    while len(sample) < sample_size and any(round_index < len(urls) for urls in ordered_clusters):
        for urls in ordered_clusters:
            if len(sample) >= sample_size:
                break
            if round_index < len(urls):
                sample.append(urls[round_index])
        round_index += 1
    return sample


def main() -> int:
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
