"""全局配置：从 apps/api/.env 读取，全部支持环境变量覆盖。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # apps/api/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # DeepSeek：保留给 Web/知网 tool search（/v1/tools/search）使用
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # 对话模型（任意 OpenAI 兼容端点：商汤 SenseNova / Qwen / GLM / Ollama 等）。
    # 留空 = 回退 DeepSeek，保持旧行为。
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    # Web 搜索
    tavily_api_key: str = ""

    # 单源故障注入（评测/演练用；默认关闭）
    fault_inject_source: str = ""       # arxiv,web,cnki 或 *
    fault_inject_mode: str = "timeout"  # timeout|rate_limit|auth|error|empty|unreachable
    fault_inject_rate: float = 1.0      # 1.0=必现；<1 按概率触发

    # 知网检索：CNKI-Enhanced-MCP（stdio 子进程），不可用时降级为 site:cnki.net 搜索
    cnki_mode: str = "auto"        # auto | mcp | fallback
    cnki_enabled: bool = True      # A/B 开关：false 时整个知网源跳过
    cnki_force: bool = False       # 评测用：强制把 cnki 加入 Planner 的 plan
    cnki_mcp_exe: str = ""         # 留空则从 LOCALAPPDATA 默认路径查找
    cnki_data_dir: str = ""        # 留空则用 exe 同级 data/
    cnki_max_results: int = 5      # 单次检索返回上限

    # 本地模型（向量/精排）
    hf_home: str = "../../models"
    hf_endpoint: str = "https://hf-mirror.com"
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # 数据库
    database_url: str = "sqlite+aiosqlite:///./kra.db"

    # 向量库（Qdrant）
    qdrant_url: str = "http://localhost:6333"

    # 知识库检索相关度阈值：低于此值的片段视为无关（输出"未找到相关信息"）
    kb_min_score: float = 0.45
    # 表格题报告上下文条数：表格行通常集中在少数切片，缩小窗口可降低 TPM 压力并减少干扰
    table_report_sources: int = 6

    # PDF 智能解析（P1：类型分流 + 版面还原 + 图片 OCR + 页标记）
    pdf_ocr_enabled: bool = True       # false 回退 pypdf 纯文本提取
    pdf_ocr_min_chars: int = 50        # 页文本充足阈值（低于则整页 OCR）
    pdf_margin_top: float = 0.05       # 页眉剔除区（页高 5% 内）
    pdf_margin_bottom: float = 0.95    # 页脚剔除区（页高 95% 以下）

    # GROBID 章节结构抽取：auto=优先 GROBID，失败回退 PyMuPDF；pymupdf=只用旧解析
    parser_mode: str = "auto"          # auto | grobid | pymupdf
    grobid_url: str = "http://127.0.0.1:8070"
    grobid_timeout: float = 180.0

    # 上传限制
    max_upload_mb: int = 100           # 单文件上传上限（MB）
    # 批量入库时每篇最多保留切片数（0=全部）；用于 120+ 篇覆盖的快速建库/评测
    ingest_max_chunks: int = 0
    # 批量入库只算 dense、不写 sparse（加速；检索仍可回退 dense）
    ingest_dense_only: bool = False

    # VLM 图表多模态理解（OpenAI 兼容，mimo-v2.5；Key 留空自动关闭）
    vlm_api_key: str = ""
    vlm_base_url: str = "https://api.xiaomimimo.com/v1"
    vlm_model: str = "mimo-v2.5"

    @property
    def vlm_enabled(self) -> bool:
        return bool(self.vlm_api_key.strip())
    # 混合检索：稀疏词法权重显著性阈值（宽松兜底档，HYBRID_SEARCH=true 时生效）
    kb_sparse_min_weight: float = 0.5
    # 宽松兜底档 dense 下限（sparse 独有命中补算）
    kb_relax_min_score: float = 0.35
    # 混合检索开关（bge-m3 dense+sparse 双路 RRF）；false 回退纯 dense
    hybrid_search: bool = True
    # 页码感知精排：有 page_nos 的切片加一点分数，优先把可回溯原文的段落排到前面
    paged_chunk_boost: float = 0.03
    # 检索源动态路由：true=SendAPI 只分发 Planner 规划的源；false=旧固定扇出（A/B 用）
    dynamic_routing: bool = True
    # 查询增强：提问前 LLM 改写+关键词扩展（false 回退原问题直接检索）
    query_processing: bool = True
    # 多查询检索：改写时生成 sub_queries 变体，多路语义检索后跨查询 RRF 合并（false 仅主查询）
    multi_query: bool = True
    # 精排与 RRF 的混合系数：1.0 = 纯精排（默认），<1 时按 alpha·精排分 + (1-alpha)·RRF名次分 排序。
    # 纯精排会把"开头是作者列表/公式"的切片压到底（实测 sf01 排 40、sf30 排 46）。
    rerank_blend_alpha: float = 1.0
    # 跨语言检索：把中文问题经 LLM 转成英文重述，作为独立一路参与 RRF 合并。
    # 动机是中英混排知识库下，中文查询的向量偏向中文切片，英文原文排不上来
    # （实测 sf06 中文查询排名 3、英文查询排名 1）。开关用于 A/B 验证其净收益。
    en_query_enabled: bool = True
    # 英文重述路的 RRF 权重。中文路与子查询等权时，英文路即使把正确切片排到第 1，
    # 也会被多条中文路累积的分数稀释（实测 sf15/sf27/sf28/sf30 英文路均排第 1 却进不了 top-12）。
    en_query_weight: float = 2.0
    # HyDE：LLM 生成假设性答案作为一路检索变体（每问 +1 次 LLM 调用，评估侧结果有缓存）。
    # 默认关闭——A/B 实测单独收益微弱（R@12 0.533 vs 0.523），与兄弟扩展叠加反而略降
    # （sibling_hyde 0.700 < sibling 0.725，变体候选挤占窗口）；需要时可开
    hyde_enabled: bool = False
    # 兄弟切片扩展：命中切片的 ±1 邻居自动进入候选（零 LLM 成本）
    sibling_expand: bool = True
    # 评分快通道：精排 Top3 均分 ≥ 此值时跳过 LLM 评分与反思循环（高质量检索直达生成）
    grade_fastpath_top3: float = 0.60
    # 研究时间预算：反思重查决策时已耗时超此值则直接生成（防 3 分钟长尾）
    research_time_budget_sec: float = 60.0
    # 重排：RRF 融合后 bge-reranker-v2-m3 模型精排（false 仅 RRF 排名）
    rerank_enabled: bool = True
    # 检索候选池：先取更大的 top_k 交给精排，最终仍只保留 12 条来源。
    # 扩大池子对长尾问题更友好，代价是 rerank 多打分几十条（CPU ~0.2s/条）。
    kb_candidate_k: int = 40
    kb_candidate_k_escalated: int = 60

    # 服务
    api_host: str = "0.0.0.0"
    api_port: int = 8900
    cors_origins: str = "http://localhost:5173"

    # 认证与租户：API Key → (tenant_id, user_id)
    # API_KEYS 格式：key1:tenant-a:user-a,key2:tenant-b:user-b
    auth_enabled: bool = True
    api_keys: str = ""
    # 本机回环地址免鉴权（本地开发/评测用；生产/反代部署必须设 false）
    auth_localhost_bypass: bool = False
    session_secret: str = ""
    session_ttl_hours: int = 72
    default_tenant_id: str = "default"
    default_user_id: str = "local"
    # 只读分享链接有效期（天）
    share_ttl_days: int = 7

    # 启动预热（本地模型/知网 MCP）；测试/E2E 可关闭
    warmup_models: bool = True

    # 任务队列/事件
    research_worker_count: int = 1
    research_task_timeout_sec: float = 600.0
    research_max_attempts: int = 3
    event_poll_ms: int = 200

    # 上传配额与解析超时
    max_tenant_storage_mb: int = 4096
    max_documents_per_tenant: int = 5000
    parse_timeout_sec: float = 300.0
    max_pdf_pages: int = 500

    # 评估裁判 LLM（仅 eval_run.py 的 ragas 评分使用，与被测系统解耦）。
    # 留空回退 DeepSeek 主配置；可指向任意 OpenAI 兼容端点（如 Ollama 本地模型）省钱。
    eval_judge_model: str = ""
    eval_judge_model_2: str = ""      # 双裁判：第二个模型（留空=单裁判）
    # 成本看板：按每千 token 单价估算（留 0 时只统计 token，不折算金额）
    llm_price_in_per_1k: float = 0.0
    llm_price_out_per_1k: float = 0.0
    eval_judge_base_url: str = ""
    eval_judge_api_key: str = ""

    @property
    def models_dir(self) -> Path:
        return (BASE_DIR / self.hf_home).resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.chat_api_key)

    @property
    def chat_api_key(self) -> str:
        """对话模型 key：显式 LLM_API_KEY 优先，否则回退 DeepSeek。"""
        return (self.llm_api_key or self.deepseek_api_key).strip()

    @property
    def chat_base_url(self) -> str:
        if self.llm_api_key.strip():
            return (self.llm_base_url or self.deepseek_base_url).strip()
        return self.deepseek_base_url.strip()

    @property
    def chat_model(self) -> str:
        if self.llm_api_key.strip():
            return (self.llm_model or self.deepseek_model).strip()
        return self.deepseek_model.strip()

    @property
    def chat_provider(self) -> str:
        base = self.chat_base_url.lower()
        if "sensenova" in base:
            return "sensenova"
        if "deepseek" in base:
            return "deepseek"
        if "ollama" in base or "11434" in base:
            return "ollama"
        return "openai"

    @property
    def tavily_enabled(self) -> bool:
        return bool(self.tavily_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
