# Windows 网络盘升级后的正式验收

仅在用户要求升级后验收、或处理“上级目录只能读属性，资料根可以读取”的初始化故障时使用。本流程不要求普通资料查询重新安装或重新配置。首次来源选择仍使用 onboarding.md 的 A/B 两项；用户已经给出明确路径时直接使用该路径。

## 1. 核对正式安装与执行位置

操作必须由公司 Windows 电脑上当前用户的获准本地执行环境完成。能在该电脑的资源管理器打开共享目录，不代表当前脚本进程就在该电脑或使用同一网络身份。先核对运行位置及当前安装目录，不使用临时诊断进程中的补丁作为正式结果。

按客户端支持的更新方式备份完整旧安装到 Skill 发现目录之外，关闭使用该 Skill/MCP 的相关旧会话并让相应 broker 正常退出。不要广泛终止其他 Python 进程，也不删除活跃锁文件。用发布的完整包替换安装，保留独立的既有配置/索引；手动安装时先在专用新目录解压并核验，避免新旧文件混装。必要时由客户端/IT 完成安装目录的替换，不自动修改客户端配置。

ZIP 根目录必须直接包含唯一 SKILL.md，以及 scripts/、runtime/ 等同级资源。核对发布记录中的 ZIP SHA-256；解压后依据 `PACKAGE_MANIFEST.json` 的 `files` 逐项核对相对路径、大小和 SHA-256。核验脚本只读取安装文件，不访问资料或创建索引。文件缺失或不匹配时重新取得完整包，不以清单中的版本号代替完整性检查。

下列命令在正式 Skill 根目录执行，使用公司批准的 Python 3.9+。先只读核对加载的位置与版本：

```text
python -B -c "import sys;sys.path.insert(0,'scripts');from _df_runtime import bootstrap;bootstrap();import team_drive;print(team_drive.__version__);print(team_drive.__file__)"
python scripts/df-init.py profiles
```

运行时应为 `0.7.0`，模块路径应落在当前正式安装的 `runtime/python/team_drive` 中。SkillHub 的显示版本（例如 `0.2.0`）独立管理，不能用它判断内置程序版本。已有配置使用自定义状态基目录时，`profiles` 需传原来的 `--state-dir`；不要扫描整台电脑寻找配置。

## 2. 正式计划：先检查来源和本地索引位置

把以下占位路径替换为本次用户明确提供的目录或盘符，不能猜测 UNC 的剩余部分，也不重新创建网络映射：

```text
python scripts/df-init.py classify --source <用户提供的网络目录>
python scripts/df-init.py plan --choice team --source <用户提供的网络目录>
```

Windows 默认状态基目录为 `%LOCALAPPDATA%\df-mcp`。如用户明确指定其他固定本地目录，追加 `--state-dir <该本地基目录>`。以返回的 `config.state_dir` 为最终索引位置，不将数据库放在资料盘、UNC、同步盘、安装目录或任何 `.codex` 目录中。

保存完整成功的 plan JSON 到获准的本地工作区，保持 UTF-8，不保存在资料源，也不手工拼改 plan/config 字段。将以下内容放入一张配置确认卡：

- provider 为 `windows_share`；输入路径及实际 `resolved_root` 与用户指定的资料一致。
- 本次包含资料根内的子文件夹，不扩大到上级组织目录或其他团队资料。
- 实际固定本地索引完整位置、配置位置，以及源资料只读。
- 确认后将保存配置并扫描当前范围。

此阶段 `index_created` 应为 false。plan 失败时记录结构化错误码及下一步，不改变共享盘权限，不使用临时 monkey patch，不跳过 SMB、身份、DFS 目标或重解析点检查。

## 3. 按确认的计划初始化并检查索引

只有这份实际配置已经获得用户确认，才执行下列 apply；已有完全一致的明确确认可复用，不反复询问。升级代码获准不等于任意来源/索引位置均已获准。

```text
python scripts/df-init.py apply --plan <保存的完整plan.json> --confirmed
```

使用返回的实际 `config_path`，不根据哈希目录名称自行拼接。apply 成功表示配置保存，尚不能宣布索引完成。已有完全一致且获确认的配置直接复用，不重新 apply。

0.6.2 提供统一复测入口。首次连接会启动索引，因此仅在上面的实际配置已获确认后传 `--confirmed-config`：

```text
python scripts/df-check.py --config <实际config_path> --confirmed-config --wait-ready 30
```

如需比较同一关键词两次查询耗时，追加 `--query <用户提供的关键词>`，用可靠参数传递保留原文；关键词不进入报告。该入口保持同一连接，核对实际服务与包内版本，跟踪同一任务，扫描未完成或失败时直接报告进度及错误。扫描完成后，最多查 10 页文件清单并选择首个 ready 的支持格式文件，执行 inspect 和最多 3 个片段的同版本 read；有查询参数时随后执行两次 keyword 查询。它不安装、保存配置、强制刷新、取消任务或创建第二个索引。

