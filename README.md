# 物流售后多 Agent 客服

基于 **LangGraph** 的物流售后智能客服：七节点工作流编排，LLM 自主 tool-calling、RAG 政策检索 + Rerank、SSE 真流式回复，以及不依赖大模型的确定性风控与工单/人工复核闭环。

## 核心特性

- **七节点工作流**：意图路由 → 信息抽取 → 政策检索 → 工具调用 → 规则风控 → 工单生成 → 回复组装，状态在节点间显式流转。
- **规则优先 + 按需 LLM**：意图先走确定性规则，命中即跳过模型；未命中才用一次调用同时完成意图分类与字段抽取。政策检索与工具调用在信息完整后并行执行（fanout 空节点汇聚），缩短关键路径。
- **真 tool-calling**：把 `query_order` / `query_logistics` / `check_refund_eligibility` 以 OpenAI function schema 交给模型，由模型自行决定调用哪些工具、并行调用，并用早停策略控制轮次；订单号与诉求明确时走确定性工具路由，不再多花一轮模型调用。
- **检索依赖治理**：Embedding / Rerank 具备超时、并发闸门、半开探测与熔断，配合本地语料缓存与结果 TTL 缓存，外部依赖抖动时毫秒级降级而不拖慢整条链路，并在 `done` 事件与 `/api/metrics` 中透明上报。
- **真 RAG**：Chroma（cosine）+ 阿里百炼 Embedding 召回，`TextReRank` 二次重排；按 `source` 增量更新，MD5 跳过未变更文件；回答带 `[来源: 文件名#片段序号]` 引用。
- **真流式**：`POST /api/chat` 返回 `text/event-stream`，逐节点推送 `stage` 事件，最终回复直接转发模型 token，而非伪流式切片。
- **确定性风控**：退款金额阈值（≤50 自动 / 50–500 人工 / >500 主管）、理赔与换货强制人工、订单不存在强制人工，全部由规则引擎判定，不让 LLM 决定是否放款。
- **工单闭环**：自动生成工单并落库，支持按工单号查询与人工复核（通过/驳回）。
- **可评测**：内置 20 条标注测试集与离线评测脚本，输出意图、抽取、状态、人工转接、工具调用率与耗时。

## 架构

```mermaid
graph LR
    U[用户] -->|POST /api/chat| API[FastAPI SSE]
    API --> FAQ{FAQ 命中?}
    FAQ -->|是| F[直接返回<br/>source=faq]
    FAQ -->|否| R[① router<br/>意图分类]
    R --> E[② info_extract<br/>结构化抽取]
    E -->|缺失必填字段| ASK[追问<br/>status=pending_info]
    E -->|信息完整| FAN[并行分发]
    FAN --> K[③ knowledge_retrieve<br/>向量召回 + Rerank]
    FAN --> T[④ tool_agent<br/>规则优先 / LLM 调用工具]
    K --> P[⑤ policy_check<br/>规则风控]
    T --> P
    P --> TK[⑥ ticket_generate<br/>工单落库]
    TK --> PR[⑦ prepare_reply<br/>组装上下文]
    PR --> S[LLM 流式回复<br/>+ done 事件]
```

风控规则与政策文档保持一致：

| 订单金额 | 处理方式 |
| --- | --- |
| ≤ 50 元 | 自动退款 |
| 50–500 元 | 人工审核 |
| > 500 元 | 售后主管审批 |
| 破损理赔 / 换货 / 投诉 | 强制人工 |
| 订单不存在 | 强制人工核实 |

## 演示

物流查询：模型自行调用 `query_logistics`，七节点进度以 chip 形式流式展示，回复带政策来源引用。

![物流查询演示](docs/demo-logistics.png)

退款申请：680 元订单命中「> 500 元需主管审批」规则，风控判定转人工并生成工单，模型只负责组织措辞。

![退款转人工演示](docs/demo-refund.png)

> 截图来自本地真实运行（`python -m uvicorn main:app`），非设计稿。

## 技术栈

FastAPI · LangGraph · LangChain · ChromaDB · OpenAI 兼容 SDK（火山方舟 Ark）· 阿里百炼 Embedding / Rerank · SQLite · SSE

## 目录结构

