# Reading workflow

## Scope and health

Call status and confirm the connected provider/source identity, service capabilities, coverage and current job when provided. For Graph, use the configured cloud scope, not the empty local anchor as a document directory. If authorization is missing, follow the explicit login procedure in [requirements](requirements.md); do not ask for credentials in chat. On older services use job_status to establish whether a scan is running. For a lookup, a few verified excerpts may suffice. Totals, rankings, cross-file comparisons and absence claims require a defined population: project, period, source set, approval status, currency and deduplication keys as applicable.

Use the conversation to choose the workflow. If a capable service's optional plan tool would help, treat its lookup/version/exhaustive/clarification classification as a heuristic suggestion. Do not call it on every question or let a lookup classification relax completeness requirements. Resolve material ambiguity from available context before asking the user.

Enumerate that population through every list page; track exclusions and failed relevant files. Graph list/search rechecks current access and etag for returned candidates; a permission, network or version error does not count as an empty or completed page. If pagination changes generation/session, restart enumeration. Request refresh when freshness matters, then inspect job completion and failures. Do not repeatedly queue identical jobs or busy-poll. Cancel only a job no longer needed or explicitly stopped by the user.

## Find and verify

Search within the smallest appropriate prefix using names, project IDs, meaningful terms and aliases. Keyword is the default; multi-term keyword searches can match evidence across a file. Where explicitly configured and supported, hybrid/semantic retrieval can help locate paraphrases, and reranking can reorder candidates when requested. Check actual retrieval mode, model availability, indexed coverage, limits and degradation. Rankings and snippets are candidate selection aids; no search mode proves that every business-relevant source was found.

Choose sources by explicit approval/applicability/supersession evidence, not filename or mtime alone. Disambiguate duplicate filenames by relative path and identity. For cloud files preserve `file.origin` (drive/item identity, available web_url, etag and content_sha256). Equal content hashes identify byte-identical copies, not which business record should be counted. Cloud `source_version` combines identity, remote version and content hash, so it is not interchangeable with content_sha256.

Inspect structure, version and warnings, then read the relevant range with `expected_version`. Include the fact's header, units, notes and exceptions. Inspect groups give segment offsets; selectors use source locations (CSV rows, XLSX sheet/rows, PDF page, PPTX slide). With a selector, `start` is the offset within matching segments; there is no A1 column clipping. Text `line_bucket` ranges are navigation buckets, not the document's actual extent.

If context is available and the hit has an actual content ordinal, pass that ordinal and the inspected `expected_version` to gather bounded candidate surroundings. Keep original locators, review `context_reason`, candidate-header caveats, gaps and excluded boundaries. A title or first extracted row is not automatically a verified table header, and notes on later pages can be missed. Use read/inspect to fill the relevant gaps; a context response cannot substitute for the entire population needed for a total or absence claim. On 0.3, continue using versioned read directly.

Follow relevant pagination with the same selector/version. A final selected page does not prove the whole file was read. Partial text extraction may support a bounded finding; retain explicit extraction gaps, including PDF pages without usable text. If the source changes, discard the mixed-version chain and restart that file.

## Collect and compute

For multiple sources, use `team_drive_evidence_bundle` when available, supplying inspected file IDs/versions and a plain-language scope. Check its manifest, per-file results and gaps before computing. A bundle keeps evidence together; it cannot certify that the chosen population is complete or approved, and its observation window is not a transactional folder snapshot.

On an older service, export each source with its expected version and keep an equivalent manifest: included/excluded files, versions, locators, warnings/gaps, observations and intended filters. Export includes all supported segments, not a prior read selector. Pass returned absolute artifact paths only to authorized local analysis tools; do not upload them by implication.

Calculate with deterministic code/tools, exact decimal money arithmetic, explicit currency/rounding, agreed business filters and deduplication keys. Reconcile segment counts with actual business rows. When relevant inputs are missing or partial, resolve them or label a bounded subtotal rather than an unconditional total/ranking.

For a CSV calculation, see the [simulated worked example](evidence-worked-example.md) for JSONL row reconstruction, explicit filters, Decimal arithmetic and a row-level inclusion/gap ledger; adapt its business definitions to the actual evidence.

## Answer

Lead with the finding and cite exact relative file, returned locator and source version. For cloud files link the returned `file.origin.web_url` when available; otherwise retain relative path and item identity without inventing a URL. Never cite temporary download URLs or the local anchor. For exhaustive work state the population, exclusions, gaps, observation window and calculation method to the extent needed for verification. Public research can supply separately sourced public facts; it cannot replace an internal approval, budget or contract term.
