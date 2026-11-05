# Windows 临时修复迁移与并发验收

准备移除临时补丁后重新安装的用户，可先按[Windows 重新安装步骤](WINDOWS_REINSTALL.md)操作；本页提供详细迁移检查、恢复策略和实机验收。无需卸载 Codex 或删除整个 `.codex`。

此修复修改 MCP 服务的状态目录检查、worker 连接和共享索引管理。更新 Skill 不会改变 Windows 目录句柄或 SQLite 的行为。0.3 使用一个按需启动的本地 broker 管理索引，多个 stdio 桥接客户端通过 Windows named pipe 共享同一引擎；不安装系统服务，也不让多个 worker 直接写同一个数据库。

**验证边界：**开发机为 macOS，无法读取目标 Windows 电脑中的临时 launcher、Codex 配置、修改后的 `dist/index.js` 或旁边的备份。本说明是迁移和验收步骤，不表示这些 Windows 文件已经检查、替换或通过实测。用户报告的“两份独立 MCP 客户端状态正常、`.codex` 原子重命名成功”属于临时修复结果，不能替代下方实际 Codex 多聊天验收。

## 先检查当前临时修复

在目标 Windows 电脑记录当前安装目录、源目录、状态目录以及启动命令。源目录应保持用户已批准的 `C:\MCP TEST`，以下步骤不在其中创建、修改、重命名或删除文件，也不改变 ACL。

