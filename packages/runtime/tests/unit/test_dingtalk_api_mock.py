"""DingTalk OpenAPI 封装的本地验证（L1：httpx MockTransport，零网络零凭证）。

本地验证分层（见 docs/dingtalk-connector-design.md §7 与实现笔记）：
- L0 纯离线：test_dingtalk_local.py / test_dingtalk_channel.py（menus/tap 翻译）
- **L1 本文件**：MockTransport 打桩钉钉端点，验证 media.py/cards.py 的
  URL/方法/header/body 协议形状、缓存/限流行为、错误分支与降级链
- L2 数据入口回放：构造钉钉消息 dict 直喂 adapter.handle_bot_message（需完整
  venv，见 test_dingtalk_replay.py 的环境守卫）
- L3 真实自聊联调：免费企业内部应用 + Stream 免公网（手动，见设计 §2.1）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from ginno_runtime.channels.dingtalk.cards import CardClient, StreamCard
from ginno_runtime.channels.dingtalk.media import DingTalkApiError, DingTalkClient


class _Recorder:
    """按 (method, host, path) 分发的 MockTransport handler + 请求记录。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.host, request.url.path)
        if key == ("POST", "api.dingtalk.com", "/v1.0/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "tk-1", "expireIn": 7200})
        if key == ("POST", "api.dingtalk.com", "/v1.0/robot/oToMessages/batchSend"):
            return httpx.Response(200, json={"processQueryKey": "pqk-1"})
        if key == ("POST", "api.dingtalk.com", "/v1.0/robot/messageFiles/download"):
            return httpx.Response(
                200, json={"downloadUrl": "https://mock.local/f/dl?sig=1"})
        if key == ("GET", "mock.local", "/f/dl"):
            return httpx.Response(200, content=b"FILE-BYTES-0123456789" * 1000)
        if key == ("POST", "oapi.dingtalk.com", "/media/upload"):
            return httpx.Response(200, json={"errcode": 0, "media_id": "m-1"})
        if key == ("POST", "api.dingtalk.com", "/v1.0/card/instances/createAndDeliver"):
            return httpx.Response(
                200, json={"result": {"deliverResults": [{"success": True}],
                                      "outTrackId": "from-server"}})
        if key == ("PUT", "api.dingtalk.com", "/v1.0/card/streaming"):
            return httpx.Response(200, json={})
        if key == ("PUT", "api.dingtalk.com", "/v1.0/card/instances"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"unmocked {key}"})


def _client(**kw) -> tuple[DingTalkClient, _Recorder]:
    rec = _Recorder()
    c = DingTalkClient("ak", "sk", **kw)
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    return c, rec


def _hits(rec: _Recorder, method: str, path: str) -> list[httpx.Request]:
    return [r for r in rec.requests
            if r.method == method and r.url.path == path]


# ---------------------------------------------------------------- token

async def test_token_cached_until_expiry():
    c, rec = _client()
    assert await c.token() == "tk-1"
    assert await c.token() == "tk-1"
    assert len(_hits(rec, "POST", "/v1.0/oauth2/accessToken")) == 1
    # 过期（提前 120s 余量）→ 懒刷新触发第二次
    c._token_expire_at = time.monotonic() - 1
    assert await c.token() == "tk-1"
    assert len(_hits(rec, "POST", "/v1.0/oauth2/accessToken")) == 2


async def test_bad_credentials_raise_dingtalk_error():
    c, rec = _client()

    def deny(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(200, json={})  # 缺 accessToken 字段

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(deny))
    with pytest.raises(DingTalkApiError):
        await c.token()


# ---------------------------------------------------------------- 单发/上传/下载

async def test_send_single_message_body_shape():
    c, rec = _client()
    await c.send_single_message("staff-9", "sampleMarkdown",
                                {"title": "Ginno", "text": "正文"})
    reqs = _hits(rec, "POST", "/v1.0/robot/oToMessages/batchSend")
    assert len(reqs) == 1
    body = json.loads(reqs[0].content)
    assert body["robotCode"] == "ak"          # robot_code 缺省 = app_key
    assert body["userIds"] == ["staff-9"]
    assert body["msgKey"] == "sampleMarkdown"
    assert json.loads(body["msgParam"]) == {"title": "Ginno", "text": "正文"}
    # 鉴权头来自 token 缓存
    assert reqs[0].headers["x-acs-dingtalk-access-token"] == "tk-1"


