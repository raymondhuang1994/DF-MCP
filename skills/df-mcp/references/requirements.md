# Operating requirements

## Connected service

The complete 0.7 Skill ZIP runs its Python helpers with Python 3.9+ and bundled parser dependencies. The original stdio MCP entrypoint additionally uses Node.js 22+. Local/Graph sources run on Windows, macOS or Linux; windows_share requires Windows. See onboarding.md and cli.md for the two-choice setup and script interface. Source selection belongs to explicit configuration; no MCP tool can add or change a local root or cloud drive/folder. Use `team_drive_status` and the declared schemas to identify the connected release/capabilities, provider, source identity, health, current job and storage mode. Do not infer a connection or cloud authorization from the presence of this Skill.

Releases 0.3–0.7 use lightweight stdio bridges to one managed local broker for the configured source/state. Chats share one index and scan scheduler, with separate session cursors. The broker starts on demand and exits after its last client has disconnected for the configured idle interval (default 60 seconds). Local Unix sockets or Windows named pipes are used; this is not a remote HTTP or permanent operating-system service. Do not create a new state directory for every chat. Old 0.2 exclusive workers and 0.2.1 isolated pools have different behavior: diagnose the actual version before recommending recovery. A newer Skill does not upgrade an existing connection; discover tools and status capabilities before using optional additions or the 0.5 cloud provider.

Default service settings are `service.idle_seconds: 60` and `service.max_clients: 64`; these are deliberate administrator configuration, not limits to raise automatically. State is outside every `.codex` directory and `CODEX_HOME`, including physical aliases. Old root-level/pool caches are preserved until explicit maintenance; changing source uses a separate confirmed profile, not an in-place index rebind.

## Source and local outputs

For `local`, root is this service's file boundary. Sources are opened for reading, and source traversal/links are guarded. The local provider uses ordinary fixed local Windows disks and rejects network/removable/device paths and reparse links. Use the explicit windows_share provider for an approved UNC/mapped network source; do not loosen the local provider or state-directory checks. Windows handles can prevent source-directory renaming while open; stop the service before reorganizing it. The boundary does not remove write access from unrelated host tools.

Indexes, generated Wiki pages, embedding caches, snapshots, exports and evidence manifests are source-derived private data in separate local state. Keep calculations and reports in separate approved output locations. Do not change source permissions, force cloud hydration or unlock another application's files. Recognized offline/recall files are excluded by the local provider; Windows cloud reparse files can be excluded even when locally downloaded. An approved ordinary local copy or separately configured Graph source is an explicit alternative, not an automatic fallback.

The service scans at startup and polls while alive (local default 30 seconds; cloud configuration wizard default 300 seconds). A completed earlier inventory does not mean a new refresh has finished: inspect `current_job_id` and call `team_drive_job_status`. Refresh after a known change when needed. Use `force: true` after fixing an unchanged parse failure; it is not a general remedy for a deterministic limit.

## Windows network share source

Version 0.6 adds explicit windows_share access through the current Windows identity. A mapping must resolve to a verified shared path. Indexes stay on fixed local storage. Scope, current file identity/access and content versions are checked; a failed scan is not a deletion. Windows network I/O can wait for the operating system and has no promised hard application timeout. DFS identity/referral changes require review, not a silent new root. Corporate DFS/SMB and VPN behavior still need real-machine acceptance. No username/password is collected or a new network mapping created.

## Microsoft Graph source

Version 0.5 implements the `microsoft_graph` provider for an explicitly selected OneDrive/SharePoint drive/folder. The local `root` is a dedicated empty cloud-anchor that preserves local state guards; it is not a document folder or a mirror. Use cloud tenant/application/drive/folder identity, file provenance and the logged-in account binding. State and credentials are separate private local paths. Do not put documents into the anchor, use a sync folder as a substitute or share auth caches between users.

Missing or expired authorization requires an explicit user login. The complete ZIP uses `df-init.py cloud-login` with the IT-approved account settings described in onboarding.md; the original repository also supports `node scripts/login-cloud.mjs --config '<cloud-config.json>'`. Complete Microsoft's device verification flow using the selected account. Do not collect passwords, device codes, access tokens or refresh tokens in chat. Do not automatically register an Entra application, consent to broader permissions or modify the user's source. If the cloud profile has not been configured, explain the required approved IDs and configuration entrypoint; source/authentication setup is not part of an ordinary evidence question.

