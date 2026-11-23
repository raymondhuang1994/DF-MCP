# Source selection and evidence handoff

Read this when a router/advisor such as DF Agent selects among existing sources or hands evidence to an analysis tool. DF MCP owns source configuration, access checks, indexes, extraction and technical coverage. The host chooses the business question and scope; analysis tools calculate; the advisor checks whether evidence supports the answer. No layer infers business completeness from a successful read.

## Existing source catalog

`python scripts/df-sources.py list --state-dir <approved-state-base>` returns `df-source-catalog/1`. It only reads bounded, existing profile configurations. It does not connect to a source, read credentials, create an index or choose a default profile.

Each `profiles` item includes `profile_id`, `provider`, `source`, `config_path`, `state_path`, `config_fingerprint`, `catalog_source_id` and `source_availability: not_checked`. The catalog identity describes saved configuration; it is not the reader's observed source identity. Warnings or `truncated:true` prevent absence claims about profiles outside this inspection.

Select the actual source the user authorized, not whichever profile is first or most recently used:

```text
python scripts/df-sources.py select --state-dir <approved-state-base> --profile-id <returned-id> --config-fingerprint <returned-fingerprint>
python scripts/df-tool.py search --state-dir <approved-state-base> --profile-id <returned-id> --config-fingerprint <returned-fingerprint> --params-file <query.json> --paginate --evidence-envelope
```

`select` returns `selected` and still does not connect. A tool invocation can start the selected reader and its configured scan, so its source and independent runtime directory need prior authorization. The CLI resolves that exact profile again and pins the configuration through broker startup. `CONFIG_CHANGED`, an unknown profile or an incomplete catalog never falls back to another source. Existing explicit `--config` invocations remain supported; `--state-dir` only applies to `--profile-id`.

## Optional evidence envelope

`--evidence-envelope` adds a `df-evidence-envelope/1` wrapper to CLI output. Default CLI and native MCP responses remain compatible.

| Field | Meaning |
|---|---|
| `source` | Connected configuration ID, selected profile when available, provider and configured source/prefix. `source_id` only uses an identity reported by the reader; unknown values remain null. `catalog_source_id` is separately named. |
| `technical` | Reader-reported request success, selection pagination, extraction and inventory flags. Unobserved flags remain null. |
| `result` | Complete CLI result, preserving reader errors, versions, locators, warnings, retrieval fallback, coverage and pending reparse state. Pagination uses the CLI's existing pages/budget contract. |
| `business_completeness` | Always `not_established`; the wrapper cannot certify a business population or answer. |

A failed connection has unknown source fields and retains its original error. A completed page/selection is not proof that the entire file, folder or business population was consumed. For calculations, use complete version-pinned exports or evidence bundles and preserve their source list, units, dates and gaps. Preserve downstream `checks_status` and `needs_review` independently of this wrapper.

If readiness checking stops the requested operation, `result.readiness` contains the actual observation's `method` (`status` or `job_status`) and complete native `result`, including job identity, error detail and scan progress. The outer `method` still names the requested operation; it does not mean that operation ran. Inspect the native diagnostic as well as the CLI readiness error.

## Updating an index without misusing old extraction

Normal queries still revalidate access and versions. Query-code or alias changes do not by themselves erase extracted contents. An extraction implementation/schema or extraction-limit change marks old rows `pending_reparse`; candidates remain discoverable, but `extraction_current` is false and inspect/read/context/export reject them with `EXTRACTION_UPDATE_REQUIRED` until current extraction succeeds. Wiki dependencies become stale too.

`coverage.current_indexed_files` counts current readable extraction; `pending_reparse` is a separate gap. Do not use an old export to bypass a failed current read. Resumable directory batches publish verified progress, but pending/blocked directories, failed files, unsupported formats and partial extraction stay visible. Resuming is an explicit bounded operation on the same configured source, not permission to change scope or raise limits automatically.
