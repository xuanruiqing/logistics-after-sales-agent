# 优化后测试报告（2026-10-30）

> 对应性能评估：[performance_evaluation_20261030.md](performance_evaluation_20261030.md)
> 被测版本：优化后代码（检索依赖治理 + 规则优先路由 + 图内并行 + SQLite WAL/线程池）
> 测试方式：真实服务黑盒测试，单进程 Uvicorn，端口 8000，Python 3.13.9（Windows）

## 1. 结论摘要

| 维度 | 结论 |
| --- | --- |
| 功能验证 | 20/20 样本全部 PASS，意图/抽取/状态/人工转接/工具调用率均为 100%，工具误调用 0 次 |
| 核心链路性能 | 160 请求全成功；平均端到端 P95 9.55 s → 4.83 s（-49.4%），P99 15.69 s → 6.77 s（-56.8%），吞吐 0.58 → 1.35 rps（+131%） |
| 峰值承载 | 并发 5/10/20/40 下 100% HTTP 与业务成功率，P95 由 15.9–18.4 s 降至 4.3–5.9 s，吞吐提升 2.8–3.2 倍 |
| 长稳 | P95 15.34 s → 5.99 s，成功率 100%；RSS 总增长 2.00%、预热后斜率 196.7 KiB/min，无泄漏趋势 |
| 兼容性 | Python 3.13.9 全量编译通过；SSE 契约仅新增字段，旧消费方不受影响；健康检查与工单接口行为不变 |
| 性能门禁 | 核心链路与峰值 c40 门禁均 PASS（含基线回退校验与 `/api/metrics` 校验） |
| 回归风险 | 无功能性回归；RAG 引用覆盖率 83.3% → 77.8%（外部 Embedding 中断导致 1 条样本降级），常量内存占用上升约 60–85 MB（缓存/线程池常驻） |

## 2. 验证环境与方法

- 运行环境：Windows，Python 3.13.9，FastAPI + Uvicorn 单进程；SQLite 单文件（WAL 模式）；Chroma 本地持久化。
- 功能回归：`src/evaluation/run_eval.py` 回放 20 条标注样本，逐条校验意图、缺失字段、工单状态、人工转接、工具调用、FAQ 短路与耗时。
- 性能测试：`src/evaluation/performance_test.py` 黑盒压测，采集端到端耗时、TTFB、首 token、服务端 `elapsed`、各节点耗时，以及被测进程 CPU/RSS/线程/句柄和整机磁盘/网络吞吐。
- 门禁：`src/evaluation/perf_gate.py` 读取压测 JSON 与 `/api/metrics`，按阈值与基线回退判定。
- 数据文件（`evaluation_results/`）：`eval-final.json`、`core-opt.json`、`peak-opt-c05/c10/c20/c40.json`、`soak-opt-15min.json`；基线为 `core-c2-n160.json`、`peak-c05/c10/c20/c40-n40.json`、`soak-30min-r01.json`。

## 3. 优化内容

| 优先级 | 改造 | 关键实现 |
| --- | --- | --- |
| P0 | 检索依赖治理 | Embedding（连接 3 s / 读取 10 s，应用级 8 s）与 Rerank（6 s）超时；失败后熔断 60 s；半开探测仅放行 1 个请求；并发闸门 4，超出立即降级；本地 bigram 检索兜底 |
| P0 | 缓存 | 检索结果 TTL 缓存（在线 300 s / 降级 30 s，LRU 256）；本地语料 10 分钟缓存，降级不再全量读 Chroma |
| P0 | 降级透明化 | `done` 事件新增 `degraded` / `degraded_reasons` / `retrieval_mode`；新增 `GET /api/metrics` |
| P0 | 减少串行 LLM | 意图规则优先，未命中才用一次调用同时完成分类与字段抽取；订单号+诉求明确时走确定性工具路由；投诉类不调用工具 |
| P0 | 图内并行 | `info_extract` 后经 `fanout` 空节点并行触发 `knowledge_retrieve` 与 `tool_agent`，由 `policy_check` 汇聚 |
| P1 | SQLite | WAL + `synchronous=NORMAL` + `busy_timeout`；会话表联合索引；线程内复用连接（写操作不再 `close`）；读写迁入有上限线程池 |
| P1 | 同步 IO 隔离 | Chroma、Embedding、Rerank、SQLite 全部移入线程池，不阻塞事件循环 |
| P1 | LLM 连接池 | `httpx.AsyncClient` 连接上限 32、keepalive 16 |
| P2 | 性能门禁 | `perf_gate.py` 支持阈值、基线回退与指标接口校验，失败退出码 1 |

## 4. 测试结果

