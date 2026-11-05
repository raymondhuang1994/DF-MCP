# 首次配置：两个选项，完整包执行

## 运行环境与已有配置

使用宿主已批准的本地脚本能力。操作系统名称不能单独证明代码运行在用户电脑：先核对工具实际执行位置。不要把云容器临时盘当成独立本地持久目录。

完整 ZIP 的 Python helpers 位于 scripts/。先运行只读检查：

```text
python scripts/df-init.py probe
python scripts/df-init.py profiles
```

profiles 默认检查当前用户默认状态基目录；用户曾指定其他位置时用 `--state-dir <base>`。不要遍历整台电脑寻找配置。已有来源和当前任务一致时直接复用；多个来源时按用户范围选择，不自动合并所有资料。

probe 只发现当前用户已连接的网络资源和有限的已知名称，不能证明云账号已授权、文件都能解析或当前磁盘跨会话持久化。依赖缺失/未批准执行时给出所缺条件，不自动安装系统运行时。

## 面向同事的说明和选择

开始说明：原资料只读；索引、解析缓存另存本机；回答会把必要片段提供给当前宿主配置的模型，请使用公司批准的环境。

只展示 SKILL.md 中的 A/B 菜单。A 为 Digital Finance 网络公盘、SharePoint、OneDrive 的统一入口；B 为本地磁盘。让用户回复字母或直接提供路径/云盘链接。不要展开成四项技术选择。

A 没有明确地址时，使用 probe 的 Digital Finance / Digital Finance_0 候选。唯一匹配也要展示实际完整位置纳入最终确认；多个匹配让用户选择；没有匹配请提供地址。不能猜盘符、补截图中被省略的UNC地址或访问旁边其他团队资料库。

B 使用宿主可用的文件夹选择工具，否则让用户粘贴完整目录。选择具体资料文件夹，不能默认扫描 C 盘。明确包含子文件夹。

直接提供地址时先运行：

```text
python scripts/df-init.py classify --source <literal-source>
```

UNC/映射网络盘走 windows_share；普通固定本地目录走 local；支持的微软云盘链接走下方 Graph 流程。不要根据盘符样子或用户称呼推断实际来源。未知域名/URL、离线共享盘、云占位符、路径链接等按错误说明处理，不绕过限制。

## 本地和网络共享：plan → 确认 → apply

A 对应 `--choice team`，B 对应 `--choice local`：

```text
python scripts/df-init.py plan --choice team --source <verified-share-path>
python scripts/df-init.py plan --choice local --source <selected-local-folder>
```

可追加 `--state-dir <approved-local-base>`。plan 不创建索引、配置或状态目录；只核对所选目录和边界。默认本地状态路径在 probe 返回值中。保存完整 plan JSON 到宿主允许的工作区临时文件（不是源资料目录），不要自行拼接配置字段。

展示一张确认卡：资料类型、实际完整目录/云账号及目录、包含子文件夹、独立索引完整位置、原资料只读、接下来创建状态并扫描。用户确认后：

```text
python scripts/df-init.py apply --plan <saved-plan.json> --confirmed
```

apply 会重新核验计划与源、独占保存配置，保留兼容的已有设置；返回实际 config_path。未确认不传 --confirmed。失败不能悄悄换目录、删除旧配置或扩大来源。取消不创建索引；此前显式完成的云账号登录可能已保留独立账号配置和凭证。

然后在同一已确认范围内调用 df-tool status 启动索引，查看 current_job_id/job_status。区分“配置保存”“连接成功”“索引中”“索引完成且有缺口”。不要只凭 apply 成功就宣称可以完整回答。详见 cli.md。

## SharePoint / OneDrive：公司预设，员工登录选择

使用 IT 提供的公开客户端 tenant_id/client_id 与获准只读 scopes，可参考 assets/company-cloud.example.json。这些技术参数由部署者准备，不能编造，也不要让小白员工自己注册应用。配置模板不包含密码或令牌；授权不等于默认扫描全租户。

用户明确要求连接微软账号后：

```text
python scripts/df-init.py cloud-login --tenant-id <IT-value> --client-id <IT-value> --confirmed
```

只在 IT 已明确批准时附加 `--scope`，每个 scope 一次；默认 Files.Read。可附 --state-dir。这一步会创建独立本机账号配置/凭证并启动设备登录，尚未选择资料或建索引。登录提示仅用于当次本人授权，可展示微软验证地址和当次设备码；不索取用户密码、token，也不把设备码或带凭证的分享链接另存报告或安装包。使用宿主合适的登录显示流程，失败不要假装成功。公司策略不允许该登录方式时停止并交给 IT。

成功后使用返回的 account_config_path，做只读、有限的元数据发现：

```text
python scripts/df-cloud.py resolve --account-config <account-config.json> --url <folder-url>
python scripts/df-cloud.py drives --account-config <account-config.json>
python scripts/df-cloud.py site --account-config <account-config.json> --url <explicit-site-url>
python scripts/df-cloud.py drives --account-config <account-config.json> --site-id <returned-site-id>
python scripts/df-cloud.py folders --account-config <account-config.json> --drive-id <returned-drive-id>
python scripts/df-cloud.py folders --account-config <account-config.json> --drive-id <returned-drive-id> --folder-id <returned-folder-id>
python scripts/df-cloud.py folder --account-config <account-config.json> --drive-id <returned-drive-id> --folder-id <returned-folder-id>
```

/me/drives 不是全部共享文档库；站点查找可能需要另外已获批的只读权限。不要为发现文件夹自动申请更大权限。元数据选择还不是完整文件扫描。短分享链接、跨库快捷方式或授权密钥链接不使用写权限兑换；改为从已有文档库选目录，或由 IT 提供明确的 drive/folder ID。已知 drive/site 可作为 resolve 的 --drive-id/--site-id。

保存最终 `folder`/`resolve` 结果为 selection JSON；然后用与登录相同的 IT 配置、scopes 和 state-dir：

```text
python scripts/df-init.py plan --choice team --selection-file <selection.json> --tenant-id <IT-value> --client-id <IT-value>
```

plan 绑定登录账号和 drive/folder。显示账号、稳定链接、子目录范围及索引目录，用户确认后按前面的 apply 流程；apply 会重新核对云端身份与文件夹，不自动下载全库。账号缓存保留在 accounts 下，资料索引各自放 profiles 下，不共享凭证。

## 后续管理

默认 profiles 可在新对话中重新发现。自定义 base 由用户/公司配置提供，不向默认路径另写隐藏跳转注册表。新增来源独立 plan/apply，不原地替换旧索引绑定。跨库问题逐一确认范围并保留各自来源与版本。

更换索引位置、清理和迁移不是普通查询。说明需要停止相关连接、备份完整状态或在新目录重建，再按另行确认的方案执行；不要运行中只搬一个 SQLite 主文件。保持源目录与状态/程序目录不相互包含。