Default delegated permission is `Files.Read`. SharePoint team content may require IT-approved explicit `Files.Read.All` or `Sites.Read.All`; never silently broaden permissions. The configured folder limits this program, not necessarily the token's entire authorization scope. Real-tenant policies, permitted files and revocation behavior have not been proven by mock tests.

The normal OAuth cache preserves a stable account identity across refreshes. External `graph.token_env` is an advanced alternative: token fingerprints bind the state, and token changes during a run are rejected. Rotation needs a deliberately separate profile/state; do not bypass the mismatch by deleting identity records or reusing another account's cache. Prefer the normal OAuth flow for ongoing use.

Cloud list/search rechecks each returned candidate's current metadata access and etag before exposing its cached information. Calls can fail on revoked access, changed versions, throttling or a page budget. This is sequential request-scoped verification, not an atomic or permanent permission snapshot. Inspect/read/context/export additionally verify remote content; do not continue from an earlier response after a failed check. Cloud access requires network, and content validation may download a file again.

## Optional local model retrieval

Semantic embeddings and reranking are disabled by default. Enabling them requires administrator configuration of a specific model and local HTTP(S) endpoint. Accepted endpoints use localhost or literal loopback/private addresses; arbitrary hostnames, public addresses, embedded credentials, redirects and ambient HTTP proxies are rejected. This is a network call to the configured service, not proof that the model process runs on the same computer. Configure only a service approved to receive the selected queries and source text.

Semantic indexing is bounded and incremental. A failed or partial model index does not turn readable source files into complete semantic coverage; inspect status and per-search diagnostics. Model identity/revision and extraction text/version bind the persistent embedding cache. Available code and synthetic tests do not establish quality, hardware capacity or latency for a real model and team corpus. Validate those with representative questions before treating semantic/rerank results as a quality improvement.

Retrieval model configuration is separate from the agent host's answer model: even local parsing and local retrieval do not guarantee local answer inference. The Skill does not change either configuration or grant transmission permission.

## Diagnostics and maintenance

On 0.4 and newer services, `team_drive_diagnostics` reports bounded operation samples and timing aggregates. Diagnostics are enabled by default and retain at most 1,000 events for seven days; administrator settings can disable collection or change bounded retention. Records contain operation names, timestamps, elapsed time, success and sanitized error codes, never query text, source contents or paths. Retention-limited samples are not all-time usage or answer-accuracy measures. Check `write_failures`; diagnostic write failure does not replace the evidence result. Older services without this tool can use their existing status/doctor workflow.

Only the full source checkout provides `node scripts/doctor.mjs --config '<config.json>'`, which reads setup metadata without starting a worker, scanning sources or writing state. For the complete ZIP, use the setup checks in onboarding.md and the Python status/diagnostics commands in cli.md; live status may start indexing. These checks report local paths; redact them before sharing. Offline success does not prove live source access or lock health. The repository's MCP diagnostic CLI requires explicit `--config` or `--demo`; demo means simulated data, not the user's configured corpus.

Use maintenance only for the stated operational need and authorized scope. Preview first; apply deletes only explicitly selected derived files after lease checks. Export-age cleanup preserves bundle manifests and all exports they reference; unverified bundle references fail closed. Index reset preserves evidence artifacts. Source-identity replacement requires an explicit configuration/maintenance decision, never an automatic retry flag. Never delete lock files to evict a live service. Rebuild from sources only when the reported recovery action calls for it. A closed old MCP transport needs a fresh client connection; a configuration edit cannot revive that pipe.

## Scope and limits

An approved source registry and metric dictionary help establish project/period/status, currency and deduplication. The reader does not automatically decide business authority. Use explicit user scope; ask only when unresolved plausible definitions materially change the answer.

No remote HTTP MCP service, NFS adapter, shared-drive synchronization, Teams chat connector, employee permission synchronization, OCR, image/layout understanding, macros, workbook recalculation, arbitrary SQL/code execution or guaranteed atomic cross-file snapshot. Graph file access is implemented separately from Teams chat or organization-wide discovery. Runtime limits and format warnings take precedence over prose examples. macOS/Windows have input/output/time bounds, not Linux's additional parser address-space limit.
