"""Bounded sitemap discovery through the anonymous robots-aware transport."""
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree


MAX_SITEMAP_DOCUMENTS = 3
MAX_SITEMAP_URLS = 180


def discover(origin, robots, fetch, *, time_left=lambda: 1):
    candidates = list(dict.fromkeys(
        robots.get('document', {}).get('sitemap', []) or [urljoin(origin, '/sitemap.xml')]
    ))
    queue = candidates[:MAX_SITEMAP_DOCUMENTS]
    visited, urls, statuses, checked_documents = set(), set(), [], []
    indexed_children = set()
    limitations = set()
    if len(candidates) > MAX_SITEMAP_DOCUMENTS:
        limitations.add('DOCUMENT_LIMIT')

    while queue and len(visited) < MAX_SITEMAP_DOCUMENTS and time_left() > 0:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        response = fetch(url)
        code = response.get('status_code')
        if code != 200:
            outcome = 'missing' if code == 404 else 'error'
            statuses.append(outcome)
            checked_documents.append({
                'url': url,
                'status_code': code,
                'outcome': outcome,
                'error': (response.get('evidence', {}) or {}).get('error'),
            })
            if code != 404 or url in indexed_children:
                limitations.add('FETCH_FAILED')
            continue
        text = response.get('html', '')
        if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
            statuses.append('error')
            checked_documents.append({'url': url, 'status_code': code, 'outcome': 'unsafe_xml', 'error': 'DOCTYPE/ENTITY excluded'})
            limitations.add('FETCH_FAILED')
            continue
        try:
            document = ElementTree.fromstring(text)
        except (ElementTree.ParseError, ValueError):
            statuses.append('error')
            checked_documents.append({'url': url, 'status_code': code, 'outcome': 'unparseable', 'error': 'invalid XML'})
            limitations.add('FETCH_FAILED')
            continue
        kind = document.tag.rsplit('}', 1)[-1]
        if kind not in {'sitemapindex', 'urlset'}:
            statuses.append('error')
            checked_documents.append({'url': url, 'status_code': code, 'outcome': 'unparseable', 'error': f'unsupported root element: {kind}'})
            limitations.add('FETCH_FAILED')
            continue
        statuses.append('ok')
        checked_documents.append({'url': url, 'status_code': code, 'outcome': 'ok', 'error': None})
        for node in document.iter():
            if node.tag.rsplit('}', 1)[-1] != 'loc' or not node.text:
                continue
            target = node.text.strip()
            try:
                parsed = urlsplit(target)
            except ValueError:
                continue
            if parsed.scheme not in {'http', 'https'} or parsed.netloc != urlsplit(origin).netloc:
                continue
            if kind == 'sitemapindex':
                indexed_children.add(target)
                if target not in visited and target not in queue and len(visited) + len(queue) < MAX_SITEMAP_DOCUMENTS:
                    queue.append(target)
                else:
                    limitations.add('DOCUMENT_LIMIT')
            elif len(urls) < MAX_SITEMAP_URLS:
                urls.add(target)
            elif target not in urls:
                limitations.add('ENTRY_LIMIT')

    if queue:
        limitations.add('TIME_BUDGET' if time_left() <= 0 else 'DOCUMENT_LIMIT')
    status = 'ok' if 'ok' in statuses else 'missing' if statuses and all(s == 'missing' for s in statuses) else 'error'
    if status == 'error':
        limitations.add('FETCH_FAILED')
    return {
        'status': status,
        'entries': [{'loc': u} for u in sorted(urls)],
        'checked_urls': sorted(visited),
        'checked_documents': checked_documents,
        'partial': bool(limitations),
        'limitation_reasons': sorted(limitations),
    }
