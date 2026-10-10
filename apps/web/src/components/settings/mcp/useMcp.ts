"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "@/lib/runtime";
import type { McpServerStatus } from "@/lib/runtime";

export type McpCfg = { mcpServers: Record<string, unknown> };

// §8.4：registry 的 load() 兜底认 servers 顶层键，但 UI 写回永远归一成 mcpServers，
// 避免同一文件两种键并存漂移。
function normalizeCfg(raw: unknown): McpCfg {
  const c = (raw ?? {}) as Record<string, unknown>;
  const pick = (v: unknown): Record<string, unknown> | null =>
    v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  const m = pick(c.mcpServers) ?? pick(c.servers) ?? {};
  return { mcpServers: m };
}

// MCP 设置页共享状态：5s 轻轮询（保留现状语义——GET /api/mcp 内置惰性重试，
// 轮询本身就是自愈时钟）+ 读改写 helper（§4.2 契约：GET config → 单点修改 →
// PUT 全文，禁止前端拼整个文件）+ ?tools=1 的按需明细缓存。
// mutate 经内部 promise 链串行化——读改写是全文覆盖，两个写操作并发时
// 后写者会吃掉先写者的改动（快速 toggle 两个 server / toggle + 工具开关
// 撞车就会丢一次写）。
export function useMcp() {
  const [cfg, setCfg] = useState<McpCfg>({ mcpServers: {} });
  const [graphTools, setGraphTools] = useState<string[]>([]);
  const [status, setStatus] = useState<McpServerStatus[]>([]);
  const [detailByName, setDetailByName] = useState<Record<string, McpServerStatus>>({});
  // mcp.json 解析失败（后端 parseError）：非空时 UI 锁写 + 横幅报警
  const [cfgError, setCfgError] = useState<string | null>(null);
  // mutate 的 memoized 闭包经 ref 读最新值（state 直接进 deps 会让 mutate
  // 每次报警变化都重建，排队中的旧闭包反而读不到新值）。
  const cfgErrorRef = useRef<string | null>(null);
  const alive = useRef(true);
  const writeQueue = useRef(Promise.resolve()); // mutate 串行队列

  const load = useCallback(async () => {
    try {
      const c = await api.getMcpConfig();
      if (!alive.current) return;
      setCfg(normalizeCfg(c));
      cfgErrorRef.current = c.parseError ?? null;
      setCfgError(cfgErrorRef.current);
      const i = await api.getMcp();
      if (!alive.current) return;
      setGraphTools(i.tools ?? []);
      setStatus(i.status ?? []);
    } catch {
      /* ignore：轮询下轮再来 */
    }
  }, []);

  // 详情页 / Tools tab 才需要的重量级查询（toolDetails 只在 ?tools=1 返回）。
  // 后端未支持该参数时响应不含 toolDetails，调用方按字段缺失降级。
  const loadDetail = useCallback(async (name: string) => {
    try {
      const i = await api.getMcp({ tools: true });
      if (!alive.current) return;
      setStatus(i.status ?? []);
      const s = (i.status ?? []).find((x) => x.name === name);
      if (s) setDetailByName((prev) => ({ ...prev, [name]: s }));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    const iv = setInterval(() => void load(), 5000);
    return () => {
      alive.current = false;
      clearInterval(iv);
    };
  }, [load]);

  // reconnect 端点全量返回 status——直接吃，省一次 GET（§4.3）。
  const applyStatus = useCallback((list: McpServerStatus[]) => {
    setStatus(list);
  }, []);

  // 读改写：fn 在「配置文件原文的 mcpServers」上做单点修改，返回 false 表示放弃写入。
  // opts.reload = true 时追加 reloadMcp 全量重建（仅连接参数变更 / Advanced / 新增向导）。
  // 两个行为契约：
  // - 损坏锁写：cfgError 非空（mcp.json 解析失败）时直接拒绝——此时后端返回
  //   空 mcpServers，任何 PUT 都是全文覆盖，会把用户原配置毁掉。修复通道
  //   只有 saveAdvanced 的显式全文覆盖。
  // - 串行化：所有写操作排队执行，消除 GET→GET→PUT→PUT 的并发覆盖窗口。
  const mutate = useCallback(
    (
      fn: (servers: Record<string, unknown>) => boolean | void,
      opts?: { reload?: boolean; ignoreCfgError?: boolean },
    ): Promise<boolean> => {
      const run = async (): Promise<boolean> => {
        if (cfgErrorRef.current && !opts?.ignoreCfgError) return false;
        try {
          const next = normalizeCfg(await api.getMcpConfig());
          const changed = fn(next.mcpServers);
          if (changed === false) return false;
          await api.putMcp(next);
          if (opts?.reload) await api.reloadMcp();
          setCfg(next); // 乐观同步，随后的 load() 校正
          await load();
          return true;
        } catch {
          return false;
        }
      };
      const p = writeQueue.current.then(run, run);
      // 队列只关心顺序，不关心结果；失败已由 run 内部消化。
      writeQueue.current = p.then(
        () => undefined,
        () => undefined,
      );
      return p;
    },
    [load],
  );

  return { cfg, cfgError, graphTools, status, detailByName, load, loadDetail, mutate, applyStatus };
}
