"""钉钉互动卡片：AI 流式卡片（打字机）与 ask_user 问题卡片（设计 F3/F4）。

设计出处：docs/dingtalk-connector-design.md §4。

- F3 流式输出：create_stream_card（占位「⏳ 思考中…」）→ stream_update
  （打字机增量渲染，全量模式）→ finalize（终态定格，done/error）；
- F4 ask_user：send_question_card（按钮 +「其他」输入，回调值 JSON 串）+
  disable_question_card（答题后置灰防重）。

**降级分支**（template_id 为空）：全部方法退化为 markdown 消息直发——
流式卡片占位/终态各发一条（无打字机），问题卡片退化为编号列表 +
「回复数字/直接回复文字」（F3 异常路径：卡片创建失败 → 普通 markdown 兜底）。

接口参数已对照官方 dingtalk-stream-sdk-python（card_replier.py：createAndDeliver
/ streaming / instances 三接口）与 openclaw-channel-dingtalk 实现逐字段核验；
与任务书出入处以官方实现为准并注明（如 streaming 为 **PUT**）。不确定参数
标注 ``TODO(dingtalk)``，不编造。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ginno_runtime import jsonx as json

from .media import MSG_MARKDOWN, DingTalkApiError, DingTalkClient

# AI 卡片 flowStatus 槽位值（官方 dingtalk-stream SDK AICardStatus 枚举同源）。
FLOW_PROCESSING = 1  # 处理中（占位 / 生成中）
FLOW_FINISHED = 3  # 执行完成（终态-正常）
FLOW_FAILED = 5  # 执行失败（终态-错误）

# finalize(status=…) → flowStatus；未知 status 按正常终态处理。
_FINAL_STATUS = {"done": FLOW_FINISHED, "error": FLOW_FAILED}


@dataclass
class StreamCard:
    """一张已投放的 AI 卡片锚点（F3 步骤 1）。

    ``out_track_id == ""`` 表示**降级模式**（无卡片模板）：stream_update
    退化为 no-op，finalize 退化为整条 markdown 消息发送。
    """

    card_instance_id: str
    out_track_id: str
    staff_id: str


class CardClient:
    """卡片编排层：复用 :class:`DingTalkClient` 的 token/限流/错误规整传输。"""

    def __init__(
        self, client: DingTalkClient, template_id: str | None = None, *, stream_key: str = "text"
    ) -> None:
        self.client = client
        self.template_id = template_id or ""
        # TODO(dingtalk): stream_key/flowStatus 是卡片搭建平台 AI 卡片模板的槽位契约，
        # 此处取官方 AI 卡片模板通用变量名（text / flowStatus）；接入自建模板时需核对。
        self.stream_key = stream_key

    @property
    def degraded(self) -> bool:
        """无卡片模板 → 全部方法降级为 markdown 消息（F3 异常路径同款兜底）。"""
        return not self.template_id

    # ---- 降级分支 ----------------------------------------------------------

    async def _send_markdown(self, staff_id_or_conversation: str, text: str) -> None:
        """降级分支：markdown 消息直发（标题取首个非空行，去 markdown 标记）。"""
        title = next(
            (ln.lstrip("#*- ").strip() for ln in text.splitlines() if ln.strip()),
            "Ginno",
        )[:30] or "Ginno"
        await self.client.send_single_message(
            staff_id_or_conversation, MSG_MARKDOWN, {"title": title, "text": text}
        )

    # ---- 传输（失败重试 1 次后抛错，调用方负责降级） ------------------------

    async def _request_with_retry(self, method: str, path: str, body: dict) -> dict:
        """卡片接口调用：失败重试 1 次后抛 :class:`DingTalkApiError`（F3 异常）。"""
        last: DingTalkApiError | None = None
        for _ in range(2):
            try:
                return await self.client.api_request(method, path, json_body=body)
            except DingTalkApiError as e:
                last = e
        assert last is not None  # 至少一次尝试后才会走到这里
        raise last

    def _create_and_deliver_body(
        self, out_track_id: str, card_param_map: dict[str, str], staff_id: str
    ) -> dict:
        """组装 createAndDeliver 请求体（单聊 IM_ROBOT 场域；群聊 P1 再扩展）。

        场域/投放参数取自官方 SDK 与 openclaw 双验证的 openSpaceId 模式：
        单聊 ``dtv1.card//IM_ROBOT.<staffId>`` + imRobotOpenDeliverModel。
        cardParamMap 值必须全为字符串（钉钉约定）。
        TODO(dingtalk): 任务书提及的 userIds/imGroupOpenConversationId 简化投放
        参数与 openSpaceId+deliverModel 模式二选一；如自建模板走简化参数，
        以官方文档《创建并投放卡片》为准调整。
        """
        return {
            "cardTemplateId": self.template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": card_param_map},
            "callbackType": "STREAM",  # F4 步骤 2：按钮回调与消息同一条 Stream WS
            "imGroupOpenSpaceModel": {"supportForward": False},
            "imRobotOpenSpaceModel": {"supportForward": False},
            "openSpaceId": f"dtv1.card//IM_ROBOT.{staff_id}",
            "userIdType": 1,
            "imRobotOpenDeliverModel": {
                "spaceType": "IM_ROBOT",
                "robotCode": self.client.robot_code,
            },
        }

    @staticmethod
    def _ensure_delivered(data: dict) -> None:
        """校验 createAndDeliver 投放结果（deliverResults 全 success 才算成）。"""
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        deliver_results = result.get("deliverResults") or []
        failed = [r for r in deliver_results if isinstance(r, dict) and r.get("success") is False]
        if failed:
            raise DingTalkApiError(
                failed[0].get("errorCode") or "DeliverFailed",
                str(failed[0].get("errorMsg") or failed[0]),
            )

    # ---- F3：流式 AI 卡片三态 ----------------------------------------------

    async def create_stream_card(
        self, staff_id: str, placeholder: str = "⏳ 思考中…"
    ) -> StreamCard:
        """创建并投放 AI 卡片，初始内容为占位符（F3 步骤 1）。

        POST /v1.0/card/instances/createAndDeliver。降级模式：占位内容直接以
        markdown 消息发出，返回空 out_track_id 的 StreamCard。
        """
        if self.degraded:
            await self._send_markdown(staff_id, placeholder)
            return StreamCard(card_instance_id="", out_track_id="", staff_id=staff_id)
        out_track_id = f"ginno-card-{uuid.uuid4().hex}"
        body = self._create_and_deliver_body(
            out_track_id,
            {self.stream_key: placeholder, "flowStatus": str(FLOW_PROCESSING)},
            staff_id,
        )
        data = await self._request_with_retry("POST", "/v1.0/card/instances/createAndDeliver", body)
        self._ensure_delivered(data)
        # 响应可能回带 outTrackId（openclaw 读法）；未带则以本地生成为准——
        # 后续 streaming/instances 更新锚定的就是本地 outTrackId。
        card_instance_id = str(
            data.get("outTrackId") or (data.get("result") or {}).get("outTrackId") or out_track_id
        )
        return StreamCard(
            card_instance_id=card_instance_id, out_track_id=out_track_id, staff_id=staff_id
        )

    async def stream_update(self, card: StreamCard, markdown_text: str) -> None:
        """AI 卡片流式更新——打字机（F3 步骤 2，节流批量后调用）。

        PUT /v1.0/card/streaming（api-streamingupdate；**PUT**，官方 SDK 与
        openclaw 一致——任务书写的 POST 有误，以实测为准）：

        ``{outTrackId, guid, key, content, isFull, isFinalize, isError}``。

        采用 isFull=True **全量模式**：上游传累积 markdown（tap 已节流合并），
        免服务端增量拼接协议（openclaw 同款）。降级模式直接返回（无打字机，
        终态由 finalize 全量发送）。
        """
        if not card.out_track_id:
            return
        body = {
            "outTrackId": card.out_track_id,
            "guid": uuid.uuid4().hex,
            "key": self.stream_key,
            "content": markdown_text,
            "isFull": True,
            "isFinalize": False,
            "isError": False,
        }
        await self._request_with_retry("PUT", "/v1.0/card/streaming", body)

    async def finalize(self, card: StreamCard, markdown_text: str, status: str = "done") -> None:
        """终态全量定格（F3 步骤 6/8）：PUT /v1.0/card/instances 整卡更新。

        status ``"done"`` → flowStatus=FINISHED(3)；``"error"`` → FAILED(5)
        （⚠️ 停止/出错标注文本由 tap 侧拼进 markdown_text，本层纯传输）。
        降级模式：整条 markdown 全量发送（终态保证可见，F3 降级路径）。
        """
        if not card.out_track_id:
            await self._send_markdown(card.staff_id, markdown_text)
            return
        body = {
            "outTrackId": card.out_track_id,
            "cardData": {
                "cardParamMap": {
                    self.stream_key: markdown_text,
                    "flowStatus": str(_FINAL_STATUS.get(status, FLOW_FINISHED)),
                }
            },
        }
        await self._request_with_retry("PUT", "/v1.0/card/instances", body)

    # ---- F4：ask_user 问题卡片 --------------------------------------------

    async def send_question_card(self, staff_id: str, question: dict) -> str:
        """投放 ask_user 互动卡片（F4 步骤 2），返回 question id。

        question：``{"id", "question", "header", "options": [str],
        "allow_free_text": bool}``。每个选项一枚按钮，回调值为 JSON 串
        ``{"q": <id>, "act": "opt"|"free"|"skip", "i": <index>}``——tap 侧
        STREAM 回调按此协议组装 resume payload（F4 步骤 3）。outTrackId
        直接取 question id（disable_question_card 据此定位）。

        降级模式：编号列表 + 「直接回复文字」提示（防重由 resume peek 兜底）。
        """
        qid = str(question.get("id") or uuid.uuid4().hex)
        header = str(question.get("header") or "需要确认")
        text = str(question.get("question") or "")
        options = [str(o) for o in (question.get("options") or [])]
        allow_free = bool(question.get("allow_free_text"))

        if self.degraded:
            lines = [f"**{header}**", "", text, ""]
            lines += [f"{i + 1}. {opt}" for i, opt in enumerate(options)]
            if allow_free:
                lines += ["", "✏️ 也可直接回复文字（自由作答）"]
            await self._send_markdown(staff_id, "\n".join(lines))
            return qid

        def cb(act: str, index: int) -> str:
            return json.dumps({"q": qid, "act": act, "i": index}, ensure_ascii=False)

        # TODO(dingtalk): 按钮变量契约（option_i / option_i_callback / free_callback /
        # skip_callback / answered）需与卡片搭建平台实际模板逐一对齐，以下为约定。
        param: dict[str, str] = {
            "header": header,
            "question": text,
            "allow_free_text": "true" if allow_free else "false",
        }
        for i, opt in enumerate(options):
            param[f"option_{i}"] = opt
            param[f"option_{i}_callback"] = cb("opt", i)
        param["free_callback"] = cb("free", -1)
        param["skip_callback"] = cb("skip", -1)

        body = self._create_and_deliver_body(qid, param, staff_id)
        data = await self._request_with_retry("POST", "/v1.0/card/instances/createAndDeliver", body)
        self._ensure_delivered(data)
        return qid

    async def disable_question_card(self, question_id: str) -> None:
        """答题后置灰全部按钮（F4 步骤 6 防重：回调到达即置灰）。

        PUT /v1.0/card/instances 更新 ``answered`` 变量，按钮禁用态由卡片
        模板依据该变量渲染。降级模式无按钮可置灰，静默返回。
        TODO(dingtalk): answered → 按钮禁用的联动需在卡片模板中配置，变量名以模板为准。
        """
        if self.degraded:
            return
        body = {
            "outTrackId": question_id,
            "cardData": {"cardParamMap": {"answered": "true"}},
        }
        await self._request_with_retry("PUT", "/v1.0/card/instances", body)
