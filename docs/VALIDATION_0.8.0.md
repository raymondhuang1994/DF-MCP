# DF MCP 0.8.0 validation

This candidate separates the evidence service from host routing and business review. Local checks used Windows and Python 3.12 with synthetic sources. The original 0.7 resumable scan and Wiki implementation is retained. No production skill installation or N-drive scan was performed for this candidate.

| Check | Observed result |
|---|---|
| Engine, recovery, resumable scans, CLI, broker and service configuration | 109 distinct tests: 104 passed, 5 conditional skips; the shutdown-reopen fixture was verified separately after waiting for the old worker to finish |
| Long-path extraction and protected private I/O suites | 90 tests: 82 passed, 8 platform skips |
| Source catalog and optional evidence envelope | 14 passed, including a real profile-to-broker CLI read chain and preservation of failed readiness observations |
| Acceptance, cloud engine, contextual evidence, retrieval, scan progress, evaluation and upgrade | 90 distinct tests passed with no skips, combining completed groups and isolated failed-case reruns |
| Latest CLI readiness change | 7 CLI tests passed; repeated tests are not added to a combined total |
| Wiki | 22 tests: 21 passed, 1 symlink-privilege skip |
| JavaScript installer, doctor, cloud configuration and backend | 64 tests: 61 passed, 3 conditional skips |
| Native MCP and synthetic Microsoft Graph MCP | Passed; 18 tools, no real Microsoft tenant |
| Concurrent clients and recovery | Passed; six clients, one persistent index, crash recovery and unchanged parsed-row reuse |
| Fresh synthetic installation and upgrade | Passed; private state, Unicode paths, two clients and idle-restart cache reuse |
| Self-contained Skill archive | Passed with system site-packages disabled; package/runtime version equality, shared broker and external index reuse checked |
| Fixed synthetic retrieval/evidence evaluation | 14 of 14 passed; no LLM answer-accuracy measurement |
| TypeScript, Skill metadata and public content | Build and validators passed; source scan excludes credentials, private documents and runtime state |

The package test retains its same-instance assertion and allows a 15-second idle lease because cold Python startup on Windows can exceed one second. Its broker cleanup remains bounded. The CI job allowance is 30 minutes; the recovery/resumable group alone took about seven minutes locally, and per-test limits remain in place.

Directory checkpoints cannot resume inside one very large or slow directory. `indexed_files` retains its compatible cached-candidate meaning; use `current_indexed_files`, `pending_reparse` and `extraction_current` for current evidence. Parser-dependency or segment-persistence changes require maintainers to advance `EXTRACTION_CACHE_SCHEMA`.

These results do not establish complete N-drive coverage, Microsoft production authentication, company ACL behavior or real business-answer accuracy. The candidate's six-platform/Python CI matrix is assessed from the PR's actual runs, not from historical releases.

The legacy model-timeout fixture now measures the real HTTP call rather than the entire file scan. It retains its two-second ceiling, requires an observed call and keeps all fallback/recovery/cache-reuse assertions. An isolated diagnostic measured a 0.235-second HTTP budget failure while full scan time varied from 1.359 to 3.109 seconds. A prior evaluation resource-exhaustion error did not recur in the isolated rerun.
