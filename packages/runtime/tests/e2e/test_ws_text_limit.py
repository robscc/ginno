"""D11：WS 入站文本帧 1MB 上限——超限回明确 error 事件且不断连。

侦察实测定罪（2026-11-22）：uvicorn 默认 ws_max_size=16MB，17MB 帧触发
1009 MESSAGE_TOO_BIG 断连（前端只见断连重连、无任何提示）；1/8/15MB 帧
则被静默接受（无校验无报错）。本基线钉住修复后的两条边界：
  ① <1MB 的帧照常进消息泵（不误伤）；
  ② >1MB 的文本帧收到 error（i18n_key=stream.error_text_too_long）且
     连接保持打开——同连接后续小帧仍正常处理（不是断连了事）。
"""

from __future__ import annotations

import pytest
from conftest import script

from ginno_runtime import jsonx as json
from ginno_runtime.api.stream.ws import loop as ws_loop


def _oversized_json(limit_bytes: int) -> str:
    """构造编码后恰超 limit 的合法 JSON 文本帧（CJK 每字 3 字节，加速填充）。"""
    pad = "长" * (limit_bytes // 3 + 64)
    return json.dumps({"type": "steer", "text": pad, "session_id": "s"})


def test_oversized_text_frame_gets_explicit_error_and_connection_survives(
    client, create_session, ws_conv
):
    model = type("M", (), {})  # 不需要模型——不发起真 turn
    sid = create_session(model)
    with ws_conv(sid) as conv:
        # ① 边界内：小帧正常进泵（steer 无活跃 turn → absorbed/queued 回执，
        #    或 invalid-json/unknown-type 也行——只要不是 text_too_long）。
        conv.send({"type": "turn_state"})
        ev = conv.recv()
        assert ev.get("event") != "error" or ev.get("i18n_key") != "stream.error_text_too_long"

        # ② 超限帧：明确 error，连接不断。
        conv.ws.send_text(_oversized_json(ws_loop.MAX_INBOUND_TEXT_BYTES))
        ev = conv.recv()
        assert ev.get("event") == "error", ev
        assert ev.get("i18n_key") == "stream.error_text_too_long"
        assert ev.get("params", {}).get("limit") == 1

        # ③ 同连接后续小帧仍被处理（没断连、没卡死）。
        conv.ws.send_text("not-json")
        ev = conv.recv()
        assert ev.get("event") == "error"
        assert ev.get("i18n_key") == "stream.error_invalid_json"


def test_limit_is_1mb_and_matches_frontend_constant():
    # 与 apps/web/src/lib/limits.ts MAX_SEND_TEXT_BYTES 同值（1MB）——改一处
    # 必须同步另一处，此断言提醒跨界改动。
    assert ws_loop.MAX_INBOUND_TEXT_BYTES == 1_048_576
