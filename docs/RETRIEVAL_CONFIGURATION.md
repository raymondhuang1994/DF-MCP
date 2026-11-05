# 可选本地检索模型

默认 `mode="keyword"` 使用关键词、别名和 BM25，不需要模型。semantic embedding 与 rerank 都默认关闭。项目不安装或下载模型；管理员需另行准备经过批准的本地服务，并用真实模型与代表性问题验收效果。

## 安全默认示例

[retrieval.local.example.json](../examples/retrieval.local.example.json) 是单独的 `retrieval` 设置对象，**两项 enabled 都是 false**。示例端口和模型名称是占位值，不表示有服务运行。

- 用于 MCP：把整个对象作为现有 JSON 配置的 `retrieval` 字段，保留原 `root`、`state_dir` 和其他配置。
- 用于合成评测：将仅包含此对象的独立文件传给 `--retrieval-config`，不用添加 root 或 state_dir。评测会自行建立临时语料。
- 只有端点、模型、模型版本、资料使用范围已确定后才将相应 enabled 改为 true。修改运行配置后关闭相关客户端，待 broker 退出再重新连接。

端点仅接受 HTTP(S) 的 localhost、回环地址或明确的 RFC1918/ULA 私有 IP，拒绝任意公共域名、凭据 URL、查询参数和重定向，不使用代理环境变量。该限制只约束调用的地址；本地服务自身是否把请求转发出去，需要管理员另行确认。当前接口没有 API key 配置；需要鉴权的服务不能直接照抄示例使用。

## 支持的接口契约

embedding 端点接收 POST JSON：

```json
{"model":"configured-model","input":["synthetic example text"]}
```

返回 HTTP 200 和 JSON `data` 数组，每项提供对应输入的唯一 `index` 与非零、有限数值的 `embedding` 向量。响应中的排列顺序可以不同，index 才是对应依据。向量维度必须一致，上限 4096；这不表示支持每家模型服务的所有响应格式。

rerank 端点接收 POST JSON：

```json
{"model":"configured-model","query":"synthetic question","documents":["candidate text"],"top_n":1}
```

返回 HTTP 200 和 JSON `results` 数组，完整包含每个候选的唯一 `index` 与有限数值 `relevance_score`。不符合契约、超时或服务失败会产生受控诊断，保留原有候选排序，而不会把失败伪装成完成重排。

## 配置与调用

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `candidate_limit` | 100 | 有界候选文件池，非匹配总量；范围 1–1000 |
| `rrf_k` | 60 | RRF 排名融合常数；范围 1–1000 |
| `semantic.enabled` | false | 是否允许对源提取文本构建模型向量 |
| `semantic.model_revision` | 空 | 管理员维护的模型版本标识；建议使用不可变 revision |
| `semantic.batch_size` | 16 | 每批最多输入数；范围 1–64 |
| `semantic.max_segments` | 20000 | 持久向量缓存片段上限；范围 1–100000 |
| `semantic.max_text_chars` | 8000 | 每片段发送模型的字符上限；范围 1–32000；截断会在覆盖中报告 |
| `semantic.minimum_similarity` | 0.0 | 余弦候选阈值；需针对模型调试，不能当作答案置信度 |
| `semantic.per_file_budget_seconds` | 20 | 每文件填充向量缓存的时间预算；未完成部分在后续扫描继续 |
| `timeout_seconds` | 10 | 每个模型接口的超时设置，范围 0.1–20 秒；整个任务耗时还包括索引、读取和多次调用 |
| `rerank.enabled` | false | 是否允许调用重排端点；仍需请求 `rerank=true` |
| `rerank.max_candidates` | 30 | 一次最多重排的候选数；范围 1–100 |

search 的 mode 默认 keyword。`hybrid` 合并关键词和语义排名；语义不可用时可以退回 BM25，并明确返回实际 mode 与错误。`semantic` 不提供关键词兜底的命中结果，必须检查 semantic 的可用性和覆盖。`rerank=true` 是单独请求；仅配置 enabled 并不代表每次搜索已经重排。

向量存在 SQLite 中，按来源版本、片段文本摘要、endpoint、model、model_revision 与文本限制绑定，采用有界精确余弦比较，不需要额外向量数据库。这种实现不承诺大规模吞吐。模型权重在同名、同端点、同维度下变更不能自动识别；管理员必须相应更新 model_revision，使缓存重新建立。

检查 `team_drive_status.retrieval` 与每次 search 的 `retrieval`：semantic 是否 ready、覆盖是否完整、文本输入是否截断、模型版本是否固定、错误和候选上限；rerank 是否实际 ready。索引数量、相关性分数与检索无结果都不能证明业务完整性。候选还需 inspect/context/read 核验原文版本和位置。

## 比较效果

先运行默认基线：

```sh
npm run evaluate -- --output .local/evaluation-keyword.json
```

在独立配置中明确启用并填好真实本地模型后，可使用：

```sh
npm run evaluate -- --mode hybrid --retrieval-config '<检索设置JSON>' --output .local/evaluation-hybrid.json
npm run evaluate -- --mode hybrid --rerank --retrieval-config '<检索设置JSON>' --output .local/evaluation-rerank.json
```

这些命令只给配置的检索模型发送生成的合成语料，不接现有资料配置，不生成最终 LLM 答案。若示例仍保持 disabled，hybrid/semantic 的模型验收会失败，即使 BM25 找到了正确文件，也不会把降级结果记为语义通过。报告保留实际模式、Recall@K、MRR、证据检查及耗时。模拟接口的协议测试和小型合成检索通过，都不能代替真实模型、真实资料与人工标准答案的业务验收。
