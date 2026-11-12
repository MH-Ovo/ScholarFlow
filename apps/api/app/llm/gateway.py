"""LLM 网关：任意 OpenAI 兼容对话模型；未配置时回退 Mock。

- 默认读 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（商汤 SenseNova、Qwen、GLM、Ollama 等）
- 三者留空时回退 DEEPSEEK_*，保持旧行为
- DEEPSEEK_API_KEY 仍单独留给 web_search 的 tool search 使用
Mock 模式产出模板化研究报告（含 [n] 引用），保证无 Key 时演示链路可跑。
"""
import asyncio
import json
import re
import time
from typing import Any, AsyncIterator

from app.core import trace
from app.core.config import get_settings

settings = get_settings()


def _extract_json(text: str) -> dict | list | None:
    """从 LLM 输出中稳健提取 JSON（容忍 markdown 代码块包裹）。"""
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = match.group(1) if match else text
    try:
        return json.loads(candidate.strip())
    except (json.JSONDecodeError, ValueError):
        return None


class LLMGateway:
    """统一 LLM 出口：report 流式生成 + 结构化 JSON 抽取。"""

    def __init__(self) -> None:
        self._chat = None
        self._chat_json = None
        if settings.llm_enabled:
            from langchain_openai import ChatOpenAI

            common = dict(
                model=settings.chat_model,
                api_key=settings.chat_api_key,
                base_url=settings.chat_base_url,
                # 免费档（如商汤 SenseNova）会间歇返回 429 "rps exhausted"。
                # SDK 按指数退避重试；2 次只覆盖约 1.5 秒，实测不足以跨过限流窗口，
                # 报告会静默降级成片段摘录。6 次可覆盖约 30 秒。
                max_retries=6,
            )
            # 报告生成：保留模型默认的思考能力，流式输出
            self._chat = ChatOpenAI(temperature=0.3, streaming=True, **common)
            # 结构化抽取：DeepSeek 推理模型默认会把 token 预算耗在思考链上，
            # 导致 content 为空或夹带解释文字；其他兼容端点不传这个专有参数。
            json_kwargs = {"temperature": 0.1}
            if settings.chat_provider == "deepseek":
                json_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            self._chat_json = ChatOpenAI(**json_kwargs, **common)

    @property
    def mode(self) -> str:
        return settings.chat_provider if self._chat else "mock"

    # ---------- 报告流式生成 ----------
    async def stream_report(self, prompt: str, mock_text: str) -> AsyncIterator[str]:
        started = time.time()
        parts: list[str] = []
        error: str | None = None
        try:
            if self._chat:
                try:
                    # 超时兜底：LLM 流式中断/超时不会无限等待，降级用真实片段摘录补全
                    # 注意：不能用 asyncio.wait_for 包 async generator（会 TypeError），
                    # 必须用 asyncio.timeout 上下文管理器包裹迭代
                    async with asyncio.timeout(240.0):
                        async for chunk in self._chat.astream(prompt):
                            if chunk.content:
                                text = str(chunk.content)
                                parts.append(text)
                                yield text
                    return
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    # 真实调用失败/超时 → 降级摘录补全（保证链路不中断），并记录原因
                    try:
                        print(f"[kra][llm] 真实 LLM 流式降级: {error}", flush=True)
                    except Exception:
                        pass
            for piece in _chunk_text(mock_text):
                parts.append(piece)
                yield piece
                await asyncio.sleep(0.015)
        finally:
            trace.record_llm(
                "report",
                settings.chat_model if self._chat else "mock",
                prompt,
                "".join(parts),
                (time.time() - started) * 1000,
                error=error,
            )

    # ---------- 结构化抽取（规划 / 图谱） ----------
    async def extract_json(self, prompt: str, mock_payload: Any) -> Any:
        started = time.time()
        output = ""
        usage = None
        error = None
        parsed = None
        if self._chat_json:
            try:
                resp = await self._chat_json.ainvoke(prompt)
                output = str(resp.content)
                usage = getattr(resp, "usage_metadata", None)
                if usage is not None and not isinstance(usage, dict):
                    usage = dict(usage)
                parsed = _extract_json(output)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
        trace.record_llm(
            "json",
            settings.chat_model if self._chat_json else "mock",
            prompt,
            output,
            (time.time() - started) * 1000,
            usage=usage,
            error=error or (None if parsed is not None else "json_parse_failed"),
        )
        return parsed if parsed is not None else mock_payload


def _chunk_text(text: str, size: int = 6) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


_gateway: LLMGateway | None = None


def get_llm() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway
