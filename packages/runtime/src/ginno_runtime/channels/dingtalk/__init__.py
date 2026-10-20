"""钉钉渠道本地逻辑（无网络依赖部分）。

设计出处：docs/dingtalk-connector-design.md §3.1。

本包内模块均为纯本地逻辑（不 import 钉钉 SDK、不发网络请求）：
- ``sessions`` — staffId/conversationId ↔ ginno session_id 映射表（F2）；
- ``menus``    — 菜单/命令匹配与模板渲染（F8）。

网络相关模块（adapter/tap/cards/media）见同目录，另行实现。
"""