stdout 是可回传的脱敏 JSON，可由本地 GPT 用 UTF-8 保存到获准的本地验收目录；不要存到资料源。报告包含计数、耗时、错误码和技术核对结果，不含公司身份、文件名、路径、正文、查询或原始堆栈。`ok:true` 仅表示这轮自动技术检查通过；`business_accuracy=requires_local_human_review` 表示正文与定位仍需第 4 节本地核对。空结果、空索引、不完整扫描或变化中的 generation 不用于宣称检索性能合格。工具查询计时不含 CLI 启动，连接和总耗时单独列出。

等待上限为 120 秒，在工具调用之间检查；不承诺硬取消正在阻塞的 Windows I/O。等待结束不等于取消索引。`index_not_ready` 可继续观察既有任务；`index_task_failed` 先处理所报原因，不反复重建。`sample_page_budget` 只表示样本发现预算耗尽，不能解释为资料源没有可读文件。完整复测也只验收当前配置的 provider，不证明其他网络盘或微软租户已验证。

需要进一步查看原始状态时使用：

```text
python scripts/df-tool.py status --config <返回的config_path> --wait-ready 30
python scripts/df-tool.py job_status --config <返回的config_path> --params-file <job.json>
```

`job.json` 为 `{"job_id":"<status实际返回的current_job_id>"}`。若没有 current_job_id，可不传 params-file 查询最新任务，并结合 status 的覆盖情况解释结果。`INDEX_STILL_RUNNING` 表示这次等待结束，继续检查既有任务；不创建第二个索引。任务 failed/cancelled 或来源不可访问时，保留失败状态，不能从旧缓存声称当前资料可读。

核对实际 `service_version` 为 `0.7.0`、provider 为 `windows_share`，来源绑定与确认卡一致。报告任务状态和 `coverage` 的 `inventory_complete`、`present_files`、`indexed_files`、`partial_files`、`states`。扫描阶段另看 job.scan_progress：已枚举或待发布数不等于正式索引文件数。失败时报告 error_detail 的阶段、操作和错误码；不要把服务健康当作索引成功。明确列出 failed/blocked/unsupported/online_only/oversized/pending 等实际缺口；需要具体文件时用下面的 list 返回其 `error`、`format_warnings` 和 `content_coverage`。未完成全量分页时不得声称已列尽所有失败文件。

## 4. 至少一个真实文件 inspect → read

在已确认范围中选择用户指定或普通可读的真实文件。文件 ID 只能来自本次工具结果；不使用虚构测试文件冒充公司资料验收。

```text
python scripts/df-tool.py list --config <config_path> --paginate
python scripts/df-tool.py inspect --config <config_path> --params-file <inspect.json> --paginate
python scripts/df-tool.py read --config <config_path> --params-file <read.json> --paginate
```

- `inspect.json`：`{"file_id":"<list实际返回的file_id>"}`。
- `read.json`：`{"file_id":"<同一个file_id>","expected_version":"<inspect实际返回的source_version>"}`；分页 inspect 的版本在各页 `file.source_version` 中。
- JSON 文件保存在获准本地工作区；使用结构化写入或可靠 UTF-8 写入保留路径和文本，避免将用户文本拼入 shell 命令。

read 必须成功返回该文件的实际 `segments`、`source_version`、`origin` 和片段 `locator`。确认版本与 inspect 一致；变更错误时重新 inspect/read，不混合新旧版本。至少展示一段有意义的内容及其定位。完整文档过大时可有界读取并标明范围，但不能把分页预算耗尽当作完整阅读。源文件不支持解析或无法读取时明确记录，选其他可读文件须仍在已批准范围内。

## 5. 记录结论和回退

验收报告分别记录：正式安装版本/包哈希、来源是否匹配、固定本地索引位置、plan/apply 结果、索引任务和覆盖缺口、真实文件 inspect/read 的同一版本及具体定位。正文只保留证明读取成功所需的最小片段；公司完整 UNC、个人路径和资料内容不粘贴到公开 GitHub 问题、PR 或通用安装包中。

可表述为“已完成正式初始化，并成功读取所选真实文件；全库仍有这些缺口”。仅 plan 成功时表述为“来源预检通过，尚未完成索引及内容读取验收”。开发机模拟测试、Windows CI 和公司实机结果分开记录。

需要回退时停止本次连接并恢复备份的完整旧安装，再重新打开客户端。保留独立状态，先核对兼容性；不删除数据库、凭据、来源身份绑定或锁文件来强迫旧程序连接。旧版可能仍有原来的祖先目录权限问题，回退成功不表示该问题已修复。
