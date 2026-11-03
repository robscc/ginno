# 侧栏运行中会话强提示 — 产品设计

> 2026-11-02 定稿。两决策已拍板:视觉强度 A 强(边缘条+脉冲点)、维持现有排序
> 不加 Running 分组。纯前端特性,零后端改动。

## §0 问题

主会话行没有任何运行中标识——切走会话后,侧栏分不清哪些会话还活着。TopBar
只覆盖当前打开的会话;未读紫点要等 turn 跑完才亮;子会话有状态 emoji 但主行
没有;父行「● N」徽标只数子代理。

## §1 数据源

`GET /api/running`(`running_work.py running_work_report`,事实源
`_RUNNING_TURNS`)轮询 **5s**,AppShell 持有 `runningIds: Set<string>` 穿给
会话行——与关联会话 chip、连接器状态点同一模式。后台命令部分(tasks)不进
本特性:`nav.tasksRunning` 已有归属,只做 session turn。

## §2 视觉(A 强,已拍板)

```
┌─ 侧栏行 ─────────────────────────────┐
│ ▌ 🦆 会话标题              ● 3m  ⋯  │  ▌ = 左边缘 3px 强调条(--st-running,呼吸透明度动画)
│        ^agent图标       ^脉冲点+时间 │  ● = 脉冲点(--st-running)
└──────────────────────────────────────┘
```

- 左边缘强调条:余光可辨、不抢标题空间(IDE 运行 tab / CI 行同范式)。
- 行尾脉冲点放在相对时间前;tooltip「Running」(chat.status 现有词)。
- 语义色规范:全部走 `--st-running`,禁绿色;顺带把父行「● N」徽标的
  `bg-green` 换成 `--st-running`(一致性修正)。

## §3 状态机(与未读点天然互斥)

```
idle ──turn开始──▶ running(边缘条+脉冲点)──收口──▶ unread(紫点,现有)──打开──▶ idle
```

运行中不可能同时未读,两个点不打架。

## §4 范围与边界

- 当前打开的会话照常显示(与 TopBar 一致,无害)。
- 子会话行维持现有状态 emoji,不叠加(已有强标识)。
- 归档区行同样显示(运行中被手动归档是合法态)。
- 排序/分组逻辑不动(useSessionGrouping 零改)。

## §5 P2 增强(不在首版)

1. 应用隐藏时 Dock 图标角标显示运行数(notify-rust UN 后端已在)。
2. 全局 WS 广播替代轮询(即时亮/灭;server 已有 _RUNNING_TURNS 转移点可挂)。
