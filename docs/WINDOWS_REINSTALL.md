# Windows：从临时补丁重新安装 df-mcp

适用于已用过 `df-mcp-instance-launcher.cjs`、手动修改过 `dist/index.js`，准备改用 GitHub 正式实现的电脑。这里只重装 **df-mcp 服务与 Skill**，不需要卸载 Codex，也不要删除整个 `.codex`、聊天记录或源资料。新版支持多个聊天共享一个索引；单纯删除旧程序目录或重装 Skill 不会清除 Codex 中的旧启动配置。

以下使用已获准的 `C:\MCP TEST`。其他使用者应换成自己明确批准的来源目录。整个流程不向源目录写文件、不修改其权限。Windows 实机验收由安装后的实际调用确认，不能仅凭安装命令成功或公共 CI 通过判断。

## 1. 保存旧设置，关闭 Codex

记录旧 df-mcp 程序位置、JSON 配置和状态目录。备份实际 Codex `config.toml`、旧 launcher、修改过的 `dist/index.js`、已有备份与 JSON 配置到独立的私有目录；这些备份不要上传 GitHub。Codex 配置通常在 `%USERPROFILE%\.codex`，设置了 `CODEX_HOME` 时以该目录为准。详细核对项目见[迁移说明](WINDOWS_MIGRATION.md#先检查当前临时修复)。

完全退出 Codex。在任务管理器按命令行路径确认旧 df-mcp 的 Node/Python 已退出；不要批量结束所有 Node/Python 进程。若无法确认旧残留进程，保存其他工作后重启 Windows，并在完成第 4 步前保持 Codex 关闭。不要删除锁文件来解除占用。

## 2. 在新目录安装正式代码

准备 Node.js 22+、Python 3.9+ 和 Git。打开一个新的 PowerShell 窗口执行以下命令。命令使用 `npm.cmd`，避免 PowerShell 把 `npm` 解析为受执行策略限制的 `npm.ps1`；无需为此放宽系统执行策略。程序目录、状态目录和来源互不包含；这里选用用户本地磁盘中的固定路径，不按每次连接创建目录。

```powershell
$ErrorActionPreference = 'Stop'
$install = Join-Path $env:LOCALAPPDATA 'df-mcp-program\0.3.0'
if (Test-Path -LiteralPath $install) { throw '安装目录已存在；先检查它，不要覆盖或自动删除。若它是本次已下载的新副本，可进入该目录继续未完成步骤。' }
New-Item -ItemType Directory -Path (Split-Path -Parent $install) -Force | Out-Null
git clone https://github.com/raymondhuang1994/team-drive-mcp.git $install
if ($LASTEXITCODE -ne 0) { throw '下载失败，停止安装。' }
Set-Location -LiteralPath $install

# 仅清除此 PowerShell 会话继承的旧解释器覆盖，不修改系统全局环境。
Remove-Item Env:DF_MCP_PYTHON -ErrorAction SilentlyContinue
Remove-Item Env:TEAM_DRIVE_PYTHON -ErrorAction SilentlyContinue
npm.cmd ci --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw 'Node 依赖安装失败，停止安装。' }
npm.cmd run setup -- --skip-config
if ($LASTEXITCODE -ne 0) { throw 'Python 环境或构建失败，停止安装。' }

$env:DF_MCP_PYTHON = Join-Path $install '.venv\Scripts\python.exe'
$stateBase = Join-Path $env:LOCALAPPDATA 'df-mcp-managed'
npm.cmd run configure -- --root 'C:\MCP TEST' --state-dir $stateBase --yes
if ($LASTEXITCODE -ne 0) { throw '配置失败，停止安装。' }
npm.cmd run install:skill -- --replace
if ($LASTEXITCODE -ne 0) { throw 'Skill 安装失败，按错误中的备份/恢复提示处理。' }
```

`--replace` 会先验证新 Skill，再备份已有 `df-mcp` / `team-drive-read` Skill；首次安装也可使用。源资料和旧状态不参与替换。记录 configure 输出的实际 `config.json` 路径及完整 MCP 注册命令。确认 JSON 的 `root` 仍是批准的来源，`state_dir` 位于 `%LOCALAPPDATA%\df-mcp-managed` 下，并且不在 `.codex`、自定义 `CODEX_HOME` 或来源目录内。不要复制旧索引数据库到新状态中。

## 3. 先验证新服务

仍在上述 PowerShell 窗口、新安装目录执行；将占位符换为 configure 刚打印的实际路径：

```powershell
$config = '<configure 输出的 config.json 绝对路径>'
npm.cmd run doctor -- --config $config
if ($LASTEXITCODE -ne 0) { throw '诊断失败；先处理错误，不替换 Codex 连接。' }
node scripts/call-tool.mjs team_drive_status --config $config
if ($LASTEXITCODE -ne 0) { throw '服务调用失败；先处理错误，不替换 Codex 连接。' }
```

确认实际 status 为 `ok: true`、`service_version` 为 `0.3.0`、`state_storage.mode` 为 `shared`。初次索引可能仍在进行，按返回的作业状态等待；`doctor` 是离线检查，其成功不代表服务已连通。此处运行状态检查会按配置建立派生索引，来源保持只读。

## 4. 清除旧注册，再配置唯一的新入口

保持 Codex 关闭，打开当前实际生效的 `config.toml`；第 1 步另存的备份副本保持原样。找到 `[mcp_servers.df_mcp]` 及它的子表（例如 `.env`）。把本服务的旧条目整体替换为新入口；先记录仍需保留的已批准超时设置，旧 launcher 的环境变量和参数不照搬。若同时存在旧 `[mcp_servers.team_drive]`，核对确实是同一 df-mcp 后移除该重复条目及其子表。不要动其他 MCP 服务或 Codex 设置。

按 configure 输出填入四个真实绝对路径，只保留一个 df-mcp 注册：

```toml
[mcp_servers.df_mcp]
command = '<configure 输出的 Node 可执行文件绝对路径>'
args = ['<新安装目录>\dist\index.js', '--config', '<新配置的 config.json 绝对路径>', '--python', '<新安装目录>\.venv\Scripts\python.exe']
```

新入口必须直接引用新安装的 `dist/index.js`，不能再经过 `.codex\df-mcp-instance-launcher.cjs`，也不能引用旧安装、旧 JSON 或每连接独立的临时目录。不要新增第二个同名 TOML 区块。已经熟悉 Codex CLI 的用户也可移除核实过的旧注册后使用 configure 打印的完整注册命令，完成后检查配置结果相同。

## 5. 重开并确认可用

1. 启动 Codex，开两条同时在线的新聊天。各自调用 `team_drive_status`：应为 `ok: true`、版本 `0.3.0`、`shared`、`index_copies: 1`，且两者 `instance_id` 相同。
2. 等待首次索引完成，两条聊天分别 list → inspect → read 来源中同一份已存在的小文件，正文、定位和来源版本应一致，无 `STATE_IN_USE` / `Transport closed`。
3. 正常使用 Codex 保存状态，确认无 `.codex-global-state.json` 的 `EBUSY`。不要直接修改真实全局状态文件作为测试。
4. 完全退出 Codex，等待后台按配置空闲退出（默认 60 秒），再重开两条聊天重复 status/read。新的后台实例 ID 可以改变；来源绑定和已提交索引应复用，不能随聊天数量复制索引。

若任一项失败，保留具体错误和本服务的启动配置用于排查，不以反复删锁、复制数据库或扩大来源权限绕过问题。更完整的文件哈希、维护与磁盘检查见[目标机验收](WINDOWS_MIGRATION.md#目标-windows-电脑验收)。

全部通过后再归档或移除**已核实不再使用**的旧 df-mcp 程序、launcher 和派生缓存。先确认旧目录内是否混放源资料、导出或证据包，并保留这些内容及必要的恢复备份。不要删除整个 `.codex`，不要删除 `C:\MCP TEST`，也不要把仍在使用的新状态目录当旧缓存清理。