1. 读取实际 Codex 配置中的 `[mcp_servers.df_mcp]`，记录 `command`、`args` 和该项已有环境变量。通常配置位于 `%USERPROFILE%\.codex\config.toml`；设置了 `CODEX_HOME` 时以该路径为准。只检查本服务条目，不把其他条目或凭据发布到日志。
2. 检查 `.codex\df-mcp-instance-launcher.cjs`，以及配置实际引用的 launcher（如路径不同）。查看它怎样选择 `state_dir`、启动 Python/Node、退出和清理，以及是否按 PID、随机值或时间生成目录。列出实际产生的状态目录，但此时不删除。
3. 检查安装目录 `dist\index.js`、对应 `src\` 与 `python\team_drive\` 源码，并与旁边已有的修复前备份比较。记录已有备份的文件名、时间、SHA256 和所属版本。不要直接用来源不明的旧备份覆盖当前文件。
4. 确认当前配置所指的 JSON 配置和 `state_dir`。记录每个旧状态目录对应的源根目录；绝不能把源目录误认为可删除缓存。
5. 完成检查后，在获准的外部维护目录另存一份当前 launcher、`config.toml`、原 JSON 配置、`dist\index.js` 和相关已有备份。维护备份包含本机路径或环境配置，应保持私有。现有“修复前备份”与本次“替换前备份”分别保留。

可用 PowerShell 的 `Get-Content -LiteralPath`、`Get-FileHash -LiteralPath ... -Algorithm SHA256` 和 `Get-ChildItem -LiteralPath` 检查上述**明确路径**。不使用跨主目录的递归删除或全局搜索替换。

## 构建服务并生成外部配置

在源目录以外的受管理安装目录中更新到本次源码，再运行：

```powershell
npm ci --ignore-scripts
npm run setup -- --skip-config
npm test
npm run test:installer
npm run test:doctor
npm run test:backend
npm run test:mcp
npm run test:concurrency
npm run test:install
```

`setup` 从受管理源码构建 `dist/index.js`。不要把临时修改过的 `dist` 当作下一版的源码，也不要只重装 Skill。保留构建结果和运行版本记录；任一步失败先处理该失败，不开始替换生产连接。

生成获准源目录对应的配置。默认状态基目录是 `%LOCALAPPDATA%\df-mcp`，若该目录内已有不同配置，选择明确的新外部基目录，例如：

```powershell
$stateBase = Join-Path $env:LOCALAPPDATA 'df-mcp-managed'
npm run configure -- --root 'C:\MCP TEST' --state-dir $stateBase --yes
```

向导只保存配置，不启动索引。查看输出的完整 `config.json` 路径，确认 `root` 仍是 `C:\MCP TEST`，`state_dir` 位于外部基目录内。安装器和服务都拒绝任何 `.codex` 祖先目录、自定义 `CODEX_HOME` 及其物理别名；因此不能把状态放到 `.codex\cache` 或通过 junction 绕过检查。

可选 JSON 配置项 `"service": {"idle_seconds": 60, "max_clients": 64}` 控制最后一个客户端断开后的空闲退出时间和连接上限；分别允许 1–3600 秒、1–256 个客户端。所有 Codex 聊天应引用**同一个配置文件**，不为每个连接创建索引。向导按根目录/状态绑定复用 profile，原样保留合法自定义选项与键顺序；不覆盖不同来源。旧 `state_pool` 属于兼容模式，不是新 MCP 的并发上限。

## 替换启动入口

先退出全部使用旧连接的 Codex 聊天，并完全退出 Codex。确认属于本 df-mcp 安装的旧 Node/Python 进程已经退出；使用命令行路径核对身份，不要结束所有 Node/Python 进程。旧 worker 未退出时，改配置不会释放旧路径句柄或接管其 stdio 管道。

在备份已经完成的前提下，仅把实际 `[mcp_servers.df_mcp]` 的启动命令和参数改为普通 Node 入口与向导生成的配置。下面是结构示例，路径占位符必须换为刚核实的实际绝对路径：

```toml
[mcp_servers.df_mcp]
command = '<已核实的 Node 可执行文件绝对路径>'
args = ['<安装目录>\dist\index.js', '--config', '<外部配置目录>\config.json', '--python', '<安装目录>\.venv\Scripts\python.exe']
```

优先使用新版配置向导打印的完整命令，它包含绝对 Node 与专用 Python 路径，避免桌面 PATH 或旧 Python 环境变量选错解释器。保留其他 MCP 条目，以及本条目仍需要的已批准超时和环境配置；删除本条目中仅用于旧临时 launcher 的参数或变量前先核对其用途。新 `command` / `args` 不再经过 `.codex\df-mcp-instance-launcher.cjs`。保留该文件和备份用于追溯，完成验收后才按维护流程归档；它位于磁盘上但未被引用，不会启动旧索引。

重开 Codex，创建新聊天验证。已经报 `Transport closed` 的旧聊天可能仍持有失效 MCP 连接，需要关闭并新建聊天或重新连接。服务可在活动 Node 传输下按需恢复意外断开的共享服务连接；已被客户端关闭的 stdio 传输不能由修改配置或 Skill 复活。

回滚时也先关闭相关服务，再恢复**整套匹配的启动配置与程序版本**。不要只恢复旧 `dist`，却保留新配置或不兼容状态；不要恢复位于 `.codex` 内的旧状态布局而宣称问题已修复。源目录不属于回滚范围。

## 索引恢复和磁盘清理

共享状态位于 `state_dir\shared-v1\index`，控制文件位于 `shared-v1\control`。一个 broker 持有租约、调度扫描，所有聊天共享一份索引；各聊天的 cursor 隔离。最后一个客户端退出后服务默认空闲 60 秒退出，下次启动复用已提交索引。进程退出由操作系统释放租约，不靠删除 `.worker.lock` 解锁。

数据库使用磁盘工作副本和原子提交的快照，启动只恢复已提交版本。正常运行和提交阶段最多约 3 份完整数据库；损坏快照至多另保留一份隔离副本。空间随源资料规模变化，不随聊天数复制。共享引擎已结束任务历史最多 100 条，取得独占租约时按受控名称清理解析临时文件。主动生成的导出和依据包需按明确策略保留。

维护命令默认只预览；先用实际配置路径设置 `$config`，查看结果后才加入 `--apply`：

```powershell
$config = '<向导输出的配置文件绝对路径>'
node scripts/maintain-state.mjs --config $config --prune-exports-days 30
node scripts/maintain-state.mjs --config $config --prune-exports-days 30 --apply
```

`--prune-exports-days 30` 仅选择超过 30 天、且未被依据包引用的独立受控导出。`bundles/<uuid>.json` 清单及其引用的导出始终保留，无自动 TTL；引用无法核验时以 `BUNDLE_REFERENCES_UNVERIFIED` 拒绝清理。维护器使用与共享服务相同的租约，跳过忙状态，拒绝链接和非预期对象，不递归删除未知目录。为完成维护，先关闭相关 MCP 连接并等待 broker 空闲退出。锁文件仍在磁盘上不代表服务仍活跃，不要手动删除锁文件判断或解除占用。

正常关闭后的已完成检查点会复用；进程中断后从上一已提交检查点恢复，当前未提交操作可能需要重做。不能将进程强杀测试外推为所有断电或磁盘故障已经验证。遇到 `STATE_REBUILD_REQUIRED` 或确认的数据库损坏，先停服务、预览，再重建受控共享索引：

```powershell
node scripts/maintain-state.mjs --config $config --reset-index
node scripts/maintain-state.mjs --config $config --reset-index --apply
```

下次连接重新扫描来源；重建的是派生索引，源文件不变。维护器覆盖受控共享索引及已有编号 1–32 的旧槽，不受当前旧槽配置上限影响；保留导出、依据包、未知文件、目录和锁文件。若获准来源目录被替换导致身份变化，只有明确指定 `--reset-index --accept-source-replacement` 才接受新身份；源缺失时需有效 profile 绑定才能做安全清理。旧缓存需要单独确认生命周期，不因升级而自动删除。

旧版本位于 `state_dir` 根层的数据库、临时 launcher 生成的目录和不再使用的 profiles 不自动迁移或删除。确认全部旧服务已停止、逐个核对配置与来源、保留所需导出后，按本机维护流程归档或移除这些明确的派生目录。新维护器不对旧未知目录做广泛递归清理。源根目录 `C:\MCP TEST` 永远排除在清理范围之外。

## 目标 Windows 电脑验收

以下测试必须在目标 Windows 的实际 Codex 上完成并保存结果。独立 SDK 客户端通过，只证明协议与服务链可用。

1. **冻结输入：**选取 `C:\MCP TEST` 中一份已存在、获准读取的小文本文件；记录相对路径、SHA256、修改时间和当前 ACL。不要为本次验收在该源目录新增或改写测试文件。
2. **两个同时在线的聊天：**打开聊天 A、B，让两条聊天保持连接。各自调用 `team_drive_status`，确认 `ok: true`、相同来源、`state_storage.mode` 为 shared、相同 broker 实例/索引且会话游标互不混用。初次索引完成后，各自 list/inspect/read 同一文件，核对正文、来源版本与定位一致，且无 `STATE_IN_USE`。
3. **Codex 配置原子替换：**两条连接仍在线时，让 Codex 正常更新自身状态，观察是否还有 `.codex-global-state.json` 的 `EBUSY`。如需直接文件系统探测，只在 `.codex` 创建、替换并移除专门的临时探测文件；不要直接重命名或覆盖真实全局状态文件。记录文件系统和具体结果。
4. **复用与重启：**记录共享实例、来源身份和索引目录状态。关闭两条聊天、完全退出 Codex，确认相关服务退出，再重开 Codex 新建 A、B。重复 status/read，确认仍正常、既有索引可复用；反复开关聊天不会按连接次数生成重复索引。
5. **容量与异常恢复：**在独立测试配置中达到客户端上限，应得到明确可重试的容量错误；释放连接后可用。单独验证共享 broker 意外退出后的恢复与最近已提交检查点，保持源文件只读。不要用结束 Codex 或破坏真实数据库的方式做崩溃测试。
6. **清理：**停服务后先运行维护预览，再按已确认保留期应用，检查忙服务状态/未知对象不会被删。记录前后磁盘占用，重开两个聊天再次读取。若要测试损坏重建，使用另一个外部派生状态目录，避免破坏当前可用索引。
7. **源只读复核：**比较初始与最终源文件 SHA256、修改时间、路径和 ACL，确认无本服务导致的改变。记录 Windows、Codex、Node、Python 与服务版本，以及尚未执行或未通过的项。

只有以上实际多聊天、重启、文件锁和维护检查全部通过，才能把目标 Windows 环境标为验收完成。执行前保持“待验收”，不要把 macOS 单机或独立 MCP 客户端测试外推为 Windows Codex 已修复。
