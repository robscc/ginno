"use client";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { Puzzle, RefreshCw, Save, ShieldCheck } from "lucide-react";

// Claude Code Mods 设置页(claude-code-mods-design.md §7.5):mod 列表
// (名称/版本/状态/来源)、启停、配置 JSON 编辑、验证报告、运行时状态。
// 后端 REST/WS 由 runtime 侧并行实现——字段缺失时优雅降级,不报错。

const STATUS_CLASSES: Record<string, string> = {
  loaded: "text-green",
  error: "text-red",
  disabled: "text-faint",
  restarting: "text-yellow",
};

export function ModsSettings() {
  const t = useTranslations("settings.mods");
  // 状态/来源是服务端枚举,渲染时动态拼 key——走 engine 同款 string 签名
  // 收敛版本,静态 key 仍用受检的 t。
  const tr = t as unknown as {
    (key: string, values?: Record<string, string | number>): string;
  };
  const [mods, setMods] = useState<api.ModInfo[]>([]);
  const [broker, setBroker] = useState<api.ModsStatus["broker"]>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null); // 正在操作的 mod 名 / "validate"
  const [msg, setMsg] = useState("");
  // 配置编辑:当前展开编辑的 mod 名 + textarea 内容
  const [editName, setEditName] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [report, setReport] = useState<{ name: string; text: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listMods();
      setMods(Array.isArray(data?.mods) ? data.mods : []);
      setBroker(data?.broker ?? null);
    } catch {
      // sidecar 未起:保留上次状态,运行时区显示未连接
      setBroker(null);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // engine 的 mod.state.changed → 窗口事件:设置页开着就重拉
    const onChange = () => void refresh();
    window.addEventListener("ginno:mods-state-changed", onChange);
    return () => window.removeEventListener("ginno:mods-state-changed", onChange);
  }, [refresh]);

  const toggle = async (m: api.ModInfo) => {
    setBusy(m.name);
    setMsg("");
    try {
      await api.updateMod(m.name, { enabled: !(m.enabled ?? m.status !== "disabled") });
      await refresh();
    } catch {
      setMsg(t("actionFailed"));
    } finally {
      setBusy(null);
    }
  };

  const saveConfig = async (name: string) => {
    setMsg("");
    let parsed: unknown;
    try {
      parsed = JSON.parse(editText);
    } catch (e) {
      setMsg(t("configInvalid", { msg: e instanceof Error ? e.message : String(e) }));
      return;
    }
    setBusy(name);
    try {
      await api.updateMod(name, { config: parsed as Record<string, unknown> });
      setMsg(t("configSaved"));
      await refresh();
    } catch {
      setMsg(t("actionFailed"));
    } finally {
      setBusy(null);
    }
  };

  const validate = async (name: string) => {
    setBusy(`validate:${name}`);
    setMsg("");
    setReport(null);
    try {
      const r = await api.validateMod(name);
      setReport({ name, text: JSON.stringify(r, null, 2) });
    } catch {
      setMsg(t("validateFailed"));
    } finally {
      setBusy(null);
    }
  };

  const brokerUp = !!broker?.available;
  const nodePath = broker?.node_path ?? broker?.nodePath;

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div className="flex items-center gap-2">
        <Puzzle className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
        <button
          onClick={() => void refresh()}
          className="ml-auto rounded p-1 text-faint hover:text-txt"
          title={t("refresh")}
          aria-label={t("refresh")}
        >
          <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />
        </button>
      </div>
      <p className="text-sm text-muted">{t("intro")}</p>

      {/* 运行时状态:broker/node 两级探测(external-agents 的 available() 模式)。
          GET /mods 元信息缺失时降级为「未连接」。 */}
      <div className="rounded-lg border border-line bg-card/40 p-4">
        <div className="mb-2 text-sm font-medium text-txt">{t("runtime")}</div>
        <div className="space-y-1.5 text-sm">
          <div className="flex items-center gap-2">
            <span className={brokerUp ? "text-green" : "text-faint"}>{brokerUp ? "✓" : "✗"}</span>
            <span className="text-txt">{t("broker")}</span>
            {brokerUp && broker?.version && (
              <span className="text-xs text-faint">{t("brokerVersion", { version: broker.version })}</span>
            )}
            {!brokerUp && loaded && <span className="text-xs text-faint">{t("brokerUnavailable")}</span>}
          </div>
          <div className="flex items-center gap-2 text-xs text-faint">
            <span>{t("nodePath")}</span>
            <span className="font-mono">{nodePath || t("notDiscovered")}</span>
          </div>
          {broker?.error && <div className="text-xs text-red">{broker.error}</div>}
          <p className="text-xs text-faint">{t("dirHint")}</p>
        </div>
      </div>

      {/* mod 列表 */}
      <div className="rounded-lg border border-line bg-card/40 p-4">
        <div className="mb-3 text-sm font-medium text-txt">{t("list")}</div>
        {!loaded ? (
          <div className="text-xs text-faint">{t("loading")}</div>
        ) : mods.length === 0 ? (
          <div className="text-xs text-faint">{t("noMods")}</div>
        ) : (
          <div className="space-y-2">
            {mods.map((m) => {
              const status = m.status ?? "unknown";
              const enabled = m.enabled ?? status !== "disabled";
              return (
                <div key={m.name} className="rounded-lg border border-line px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm text-txt">{m.name}</span>
                    {m.version && <span className="text-xs text-faint">v{m.version}</span>}
                    <span className={`text-xs ${STATUS_CLASSES[status] ?? "text-faint"}`}>
                      {tr(`status.${status}`)}
                    </span>
                    {m.source && (
                      <span className="rounded bg-line/60 px-1.5 py-px text-[10px] text-muted">
                        {tr(`source.${m.source}`)}
                      </span>
                    )}
                    <label className="ml-auto flex cursor-pointer items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={enabled}
                        disabled={busy === m.name}
                        onChange={() => void toggle(m)}
                        className="h-3.5 w-3.5 accent-[#a78bfa]"
                      />
                      <span className="text-xs text-muted">{t("enable")}</span>
                    </label>
                  </div>
                  {m.error && <div className="mt-1 text-xs text-red">{m.error}</div>}
                  {m.dir && (
                    <div className="mt-1 truncate text-xs text-faint" title={m.dir}>
                      {t("dirLabel")}: <span className="font-mono">{m.dir}</span>
                    </div>
                  )}
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={() => {
                        // 展开配置编辑:以服务端返回的 config 为初始值
                        setEditName(editName === m.name ? null : m.name);
                        setEditText(JSON.stringify(m.config ?? {}, null, 2));
                        setMsg("");
                      }}
                      className="text-xs text-muted hover:text-txt"
                    >
                      {t("editConfig")}
                    </button>
                    <button
                      onClick={() => void validate(m.name)}
                      disabled={busy?.startsWith("validate:") === true}
                      className="flex items-center gap-1 text-xs text-muted hover:text-txt disabled:opacity-50"
                    >
                      <ShieldCheck className="h-3 w-3" />
                      {t("validate")}
                    </button>
                  </div>
                  {editName === m.name && (
                    <div className="mt-2">
                      <textarea
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        rows={6}
                        spellCheck={false}
                        className="w-full rounded-md border border-line2 bg-card px-2 py-1.5 font-mono text-xs text-txt"
                      />
                      <div className="mt-1 flex items-center gap-2">
                        <button
                          onClick={() => void saveConfig(m.name)}
                          disabled={busy === m.name}
                          className="flex items-center gap-1 rounded-md bg-violet px-2.5 py-1 text-xs text-white disabled:opacity-50"
                        >
                          <Save className="h-3 w-3" />
                          {t("saveConfig")}
                        </button>
                        <span className="text-[10px] text-faint">{t("configHint")}</span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 验证报告(broker validate:注册事件 × 触发、$ 调用 × 实现) */}
      {report && (
        <div className="rounded-lg border border-line bg-card/40 p-4">
          <div className="mb-2 text-sm font-medium text-txt">{t("validateReport", { name: report.name })}</div>
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-base p-3 font-mono text-xs text-txt">
            {report.text}
          </pre>
        </div>
      )}

      {msg && <div className="text-xs text-red">{msg}</div>}
    </div>
  );
}
