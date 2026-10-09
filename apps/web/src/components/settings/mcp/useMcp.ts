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
export function useMcp() {
  const [cfg, setCfg] = useState<McpCfg>({ mcpServers: {} });
  const [graphTools, setGraphTools] = useState<string[]>([]);
  const [status, setStatus] = useState<McpServerStatus[]>([]);
  const [detailByName, setDetailByName] = useState<Record<string, McpServerStatus>>({});
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const c = await api.getMcpConfig();
      if (!alive.current) return;
      setCfg(normalizeCfg(c));
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
  const mutate = useCallback(
    async (
      fn: (servers: Record<string, unknown>) => boolean | void,
      opts?: { reload?: boolean },
    ): Promise<boolean> => {
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
    },
    [load],
  );

  return { cfg, graphTools, status, detailByName, load, loadDetail, mutate, applyStatus };
}
