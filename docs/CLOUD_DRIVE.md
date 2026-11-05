# OneDrive 与 SharePoint 云盘接入

此入口用 Microsoft Graph 读取一个明确配置的 drive/folder，沿用本机 stdio MCP 和本地证据索引。云盘是数据来源，MCP 仍由本机客户端启动；这不是 HTTP MCP、SMB 挂载或共享管理员账号的团队后台。现有 `provider="local"` 配置继续保留原行为。

当前交付使用合成配置和模拟网络测试，**未连接真实租户，也未完成公司条件访问、权限撤销和真实文件的部署验收**。先完成下面的应用配置和小范围验证，再接入团队资料。支持范围以实际运行版本和 status 为准。

## 准备身份与读取范围

需要由组织允许使用的 Microsoft Entra 公共客户端应用、登录账号，以及已经核对的文档库和文件夹 ID。应用 ID 是标识，不是 API 密钥；本流程不需要 client secret。设备代码授权需要应用允许 public client flows，设置步骤见[微软桌面应用配置](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration)。

| 参数 | 来源与用途 |
| --- | --- |
| `tenant_id` | Entra 的 Directory ID。团队通常使用固定租户 UUID；也接受官方 `organizations`、`consumers`、`common` 登录受众别名，应用注册必须支持相应账号类型 |
| `client_id` | 应用注册中的 Application ID，格式为 UUID |
| `drive_id` | 指定 OneDrive 或 SharePoint 文档库的 drive ID |
| `folder_id` | 该 drive 内获准读取文件夹的实际 item ID；不能传入分享链接、本机路径或 `root` 别名 |

由管理员或获准工具取得 ID；本配置命令不会搜索全租户来猜测来源。Graph 支持[列出用户、组或站点的 drives](https://learn.microsoft.com/en-us/graph/api/drive-list?view=graph-rest-1.0)，再用[driveItem 元数据接口](https://learn.microsoft.com/en-us/graph/api/driveitem-get?view=graph-rest-1.0)核对目标文件夹。Teams 不同频道可能对应不同站点/文档库，不能把一个 ID 当作全部团队资料。

默认 delegated scope 为 `Files.Read`，登录流程补充 `offline_access`、`openid`、`profile`。配置的 `graph.scopes` 可显式使用 `Files.Read`、`Files.Read.All` 或 `Sites.Read.All` 中获准的只读权限；不会自动升级权限。微软为[文件夹枚举](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0)列出 `Files.Read` 为最小 delegated 权限，但组织共享资料是否可读仍需按目标资源、账号权限及租户策略验证。

**配置 folder 是本程序的读取边界，不等于 Entra 令牌只能访问该文件夹。** 需要云端资源级限制时，应由 IT 另行评估 Selected 权限；这些权限同时要求同意、资源授权和正确 token，本版登录 scope 白名单尚不支持 Selected 配置，不能宣称已经实现该授权模式。[微软 Selected 权限说明](https://learn.microsoft.com/en-us/graph/permissions-selected-overview)

## 配置与登录

先在独立安装目录完成通常的依赖安装和构建，然后运行以下命令；占位符必须替换为已确认的实际 ID。命令在 PowerShell、macOS/Linux shell 中均为一行执行：

```sh
node scripts/configure-cloud.mjs --tenant-id '<租户UUID或登录受众别名>' --client-id '<应用UUID>' --drive-id '<文档库ID>' --folder-id '<文件夹item ID>' --yes
```

可加 `--state-dir '<独立本机状态基目录>'`。`--yes` 明确确认四个 ID；缺少任一参数会失败，不会选择默认资料或弹出隐式登录。命令只创建配置和空 anchor，不登录、不请求 Graph、不创建索引或令牌缓存。它会调用本机目录边界检查。

每个来源建立独立的 `profiles/graph-<hash>/`：

```text
config.json       配置与云来源标识
cloud-anchor/     专用空本机边界目录
auth.json         显式登录成功后保存的私有认证缓存
state/            MCP 启动后使用的索引、解析临时文件和导出
```

`root` 指向 cloud-anchor，用于保留现有本机状态边界检查；它不是云盘镜像，也不是用户资料目录。anchor 必须保持为空，与 state/auth 文件不重叠。所有路径必须使用普通本机存储，不在 `.codex`、`CODEX_HOME`、网络盘或同步盘下，不经符号链接/junction。配置入口拒绝路径链接和安装目录重叠；POSIX profile 使用 0700。不要把 auth 文件、状态、导出或真实配置提交到 Git。

重复运行同一组 ID 会核对原来源及 root/state/auth 绑定，并逐字节保留现有配置和运行选项。冲突会拒绝覆盖；更换 ID 得到另一个 profile，原 profile 保留。不要通过手改已有身份字段切换资料库。

配置中的 `graph` 示意如下；路径由向导生成，不应将下面的占位符直接保存为运行配置：

```json
{
  "tenant_id": "<租户UUID或登录受众别名>",
  "client_id": "<应用UUID>",
  "drive_id": "<文档库ID>",
  "folder_id": "<文件夹item ID>",
  "auth_cache": "<私有profile目录>/auth.json"
}
```

用向导输出的 config 路径显式登录：

```sh
node scripts/login-cloud.mjs --config '<配置JSON绝对路径>'
```

该命令调用专用 Python 的 `team_drive.graph_auth`，输出 device_code 阶段的验证网址、用户代码与期限。由账号持有人在微软验证页完成登录和权限同意，不把代码发送给他人。成功输出 complete 阶段；错误返回脱敏信息及退出码 1，取消返回 130。成功登录不表示已枚举或读取文件。设备代码只能在用户准备登录时发起，协议见[微软设备授权流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)。

JSON 配置不保存 token；认证缓存是单独的私有文件。Windows 使用当前用户 DPAPI，POSIX 使用当前用户所有的私有目录及 0600 缓存文件；POSIX 文件本身没有额外加密，需依靠本机账户权限和磁盘保护。已有缓存绑定租户、应用、账号和 scope，重新登录其他账号会拒绝覆盖；可明确使用另一个 `--state-dir` 建立独立 profile，保留原账号配置。令牌过期后可能通过 refresh token 更新，撤销、条件访问或账号状态变化仍可能要求重新显式登录；不会在 MCP 请求里偷偷发起设备代码流程。[微软刷新令牌说明](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)

有明确运维需求时可配置 `graph.token_env` 为环境变量名称，由启动 MCP 的环境提供 access token；配置中只保存变量名，不保存值。环境 token 的指纹纳入缓存身份，运行中 token 变化会被拒绝；轮换后需显式建立独立 profile/state，不能复用原身份状态。该模式不支持设备登录或自动刷新，桌面程序也未必继承终端环境。一般使用上面的 OAuth 缓存流程，以稳定账号身份刷新令牌。

## 注册与验收

复制配置命令打印的原有 `codex mcp add df_mcp -- <Node> <dist/index.js> --config <JSON> --python <Python>` 注册命令。若已有 `df_mcp`，更新原条目并完整重新连接，不重复注册。保留旧 local 配置和运行目录以便回退。当前连接不会因磁盘文件变化自动切换到新来源。

首次云盘验收至少包括：

1. status 显示 `microsoft_graph` 及预期来源；确认当前账号、文档库、文件夹和索引范围。
2. 小目录完成枚举后检查全部分页和失败/排除记录；分页、限流、断网及预算耗尽不能解释为“没有文件”。
3. 对已知文字文件执行 search → inspect → context/read，核对原文、定位、来源版本与云端文件。
4. 在专用测试目录修改、删除和移动文件，确认旧版本不能继续作为最新证据；使用测试账号撤销访问，检查读取失败及旧缓存处理。
5. 测试登录失效、重新登录、两条本机会话、关闭重开，以及目标机器的代理/条件访问策略。完整汇总仍须枚举所有相关文件和披露缺口。

真实租户通过前，模拟测试只证明实现处理了所覆盖的接口响应和错误情形；不能承诺企业权限、跨账号隔离、任意规模或最终回答准确率。多人使用应各自登录并保留独立配置/缓存，不共享 auth.json，也不把同一 Windows/macOS 账号下的本机 broker 当作多用户权限服务器。

## 界限与运行参数

云端读取使用 Graph 的文件夹分页、元数据和内容接口，本地解析仍受原格式和资源限制约束。扫描在限定范围内递归枚举，只有整次目录盘点成功才发布清单；失败不发布空清单来误判删除。快捷方式、remoteItem、package 和非普通文件项目不会被当作可读取文件。没有全盘镜像、delta 同步或目录业务完整性保证。

文件证据版本结合 drive/item 身份、版本标记与实际下载内容 SHA256；下载前后复核云端元数据。`file.origin` 包含可用的 `web_url`、`etag` 与内容校验后的 `content_sha256`，`source_version` 则是复合版本。引用使用返回的 web_url、locator 和版本；web_url 缺失时保留路径及 item 身份，不拼接猜测链接或引用临时下载地址。

list/search 返回缓存候选前，会逐项核验当前 Graph 访问及 etag；校验失败则本次调用失败，不能把旧页作为成功结果。逐项检查不是整库权限原子快照，仍需在目标租户验证权限撤销与并发变更行为。重复证据读取仍有网络与内容校验成本，不能宣称本地缓存让云盘读取完全离线或恒定耗时。云端鉴权失败与覆盖缺口必须保留，缓存不得当作当前仍有权访问的证明。

OCR、SMB/NFS、自动权限授予、原文件写入、Office 宏、跨账号共享缓存以及政府/中国专有云端点不在本入口范围内。登录与 Graph 数据传输需要网络；可选 embedding/rerank 仍独立配置且默认关闭。

当前 HTTP 客户端不采用系统或 `HTTP_PROXY` / `HTTPS_PROXY` 代理，需能直接连接微软官方授权、Graph 及受支持的内容下载域名。必须通过显式代理出网的企业环境尚不支持，可能返回 `AUTH_UNAVAILABLE` 或 `GRAPH_UNAVAILABLE`；应由 IT 先确认网络条件。

向导默认 `poll_seconds=300`。`graph` 可选运行限制如下；达到上限是覆盖缺口，不代表业务材料完整：

| 设置 | 默认值 | 允许范围 |
| --- | --- | --- |
| `timeout_seconds` | 10 秒 | 0.1–20 秒 |
| `max_items` | 10000 | 1–100000 |
| `max_pages` | 1000 | 1–10000 |
| `max_depth` | 32 | 1–128 |
| `scan_budget_seconds` | 120 秒 | 1–600 秒 |

Graph 下载契约正在演进。当前[内容下载文档](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0)描述短期预认证 302 地址；微软另公告自 2027-04-01 起受影响 API 开始移除预认证 URL，下载接口改为直接返回内容。真实租户需验证实际下载响应，不能把临时下载 URL 当作长期引用，也不能把 Graph token 发往任意跳转主机。[微软迁移公告](https://devblogs.microsoft.com/microsoft365dev/pre-authenticated-tempauth-urls-are-retiring-move-to-microsoft-entra-id-tokens-before-april-1-2027/)

合成配置/登录调度检查可运行 `npm run test:cloud-configure`；`npm run test:cloud` 验证真实 MCP 传输至本地解析/存储全链，Graph 响应使用合成数据且禁止真实 HTTP。这些测试不执行真实设备登录，也不能代替上面的云租户验收。
