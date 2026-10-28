"""企业微信智能机器人渠道本地逻辑（无网络依赖部分）。

设计出处：docs/wecom-connector-design.md。

本包纯本地模块：
- ``protocol`` — 长连接帧协议（cmd/headers/body）的构造与解析（纯函数）；
- ``sessions`` — userid/chatid ↔ ginno session_id 映射表。

网络相关模块（client/adapter/tap/media）见同目录，另行实现。
"""