async def test_upload_media_lru_cache(tmp_path: Path):
    c, rec = _client()
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG")
    assert await c.upload_media(f, type="image") == "m-1"
    assert await c.upload_media(f, type="image") == "m-1"     # LRU 命中
    assert len(_hits(rec, "POST", "/media/upload")) == 1
    f.write_bytes(b"\x89PNG-changed")                          # mtime/size 变 → miss
    assert await c.upload_media(f, type="image") == "m-1"
    assert len(_hits(rec, "POST", "/media/upload")) == 2
    up = _hits(rec, "POST", "/media/upload")[0]
    assert up.url.params["type"] == "image"
    assert up.url.params["access_token"] == "tk-1"


async def test_download_message_file_two_hop(tmp_path: Path):
    c, rec = _client()
    dest = tmp_path / "sub" / "f.bin"
    out = await c.download_message_file("dc-1", dest)
    assert out == dest and dest.is_file()
    assert dest.read_bytes() == b"FILE-BYTES-0123456789" * 1000
    assert not dest.with_name(dest.name + ".part").exists()   # 原子落盘
    dl = _hits(rec, "POST", "/v1.0/robot/messageFiles/download")
    assert json.loads(dl[0].content) == {"downloadCode": "dc-1", "robotCode": "ak"}
    assert len(_hits(rec, "GET", "/f/dl")) == 1


# ---------------------------------------------------------------- 卡片（有模板）

async def test_stream_card_lifecycle_with_template():
    c, rec = _client()
    cards = CardClient(c, "tpl-001")
    card = await cards.create_stream_card("staff-9")
    assert isinstance(card, StreamCard) and card.staff_id == "staff-9"
    create = _hits(rec, "POST", "/v1.0/card/instances/createAndDeliver")[0]
    body = json.loads(create.content)
    assert body["cardTemplateId"] == "tpl-001"
    assert body["outTrackId"]
    await cards.stream_update(card, "段落1")
    st = _hits(rec, "PUT", "/v1.0/card/streaming")
    assert st and json.loads(st[0].content)["outTrackId"] == card.out_track_id
    await cards.finalize(card, "段落1 全文", "done")
    fin = _hits(rec, "PUT", "/v1.0/card/instances")
    assert fin and json.loads(fin[0].content)["outTrackId"] == card.out_track_id


async def test_deliver_failure_raises():
    c, _ = _client()

    def fail_deliver(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("createAndDeliver"):
            return httpx.Response(200, json={"result": {"deliverResults": [
                {"success": False, "errorCode": "E1", "errorMsg": "no"}]}})
        return httpx.Response(200, json={})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(fail_deliver))
    cards = CardClient(c, "tpl-001")
    with pytest.raises(DingTalkApiError):
        await cards.create_stream_card("staff-9")


# ---------------------------------------------------------------- 卡片（降级）

async def test_card_degrades_without_template():
    c, rec = _client()
    cards = CardClient(c, None)  # template_id 为空 → markdown 降级
    card = await cards.create_stream_card("staff-9", placeholder="⏳ …")
    assert card.out_track_id == ""               # 降级标记（tap 零分支）
    await cards.stream_update(card, "增量")        # no-op：不发 streaming
    assert _hits(rec, "PUT", "/v1.0/card/streaming") == []
    await cards.finalize(card, "全量正文", "done")  # 终态全量 markdown
    sends = _hits(rec, "POST", "/v1.0/robot/oToMessages/batchSend")
    assert len(sends) == 2                        # 占位 + 终态
    assert json.loads(sends[0].content)["msgKey"] == "sampleMarkdown"
    assert "⏳" in json.loads(sends[0].content)["msgParam"]
    assert "全量正文" in json.loads(sends[1].content)["msgParam"]


async def test_question_card_callback_payload():
    c, rec = _client()
    cards = CardClient(c, "tpl-001")
    qid = await cards.send_question_card("staff-9", {
        "id": "q-1", "question": "选哪个？", "header": "选择",
        "options": ["A", "B"], "allow_free_text": True})
    assert qid
    create = _hits(rec, "POST", "/v1.0/card/instances/createAndDeliver")
    body = json.loads(create[-1].content)
    param = body["cardData"]["cardParamMap"]
    assert param["question"] == "选哪个？" and param["option_0"] == "A"
    # 回调值协议（F4 步骤 2）：{"q","act","i"} 必须可被 adapter.handle_card_callback 反解
    cb = json.loads(param["option_0_callback"])
    assert cb == {"q": "q-1", "act": "opt", "i": 0}
    assert json.loads(param["skip_callback"])["act"] == "skip"