```text
.
├── main.py                     # FastAPI 入口：SSE 接口、工单接口、指标接口、启动初始化
├── index.html                  # 原生 JS 聊天页（解析 SSE）
├── data/
│   └── shouhouguize.txt        # 售后政策知识库（可替换为 PDF）
├── docs/                       # README 演示截图
├── src/
│   ├── agent/
│   │   ├── graph_builder.py    # LangGraph 七节点编排
│   │   ├── nodes.py            # 各节点实现
│   │   ├── prompts.py          # router / extract / react / reply 提示词
│   │   ├── tools.py            # 工具 schema 与执行器
│   │   ├── policy.py           # 确定性风控规则
│   │   ├── services.py         # 工单服务
│   │   └── state.py            # 工作流状态定义
│   ├── rag/
│   │   ├── ingest.py           # 切分、MD5 增量、Chroma 入库
│   │   └── retriever.py        # 召回 + 阈值过滤 + Rerank
│   ├── db/database.py          # 订单、物流、工单、会话历史
│   ├── faq/                    # 高频政策问题短路
│   ├── evaluation/
│   │   ├── dataset.json        # 20 条标注测试集
│   │   ├── run_eval.py         # 离线功能评测脚本
│   │   ├── performance_test.py # 黑盒性能测试与资源采集
│   │   └── perf_gate.py        # 性能门禁（阈值 + 基线回退校验）
│   ├── schemas.py              # Pydantic 模型
│   ├── clients.py              # LLM / Embedding 客户端
│   ├── config.py               # 环境变量与阈值
│   └── observability.py        # 节点 / LLM / 工具日志
├── requirements.txt
├── .env.example
└── LICENSE
```

## 快速开始

```bash
# 1. 安装依赖（建议 Python 3.10+）
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置密钥
cp .env.example .env             # 填入 Ark 与 DashScope 密钥

# 3. 启动（首次启动会自动建库并把 data/ 入库）
uvicorn main:app --host 0.0.0.0 --port 8000
```

打开 <http://127.0.0.1:8000> 即可对话。可直接试用：

- `订单DD20260901001物流到哪了`
- `我要退款，订单DD20260910004`（39 元，自动通过）
- `我要退款，订单DD20260910003`（680 元，转人工）
- `我要退款`（缺订单号，触发追问）

启动时会做三件事：初始化 SQLite、按 MD5 增量同步 `data/` 到 Chroma、编译 LangGraph。知识库为空时会自动重建，无需手动清库。

## 关键配置

全部通过环境变量覆盖，默认值见 [.env.example](.env.example) 与 [config.py](src/config.py)。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBED_CONNECT_TIMEOUT` / `EMBED_TIMEOUT` | 3 / 8 s | Embedding 连接与读取超时；应用级 8 s 超时兜底 |
| `RERANK_TIMEOUT` | 6 s | Rerank 超时，超时降级为向量相似度排序 |
| `RETRIEVAL_COOLDOWN` | 60 s | 检索依赖失败后的熔断冷却时长 |
| `EMBED_MAX_CONCURRENCY` | 4 | 同时在途的 Embedding 请求上限，超出立即降级 |
| `RETRIEVAL_CACHE_TTL` / `CORPUS_CACHE_TTL` | 300 / 600 s | 结果缓存与本地语料缓存有效期 |
| `DB_EXECUTOR_WORKERS` / `IO_EXECUTOR_WORKERS` | 4 / 8 | SQLite 与检索同步 IO 的线程池上限 |
| `LLM_MAX_CONNECTIONS` | 32 | LLM HTTP 连接池上限 |
| `SQLITE_BUSY_TIMEOUT` | 5 s | SQLite 写锁等待上限（已启用 WAL） |

## 接口说明

### `POST /api/chat`

```json
{ "session_id": "sess_xxx", "user_input": "订单DD20260901001物流到哪了" }
```

返回 `text/event-stream`，事件类型：

| 事件 | 字段 | 说明 |
| --- | --- | --- |
| `stage` | `node` / `label` / `detail` | 节点进度，如 `router · query_logistics` |
| `token` | `content` | 回复增量文本 |
| `done` | `ticket_id` / `status` / `need_human` / `citations` / `elapsed` / `degraded` / `degraded_reasons` / `retrieval_mode` | 结束事件；后三个字段描述本次检索是否降级及原因 |

`status` 取值：`ai_solved` / `pending_human` / `pending_info` / `error`。

### 其他接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tickets/{ticket_id}` | 查询工单 |
| `POST` | `/api/tickets/{ticket_id}/review` | 人工复核：`{"approved": true, "comment": "..."}` |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/metrics` | 运行指标：`uptime_s` 与检索请求数、在线成功率、缓存命中、熔断/闸门拒绝、Embedding 超时与均值耗时 |

## 评测

测试集见 [dataset.json](src/evaluation/dataset.json)，覆盖物流查询、退款三档金额、换货、理赔、投诉、缺字段追问、部分退款缺商品编号、订单不存在、FAQ 短路等场景。

```bash
# 先启动服务，再运行
python -m src.evaluation.run_eval --base-url http://127.0.0.1:8000
```

2026-10-30 优化后本地实测（20 条，单次串行回放）：

| 指标 | 优化后 | 优化前（2026-10-26） |
| --- | --- | --- |
| 意图准确率 | 100%（18/18） | 100% |
| 信息抽取准确率 | 100%（18/18） | 100% |
| 工单状态准确率 | 100%（20/20） | 100% |
| 人工转接准确率 | 100%（20/20） | 100% |
| 工具调用率（应调用样本） | 100% | 100% |
| 工具误调用 | 0 次 | 0 次 |
| RAG 引用覆盖率 | 77.8% | 83.3% |
| FAQ 命中 | 2/2 | 2/2 |
| 平均耗时 | 3.56 s | 8.83 s |
| P95 耗时 | 5.15 s | 16.28 s |
| 非 FAQ 平均耗时 | 3.16 s | 9.81 s |

> 功能口径全部保持一致；RAG 引用覆盖率的 1 条差异来自该次外部 Embedding 抖动触发关键词降级（L03 未召回政策片段），不影响状态与风控结论，降级已在 `done` 事件中标注。

## 性能测试与门禁

```bash
# 核心链路：20 轮 × 8 请求，并发 2
python -m src.evaluation.performance_test --mode core --repeat 20 --concurrency 2 --output evaluation_results/core-opt.json
# 峰值：40 请求，指定并发
python -m src.evaluation.performance_test --mode load --requests 40 --concurrency 10
# 长稳：固定速率投放
python -m src.evaluation.performance_test --mode soak --duration 900 --rate 0.1

