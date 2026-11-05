# DF-MCP

[![Windows, macOS and Linux validation](https://github.com/raymondhuang1994/DF-MCP/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/raymondhuang1994/DF-MCP/actions/workflows/ci.yml)

给 AI 使用的团队文件读取工具，支持普通本地目录、显式配置的 Windows 网络公盘，以及 OneDrive/SharePoint 云盘文件夹。本仓库为独立的 **DF-MCP** 项目，Skill 名称和工具前缀沿用 `df-mcp`、`team_drive_`。架构：**Skill + Python 工具入口 / 本机 MCP + SQLite 增量索引 + 本地 Wiki**。优先保证来源、版本和读取范围可核查，再优化定位速度。所有附带业务数据均为 **SIMULATED / 完全虚构**。

- [0.7 可恢复扫描、本地 Wiki 与原文核验](docs/UPGRADE_0_7.md)
- [Wiki 操作与证据约束](skills/df-mcp/references/wiki.md)
- [0.6.1 网络盘祖先目录权限修复与正式验收](docs/UPGRADE_0_6_1.md)
- [0.6.2 扫描进度、性能与本地状态路径修复](docs/UPGRADE_0_6_2.md)
- [0.6 完整 Skill ZIP、两选项初始化与网络公盘](docs/UPGRADE_0_6.md)
- [给同事的安装说明](skills/df-mcp/INSTALL.md)
- [0.5 OneDrive/SharePoint 配置、登录与验收](docs/CLOUD_DRIVE.md)
- [0.4 本地升级、兼容性与回退](docs/UPGRADE_0_4.md)
- [可选本地检索模型配置](docs/RETRIEVAL_CONFIGURATION.md)
- [完整中文开发方案](docs/TEAM_AGENT_DEVELOPMENT_PLAN.md)
- [Skill 操作规范](skills/df-mcp/SKILL.md)
- [验证记录与限制](docs/VALIDATION.md)
- [Windows 只读边界与可选隔离部署](docs/WINDOWS_READ_ONLY.md)
- [Windows 临时修复迁移与并发验收](docs/WINDOWS_MIGRATION.md)
- [Windows 旧临时补丁：重新安装步骤](docs/WINDOWS_REINSTALL.md)
- [10 个固定评估任务](tests/evaluation.xml)
- [OCR 可行性调查：本轮未实施](docs/OCR_FEASIBILITY.md)

**MCP 服务负责读什么、实际读到了什么以及如何验证；Skill 负责指导 Agent 如何使用这些证据。** 服务提供受限读取、索引、版本、定位和覆盖状态；Skill 指导候选发现、补读上下文、确定统计范围、引用和说明缺口。只更新 Skill 不会增加服务工具，也不会自动获得文件、模型或云盘权限。本文的 0.7 能力以实际连接的 `team_drive_status` 和工具列表为准；历史 CI 徽章及验证记录不能代替本次版本验收。

## 本次四项升级

| 模块 | 解决的问题 | 准确性边界 |
|---|---|---|
| 可恢复分批扫描 | 网络盘扫描中断后复用已验证进度，先使用已完成资料 | 未完成盘点明确保留缺口，不能推断未见文件已删除或不存在 |
| 固定本地 Wiki | 跨会话保存来源摘要、主题、FAQ、冲突和待确认项 | 原文件保留原位，Wiki 是可更新的派生资料 |
| Wiki 导航与原文核验 | 快速找到相关来源，再定位具体证据 | 回答使用文件版本、定位及当前权限校验，不把摘要当原始证据 |
| 按依赖增量更新 | 来源变化只使有关页面失效，列入重建队列 | 新摘要需从当前原文重新整理，不复用旧答案充当事实 |

Wiki 的内容整理由当前 Agent 完成；程序提供受控保存、来源验证和更新队列，不另装或默认调用云模型。首次读取、解析和整理仍需要时间，已有 Wiki 也不能省略最终原文核验。设计借鉴 [Karpathy 的 LLM Wiki 思路](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，具体实现保留 DF-MCP 的权限、版本和覆盖检查。

## 已实现的范围

- 完整 Skill ZIP：A 团队公盘/云盘、B 本地磁盘；支持回复字母或直接提供路径/微软云盘链接，先预览、确认后创建独立本地状态。
- Python CLI 与 MCP 共用核心，完整 ZIP 附带固定版本解析依赖（不附带 Python 解释器），无需 Node 即可使用脚本入口。
- 新增显式 windows_share 来源；使用当前 Windows 身份，索引仍在固定本地磁盘。公司真实 DFS/网络/VPN 场景需单独验收。

- 本机 stdio MCP，保留原有证据工具并增加 Wiki 工具；默认关键词模式不调用模型。local 来源不访问网络；microsoft_graph 来源需要访问微软授权与文件接口。
- 0.5 实现 Microsoft Graph 的 OneDrive/SharePoint 文件夹枚举、内容读取、显式设备登录和认证缓存。接口已实现，**尚未完成真实租户验收**；账号权限、企业策略和实际文件必须单独验证。
- SQLite 文件清单与关键词索引，支持中文短词、项目代号及配置别名；默认排序保留精确文件名与短语优先，再使用 BM25。
- `context` 按指定版本补读命中片段及附近候选标题、首行、单位和注释，保留原文位置、范围边界与缺口。
- `plan` 给出查找、版本比较、全量统计或澄清的规则式建议；分类不构成业务判断，统计仍须完整枚举。
- `diagnostics` 保存有界的操作耗时和错误代码，不记录问题、源正文或文件路径。
- 可显式配置本地 HTTP embedding 接口，将语义候选与 BM25 通过 RRF 融合；可另选本地 rerank。二者默认关闭，尚需真实模型与代表性资料验收。
- TXT、Markdown、CSV、TSV、XLSX、DOCX、PPTX、文字 PDF。
- 启动扫描、运行期间定期增量核对、删除失效、失败记录和强制重试。
- 分页、结构导航、原文定位、读取前 SHA256 版本校验、受控 JSONL 导出。
- 显式选择本地目录或云 drive/folder；源文件只读，索引、日志、临时解析文件及导出保存在独立本机状态目录。
- 多个 stdio 连接共享一个受管理的本地索引服务；状态目录禁止位于 `.codex` 或 `CODEX_HOME` 下。
- 授权根目录边界、符号链接/Windows junction 与 reparse 路径拒绝、压缩文件/XML 限制、超时、明确错误。

**运行平台：Windows、macOS / Linux，Python >=3.9；原 MCP 入口另需 Node.js >=22。windows_share 仅 Windows。** Windows 的本地来源和本机状态面向固定本地磁盘上的普通目录，支持中文及空格路径；local 来源仍拒绝 UNC/网络映射盘、可移动盘、设备路径、junction、符号链接及其他 reparse 路径；网络公盘使用单独的 windows_share 来源，不能靠放宽 local 限制接入。Graph 云盘来源通过 API 读取，不依赖 OneDrive 本地同步目录。目标为 Windows 10/11；自动测试的实际环境与结果见验证记录，不能据此宣称所有公司电脑已经验收。

**当前未实现：** NFS 连接器或镜像同步、远程 HTTP MCP 服务、多用户后台与员工权限同步、OCR、原始文件下载 MCP 工具、模型自我训练、无人值守云端部署。本版仍是同机 stdio 客户端共享服务；Graph 和可选模型的 HTTP 请求不等于对团队开放 HTTP MCP。没有真实团队资料，也没有公司 PPT 模板。

## 完整 Skill ZIP

同事安装时从 [Releases](https://github.com/raymondhuang1994/DF-MCP/releases/latest) 下载发布的 `.zip` 或 `.skill` 完整包，按同页 SHA-256 清单核对。**GitHub 的 Code → Download ZIP 是源码压缩包，不能直接作为公司 Hub 的 Skill 安装包上传。** 发布工作流仅在六组平台/Python 检查通过后生成安装包；没有发布成功的版本时，按下面的命令从源码构建。

在已安装固定依赖的开发环境执行 `python scripts/build-skill.py --output <新的ZIP路径>`。ZIP 根目录直接放一个 SKILL.md，scripts/、runtime/ 等与它同级，符合公司 Hub 的上传要求；不额外套 df-mcp/ 文件夹。包内包含中文引导、Python 核心/工具、解析依赖及完整性清单；不含账号凭证、原始资料、索引、生成的 Wiki 或解释器。同事将完整包安装到本地电脑 GPT 后说“初始化 DF 资料助手”。具体见 [0.7 说明](docs/UPGRADE_0_7.md)。

纯云端运行仍需要批准的本地执行连接或独立资料服务；此 ZIP 不会部署远程服务或自动连接公司电脑。程序确认资料/索引范围后才建索引；公司 OAuth 应用由 IT 配置，员工本人登录。

## 安装：选择来源

下面的常规 setup 流程用于普通本地目录。使用 OneDrive/SharePoint API 时，按后面的云盘步骤执行 `setup --skip-config`，再用独立云盘配置入口；不要把同步盘路径填作云 API 来源。

Windows 已用过临时 launcher 或修改过 `dist/index.js`，准备卸载重装时，请按[重新安装步骤](docs/WINDOWS_REINSTALL.md)处理旧注册、解释器和状态目录。无需卸载 Codex，也不要删除整个 `.codex` 或源资料；仅重新下载程序不能清除旧连接配置。

把仓库与真实资料放在**互不包含的独立目录**，不要将运行安装放在会被定期清除的下载缓存或临时目录中。安装 Node.js 22+ 和 Python 3.9+，下载本仓库或执行下面的 clone 命令。Windows PowerShell、macOS 或 Linux 终端均可执行：

```sh
git clone https://github.com/raymondhuang1994/DF-MCP.git
cd DF-MCP
npm ci --ignore-scripts
npm run setup
npm run install:skill
```

`setup` 在仓库 `.venv` 安装 Python 依赖、编译 TypeScript，然后询问读取哪个文件夹。Windows 可通过文件夹选择窗口选取，也可按提示输入路径。向导展示读取目录与独立状态目录，确认后才保存配置；取消不保存新配置。它不在源目录创建测试文件，也不修改源目录权限。**索引在 MCP 客户端启动服务后才开始**，安装配置本身不读取整库正文。

默认关键词读取无需模型 API key 或数据库服务器。可选语义/重排功能需要另外明确配置并运行兼容的本地模型服务；安装不会下载、安装或启用模型。Windows 安装脚本优先查找 `py -3`，再尝试可用的 `python`；系统商店跳转别名不作为可用解释器。支持与构建步骤遇到错误时应先解决该错误，不能把未成功的安装当作可用服务。

Skill 安装到个人 Codex 技能目录，名称为 `df-mcp`。同名目标或旧 `team-drive-read` 已存在时默认拒绝改动；明确更新或迁移旧 `team-drive-read` 可执行 `npm run install:skill -- --replace`，脚本先在目标磁盘逐文件复制并校验完整性，再备份原 Skill 到 Codex 配置目录中的 `skill-backups/` 并发布。复制失败不改动原安装；发布失败会尝试恢复原安装，无法自动恢复时保留备份并报告路径。支持中文/空格用户路径及仓库与用户目录位于不同磁盘的安装。Skill 只包含操作规范，MCP 连接按下一节配置。

### 配置位置与后续换目录

默认状态基目录：

| 平台 | 默认位置 |
|---|---|
| Windows | `%LOCALAPPDATA%\df-mcp` |
| macOS | `~/Library/Application Support/df-mcp` |
| Linux | `$XDG_STATE_HOME/df-mcp`，未设置时为 `~/.local/state/df-mcp` |

每个本地源目录使用独立的 `profiles/<root-hash>/config.json` 与 `profiles/<root-hash>/state/`。云盘使用 `profiles/graph-<hash>/`，另含空 cloud-anchor 和独立认证缓存。向导会输出完整配置路径。共享索引位于 `state/shared-v1/index/`，控制文件位于 `state/shared-v1/control/`；连接结束后保留已提交索引，下次连接复用。配置、正文缓存、数据库、日志和导出按源资料的敏感程度管理，不放在源目录、同步共享盘或公开 Git 仓库中。

状态路径不能包含 `.codex` 目录，也不能位于自定义 `CODEX_HOME` 内。安装器和服务都检查此边界及物理路径，不能用别名、符号链接或 junction 绕过。这样 Windows 状态目录的长期句柄不会固定 Codex 的配置目录。即使默认环境变量被重定向到 Codex 目录内，配置也会拒绝，应改用明确的外部 `--state-dir`。

更换本地目录时运行 `npm run configure`，重新选择并确认，然后用向导给出的配置路径更新 MCP 连接。云来源改用 `configure:cloud` 的明确 ID 和独立 profile。不会原地把旧索引绑定到新来源，也不会删除旧配置或资料。MCP 工具本身不能更改读取范围。

已明确路径时可非交互配置，例如 Windows PowerShell：

```powershell
npm run configure -- --root 'D:\团队资料' --yes
```

`--yes` 必须与明确的 `--root` 同用，表示确认该目录。可用 `--state-dir '<独立状态基目录>'` 自定义状态基目录；向导要求源目录、状态基目录与安装目录两两不重叠，任何一个都不能包含另一个。真实资料应放在程序目录外；仓库内的合成示例仅通过专用示例配置使用，不经过真实资料安装向导。无交互终端且未给出 `--root` 会失败，不会默认扫描主目录或示例库。

### OneDrive/SharePoint API 来源

先由 IT 提供获准的公共客户端应用和来源 ID，再执行：

```sh
npm run setup -- --skip-config
npm run configure:cloud -- --tenant-id '<租户UUID或登录受众别名>' --client-id '<应用UUID>' --drive-id '<文档库ID>' --folder-id '<文件夹item ID>' --yes
npm run login:cloud -- --config '<云配置JSON绝对路径>'
npm run install:skill
```

配置命令只创建 config 和空 anchor；登录由用户在微软验证页显式完成，不需要把密码或 token 发到聊天。默认请求只读 `Files.Read`，SharePoint 团队资料可能需要 IT 批准并明确配置 `Files.Read.All` 或 `Sites.Read.All`；程序不代建应用、不自动扩大授权。配置 folder 约束程序读取范围，不等于微软 token 仅有该文件夹权限。

云配置的 `root` 是专用空 **cloud-anchor**，用于本机边界检查，不能放入资料。真实来源由 `graph` 中的 tenant/client/drive/folder 绑定。索引位于同 profile 的独立 state，OAuth 缓存另存 auth.json；不是全库镜像。复制向导打印的 MCP 注册命令，原 local 配置继续保留。完整步骤、权限边界与真实租户验收见[云盘说明](docs/CLOUD_DRIVE.md)。

## 配置 MCP 客户端

**优先直接复制配置向导输出的注册命令。** 它使用当前安装的 Node 与专用 Python 环境的绝对路径，避免桌面应用与终端的 PATH 不同、或继承了另一套 Python 环境而启动失败。以下是手动配置的同等写法；将配置路径替换成向导的实际输出。

Windows PowerShell，在仓库目录执行：

```powershell
$repo = (Get-Location).Path
$node = (Get-Command node -CommandType Application).Source
$python = Join-Path $repo '.venv/Scripts/python.exe'
$config = '<向导输出的配置文件绝对路径>'
codex mcp add df_mcp -- $node (Join-Path $repo 'dist/index.js') --config $config --python $python
```

macOS / Linux：

```sh
node_bin="$(node -p 'process.execPath')"
codex mcp add df_mcp -- "$node_bin" "$PWD/dist/index.js" --config '<向导输出的配置文件绝对路径>' --python "$PWD/.venv/bin/python"
```

移动安装目录、删除旧 Node 版本或更换 Python 后，重新运行 setup/configure 并更新注册命令；不要沿用已不存在的解释器路径。`doctor --config ...` 会检查解释器与配置，实际连接仍需用 status 验证。

只有 Codex 桌面应用、没有 `codex` 命令时，也可在 Codex 的 `config.toml` 中配置同样的入口。未设置 `CODEX_HOME` 时，文件位于用户目录的 `.codex/config.toml`；已设置时使用该目录下的 `config.toml`。保留其他设置，将下面占位符替换为向导打印的真实绝对路径。如果 `df_mcp` 已存在，只更新原条目，不重复添加同名 TOML 区块：

```toml
[mcp_servers.df_mcp]
command = '<Node 可执行文件绝对路径>'
args = ['<安装目录>/dist/index.js', '--config', '<向导生成的配置文件绝对路径>', '--python', '<专用 Python 可执行文件绝对路径>']
```

Windows 的 Python 通常为 `<安装目录>\.venv\Scripts\python.exe`，Mac/Linux 为 `<安装目录>/.venv/bin/python`。这里的 Codex 配置文件只保存启动入口；JSON 内的 `state_dir` 仍必须在 `.codex` 之外。保存后完整退出并重新打开 Codex，以释放旧连接。

这里的 `df_mcp` 是客户端连接名称；Skill 仍为 `$df-mcp`，工具名保留 `team_drive_` 前缀兼容现有调用。旧版本用户应先关闭旧会话，在客户端移除旧 `team_drive` 连接，再添加新连接，避免保留重复注册。其他支持 stdio 的客户端参考 [examples/mcp.json](examples/mcp.json)，替换路径占位符。启动新会话，让客户端发现 Skill 和工具。Windows 上已经使用临时 launcher 或修改过 `dist/index.js` 的用户，按[迁移说明](docs/WINDOWS_MIGRATION.md)检查、备份并替换。

多个 Codex 聊天使用同一份配置时，通过轻量 stdio 桥接连接同一个本地 broker，共享一个 Python 引擎、SQLite 索引和扫描调度。Mac/Linux 使用本机 Unix socket，Windows 使用 named pipe，不对外监听网络，也不安装系统服务。每个聊天有独立会话和分页游标；不能在聊天之间互换 cursor。

服务按需启动，最后一个客户端断开后空闲 60 秒退出。可用 JSON `"service": {"idle_seconds": 60, "max_clients": 64}` 配置空闲时间（1–3600 秒）和客户端上限（1–256）。达到上限时按错误提示释放不用的连接并重试，不为每个聊天复制配置或索引。`state_pool` 仅保留旧独立 worker 的兼容用途，普通 MCP 不再按聊天占索引槽。

`team_drive_status` 返回服务版本、source/config 身份、实例、健康状态、当前作业 ID（详见 job_status）和 `state_storage`。先看实际返回能力，不能凭 Skill 文件推定已连接新版。多个客户端共享刷新/取消作业：取消会影响同一来源其他会话；只有任务确实不再需要时才取消。配置在服务运行中发生变化时，按提示关闭相关连接并等待服务退出后重新连接，不混用配置。

修改启动入口后需重新连接旧聊天；已关闭的 stdio 管道不会被修改配置复活。手动 `npm start -- --config '<配置路径>'` 也只是一个桥接客户端。服务没有隐式源目录，不在协议会话中弹出目录选择框。

### 诊断当前连接

```sh
node scripts/doctor.mjs --config '<配置路径>'
node scripts/call-tool.mjs team_drive_status --config '<配置路径>'
node scripts/call-tool.mjs tools --config '<配置路径>'
```

`doctor` 仅离线读取指定配置、源码/构建版本与路径状态，不启动 worker、不扫描源文件、不创建或修复状态；其成功不能代替实时状态检查或云端授权验收。它显示本机路径，分享前需脱敏。`call-tool` 明确连接指定配置；只有显式 `--demo` 才使用虚构样本，不会默认把模拟库当成用户资料库。status 不等待全库索引，但云来源状态需要在线检查；普通读取等待初次索引最多 60 秒，大库使用持久客户端观察进度。

Mac 升级时先用原连接的配置运行 doctor，核对实际来源和版本。若原连接使用仓库虚构示例，可继续保留同一示例配置；升级服务不会自动切换到真实资料或新根目录。源码副本、构建结果与 Codex 启动入口是三个不同位置，只有重新连接后的 status 才能确认实际运行版本。

示例请求：

```text
使用 $df-mcp 查指定资料目录里本月的正式预算及可用金额，说明口径并引用单元格；缺少资料时明确说明。
```

AI 应先检查索引状态，区分正式资料与草稿，读取证据，再用确定性程序计算。搜索结果不能证明业务数据完整。

### 升级、恢复与磁盘维护

0.7 保留现有 stdio 入口、`team_drive_` 前缀和原有证据工具，增加可恢复扫描及本地 Wiki；现有 local、windows_share、microsoft_graph 来源保留各自边界，不因升级自动切换。未设置检索模型选项时继续使用关键词模式。首次连接可能因索引签名变化重新索引；应保留旧安装与配置，关闭旧连接后升级，并以实时 status 验证。原本地升级/回退原则见 [0.4 升级说明](docs/UPGRADE_0_4.md)，新增云来源使用[独立配置流程](docs/CLOUD_DRIVE.md)。回退本地旧版时恢复原 local 配置与对应状态备份，不让旧版打开云配置或新版数据库来试探兼容性。

0.3 从受管理源码构建服务，不直接导入 0.1/0.2 根层索引或 0.2.1 的 `pool-v1/`。这些旧缓存保留，首次启动共享索引会重建一次，随后所有聊天复用。来源身份不变、已有外部配置可继续使用；重新运行 configure 会按根目录/状态绑定复用配置，并原样保留自定义选项和键顺序。改变来源需要明确选择/确认并生成独立 profile，不会覆盖旧来源绑定。

共享服务使用磁盘工作数据库和已提交检查点。启动从已提交版本恢复；每次提交采用受控临时快照和原子替换，工作文件中断不会被当成新的完整版本。证据读取仍重新校验源 hash。索引是可重建缓存，不能取代源资料备份，也不能把进程强杀测试理解为所有磁盘故障/断电场景已验证。

稳定运行保留已提交库和工作库，提交时另需一个临时快照；若有损坏快照，至多另留一份隔离副本。因此应按最多约 3 份完整索引、含隔离副本时约 4 份预留空间；这是单个共享服务的空间预算，不随聊天数增加。已结束作业最多保留 100 条。解析临时文件只在独占租约下按受控规则回收，不递归删除未知内容。

```sh
node scripts/maintain-state.mjs --config '<配置路径>' --prune-exports-days 30
node scripts/maintain-state.mjs --config '<配置路径>' --prune-exports-days 30 --apply
```

维护默认预览，`--apply` 才删除选中的派生文件；`--reset-index` 可明确重建已提交/工作/临时/隔离数据库，保留导出、依据包和未知文件。来源目录身份确已更换时，只有明确的 `--reset-index --accept-source-replacement` 才接受新身份；不要为绕过错误自动加此参数。它获取与服务相同的租约并跳过忙状态。完成全量维护前关闭相关连接、等待 broker 空闲退出；不删除锁文件来强制释放服务。导出保留期只清理未被依据包引用的独立 JSONL；依据包清单及其引用的导出长期保留，不自动到期。无法核实清单引用时拒绝清理（BUNDLE_REFERENCES_UNVERIFIED）。旧缓存/profile 需核对后单独处理，源目录永远不属于清理范围。Windows 迁移及实机验收见[迁移说明](docs/WINDOWS_MIGRATION.md)。

### 只读的含义

源文件仅以读取权限打开，不提供修改、删除、重命名、移动或更改权限的 MCP 接口。解析快照、索引和生成结果写到独立状态目录。应用清理与 Skill 迁移不处理源资料。Windows 为固定目录身份而持有的句柄可能暂时阻止目录重命名或移除；需要整理源目录时先关闭服务。

这些保障约束本 MCP，**不能撤销 AI 宿主其他终端或文件工具的写权限**。需要操作系统强制只读时，由 IT 配置受限账户或隔离环境；普通安装不改变源目录 ACL。详见 [Windows 只读部署](docs/WINDOWS_READ_ONLY.md)。

local 来源仍不支持直接读取 Windows OneDrive cloud reparse 文件：即使已经下载，有些文件仍可能被跳过，在线占位文件不自动水合。可选择获准资料的普通非同步本地副本，或显式配置独立的 Graph 云来源；两者不是自动切换。被排除或占用的文件形成覆盖缺口，不能当作已读取。

## 测试与模拟演示

不配置个人目录、只构建并测试：

```sh
npm run setup -- --skip-config
npm test
npm run test:installer
npm run test:doctor
npm run test:cloud-configure
npm run test:backend
npm run test:mcp
npm run test:concurrency
npm run test:install
```

测试使用隔离临时目录和虚构资料。`--skip-config` 只跳过目录配置，不创建可用的个人读取连接。已有依赖和编译结果时可直接运行测试。

CI 在 Windows、macOS、Linux 的清洁检出中安装依赖并构建，覆盖 Python 3.9 与 3.13。`test:install` 另外使用临时用户目录运行真实配置、Skill 安装和诊断命令，再启动两份 MCP 客户端检查同文件读取、共享实例、关闭重开与来源不变。它验证默认路径与安装流程；不能代替每台电脑的企业策略、源 ACL 或 Codex 应用级验收。

新安装的最小验收：在两条聊天各自调用 status，确认版本为 0.7.0、`state_storage.mode=shared`、同一个 `instance_id` 和预期 provider，发现原有证据工具与新增 Wiki 工具，再 inspect/read 同一文件，并用 context 核对返回的原文位置与缺口。按 [0.7 验收](docs/UPGRADE_0_7.md) 验证扫描续跑、Wiki 来源校验与增量失效。完整退出并重开 Codex 后重复一次。云来源还需验证登录、权限、更新/删除和断网；目前没有真实租户验收结果。失败时保留结构化错误，用明确配置运行 doctor；不要创建随机状态目录或删除活动锁文件作为修复。

`npm run demo` 显式启动模拟目录的 stdio 服务；它不提供聊天界面，需 MCP 客户端发送协议请求。也可将客户端 `--config` 指向仓库 `examples/config.sample.json`，用 `$df-mcp` 查询样本。演示配置使用独立的 `.local/sample-v02-state/`，不会自动替换你的正式配置或复用 0.1 旧演示缓存。

0.4 增加可重复的工具链评测，默认自行生成临时语料，不读取已配置的团队资料，也不调用回答模型：

```sh
npm run evaluate -- --output .local/evaluation-keyword.json
```

JSON 报告包含检索 Recall@K/MRR、版本与定位校验、部分 PDF 缺口、分页、源修改/删除失效、十进制汇总及耗时。退出码 0 表示这组检查全部通过，1 表示有失败。报告明确区分工具链检查与 LLM 回答准确率；小型合成语料通过不证明真实业务准确率或生产延迟。可选模型评测见[配置说明](docs/RETRIEVAL_CONFIGURATION.md)。

## 工具与完整性

| 工具 | 用途 |
|---|---|
| `team_drive_status` | 覆盖范围、状态、限制、索引更新时间 |
| `team_drive_list` | 分页列出文件，包含失败/不支持的条目 |
| `team_drive_search` | 默认关键词/BM25；可选 hybrid/semantic 与 rerank；结果须回查并检查降级状态 |
| `team_drive_inspect` | 校验版本，分页导航章节/工作表/页面结构 |
| `team_drive_read` | 按工作表/行范围/PDF页/幻灯片筛选分段，支持版本锁定与续读 |
| `team_drive_context` | 指定版本与命中 ordinal，返回有界上下文、候选标题/首行/单位/注释及缺口 |
| `team_drive_plan` | 给出证据工作流建议；规则分类不能证明完整性或权威性 |
| `team_drive_diagnostics` | 有界的操作耗时、错误样本及汇总；不代表回答正确率 |
| `team_drive_export` | 将指定版本已提取片段导出到受控 JSONL，保留提取缺口 |
| `team_drive_evidence_bundle` | 将指定文件版本汇集为依据包，返回来源清单、导出、观察时间窗与缺口 |
| `team_drive_refresh` | 排队增量更新；`force=true` 重试并重建 |
| `team_drive_job_status` | 查看更新任务与失败情况 |
| `team_drive_cancel` | 请求在安全文件边界取消更新 |
| `team_drive_wiki_status` | Wiki 页数、失效状态和待重建队列 |
| `team_drive_wiki_search` | 查找经过当前来源核验的知识页线索 |
| `team_drive_wiki_read` | 核对引用后读取派生知识页，回答仍回查原文 |
| `team_drive_wiki_write` | 保存带原文引用的页面与依赖，防止覆盖冲突 |
| `team_drive_wiki_refresh` | 分批重新核验来源，列出需要更新的知识页 |

`inventory_complete` 是本次目录盘点完成；`all_present_files_indexed` 是盘点内文件均为 ready（可包含部分提取）；`all_present_files_extracted_completely` 才同时要求没有文本提取缺口；`extraction_complete` 是该解析器完成支持内容的提取。它们**都不表示业务资料齐全或视觉内容全部理解**。必须查看 `format_warnings`。致命解析失败不提供成功正文；可明确划定的部分 PDF 提取会保留可读页并标出缺口，不能作为完整文件证据。

Excel 公式与保存值分开返回；保存值可能过期，没有自动重算。Word 页码不稳定，使用段落/表格引用。PPT 图形、PDF 复杂表格、扫描件等存在明确边界。完整限制见 [formats.md](skills/df-mcp/references/formats.md)。

完整汇总应先确定权威来源和业务范围，分页列出并处理全部相关文件，检查遗漏与版本，再用 evidence_bundle 保存文件清单、版本化导出和缺口，用确定性代码计算。依据包生成成功不代表输入总体完整或业务口径已批准。导出是服务状态目录中的 JSONL，不是原始二进制文件。`artifact_path` 是运行时返回给本地分析工具的绝对路径，不能将含该路径的完整工具轨迹直接公开发布。

Graph 来源的 `file.origin` 保留云端 drive/item 身份、可用的 `web_url`、`etag` 和内容校验后的 `content_sha256`；`source_version` 是结合身份、版本标记和内容 SHA256 的复合版本，不等于纯文件内容 hash。引用优先使用服务返回的云文件 web_url，加原文 locator 与 source_version；没有 web_url 时保留相对路径及云 item 身份，不臆造网址，不引用临时下载地址。

云 list/search 在返回缓存候选前逐项检查当前 Graph 访问和 etag；某项校验失败会使调用失败，不能将旧页当成成功结果。此检查是顺序执行的请求范围检查，不是全目录权限的原子快照。inspect/read/context/export 还会校验实际内容与版本。认证、网络、权限或索引覆盖不足时披露缺口，不把空结果或旧缓存当作“没有资料”或当前仍有权访问的证明。

`context` 中的标题、首行和附近注释是从实际提取内容选出的候选，不保证它们承担正确的业务语义；表头候选不能自动视为已确认表头。该工具有片段和字节预算；邻接片段受页面、幻灯片、工作表和表格边界限制，标题与附近表外注释另作为候选返回。它不能补出未提取的图片、脚注或版式。查看 `gaps`、`excluded_by_boundary` 和 coverage，必要时用 read 继续核对。

`search` 的可选语义索引是有界候选缓存。模型不可用时，hybrid 可以明确降级为 BM25；semantic 模式会报告不可用或部分覆盖，不能把空结果理解为没有资料。阅读 `retrieval` 的实际 mode、semantic 覆盖和错误、rerank 状态。模型输出的相似度不构成事实证明。

## 更新、限制和性能

local 来源默认每 30 秒核对文件元数据，仅解析新增或变化内容；云配置向导默认 300 秒，按 Graph 范围和预算执行盘点，不提供 delta 同步。每次证据读取会重新校验内容，云文件还需下载和前后元数据检查；大文件读取仍有 I/O 与网络成本。关闭服务或休眠时不会继续更新，恢复启动后补查。原生操作系统文件事件监听不在本版中。

新增操作诊断默认开启，最多保存 1000 条、7 天，可通过 `diagnostics.enabled/max_events/retention_days` 调整。语义和重排默认关闭。启用后，向量缓存随来源版本、文本摘要和模型配置失效；本地服务中的模型权重变更需要管理员同时更新 `model_revision`，同名同维度权重变化无法自动识别。不要通过省略版本校验来换取更低耗时。

默认单文件 256 MiB、压缩包总展开体积 512 MiB、100000 个片段、每段 8000 字符、512 列、2000 PDF 页、解析超时 30 秒。超限返回错误，不静默截断。参数可配置，调整前应针对代表性文件验证内存和耗时。

Linux 解析子进程设置地址空间上限；macOS 和 Windows 没有相同的硬内存保证，依靠大小/展开限制和超时。长任务可取消和重启；扫描续跑复用经过重新核验的已提交进度，单个中断文件重新解析，不承诺从中断行继续。

基准测试：

```sh
.venv/bin/python scripts/benchmark.py --files 1000 --rows 50000
```

Windows 可将命令中的 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。生成的结果只在 `.local/` 保存。用真实规模验收前，不应承诺任意文件“秒级准确”。

## 开发与发布

TypeScript MCP 适配层在 `src/`；Python 索引与解析在 `python/team_drive/`；Skill 在 `skills/`。业务来源与指标登记示例在 `examples/`，仅供业务层参考，不代表本工具已自动认证资料权威性。

公开发布前执行：

```sh
git add .
npm run check:public
```

检查器扫描 Git 跟踪文本中的凭证模式、私人主目录、邮箱、意外二进制和运行状态文件；它不能替代人工审查。不要提交真实资料、配置、导出、工具完整轨迹、索引或缓存。源码使用 MIT 许可证；依赖保留各自许可证。
