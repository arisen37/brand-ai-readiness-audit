# Orchestration rules

1. One collection pass, with robots preflight, bounded sitemap discovery, raw crawl,
   sampled rendering, semantic page classification and local explicit-span probes.
2. Detector skills read the same store and never re-fetch.
3. Evidence-prioritization owns scoring. The entrypoint owns lifecycle and composition.
4. Bind findings to observations and validate the final report. Unresolvable evidence
   is dropped with coverage; proactive opportunities remain outside severity totals.
5. Call both the general proactive layer and crawl-render's D-EXTRACT-08 function.
6. Isolate failed detector imports/execution; unexpected lifecycle errors become
   explicit incomplete reports. See [degraded mode](degraded-mode.md).

| Stage | Limit |
|---|---|
| Robots preflight | 10 seconds |
| Sitemap and raw crawl | 90 seconds; 3 sitemaps; 180 sitemap URLs; 30 fetched pages; up to 4 concurrent page fetches |
| Render sample | 90 seconds; at most 8 pages; 15 seconds per navigation |
| Agent web-search corroboration | at most 3 entity/claim requests and 5 inspected sources per request; duplicate claim queries share one request |
| Entire audit work budget | 250 seconds shared across budgeted stages |
| CLI worker | 280 seconds default hard process deadline, overrideable with `--deadline-s`; the 30-second default margin is reserved for composition, validation, serialization, and report writing |

The shared site-request policy also caps requests at 200 and stops on 429/503. Site
requests are not retried. Agent web search occurs between preparation and finalization;
finalization validates cited results and never recrawls. Detector CPU is covered by the CLI worker deadline;
the library API has cooperative collection limits and requires caller-side process
isolation for a hard CPU deadline. Stage allowances are ceilings, not additive runtime
promises. A deadline report is incomplete, not proof of a successful typical-site audit.