# 门禁（失败时退出码为 1，可接入 CI）
python -m src.evaluation.perf_gate --input evaluation_results/core-opt.json \
  --baseline evaluation_results/core-c2-n160.json \
  --metrics-url http://127.0.0.1:8000/api/metrics
```

门禁口径：HTTP 成功率 100%、业务成功率 ≥ 95%、P50 ≤ 8 s / P95 ≤ 15 s / P99 ≤ 25 s、无降级文案导致的业务失败、长稳剔除预热后 RSS 斜率 ≤ 256 KiB/min 且总增长 ≤ 5%，并且与基线相比 P95 劣化不超过 20%、业务成功率不得下降。

## 设计要点

- **关闭思考模式**：分类/抽取/工具选择都是短决策，请求带 `extra_body={"thinking": {"type": "disabled"}}`，把单次决策从几十秒压到 1–3 秒。
- **规则优先路由**：意图先由正则规则判定，命中即跳过 LLM；未命中才用一次调用同时输出意图与订单号/商品编号，替代原来的「分类 + 抽取」两次串行调用。
- **图内并行**：信息完整后经 `fanout` 空节点同时触发政策检索与工具调用，再由 `policy_check` 汇聚，缩短关键路径且不改变状态语义。
- **结构化抽取双保险**：`response_format={"type":"json_object"}` 约束 JSON 输出，正则兜底订单号；缺失字段由确定性规则计算，不信任模型自报。
- **工具早停**：物流查询拿到 `query_logistics` 即停；退款/换货/理赔拿到订单金额即停，避免无意义的工具循环；投诉类不走工具，直接风控转人工。
- **检索可降级**：Embedding 超时（连接 3 s / 读取 10 s，应用级 8 s）或 Rerank 失败后熔断 60 秒；熔断、半开探测与并发闸门都让请求立即走本地 bigram 关键词检索，语料与结果分别有 10 分钟 / 5 分钟缓存，不会整条链路失败或每条请求都等超时。
- **同步 IO 隔离**：Chroma 查询、Embedding、Rerank、SQLite 读写全部移入有上限的线程池；SQLite 开启 WAL、`busy_timeout` 与会话索引，连接在线程内复用，避免高并发下锁等待并阻塞事件循环。
- **规则与 LLM 解耦**：金额、人工转接等关键决策由 `PolicyEngine` 计算，模型只负责表达。
- **可观测**：节点、LLM token 用量、工具耗时统一写入日志；检索依赖状态通过 `/api/metrics` 与 `done` 事件对外暴露，便于定位慢点与依赖抖动。

## 局限与 Roadmap

- 目前使用 SQLite 模拟订单/物流，生产环境需替换为真实订单中台接口。
- 会话历史仅保留最近 6 轮，未做长期记忆与用户画像。
- FAQ 为关键词匹配，后续可改为语义检索。
- 可扩展：多轮槽位补全、工单异步处理、人工坐席实时接管、LangGraph checkpoint 持久化。

## License

[MIT](LICENSE)
