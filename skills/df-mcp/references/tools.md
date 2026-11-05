# Tool contract quick reference

These inputs describe the reader's 0.7 contract: 13 evidence tools and five Wiki tools, with local, explicitly configured Windows shares and Microsoft Graph sources. The Skill is named `df-mcp`; the `team_drive_` tool prefix is retained for compatibility. The connected server's declared schema is authoritative if versions differ. Discover available tools from the connected schema and check status capabilities before using additions; status is not a substitute for tool discovery. Older services retain their supported workflows; omit unsupported parameters and use versioned read when additions are absent. Limits below use defaults, not performance guarantees. Real Graph APIs are implemented; synthetic tests do not establish real-tenant acceptance.

| Tool | Inputs | Key result / next step |
|---|---|---|
| `team_drive_status` | none | `service_version`, provider, source/config identity, `cloud_source` when applicable, shared-service health, `current_job_id` (use job_status for detail), `state_storage`, `coverage`, `last_scan_at` and limits when supported. Older versions omit newer fields. |
| `team_drive_list` | `prefix?`, `limit?` (default 20), `cursor?` | `files`, `total_matching_files`, `has_more`, `next_cursor`; inventory metadata is not freshly read evidence. |
| `team_drive_search` | `query`, `prefix?`, `limit?` (default 10), `cursor?`, `mode?` (`keyword` default, `hybrid`, `semantic`), `rerank?` (default false) | Ranked candidate files; `match.ordinal` and snippet help locate a read. `snippet_is_evidence` is false. Model-assisted results have a bounded candidate pool and retrieval diagnostics; inspect actual enabled/coverage/error state. |
| `team_drive_inspect` | `file_id`, `limit?` (default 20), `cursor?` | Verified `source_version`, file warnings and paginated `structure` groups with zero-based `start`/`end` segment ordinals. |
| `team_drive_read` | `file_id`, `start?` (default 0), `limit?` (default 10), `expected_version?`, `cursor?`, `selector?` | `segments`, locators, version and warnings. Always pass the inspected version for evidence chains. A cursor supplies the next offset; selector filters supported source locations. |
| `team_drive_context` | `file_id`, **`expected_version`**, `ordinal`, `before?`/`after?` (default 2, range 0–20), `max_segments?` (default 30, range 1–100) | Version-checked bounded context around an actual segment ordinal. Preserves locators and original ordinals, with `context_reason`, `requested`, `returned`, `coverage`, `gaps` and `excluded_by_boundary`. Has no continuation cursor; use read for missing context. |
| `team_drive_plan` | `query`, `prefix?` | Optional deterministic heuristic `classification`, steps and guardrails. It does not search, read, establish approved sources or certify completeness; do not call it mechanically for every question. |
| `team_drive_diagnostics` | `limit?` (default 20) | Recent bounded operation timing/error records and p50/p95 aggregates; not answer accuracy or all-time totals. No query, source text or paths are recorded. |
| `team_drive_export` | `file_id`, **`expected_version`**, `format?` (`jsonl` only) | Whole supported extraction, `artifact_id` relative to state, runtime absolute `artifact_path`, version, count and format warnings. No arbitrary output path. |
| `team_drive_evidence_bundle` | `files` (1–50 objects containing `file_id`, `expected_version`), `scope_description?` (default empty string; supply a scope for this workflow) | `artifact_path` / `artifact_id` for the manifest, `files`, `observation_interval`, `all_selected_versions_exported`, `all_selected_text_complete`, and failed/exported counts. `ok: true` means the manifest was created, not that every input succeeded. |
| `team_drive_refresh` | `force?` (default false) | `job_id`. Force permits retrying unchanged failed files; ordinary scans avoid repeated expensive failure. |
| `team_drive_job_status` | `job_id?` | Latest job if omitted; job state/progress and coverage. |
| `team_drive_cancel` | `job_id` | Requests cancellation at the next file boundary; current parsing is not immediately killed. |
| `team_drive_wiki_status` | `limit?` (default 20) | Page counts and bounded queue of pages requiring upkeep. |
| `team_drive_wiki_search` | `query`, `limit?` (default 10), `after?` | Verified Wiki candidates; stale page content is not returned. |
| `team_drive_wiki_read` | `page_id` | Derived page after current source-version and exact-quote checks; still requires original evidence for factual answers. |
| `team_drive_wiki_write` | `page_id`, `kind`, `title`, `body`, `citations`, `expected_revision?` (default 0), `links?` | Store a derived page and source dependencies with revision checks. See [Wiki operations](wiki.md) for citation schema and limits. |
| `team_drive_wiki_refresh` | `limit?` (default 20), `after?` | Recheck a bounded batch and queue affected pages; does not invoke an LLM to rebuild them. |

All page `limit` values must be 1–100. `query` accepts 1–500 characters. `prefix` is relative to the configured local root or cloud folder, not a sharing URL, local anchor path or shell glob. Never invent a `file_id`; obtain it from list/search.

## Cloud provenance and access

For `microsoft_graph`, the local `root` is an empty cloud-anchor used for state guards. Cloud tenant/application/drive/folder and authenticated account binding identify the source. MCP remains local stdio; setting the provider does not create a remote MCP server or grant Microsoft permissions.

`file.origin` preserves `provider`, `drive_id`, `folder_id`, `item_id`, safe `web_url` when available, `etag`, optional `ctag`, remote modification and verification times. After verified content extraction it also carries `content_sha256`. `source_version` binds cloud identity, remote version and downloaded content hash; it is not the content-only hash. Keep the whole returned provenance in manifests. In answers prefer the returned web_url plus actual locator and source_version; a missing URL is not a reason to guess one. Temporary download URLs and authentication material are not citation links.

Every returned list/search page rechecks the current metadata access and etag for each candidate, including cached ranked pages. On success `cloud_access_checked` describes that bounded check; `cloud_access_scope` states its limits. A failed candidate check fails the call instead of returning a stale page. Checks are sequential, not an atomic permission snapshot, and do not replace content verification by inspect/read/context/export. An empty successful search still does not establish business absence.

Graph inventory is paged and bounded; unsuccessful traversal does not publish a complete empty manifest. Unsupported shortcuts/packages, source changes, revoked permissions and limits require their reported handling. Network calls, revalidation and repeated content downloads add cost even with a local index. Missing auth requires explicit user login as described in [requirements](requirements.md), not collection of credentials in chat. No real-tenant permissions or latency guarantee follows from mock tests.

## Optional retrieval and context

Keyword search uses configured aliases with exact token matching and BM25 ranking. Semantic/hybrid modes require checking `retrieval.semantic` for enabled state, indexed/eligible segments, truncation and errors. `rerank: true` requests the separately configured reranker; it is false by default even when a reranker is configured. A disabled or failed semantic/rerank service is not evidence that a business fact is absent. Read the actual response's mode, fallback and scope rather than assuming a requested mode succeeded. Real-model accuracy and latency have not been established by the implementation's synthetic tests.

Model-assisted search freezes a bounded result set for session pagination. Its `total_matching_files` counts that candidate pool only, not every matching source. Preserve query, prefix, mode and rerank across cursor pages; stale/expired snapshots require a new search. Neither keyword nor semantic relevance establishes a complete business population.

`context` checks the source digest before and after selection. Its segment ordinals can be non-contiguous: the requested hit has priority, followed by extracted heading and first-row candidates, neighboring segments and nearby note/unit candidates. A worksheet/table's first extracted row is only a candidate header; it can be a title, data row or part of a split row. Notes and units are searched only in a bounded nearby extraction window. Page, slide, sheet and table boundaries can exclude relevant continuations or later-page footnotes. Inspect the returned gaps and use read/inspect for any missing qualification.

Context has both segment and serialized-byte limits; fewer than `max_segments` can be returned. `coverage.requested_neighborhood_complete` concerns only requested ordinal neighbors, and `all_identified_candidates_returned` only the candidates found by this heuristic. Neither field certifies full document, table, visual or business coverage. File-level extraction gaps remain in `content_coverage` and `format_warnings`.

`selector` is an optional nonempty object with these format-specific combinations:

- CSV/TSV: `row_start?`, `row_end?` (1-based inclusive record bounds).
- XLSX: required `sheet` and optional `row_start?`, `row_end?` (1-based inclusive sheet row bounds).
- PDF: `page` (1-based).
- PPTX: `slide` (1-based).
- Other formats: no selector; navigate through inspect groups and original segment offsets.

Do not mix these combinations. Unknown fields, reversed ranges and incompatible format selectors are rejected. There is no A1 column clipping. With a selector, `start` counts matching segments from zero; without one it counts all source segments from zero. Returned segment `ordinal` remains the original file ordinal. Inspect text `line_bucket` boundaries are navigation ranges only; use actual read locators for line citations.

`artifact_path` is intentionally an absolute path returned at runtime for local analysis tools. It is not public-safe metadata: do not publish that path, unredacted tool traces, or private exported content. Export always includes the whole supported extraction; it does not inherit read selectors.

## Coverage fields

`coverage.inventory_complete` concerns the last inventory traversal. `present_files` counts non-deleted entries; `indexed_files` counts `ready` files; `states` reports all observed states; `all_present_files_indexed` requires completed inventory and every present file ready. `partial_files` and `all_present_files_extracted_completely` distinguish ready partial extractions from complete text coverage. `business_completeness` remains `not_established` because a parser cannot establish business authority or missing out-of-root sources.

