# Python tool entrypoint

Paths below are relative to the extracted Skill root. Use the approved Python 3.9+ interpreter. The ZIP vendors pinned pure-Python parser libraries; Node/npm is unnecessary for this entrypoint. Original stdio MCP continues to require its Node runtime.

```text
python scripts/df-tool.py status --config <saved-config.json>
python scripts/df-tool.py search --config <saved-config.json> --params-file <query.json> --paginate
python scripts/df-tool.py list --config <saved-config.json> --params-file <scope.json> --paginate
python scripts/df-tool.py inspect --config <saved-config.json> --params-file <file.json> --paginate
python scripts/df-tool.py read --config <saved-config.json> --params-file <versioned-read.json> --paginate
```

Supported methods include the original 13 engine methods: status, list, search, inspect, read, context, plan, diagnostics, export, evidence_bundle, refresh, job_status, cancel; version 0.7 adds wiki_status, wiki_search, wiki_read, wiki_write and wiki_refresh. See tools.md and [Wiki operations](wiki.md) for their parameters; omit the `team_drive_` prefix for CLI method names. Use JSON files to preserve literal paths, Unicode, quotes, Markdown and query text without shell interpolation. stdout is one JSON result, exit 0 for `ok:true`, otherwise nonzero. Do not hide error results.

## Pagination and scope

The CLI uses one local broker connection per invocation. A cursor is session-bound and cannot be copied into a later CLI invocation. `--paginate` consumes the pages on the same connection, returning `pages` and `pagination_complete`. It supports list/search/inspect/read. Default max-pages is 100, configurable up to 1000. Aggregate output is bounded at 8 MiB. An exhausted budget is explicitly partial; narrow the scope or use versioned export for large files. `pagination_complete:true` only means this query's pages were consumed, not that a business population is complete.

If a permission/version check fails mid-pagination, the CLI discards preceding pages from the response and returns failure. Restart after resolving the reported condition; do not stitch failed attempts together. Without `--paginate`, `has_more` may be true and the CLI removes the unusable next_cursor.

For file reads, pass the exact expected_version from inspect/read. Multiple list pages may race an indexing update: restart after INVALID_CURSOR, not with the old cursor. For statistics, explicitly define source/prefix, collect a full file inventory, inspect failures and use evidence bundles/exports.

## Index readiness

A first status starts the configured local broker and its initial scan. Distinguish connectivity, scanning, and usable coverage. Call status, then job_status for current_job_id. A bounded `--wait-ready 30` can wait for the initial job; a timeout is not a failed index and must not trigger a second index directory. Report unsupported/failed/partial files even when a scan finishes. Scanning polls only while the broker is alive; it is not an installed unattended background service.

Windows share scans can retain completed verified directory batches across jobs and restart. Ordinary refresh or periodic scanning resumes the active cycle after validation; `force: true` starts a new cycle. Inspect `scan_resume` and `coverage.inventory_complete`. Published partial files can support bounded lookups, but unfinished traversal cannot establish whole-source totals, deletion or absence. A single directory that exceeds its budget remains pending. No application timeout can forcibly interrupt an in-flight native SMB operation.

## Acceptance report for an already confirmed configuration

```text
python scripts/df-check.py --config <saved-config.json> --confirmed-config --wait-ready 30
```

Use this for an explicit acceptance or diagnostic request, not ordinary evidence questions. The flag means the actual source, subfolder scope and independent index location already have user confirmation. Connecting may start indexing; without this flag the helper fails before connecting. It does not install, apply, force refresh, cancel or alter the source.

The helper checks the connected version against its loaded runtime, polls the same job using job_status, and returns coverage plus bounded scan progress/error details. Failed or unfinished indexing prevents sample/read and search checks. When ready, it scans at most 10 list pages for one ready supported file, inspects it, and reads at most 3 segments with the inspected version. It reports version equality, nonempty text and locator presence, but those checks do not establish business correctness or locator accuracy; verify those locally with normal inspect/read tools. A discovery budget stop does not prove that no suitable files exist.

Optional `--query <keyword>` runs that same keyword twice with limit=10 and rerank=false, without including it in the report. Pass arguments safely rather than interpolating arbitrary text into a shell. Query timings cover tool round trips on one connection, excluding CLI startup; connection and total timings are separate. Empty/incomplete results or changed index generation are not representative performance evidence.

stdout is one allowlisted JSON report, with exit 0 only when automatic technical checks pass. It excludes file names, paths, identity, text, query and raw exception messages/tracebacks. Save it as UTF-8 in the approved local working area if requested. `business_accuracy=requires_local_human_review` remains true as a requirement even when technical checks pass. Wait-ready accepts 0–120 seconds and is checked between calls, not a hard cancellation deadline for native I/O. Only the current configured provider is tested.

## Persistence and host location

The broker reuses the same configured index across invocations/chats. Last-client idle shutdown keeps committed state. Other chats use independent session cursors. This is per-user local IPC, not a multi-user web server.

Run on the company computer or its approved local executor. Pure cloud execution has no implicit access to that computer. A remote existing MCP may be used through its native tools after source verification, but this ZIP does not deploy a remote HTTP MCP server or tunnel.
