"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { ConfirmModal } from "@/components/ConfirmModal";
import { McpServerCard } from "./mcp/McpServerCard";
import { McpDetailView, type McpDetailTab } from "./mcp/McpDetailView";
import { McpAddWizard } from "./mcp/McpAddWizard";
import { McpImportModal } from "./mcp/McpImportModal";
import { useMcp } from "./mcp/useMcp";
import { fmtTokens, TOKENS_PER_TOOL, type McpServerEntry, type ParsedServer } from "./mcp/mcpShared";

type Toast = { msg: string; tone: "ok" | "err" | "warn" };
type ConfirmState = { title: string; message: string; confirmLabel?: string; onConfirm: () => void };

// Settings → Connections → MCP（设计文档 docs/mcp-server-ui-design.md）。
// 列表页（统计条 + 卡片 + Advanced 折叠区）与详情页（四 tab）在本组件内切换，
// 路由不变；向导 / 导入 / 确认均为模态。写路径全部走 useMcp().mutate 的读改写。
export function McpSettings() {
  const t = useTranslations("settings.mcp");
  const { cfg, cfgError, graphTools, status, detailByName, load, loadDetail, mutate, applyStatus } = useMcp();

  // 详情页选中的 server（页内切换，无新路由）；tab 用于卡片菜单直达对应面板
  const [sel, setSel] = useState<{ name: string; tab?: McpDetailTab } | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [restartSet, setRestartSet] = useState<Set<string>>(new Set()); // 卡片级重连中的乐观态
  // 启停开关的确认驱动 pending 态（覆盖 5s 轮询：pending 期间轮询不抢写开关值，
  // 开关显示值一律取 switchStateOf）。toggleServer 的确认循环结束/超时/异常
  // 都会清除对应条目；server 被移除由 prune effect 兜底清理，防开关永久卡灰。
  const [pendingByServer, setPendingByServer] = useState<Record<string, { on: boolean }>>({});
  const [toast, setToast] = useState<Toast | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [advText, setAdvText] = useState<string | null>(null); // null = 未编辑（显示冻结快照）
  const [advSnapshot, setAdvSnapshot] = useState<string | null>(null); // 展开时刻冻结的 cfg 快照
  const [advError, setAdvError] = useState<string | null>(null);

  const showToast = useCallback((msg: string, tone: Toast["tone"] = "ok") => {
    setToast({ msg, tone });
  }, []);
  // 写失败统一出口：损坏锁写时给出针对性文案（而非误导性的「保存失败」），
  // 其余（网络断开 / 后端 500）保持 saveFailed。cfgError 来自 ≤5s 前的轮询，
  // 足够新——损坏文件在挂载首载就会置位。
  const saveFailToast = useCallback(() => {
    showToast(cfgError ? t("cfgCorruptBlocked") : t("saveFailed"), "err");
  }, [cfgError, t, showToast]);
  useEffect(() => {
    if (!toast) return;
    const tm = setTimeout(() => setToast(null), 2800);
    return () => clearTimeout(tm);
  }, [toast]);

  // 进入详情页时拉一次 ?tools=1 明细（toolDetails / disabledTools）
  useEffect(() => {
    if (sel) void loadDetail(sel.name);
  }, [sel, loadDetail]);

  // pending 兜底清理：server 被移除（本页 Remove、导入覆盖、外部手改 mcp.json）
  // 后，对应 pending 必须消失，否则开关永久卡灰。以 cfg 键集合为真值源。
  useEffect(() => {
    setPendingByServer((p) => {
      const stale = Object.keys(p).filter((n) => !(n in cfg.mcpServers));
      if (!stale.length) return p;
      const n = { ...p };
      for (const k of stale) delete n[k];
      return n;
    });
  }, [cfg]);

  const serverNames = useMemo(() => Object.keys(cfg.mcpServers), [cfg]);

  const entryOf = useCallback(
    (name: string): McpServerEntry => {
      const e = cfg.mcpServers[name];
      return e && typeof e === "object" && !Array.isArray(e) ? (e as McpServerEntry) : {};
    },
    [cfg],
  );

  // enabled 合并视图：status 新字段优先，缺失（旧后端）回落配置文件，再缺省 = true
  const enabledOf = useCallback(
    (name: string) => {
      const s = status.find((x) => x.name === name);
      if (s?.enabled != null) return s.enabled;
      return entryOf(name).enabled !== false;
    },
    [status, entryOf],
  );

  const disabledCountOf = useCallback(
    (name: string) => {
      const s = status.find((x) => x.name === name);
      if (s?.disabledTools) return s.disabledTools.length;
      const e = entryOf(name).disabled_tools;
      return Array.isArray(e) ? e.length : 0;
    },
    [status, entryOf],
  );

  // 开关的显示值：pending 覆盖轮询，但显示「翻转前」的状态（!p.on）——
  // 确认驱动语义：滑块不乐观位移，spinner 在目标侧指示方向，pending 清除
  // （status 确认 / 超时回弹）后 checked 才真正翻转、滑块才滑动。
  const switchStateOf = useCallback(
    (name: string) => {
      const p = pendingByServer[name];
      if (p) return !p.on;
      return enabledOf(name);
    },
    [pendingByServer, enabledOf],
  );

  // graph 全名按「最长前缀」归属到 server（§6.1 前缀歧义：a 与 a_b 并存时
  // mcp_a_b_tool 应归 a_b），供详情页 Tools tab 的降级名单使用。
  const toolsOwnedBy = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const full of graphTools) {
      let owner: string | null = null;
      for (const n of serverNames) {
        const p = `mcp_${n}_`;
        if (full.startsWith(p) && (owner == null || n.length > owner.length)) owner = n;
      }
      if (owner) map.set(owner, [...(map.get(owner) ?? []), full]);
    }
    return map;
  }, [graphTools, serverNames]);

  // ---- 统计条（§5.2）：disabled 的服务器不计入 connected / failed / tools ----
  const connectedCount = status.filter((s) => s.connected && enabledOf(s.name)).length;
  const failedCount = status.filter((s) => !s.connected && enabledOf(s.name)).length;
  const toolsActive = status.reduce(
    (acc, s) => (s.connected && enabledOf(s.name) ? acc + Math.max(0, s.tools - disabledCountOf(s.name)) : acc),
    0,
  );

  // ---- 写操作（全部经 mutate 读改写） ----

  // 启停开关（§3.3/§5.2 + 确认驱动交互）：禁止乐观翻转。
  // - 关闭流：PUT enabled:false（后端 sync_configs 热同步 + 后台逐出连接）→
  //   轮询刷新直到 status.enabled===false 才翻 OFF；
  // - 开启流：PUT enabled:true + reconnect?server=（后端 connect_all 内联等待
  //   connect_timeout，返回的 status 即首查结果）→ 轮询等到 connected:true 才
  //   翻 ON；超过 connect_timeout（20s 硬上限）未连上则回弹——PUT 回
  //   enabled:false（不 reconnect），开关与配置一致地回到 OFF，toast 单行错误。
  // pending 期间控件禁用、开关显示值被 pending 覆盖（5s 轮询抢不走的即是它）；
  // finally/prune 双保险清 pending，防卡灰。工具级开关（toggleTool）不套用此
  // 模式——纯 put、下一 turn 生效。
  const toggleServer = async (name: string, wantOn: boolean) => {
    if (pendingByServer[name]) return; // 防连点/抖动
    const to = Number(entryOf(name).connect_timeout);
    // connect_timeout + 3s 余量，硬上限 20s
    const deadlineMs = Math.min((Number.isFinite(to) && to > 0 ? to : 15) * 1000 + 3000, 20_000);
    setPendingByServer((p) => ({ ...p, [name]: { on: wantOn } }));
    const wait = (ms: number) => new Promise((res) => globalThis.setTimeout(res, ms));
    try {
      const ok = await mutate((servers) => {
        const e = servers[name];
        if (!e || typeof e !== "object" || Array.isArray(e)) return false;
        const next = { ...(e as McpServerEntry) };
        if (wantOn) delete next.enabled;
        else next.enabled = false;
        servers[name] = next;
      });
      if (!ok) throw new Error("save");

      if (wantOn) {
        let connected = false;
        let lastError = "";
        const deadline = Date.now() + deadlineMs;
        for (;;) {
          try {
            const r = await api.reconnectMcp(name);
            if (r.ok === false) {
              // 未知名（并发被删等）：直接按失败回弹
              lastError = r.error ?? "";
              break;
            }
            applyStatus(r.status ?? []);
            const st = (r.status ?? []).find((x) => x.name === name);
            if (st?.connected && st.enabled !== false) {
              connected = true;
              break;
            }
            lastError = st?.error ?? lastError;
          } catch {
            /* HTTP 抖动：继续在窗口内重试 */
          }
          if (Date.now() >= deadline) break;
          await wait(1200);
        }
        if (connected) {
          showToast(t("enabledToast"));
        } else {
          // 回弹 OFF：配置回滚为 disabled（与开关一致），错误行复用后端根因。
          // 回滚本身失败（网络抖动等）不能静默——配置停在 enabled:true +
          // 连接失败，用户看到的开关位置与文件不一致，必须显式告知。
          const rollbackOk = await mutate((servers) => {
            const e = servers[name];
            if (!e || typeof e !== "object" || Array.isArray(e)) return;
            const next = { ...(e as McpServerEntry) };
            next.enabled = false;
            servers[name] = next;
          });
          showToast(rollbackOk ? lastError || t("notConnected") : t("rollbackFailed"), "err");
        }
      } else {
        let confirmed = false;
        let lastError = "";
        const deadline = Date.now() + 8000;
        let confirmedViaStatus = false;
        for (;;) {
          try {
            const i = await api.getMcp();
            applyStatus(i.status ?? []);
            const st = (i.status ?? []).find((x) => x.name === name);
            if (!st || st.enabled === false) {
              confirmed = true; // server 已被移除也算完成
              confirmedViaStatus = true;
              break;
            }
            lastError = st.error ?? lastError;
            // 兜底真值源：PUT 已落盘，但 sync_configs 若在后端抛错（文件已写、
            // 内存停留旧值），status 会永远报旧 enabled——8s 白等后开关被错误
            // 地弹回 ON。配置文件才是持久层，状态没跟上时以文件为准确认。
            const c = await api.getMcpConfig();
            const e = (c.mcpServers ?? {})[name];
            if (!e || (e as McpServerEntry).enabled === false) {
              confirmed = true;
              break;
            }
          } catch {
            /* 继续等 */
          }
          if (Date.now() >= deadline) break;
          await wait(1000);
        }
        if (confirmed) {
          // 文件已确认但 status 始终没跟上 = sync_configs 后端异常，内存与
          // 文件分叉——全量 reload 对齐（会断开重连所有连接，仅此异常路径）。
          if (!confirmedViaStatus) {
            try {
              await api.reloadMcp();
              const i = await api.getMcp();
              applyStatus(i.status ?? []);
            } catch {
              /* 下一轮 5s 轮询兜底 */
            }
          }
          showToast(t("disabledToast"));
        } else {
          showToast(lastError || t("saveFailed"), "err");
        }
      }
    } catch {
      saveFailToast();
    } finally {
      setPendingByServer((p) => {
        if (!(name in p)) return p;
        const n = { ...p };
        delete n[name];
        return n;
      });
    }
  };

  // 工具开关（§3.3）：只 putMcp，不 reload——图内工具集经 drift 机制下一 turn 重建。
  // 成功后刷新该 server 的 ?tools=1 明细缓存，避免旧 disabledTools 盖过刚写的配置。
  const toggleTool = (server: string, tool: string, on: boolean) => {
    void mutate((servers) => {
      const e = servers[server];
      if (!e || typeof e !== "object" || Array.isArray(e)) return false;
      const next = { ...(e as McpServerEntry) };
      const cur = Array.isArray(next.disabled_tools) ? (next.disabled_tools as unknown[]).map(String) : [];
      const nextList = on ? cur.filter((x) => x !== tool) : cur.includes(tool) ? cur : [...cur, tool];
      if (nextList.length) next.disabled_tools = nextList;
      else delete next.disabled_tools;
      servers[server] = next;
    }).then((ok) => {
      if (!ok) {
        saveFailToast();
        return;
      }
      showToast(t(on ? "enabledToast" : "disabledToast"));
      void loadDetail(server);
    });
  };

  const saveEntry = (name: string, entry: McpServerEntry) => mutate((s) => void (s[name] = entry), { reload: true });

  const renameServer = async (oldName: string, newName: string, entry: McpServerEntry) => {
    const ok = await mutate((s) => {
      delete s[oldName];
      s[newName] = entry;
    }, { reload: true });
    if (ok) setSel({ name: newName });
    return ok;
  };

  const removeServer = (name: string) => {
    const tools = toolsOwnedBy.get(name)?.length ?? status.find((s) => s.name === name)?.tools ?? 0;
    setConfirm({
      title: t("removeTitle"),
      message: t("removeConfirm", { name, count: tools }),
      onConfirm: () => {
        setConfirm(null);
        void mutate((s) => void delete s[name], { reload: true }).then((ok) => {
          if (!ok) {
            saveFailToast();
            return;
          }
          showToast(t("removedToast", { name }), "warn");
          if (sel?.name === name) setSel(null);
        });
      },
    });
  };

  // 手动重连 = 绕过 120s 冷却的惰性补连（不重建 registry）；结果靠 reconnect 的
  // 全量 status 直接吃 + 下轮轮询校正。
  const reconnectOne = async (name: string) => {
    setRestartSet((s) => new Set(s).add(name));
    try {
      const r = await api.reconnectMcp(name);
      if (r.ok === false && r.error) showToast(r.error, "err");
      if (r.status) applyStatus(r.status);
    } catch {
      showToast(t("reconnectFailedNet"), "err");
    } finally {
      setRestartSet((s) => {
        const n = new Set(s);
        n.delete(name);
        return n;
      });
    }
  };

  const reconnectFailed = async () => {
    const failed = status.filter((s) => !s.connected && enabledOf(s.name));
    if (!failed.length) {
      showToast(t("reconnectFailedNone"));
      return;
    }
    setReconnecting(true);
    try {
      const r = await api.reconnectMcp();
      if (r.status) applyStatus(r.status);
    } catch {
      showToast(t("reconnectFailedNet"), "err");
    } finally {
      setReconnecting(false);
    }
  };

  // 向导收尾：putMcp 增量合并 + reload 全量重建（§4.3 表）
  const saveWizard = async (name: string, entry: McpServerEntry) => {
    const ok = await saveEntry(name, entry);
    setWizardOpen(false);
    if (ok) showToast(t("wzSaved", { name }));
    else saveFailToast();
  };

  // 导入：只 putMcp 合并写回、不自动 reload（§5.3）——连接靠下轮轮询 / 重连入口
  const doImport = async (picked: ParsedServer[]) => {
    const ok = await mutate((s) => {
      for (const p of picked) s[p.name] = p.entry;
    });
    setImportOpen(false);
    if (ok) showToast(t("impDone", { count: picked.length }));
    else saveFailToast();
  };

  // Advanced 逃生舱：真值明文（§8.3）；保存前把 servers 顶层键归一成 mcpServers。
  // 展开时冻结快照（advSnapshot）——5s 轮询每轮重写 value 会打断选中文本/滚动；
  // 只有真实编辑（advText 非空）才可保存，避免「未编辑的旧快照」覆盖外部改动。
  // parse 错误与网络错误分流：此前共用一个 catch，断网也报「JSON 无效」。
  const saveAdvanced = async () => {
    if (advText == null) return;
    setAdvError(null);
    let m: { mcpServers: Record<string, unknown> };
    try {
      const parsed = JSON.parse(advText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
      m = {
        mcpServers:
          parsed.mcpServers && typeof parsed.mcpServers === "object"
            ? parsed.mcpServers
            : parsed.servers && typeof parsed.servers === "object"
              ? parsed.servers
              : parsed,
      };
    } catch (e) {
      setAdvError(t("invalidJson", { error: (e as Error).message }));
      return;
    }
    try {
      await api.putMcp(m);
      const r = await api.reloadMcp();
      setAdvText(null);
      setAdvSnapshot(advText);
      showToast(t("saved", { servers: r.servers.join(", ") }));
      await load();
    } catch {
      showToast(t("saveFailed"), "err");
    }
  };

  const card = sel ? { entry: entryOf(sel.name), status: status.find((s) => s.name === sel.name) } : null;

  return (
    <div className="px-8 py-7">
      {sel && card ? (
        <McpDetailView
          key={`${sel.name}:${sel.tab ?? ""}`}
          name={sel.name}
          entry={card.entry}
          status={card.status}
          detail={detailByName[sel.name]}
          allToolNames={toolsOwnedBy.get(sel.name) ?? []}
          restarting={restartSet.has(sel.name)}
          switchOn={switchStateOf(sel.name)}
          switchPending={!!pendingByServer[sel.name]}
          initialTab={sel.tab}
          onBack={() => setSel(null)}
          onSaveEntry={saveEntry}
          onRename={renameServer}
          onToggleTool={(tool, on) => toggleTool(sel.name, tool, on)}
          onReconnect={() => void reconnectOne(sel.name)}
          onToggleServer={(on) => void toggleServer(sel.name, on)}
          onToast={showToast}
        />
      ) : (
        <>
          {/* ---- 页头 ---- */}
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
              <p className="mt-1 max-w-[560px] text-sm leading-relaxed text-muted">
                {t("description", { count: serverNames.length })}
              </p>
            </div>
            <div className="flex shrink-0 gap-2">
              <button
                type="button"
                onClick={() => setImportOpen(true)}
                className="rounded-lg border border-line2 px-3 py-1.5 text-[13px] font-medium text-txt transition-colors hover:bg-card2"
              >
                ↓ {t("importJson")}
              </button>
              <button
                type="button"
                onClick={() => setWizardOpen(true)}
                className="rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2"
              >
                + {t("addServer")}
              </button>
            </div>
          </div>

          {/* ---- 损坏锁写横幅 ---- */}
          {cfgError && (
            <div className="mt-4 max-w-[640px] rounded-lg border border-red/40 bg-red/[0.08] px-3.5 py-2.5 text-[12.5px] leading-relaxed text-red">
              <b>{t("cfgCorruptTitle")}</b>
              <div className="mt-0.5 font-mono text-[11.5px] text-red/80">{cfgError}</div>
              <div className="mt-1 text-muted">{t("cfgCorruptBody")}</div>
            </div>
          )}

          {/* ---- 统计条 ---- */}
          <div className="mt-4 flex flex-wrap items-center gap-4 rounded-lg border border-line bg-card px-3.5 py-2 text-[12.5px] text-muted">
            <span className="inline-flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full bg-green" />
              <b className="font-semibold text-txt">{connectedCount}</b> {t("statsConnected")}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full bg-red" />
              <b className="font-semibold text-txt">{failedCount}</b> {t("statsFailed")}
            </span>
            <span className="ml-auto text-faint">
              {t("statsToolsActive", { count: toolsActive })} · {t("statsTokens", { tokens: fmtTokens(toolsActive * TOKENS_PER_TOOL) })}
            </span>
          </div>

          {/* ---- 列表操作条 ---- */}
          <div className="mb-2.5 mt-3.5 flex items-center justify-between">
            {failedCount > 0 ? (
              <button
                type="button"
                onClick={() => void reconnectFailed()}
                disabled={reconnecting}
                className="rounded-lg border border-line2 px-2.5 py-1 text-xs text-txt transition-colors hover:bg-card2 disabled:opacity-45"
              >
                ↻ {reconnecting ? t("reconnecting") : t("reconnectFailed")}
              </button>
            ) : (
              <span />
            )}
            <span className="text-xs text-faint">{t("storedIn")}</span>
          </div>

          {/* ---- 卡片列表 ---- */}
          <div className="flex flex-col gap-2">
            {serverNames.length === 0 && <p className="py-6 text-center text-[13px] text-faint">{t("empty")}</p>}
            {status
              .filter((s) => cfg.mcpServers[s.name] !== undefined)
              .map((s) => (
                <McpServerCard
                  key={s.name}
                  s={s}
                  entry={entryOf(s.name)}
                  switchOn={switchStateOf(s.name)}
                  switchPending={!!pendingByServer[s.name]}
                  restarting={restartSet.has(s.name)}
                  onOpen={() => setSel({ name: s.name })}
                  onAction={(kind) => {
                    if (kind === "tools") setSel({ name: s.name, tab: "tools" });
                    else if (kind === "perms") setSel({ name: s.name, tab: "perms" });
                    else if (kind === "reconnect") void reconnectOne(s.name);
                    else removeServer(s.name);
                  }}
                  onToggle={(on) => void toggleServer(s.name, on)}
                />
              ))}
            {/* 配置里有但 status 尚未上报的（后端未就绪/刚导入）：渲染降级卡片行 */}
            {serverNames
              .filter((n) => !status.some((s) => s.name === n))
              .map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setSel({ name: n })}
                  className="flex items-center gap-2.5 rounded-lg border border-line bg-card px-3.5 py-3 text-left transition-colors hover:border-line2"
                >
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-faint" />
                  <span className="text-sm font-semibold text-txt">{n}</span>
                  <span className="ml-auto text-xs text-faint">{t("notConnected")}</span>
                </button>
              ))}
          </div>

          {/* ---- Advanced 折叠区（逃生舱，真值明文） ---- */}
          <details
            className="mt-4 max-w-[640px] rounded-lg border border-line px-3.5 py-2.5"
            onToggle={(e) => {
              if ((e.target as HTMLDetailsElement).open) {
                // 冻结展开时刻的快照：之后 5s 轮询刷新 cfg 也不重写 textarea
                setAdvText(null);
                setAdvSnapshot(JSON.stringify(cfg, null, 2));
              }
            }}
          >
            <summary className="cursor-pointer text-[12.5px] text-muted select-none">{t("advanced")}</summary>
            <textarea
              className="field mt-2.5 font-mono text-xs"
              rows={11}
              spellCheck={false}
              value={advText ?? advSnapshot ?? ""}
              onChange={(e) => setAdvText(e.target.value)}
            />
            {advError && <p className="mt-1.5 text-xs text-red">{advError}</p>}
            <div className="mt-2 flex flex-wrap items-center gap-2.5">
              <button
                type="button"
                onClick={() => void saveAdvanced()}
                disabled={advText == null}
                className="rounded-lg bg-indigo px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-indigo2 disabled:opacity-45"
              >
                {t("saveReload")}
              </button>
              <span className="text-[11.5px] leading-relaxed text-faint">
                {t("advancedNote")} {t("advRealNote")}
              </span>
            </div>
          </details>
        </>
      )}

      {/* ---- 模态层 ---- */}
      {wizardOpen && (
        <McpAddWizard cfg={cfg} onClose={() => setWizardOpen(false)} onSave={saveWizard} onToast={showToast} />
      )}
      {importOpen && (
        <McpImportModal onClose={() => setImportOpen(false)} onImport={doImport} onToast={showToast} />
      )}
      {confirm && (
        <ConfirmModal
          title={confirm.title}
          message={confirm.message}
          confirmLabel={confirm.confirmLabel}
          onConfirm={confirm.onConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}
      {toast && (
        <div className="fixed bottom-6 left-1/2 z-[60] -translate-x-1/2">
          <div
            className={`rounded-lg border bg-card2 px-4 py-2 text-[13px] text-txt shadow-xl ${
              toast.tone === "ok"
                ? "border-green/45"
                : toast.tone === "err"
                  ? "border-red/50"
                  : "border-yellow/50"
            }`}
          >
            {toast.msg}
          </div>
        </div>
      )}
    </div>
  );
}
