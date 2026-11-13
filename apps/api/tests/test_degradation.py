"""降级必须显式标记，不能把 mock/摘录伪装成正常生成。"""
import pytest

from app.llm.gateway import LLMGateway


class _BoomChat:
    async def ainvoke(self, _prompt):
        raise RuntimeError("boom")


class _BoomStream:
    def astream(self, _prompt):
        raise RuntimeError("stream boom")


@pytest.mark.asyncio
async def test_extract_json_marks_degraded():
    gw = LLMGateway()
    gw._chat_json = _BoomChat()
    res = await gw.extract_json_result("prompt", {"ok": True})
    assert res.degraded is True
    assert res.data == {"ok": True}
    assert "boom" in res.reason


@pytest.mark.asyncio
async def test_stream_report_marks_degraded():
    gw = LLMGateway()
    gw._chat = _BoomStream()
    meta: dict = {}
    out = []
    async for piece in gw.stream_report("prompt", "fallback text", meta):
        out.append(piece)
    assert "".join(out) == "fallback text"
    assert meta["degraded"] is True
    assert "boom" in meta["reason"]


@pytest.mark.asyncio
async def test_extract_json_ok_not_degraded():
    class _OkChat:
        async def ainvoke(self, _prompt):
            class R:
                content = '{"ok": true}'
                usage_metadata = {"total_tokens": 1}

            return R()

    gw = LLMGateway()
    gw._chat_json = _OkChat()
    res = await gw.extract_json_result("prompt", {"ok": False})
    assert res.degraded is False
    assert res.data == {"ok": True}