For resumable Windows share scans, `scan_resume` distinguishes the ongoing cycle and its collection/verification progress. Verified published subsets may be searchable while `inventory_complete` is false. This cannot establish a complete population, justify absence claims or mark unseen records deleted. Resume uses ordinary refresh/polling; force starts a new scan cycle. Partial publication and resumability do not weaken the current candidate, source or content checks.

File states include `ready`, `pending`, `failed`, `blocked`, `unsupported`, `oversized`, `online_only`, `stale` and `deleted`. The local provider's `online_only` state reflects a detected operating-system condition, not Microsoft Graph availability. Inspect/read/context/export require a ready file and validate whole content; Graph also verifies remote identity/version around downloads. Ready PDF files may still have `extraction_complete: false`; read `content_coverage.missing_text_pages` and other gaps.

`extraction_complete: true` means extraction finished within supported format scope; it is false for explicit missing-text PDF pages. `content_coverage` gives total/pages-with-text/missing-text pages and `ocr_performed: false`; blank and image-only pages are not automatically distinguished. Read `format_warnings` and segment warnings anyway. `truncated: true` mirrors pagination (`has_more`) in this release: follow `next_cursor` to continue. A final page does not establish complete business coverage.

`total_segments` counts all currently extracted file segments, not proof of extraction completeness, while `total_matching_segments` counts the selector's matches. `selection_scope` distinguishes filtered segments from whole-file segments. `selection_complete_in_response` means this single response contains the whole selected set; later pages can complete the accumulated set without that field becoming true. `whole_file_in_response` is always false for filtered reads. A zero-match selector returns an empty result with its source version; do not infer a numeric zero from absence of matching cells.

List/search cursors bind method, query, index generation and server session. Inspect/read cursors bind file version and server session; read also binds the selector. Use a cursor only with its original method and parameters; after `STALE_CURSOR` or `INVALID_CURSOR`, restart the affected enumeration/read.

## Resource limits

Default source limit: 256 MiB; uncompressed/output budget: 512 MiB; parser time: 30 seconds per file; maximum 100,000 segments, 8,000 text characters per segment, 512 columns and 2,000 PDF pages. The protocol result budget is 64 KiB and a serialized source segment may not exceed 48 KiB. A limit can reject a file smaller than the byte ceiling if its structure is unusually expensive.

Linux parser workers additionally have an address-space limit. macOS and Windows use input, archive, output and time bounds without the same hard address-space guarantee. Do not claim uniform memory protection or measured capacity across platforms.

## Example call chain

For a clause lookup: status → search with a project term → inspect the selected `file_id` → read at the matching structure group's `start`, passing `expected_version` → inspect returned context/warnings → cite its locator. When available, context can first gather candidates around `match.ordinal` using the inspected version; read any gaps and necessary qualifications before answering. A path-only match has no usable content ordinal, so navigate using inspect instead.

For a full total: status/refresh → enumerate the defined scope → inspect relevant files → create an evidence bundle (or individual versioned exports on older servers) → check manifest/gaps, counts, definitions, currency and duplicates → compute deterministically. The tool chain does not supply missing business definitions automatically.

## Local diagnostic client

The complete ZIP uses the Python commands in [cli.md](cli.md), with `df-init.py probe/profiles` for setup discovery and `df-tool.py status/diagnostics` for a configured reader. Live status may start indexing. The ZIP does not include the following Node commands.

Only in the full source checkout, offline setup inspection uses `node scripts/doctor.mjs --config '<config.json>'`; it starts no worker and scans no source files. It cannot prove cloud authentication or reachability. For live MCP tools use `node scripts/call-tool.mjs tools --config '<config.json>'` or a named tool plus JSON arguments. `--demo` explicitly selects the simulated library. This Node CLI opens a client session to the managed local service, closes that session on exit, and never implicitly selects a source. Status does not wait for a full index but a cloud status can require an online source check; ordinary reads wait up to 60 seconds for initial indexing. A refresh call waits for its job. It does not automatically inspect the configuration of an existing Codex chat.

A multi-step workflow with cursors belongs in a persistent MCP session, because cursors are session-bound. Large libraries should use a persistent client rather than repeatedly starting the diagnostic CLI or treating its 60-second startup timeout as an indexing success.

## Evidence bundle

Provide 1–50 unique inspected file IDs with expected versions. The manifest records the stated scope, source identity, per-file export results, versions and extraction gaps across an observation interval. `files[].status` is `exported` or `failed`; check both `all_selected_versions_exported` and `all_selected_text_complete` before complete calculations. A partial PDF can export successfully without complete text coverage. The manifest can contain private source paths; keep it local. On older connected versions without this tool, use individual exports and retain equivalent provenance.