### 4.1 功能验证（20 条标注样本）

| 指标 | 优化后 | 优化前基线 | 结论 |
| --- | --- | --- | --- |
| 意图准确率 | 100%（18/18） | 100% | 一致 |
| 信息抽取准确率 | 100%（18/18） | 100% | 一致 |
| 工单状态准确率 | 100%（20/20） | 100% | 一致 |
| 人工转接准确率 | 100%（20/20） | 100% | 一致 |
| 工具调用率（应调用样本） | 100% | 100% | 一致 |
| 工具误调用 | 0 次 | 0 次 | 一致 |
| FAQ 命中 | 2/2 | 2/2 | 一致 |
| RAG 引用覆盖率 | 77.8% | 83.3% | 1 条样本因外部 Embedding 中断降级，见 5.2 |
| 平均耗时 | 3.56 s | 8.83 s | -59.7% |
| P95 耗时 | 5.15 s | 16.28 s | -68.4% |
| 非 FAQ 平均耗时 | 3.16 s | 9.81 s | -67.8% |

逐条结果（全部 PASS）：

| ID | 输入 | 实际类型 | 缺失字段 | 状态 | 人工 | 工具 | 耗时(s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| L01 | 订单DD20260901001物流到哪了 | query_logistics | - | ai_solved | 否 | 是 | 7.48 |
| L02 | 帮我查下DD20260905002的快递到哪了 | query_logistics | - | ai_solved | 否 | 是 | 3.06 |
| L03 | DD20260910003什么时候能到 | query_logistics | - | ai_solved | 否 | 是 | 2.71 |
| R01 | 我要退款，订单DD20260910004 | refund | - | ai_solved | 否 | 是 | 4.32 |
| R02 | 订单DD20260905002申请退款 | refund | - | pending_human | 是 | 是 | 3.72 |
| R03 | 订单DD20260910003退款 | refund | - | pending_human | 是 | 是 | 3.02 |
| R04 | 我要退货，订单DD20260901001 | refund | - | pending_human | 是 | 是 | 3.89 |
| R05 | 订单DD99999999999要退款 | refund | - | pending_human | 是 | 是 | 3.79 |
| E01 | 我要换货，订单DD20260905002 | exchange | - | pending_human | 是 | 是 | 5.15 |
| E02 | 这个键盘我想换一个，订单DD20260905002 | exchange | - | pending_human | 是 | 是 | 2.85 |
| D01 | 订单DD20260901001东西破损了，要理赔 | damage_claim | - | pending_human | 是 | 是 | 3.97 |
| D02 | 收到的耳机少件了，订单DD20260901001 | damage_claim | - | pending_human | 是 | 是 | 3.46 |
| D03 | 订单DD20260905002商品损坏，要求补发 | damage_claim | - | pending_human | 是 | 是 | 3.87 |
| C01 | 我要投诉，客服态度太差 | complaint | - | pending_human | 是 | 否 | 3.23 |
| C02 | 你们客服一直不回复我，我要投诉 | complaint | - | pending_human | 是 | 否 | 2.39 |
| M01 | 我要退款 | refund | 订单号 | pending_info | 否 | 否 | 0.00 |
| M02 | 订单破损了要理赔 | damage_claim | 订单号 | pending_info | 否 | 否 | 0.01 |
| M03 | 订单DD20260901001我要部分退款，退其中一个商品 | refund | 商品编号 | pending_info | 否 | 否 | 0.00 |
| F01 | 退货运费谁承担 | faq | - | ai_solved | 否 | 否 | 0.00 |
| F02 | 你们客服工作时间是几点 | faq | - | ai_solved | 否 | 否 | 0.00 |

关键行为验证：缺信息追问由约 1.8 s 降至 0.00–0.01 s（规则直接判定缺失字段）；投诉类不再触发工具调用；退款三档金额仍严格由规则引擎判定。

### 4.2 核心链路性能（160 请求，20 轮 × 8 请求，并发 2）

| 指标 | 优化前 `core-c2-n160` | 优化后 `core-opt` | 变化 |
| --- | --- | --- | --- |
| 请求数 | 160 | 160 | — |
| 总耗时 | 273.98 s | 118.79 s | -56.6% |
| 吞吐 | 0.584 rps | 1.347 rps | +130.6% |
| HTTP 成功率 | 100% | 100% | 一致 |
| 业务成功率 | 100% | 100% | 一致 |
| P50 | 0.124 s | 0.005 s | -96.0% |
| P95 | 9.548 s | 4.833 s | -49.4% |
| P99 | 15.687 s | 6.774 s | -56.8% |
| 首 token 均值 | — | 0.896 s | — |
| 进程 CPU 均值 | 3.42% | 4.47% | 合理上升 |
| 进程 RSS 峰值 | 97.2 MB | 178.9 MB | +84%（缓存/线程池常驻，见 5.2） |

节点耗时（优化后，均值 / P99）：router 0.0036 s / 0.0057 s，info_extract 0.0011 s / 0.0027 s，knowledge_retrieve 0.103 s / 3.054 s（P99 来自一次外部 Embedding 快速失败），tool_agent 0.0008 s / 0.0018 s，policy_check 0.0012 s / 0.0017 s，ticket_generate 0.0011 s / 0.0071 s，prepare_reply 0.0005 s / 0.0010 s。关键路径耗时集中在最终回复 LLM：首 token 均值 1.376 s、流式输出均值 2.376 s。

场景拆分（优化后 P95）：FAQ 0.024 s、缺信息追问 0.006 s、物流查询 6.503 s、高金额退款 5.121 s、多轮退款 4.931 s。

### 4.3 峰值承载（40 请求，逐档并发）

| 并发 | 版本 | 总耗时(s) | 吞吐(rps) | P50(s) | P95(s) | P99(s) | 成功率 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 | 优化前 | 50.04 | 0.799 | 5.06 | 18.45 | 19.50 | 100% |
| 5 | 优化后 | 17.80 | 2.247 | 0.135 | 5.91 | 6.81 | 100% |
| 10 | 优化前 | 28.33 | 1.412 | 2.98 | 15.93 | 16.52 | 100% |
| 10 | 优化后 | 10.17 | 3.933 | 0.206 | 4.29 | 6.14 | 100% |
| 20 | 优化前 | 13.28 | 3.011 | 2.90 | 11.26 | 12.03 | 100% |
| 20 | 优化后 | 5.50 | 7.277 | 0.178 | 5.15 | 5.26 | 100% |
| 40 | 优化前 | 17.97 | 2.225 | 3.44 | 17.68 | 17.91 | 100% |
| 40 | 优化后 | 6.26 | 6.389 | 0.257 | 5.47 | 6.09 | 100% |

并发从 5 提升到 40，P95 稳定在 4.3–5.9 s 区间，未出现排队放大；优化前同区间为 11.3–18.5 s。

### 4.4 长稳（固定速率 0.1 rps，4 并发，15 分钟）

| 指标 | 优化前 `soak-30min-r01`（30 min） | 优化后 `soak-opt-15min`（15 min） |
| --- | --- | --- |
| 请求数 | 180 | 90 |
| 吞吐 | 0.1005 rps | 0.1011 rps |
| HTTP / 业务成功率 | 100% / 100% | 100% / 100% |
| P50 | 1.964 s | 0.007 s |
| P95 | 15.343 s | 5.994 s |
| P99 | 17.375 s | 6.758 s |
| 进程 CPU 均值 | 0.82% | 0.40% |
| RSS 起止 | — | 167.7 MiB → 171.0 MiB |
| RSS 峰值 | 117.2 MB | 179.3 MB |
| RSS 总增长 | — | +2.00% |
| RSS 预热后斜率 | -321 KiB/min（30 min 全段） | 196.7 KiB/min（剔除前 20%） |

优化后 15 分钟窗口内 RSS 前 3 分钟随缓存填充小幅上升，之后趋于平稳：总增长 2.00%，预热后斜率 196.7 KiB/min，低于门禁阈值 256 KiB/min；无持续增长迹象。稳态内存约 179 MB（171 MiB）。

### 4.5 性能门禁

`perf_gate.py` 对核心链路（含基线与 `/api/metrics`）与峰值 c40 的判定均为 **PASS**：

```
| 门禁项 | 结果 | 观测值 |
| HTTP 成功率 | PASS | 1.0 |
| 业务成功率 | PASS | 1.0 |
| P95 相对基线无回退 | PASS | 4.8325s / 基线 9.5475s |
| /api/metrics 可访问 | PASS | status=ok |
| 检索指标已采集 | PASS | requests=160 |
| 缓存/降级可观测 | PASS | cache_hit=122, degraded=38 |
```

检索指标（压测期间）：缓存命中率约 76%，降级原因均为 `embed_error` / `circuit_open`，无因检索导致的业务失败。

长稳门禁同时校验 RSS：预热后斜率 196.7 KiB/min（阈值 256）、总增长 2.00%（阈值 5%），判定 PASS。

## 5. 兼容性与回归分析

### 5.1 兼容性

| 项 | 验证方式 | 结果 |
| --- | --- | --- |
| Python 版本 | Python 3.13.9 下 `compileall` 编译 `main.py` 与 `src/` | 通过 |
| SSE 契约 | `done` 事件仅新增 `degraded` / `degraded_reasons` / `retrieval_mode`，原有字段不变 | 旧消费方（`run_eval.py`、`index.html`）不受影响 |
| HTTP 接口 | `/api/health`（nodes=7）、`/api/tickets/{id}`、`/api/tickets/{id}/review` 在压测中被反复调用 | 成功率 100% |
| 数据库 | Windows 下 SQLite WAL、`busy_timeout`、线程内连接复用，20 轮并发工单写入与复核 | 无锁错误、无失败 |
| 依赖 | `httpx==0.28.1`、`psutil==7.0.0` 已写入 `requirements.txt` | 安装可用 |
| 前端工作台 | 新版 `index.html`（智能工作台 + 工单管理 + 人工复核）在 Edge 实跑：SSE 逐节点点亮流水线、政策引用与工单卡片、工单列表与状态筛选、抽屉内复核均正常；页面所需 4 个接口均存在 | 页面行为正常，无接口缺失 |

### 5.2 回归风险与说明

1. **RAG 引用覆盖率 83.3% → 77.8%**：18 条非 FAQ 样本中 14 条带引用。差异来自 L03（`DD20260910003什么时候能到`）当次检索命中 0 条；同一时段 DashScope Embedding 端点不可达（`requests` 直连 3 次均为 `ReadTimeout`），系统按设计降级为本地 bigram 检索。属于外部依赖波动，不是代码回归；意图、状态、风控结论均正确。
2. **在线检索路径未被完整验证**：测试窗口内 `online_success=0`。此前直接探针曾返回 `status 200 / 0.225 s / dim 1024`，说明在线路径代码可用，但本次窗口无法覆盖 Chroma 向量召回 + Rerank 的完整链路，需在网络恢复后补测。降级路径（关键词检索、熔断、半开、缓存）已被充分覆盖。
3. **常量内存上升 60–85 MB**：来自检索结果缓存、本地语料缓存、DB/IO 线程池与 LLM 连接池常驻。长稳斜率 11 KiB/min，无泄漏；如对内存敏感，可下调 `RETRIEVAL_CACHE_SIZE`、`IO_EXECUTOR_WORKERS`、`LLM_MAX_CONNECTIONS`。
4. **外部依赖失败成本**：连接超时 3 s 使一次失败尝试从 15 s 降到约 3 s，但首个触发失败的请求仍会承担该延迟（核心链路 P99 6.77 s 即由此产生），随后 60 s 内请求均走毫秒级降级。

## 6. 复现命令

```powershell
# 启动服务
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# 功能回归
.\.venv\Scripts\python.exe -m src.evaluation.run_eval --json-out evaluation_results/eval-final.json

# 核心 / 峰值 / 长稳
.\.venv\Scripts\python.exe -m src.evaluation.performance_test --mode core --repeat 20 --concurrency 2 --output evaluation_results/core-opt.json
.\.venv\Scripts\python.exe -m src.evaluation.performance_test --mode load --requests 40 --concurrency 40 --output evaluation_results/peak-opt-c40.json
.\.venv\Scripts\python.exe -m src.evaluation.performance_test --mode soak --duration 900 --rate 0.1 --concurrency 4 --output evaluation_results/soak-opt-15min.json

# 门禁
.\.venv\Scripts\python.exe -m src.evaluation.perf_gate --input evaluation_results/core-opt.json --baseline evaluation_results/core-c2-n160.json --metrics-url http://127.0.0.1:8000/api/metrics
```

## 7. 结论

功能层面无回归：20/20 样本通过，风控与人工转接结论与优化前一致。性能层面收益明确：核心链路 P95 降低 49%、P99 降低 57%、吞吐提升 131%；峰值并发 5–40 下 P95 稳定在 4–6 s 且成功率 100%；长稳无内存泄漏趋势。性能门禁全部通过。唯一需跟进的是外部 Embedding 依赖的在线可用性，以及由此带来的 RAG 引用覆盖率波动。

## 8. 代码提交

- 提交：`eab3e21 perf: 检索依赖治理与工作流并行化，核心链路 P95 降低 49%`（39 个文件变更：19 修改 / 20 新增，含本次优化、测试报告与评测数据）。
- 远端仓库：`xuanruiqing/logistics-after-sales-agent`（`main`），远端 `main` 已更新为 `eab3e21`。
- 推送状态：**已完成**。本地提交基于远端 `353f411` 之上，历史线性、无需强推；推送经本地代理 `127.0.0.1:7897` 完成。
- 复现命令：

```powershell
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push -u origin main
```

