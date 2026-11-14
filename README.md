# ScholarFlow · Knowledge Research Agent

基于 **LangGraph** 的 Agentic RAG 知识库助手：输入一个研究问题，系统自动完成**多源检索、质量评估、Self-RAG 反思重查和带引用溯源的研究报告生成**。

![License](https://img.shields.io/badge/License-MIT-purple.svg)
![Python](https://img.shields.io/badge/Python-3.11-blue.svg)
![Next.js](https://img.shields.io/badge/Next.js-15-black.svg)
![LangGraph](https://img.shields.io/badge/LangGraph-StateGraph-orange.svg)
![Qdrant](https://img.shields.io/badge/Qdrant-vector--db-ff3f59.svg)

> 仓库：<https://github.com/MH-Ovo/ScholarFlow>
>
> 主分支：`main`

---

## 一、项目简介

ScholarFlow 面向“需要读论文、查资料、写综述”的研究场景，把一次调研拆成可观测的 Agent 工作流：

- **本地知识库**：PDF / DOCX / MD / TXT 上传后自动解析、切片、向量化；
- **外部学术源**：arXiv 论文、知网（CNKI）文献；
- **网页源**：DeepSeek Search；
- **Self-RAG**：Grader 低分时触发 Query Rewrite 与重新检索，最多 2 轮；
- **引用溯源**：报告中的 `[n]` 可以回溯到来源、chunk 和页码；
- **可观测**：节点耗时、token、步骤状态通过 SSE、本地 JSONL trace 和 Langfuse 记录；
- **可评测**：30 条学术评估集 + RAGAS + 段落/页码命中率 + 引用回溯率。

没有 API Key 时，系统仍可用 Mock 模式跑通完整演示链路；但外部检索和真实报告质量需要配置对应 Key。

---

## 二、核心能力

| 能力 | 说明 |
|---|---|
| **Agentic RAG** | 基于 LangGraph `StateGraph` 的 Planner → 多源检索 → 融合精排 → Grader → Rewrite → Report 工作流 |
| **SendAPI 动态路由** | 按 Planner 的 `plan` 只分发 `kb / paper / web / cnki` 中实际需要的源，替代固定四路扇出 |
| **混合检索** | bge-m3 dense（1024 维）+ sparse 词法权重，RRF 融合 + bge-reranker 精排 |
| **查询增强** | LLM 改写 + 子查询 + HyDE；无命中时回退原问题，避免“改写把正确答案改没” |
| **Self-RAG** | Grader 高分直接生成；低分触发 Rewrite & Re-search，最多 2 轮；第 2 轮自动 escalation |
| **多源接入** | 统一 `SourceResult(status/items/detail)`；arXiv / Web / CNKI 适配器；单源失败不中断任务 |
| **PDF 结构解析** | PyMuPDF + GROBID：章节/段落、页码标记、parent-child 切片、扫描页 OCR |
| **引用溯源** | 来源卡片 → chunk 全文 → 页码；PDF 引用页码回溯率 94.9%（当前 20 条端到端报告） |
| **流式交互** | SSE 推送步骤状态、来源增量、Grader 评分、报告 token；支持断线重连 |
| **多轮追问** | 追问重跑流水线，最近 8 条消息进入 Planner/报告，保留历史报告版本与逐句引用 |
| **报告导出** | Markdown / Word / PDF 服务端生成，Word 与 PDF 版式一致 |
| **只读分享** | 生成带过期时间的公开链接，任何人可读报告快照，无需登录 |
| **可观测** | 本地 trace JSONL + Langfuse 导出；`/health` 返回当前 LLM 模式与模型名 |
| **可评测** | RAGAS ContextRecall / Faithfulness、段落命中率、引用回溯、动态路由 A/B、故障注入 |

---

## 三、架构与流程

```text
用户提问
   │
   ▼
Router / Planner（chat | knowledge；逐源规划 kb/paper/web/cnki）
   │
   ├─ chat ──────────────────────────────► Report
   │
   ├─ 仅 kb ──► KB 检索 ──命中──► Merger
   │                    └─未命中──► 外部源升级（paper/web/cnki）
   │
   └─ 多源 ──► SendAPI 动态分发 ──┬─► KB（Qdrant）
                                  ├─► arXiv
                                  ├─► Web（DeepSeek Search）
                                  └─► CNKI（MCP）
                                            │
                                            ▼
                              Merger：去重 + RRF + bge-reranker
                                            │
                                            ▼
                              Grader：逐条相关性评分
                                 ├─ 高分 ─────────► Report
                                 ├─ 低分 & retry<2 ─► Rewrite → 重新检索
                                 └─ 重试耗尽 ──────► Report（说明证据不足）
                                            │
                                            ▼
                          Report Writer：强制 [n] 引用 + 引用核查
                                            │
                                            ▼
                          SSE 流式报告 + 来源卡片 + 页码追溯
```

### 关键设计

- **Planner 逐源规划**：不再固定三路/四路全开；不需要的来源不调度。
- **单源故障隔离**：超时、限流、认证失败只影响该源，其他源和报告继续。
- **页码感知精排**：有 `page_nos` 的切片获得轻量加分，优先返回可回溯原文的段落。
- **表格题确定性抽取**：命中 `实体 [n]` 表格行时直接生成带引用的短答，避免 LLM 限流把答案退化成整段摘录。
- **可注册源**：新增源实现节点并注册 `SourceSpec`，graph 自动加节点和 Send 目标；merger 仍需合并结果字段。

---

## 四、技术栈

| 层 | 技术 |
|---|---|
| 前端 | Next.js 15、React 19、TypeScript、Tailwind CSS、Zustand、React Flow |
| 后端 | FastAPI、SQLAlchemy Async、SQLite（开发）/ PostgreSQL（可选） |
| Agent | LangGraph StateGraph、SendAPI、Self-RAG |
| 向量库 | Qdrant；bge-m3 dense + sparse；bge-reranker-v2-m3 |
| 文档解析 | PyMuPDF、pypdf、python-docx、RapidOCR、GROBID、可选 VLM |
| LLM | 任意 OpenAI 兼容接口；当前示例为商汤 SenseNova `deepseek-flash`；DeepSeek key 仅用于 Web Search |
| 观测 | 本地 JSONL trace、Langfuse |
| 评测 | RAGAS、自建 ContextRecall / Faithfulness / 引用回溯 / 路由 A/B / 故障注入脚本 |

---

## 五、快速开始

### 1. 环境要求

- Python 3.11
- Node.js 18+
- Docker（可选，用于 Qdrant / GROBID）
- 推荐 16GB+ 内存；bge-m3 + reranker 首次加载较慢

### 2. 克隆与安装

```bash
git clone https://github.com/MH-Ovo/ScholarFlow.git
cd ScholarFlow
```

后端：

```bash
cd apps/api
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

python -m pip install -r requirements.txt
```

前端：

```bash
cd apps/web
npm install
```

### 3. 启动 Qdrant

```bash
docker compose -f infra/docker-compose.yml up -d qdrant
```

Windows 也可以直接使用项目脚本：

```powershell
powershell -ExecutionPolicy Bypass -File infra/ensure-qdrant.ps1
```

默认地址：`http://127.0.0.1:6333`。

### 4. 下载本地模型（约 4.4GB）

```bash
cd apps/api
python scripts/download_models.py
```

模型默认落在项目 `models/` 目录（该目录不提交到 Git）：

- `BAAI/bge-m3`
- `BAAI/bge-reranker-v2-m3`

### 5. 配置 `.env`

```bash
cp apps/api/.env.example apps/api/.env
```

Windows PowerShell：

```powershell
Copy-Item apps/api\.env.example apps/api\.env
```

最小配置示例：

```ini
# 对话 / Planner / Grader / 报告生成（任意 OpenAI 兼容端点）
LLM_API_KEY=你的商汤-key
LLM_BASE_URL=https://token.sensenova.cn/v1
LLM_MODEL=deepseek-flash

# Web Search 专用；与对话模型解耦
DEEPSEEK_API_KEY=你的-deepseek-key

# 本地知识库
QDRANT_URL=http://127.0.0.1:6333

# PDF 解析：auto = 优先 GROBID，失败回退 PyMuPDF
PARSER_MODE=auto
GROBID_URL=http://127.0.0.1:8070

# 知网（可选）
CNKI_MODE=auto
CNKI_ENABLED=true
CNKI_FORCE=false
```

> 不要把 `.env` 提交到 Git。没有 Key 也能启动，系统会进入 Mock/摘录兜底模式。

### 6. 启动 GROBID（需要章节结构/页码映射时）

```bash
docker run -d --name grobid -p 8070:8070 lfoppiano/grobid:0.8.2
curl http://127.0.0.1:8070/api/isalive
```

如果本机拉取 Docker Hub 较慢，可使用镜像站：

```bash
docker run -d --name grobid -p 8070:8070 docker.1ms.run/lfoppiano/grobid:0.8.2
```

GROBID 不可用时，`PARSER_MODE=auto` 会自动回退 PyMuPDF；不会阻塞入库。

### 7. 启动后端

```bash
cd apps/api
python -m uvicorn app.main:app --host 0.0.0.0 --port 8900
```

- API 文档：<http://127.0.0.1:8900/docs>
- 健康检查：<http://127.0.0.1:8900/health>

### 8. 启动前端

```bash
cd apps/web
npm run dev
```

打开 <http://localhost:5173/>。

Windows 一键启动：

```powershell
.\start-dev.ps1
```

---

## 六、使用方式

1. 在首页输入研究问题，例如：
   - `请综述 RAG 在知识密集型任务中的进展`
   - `对比 GraphRAG 与普通 RAG 的异同`
   - `RAG 的检索粒度有哪些方案？`
2. 观察 Agent 步骤：规划 → 检索 → 融合 → 评分 → 改写 → 报告。
3. 在右侧 Sources 面板点击 `[n]` 引用，查看来源、chunk 全文和页码。
4. 在 Knowledge Base 页上传 PDF/DOCX/MD/TXT，系统自动解析、切片、向量化。
5. 追问会重跑流水线，保留历史报告版本。

---

## 七、评测与观测

### 7.1 学术评估集

- 英文 30 条，其中 2 条 `unanswerable` 不参与评分；
- 有效样本：**28 条**；
- 参考指标：ContextRecall（检索层）与 Faithfulness（生成层）。

当前记录（商汤 `deepseek-flash` 裁判，**140 篇 / 5429 切片**）：

| 指标 | 结果 | 目标 |
|---|---|---|
| ContextRecall（检索层） | **0.8129**（n=28） | ≥ 0.79 |
| Faithfulness（生成层） | **0.9493**（n=27，3098 切片语料） | ≥ 0.91 |
| Langfuse 上报 | **470 条事件，`auth_check=True`** | 完成 |

> 重索引后检索层复测 0.8129（3098 → 5429 切片）；Faithfulness 是重索引前 3098 切片语料上的结果，生成层尚未在 5429 切片上复测。

### 7.2 段落 / 页码级检索

脚本：`eval/scripts/eval_paragraph_hit.py`

当前 140 篇 / 3098 切片（其中 130 篇走 GROBID）：

| K | chunk-ID 命中 | 页码命中 |
|---|---|---|
| 3 | 75.0% | **92.9%** |
| 4 | 78.6% | 92.9% |
| 5 | 78.6% | 92.9% |
| 8 | 82.1% | 92.9% |

top-3 相对 chunk-ID 提升 **17.9 个百分点（约 18%）**。

### 7.3 引用回溯

脚本：`eval/scripts/eval_citation_traceback.py`

20 条端到端报告、269 条引用：

| 指标 | 结果 |
|---|---|
| ref 有效 | 100% |
| PDF 引用页码可定位 | **94.9%** |
| PDF 引用“页码 + 句内重合” | 82.5% |
| 全来源逐句回溯 | 71.8% |

> 92% 目标采用“PDF 引用页码回溯”口径；web/知网摘要没有页码，单独统计。

### 7.4 动态路由与故障注入

```bash
# SendAPI 动态路由 vs 固定扇出（8 条 query，Mock LLM）
python eval/scripts/eval_dynamic_routing.py --limit 8

# 单源故障注入（适配器级）
python eval/scripts/eval_source_resilience.py --reps 10

# 完整图任务级故障注入（需要 Qdrant）
python eval/scripts/eval_source_resilience_task.py --reps 2
```

已记录结果：

- 外部源调度：`1.75 / 3.00 = -41.7%`（8 条 query，Mock LLM，调度代理指标）；
- 适配器级故障注入：150 次，结构化错误率 / 降级成功率 100%；
- 任务级故障注入：30/30 完成（Mock LLM，受控注入）。

### 7.5 运行 RAGAS

```bash
cd apps/api
python -m pip install -r requirements-eval.txt

# 检索层
python eval/scripts/eval_ragas_academic.py --skip-generation --tag retrieval

# 生成层（需要后端运行）
python eval/scripts/eval_ragas_academic.py --skip-retrieval --tag generation

# 复用已有报告，只重评分
python eval/scripts/eval_ragas_academic.py \
  --skip-retrieval \
  --reuse-generation eval/results/ragas_generation_xxx.json \
  --sequential --sleep 2 \
  --tag generation_rescore
```

### 7.6 观测

```bash
# 查看本地 trace
python eval/scripts/show_trace.py --date 20261110

# 上报 Langfuse（需要 LANGFUSE_*）
python eval/scripts/export_langfuse.py --date 20261110
```

本地 trace 默认写入 `eval/results/traces/YYYYMMDD.jsonl`，即使 Langfuse 不可用也不会丢数据。

---

## 八、项目结构

```text
.
├── apps/
│   ├── api/                         # FastAPI 后端
│   │   ├── app/
│   │   │   ├── agents/              # LangGraph: graph / nodes / state / prompts / source_registry
│   │   │   ├── rag/                 # parsers / grobid_parser / chunker / models / vector_store
│   │   │   ├── integrations/        # SourceResult / adapters / arXiv / Web / CNKI
│   │   │   ├── llm/                 # OpenAI 兼容网关 + Mock
│   │   │   ├── services/            # 任务生命周期 / 入库 / trace
│   │   │   ├── api/v1/              # REST + SSE
│   │   │   └── models/              # ORM
│   │   ├── scripts/                 # 模型下载等工具
│   │   └── requirements*.txt
│   └── web/                         # Next.js 15 前端
│       └── src/
│           ├── components/          # research / sources / kb / graph / stats
│           ├── features/            # researchStore + SSE hook
│           └── app/                 # App Router 页面
├── eval/
│   ├── dataset_scholarflow.json     # 30 条英文学术评估集
│   ├── dataset_scholarflow_cn.json  # 中文评估集
│   ├── scripts/                     # RAGAS / 召回 / 引用 / 路由 / 故障注入
│   └── results/                     # 评测产物（大部分被 .gitignore 忽略）
├── infra/                           # Qdrant / docker-compose / 启动脚本
├── docs/                            # 架构图 / 截图
├── word/                            # 项目文档与评测记录（本地）
├── data/                            # 本地 PDF 语料（不提交）
├── sample-data/                     # 演示文档
├── start-dev.ps1 / start-dev.bat
└── README.md
```

---

## 九、API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/research` | 创建研究任务 |
| `GET` | `/api/v1/research/{id}/stream` | SSE：步骤 / 来源 / Grader / 报告 token |
| `GET` | `/api/v1/research/{id}` | 任务详情、报告版本、来源、统计 |
| `POST` | `/api/v1/research/{id}/followup` | 追问 |
| `POST/GET/DELETE` | `/api/v1/documents` | 知识库文档管理 |
| `GET` | `/api/v1/kb/chunk` | 引用溯源：chunk 全文 |
| `GET` | `/api/v1/kb/stats` / `/api/v1/sources/stats` | 知识库/来源统计 |
| `GET` | `/docs` | OpenAPI 文档 |

---

## 十、已知限制

1. **知网 A/B 未闭环**
   - 开关与命令已就绪，但 treatment 10 条中 9 条 CNKI MCP `unreachable` / `operation_timeout`；
   - 只有 1 条真正拿到知网来源，表面指标变化不能归因于知网；
   - 需要先修 CNKI MCP 稳定性/任务间隔，再跑两臂对比。

2. **解析器覆盖不完整**
   - 当前 140 篇文档 / 5429 切片，历史截断文档已全部重建（`is_partial=0`）；
   - 10 篇旧文档仍使用 PyMuPDF 解析，未回溯 GROBID 章节结构。

3. **引用回溯有两个口径**
   - PDF 页码回溯 94.9% 达标；
   - 全来源逐句回溯 71.8%，未达 92%；主要受 web/知网摘要无页码、句内 claim 不完全一致影响。

4. **重索引后生成层尚未复测**
   - 检索层已在 140 篇 / 5429 切片复测：**0.8129**（目标 0.79，旧基线 0.7140）；
   - Faithfulness 0.9493 是 3098 切片语料上的结果；重索引与来源元数据提示词改动后尚未重跑生成层。

5. **故障注入是受控实验**
   - 100% 降级成功率来自适配器层可控注入，不代表真实公网/知网限流下的长跑结果。

---

## 十一、路线图

- [x] Agentic RAG 核心：多源检索 + Self-RAG + 引用溯源
- [x] 智能 PDF 解析：OCR + VLM 图表描述
- [x] GROBID 章节结构 + parent-child + 页码映射
- [x] SendAPI 动态多源路由 + 源注册表
- [x] SourceResult / 适配器 / 单源故障注入
- [x] RAGAS 学术评估集 + Langfuse 观测
- [ ] 知网 A/B 稳定性修复与两臂重跑
- [x] 140 篇知识库上的 RAGAS 新基线
- [ ] 全来源引用回溯 92%
- [ ] 表格结构化解析（Docling / PP-Structure）
- [ ] 企业能力：JWT/SSO、RBAC、审计日志
- [ ] 生产部署：PostgreSQL / Redis / Celery

---

## 十二、许可证

- 代码：MIT，见 [LICENSE](LICENSE)
- 模型：bge-m3 / bge-reranker-v2-m3（BAAI，MIT）；RapidOCR（Apache 2.0）
- 第三方服务：DeepSeek、商汤 SenseNova、CNKI MCP 等需要自己申请 Key / 遵守服务条款

---

## 十三、致谢

- [LangGraph](https://www.langchain.com/langgraph)
- [Qdrant](https://qdrant.tech/)
- [BAAI bge-m3](https://huggingface.co/BAAI/bge-m3)
- [GROBID](https://github.com/kermitt2/grobid)
- [RAGAS](https://github.com/explodinggradients/ragas)
- [Next.js](https://nextjs.org/)
- [FastAPI](https://fastapi.tiangolo.com/)
