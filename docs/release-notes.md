DF-MCP 0.8.0 separates reliable evidence retrieval from host routing and business review, retaining 0.7 resumable directory batches and version-verified local Wiki drafts.

Release packaging produces `DF-MCP-0.8.0.zip` and a byte-identical `.skill`, each with one root-level `SKILL.md`, Python helpers and pinned parser dependencies. A company-approved Python 3.9+ interpreter and local execution access are required. The package contains no credentials, source documents, Wiki content or index.

- Inspect saved sources offline with `df-sources.py`; explicitly select a profile and pin its configuration through broker startup. Selection never silently falls back to another library.
- Request an optional `df-evidence-envelope/1` CLI wrapper preserving native results, errors, locators, versions and unknown coverage. Default CLI and MCP responses stay compatible.
- Add bounded scan grace and preserve valid cache on transient source timeouts. Smaller shared-drive pages still fail when current access cannot be verified.
- Keep candidates during extraction upgrades, mark them pending reparse and refuse old extracted evidence until current extraction succeeds. Query-code or alias changes no longer clear extracted contents; affected Wiki dependencies become stale.
- Fix protected Windows long-path snapshot/parser operations, retain I/O error categories and accept empty XLSX shared-string cells while rejecting invalid nonempty references.

Existing source configuration and read-only access safeguards remain. Index and Wiki state stay in a separately configured local folder. Wiki drafts are navigation aids; citation validation does not certify the interpretation. The host agent checks original evidence for factual answers and recompiles affected pages from fresh sources.

Local validation uses synthetic fixtures on Windows; the configured CI matrix also targets macOS and Linux, with results dependent on the candidate commit's run. Production network-drive latency, company Microsoft sign-in and real-document acceptance still depend on the target environment. Directory checkpoints improve interruption recovery; they do not support resuming inside one very large or slow directory or promise a fixed scan duration or complete inventory while scopes remain pending.

For compatibility, `indexed_files` can include cached candidates awaiting a new extraction. Use `current_indexed_files`, `pending_reparse` and `extraction_current` to assess current evidence. Extractor code and limits automatically invalidate extraction; maintainers must advance `EXTRACTION_CACHE_SCHEMA` when parser dependencies or segment persistence semantics change.
