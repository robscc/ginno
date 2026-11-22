"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { KeyRound, Plus, RotateCw, Trash2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import {
  blankRow,
  buildUpdates,
  resolveConflicts,
  rowsFromEntries,
  sensitiveForKey,
  type EnvBase,
  type EnvRow,
} from "@/lib/envVarsRows";

/**
 * 环境变量注入设置（settings.json 的 "env" 键，tools/env_inject.py 消费）：
 * bash 工具（含 skill 命令）起子进程时合并注入——GUI 启动不继承代理等
 * 场景。PATH 由 runtime 端做追加语义，这里不特殊处理。
 *
 * 每键 CAS 版（契约 /tmp/ginno-settings-integrity-contract.md §2/§3）：
 * 视图来自 GET /api/settings/env（敏感键只回「已设置」元数据 + version
 * token，值永不回传）；保存走 PATCH /api/settings/env 全事务每键 CAS——
 * 两窗口各改各的键互不冲突，未触碰键不发（原值/类型原样保留）。
 *
 * 状态机：
 *   loading → ready（可编辑/保存） | loadFailed（行锁定，仅可手动重试）
 *           | corrupt（settings.json 损坏：锁写 + 恢复指引，见顶部横幅）
 *   ready 下 dirty（存在任一 PATCH op）才允许保存；
 *   409 version_conflict → 草稿原样保留，给出「覆盖 / 放弃并刷新」两个
 *   显式选择（绝不盲重试）；409 settings_corrupt → 切 corrupt 态；
 *   saving 期间全部控件锁定，ref 双保险防重复提交。
 *
 * 敏感键（键名含 TOKEN/SECRET/… 或值是认证连接串）：值默认不可见——
 * 只显示「已设置」+ scheme/host 元数据；替换 = password 输入整值重输；
 * 清除 = 删除行（remove op）。已存敏感键的键名锁定（值未知，重命名
 * 语义不成立），需要改名请删除后新增。
 */
type Phase = "loading" | "ready" | "loadFailed" | "corrupt";
type Status =
  | "idle"
  | "saving"
  | "saved"
  | "saveFailed"
  | "saveFailedConn"
  | "conflict"
  | "invalid";

export function EnvVarsSettings() {
  const t = useTranslations("settings.env");
  const [phase, setPhase] = useState<Phase>("loading");
  const [rows, setRows] = useState<EnvRow[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [saving, setSaving] = useState(false);
  const [conflicts, setConflicts] = useState<api.EnvConflict[]>([]);
  const [failDetail, setFailDetail] = useState("");
  const baseEntries = useRef<EnvBase>({}); // 最近一次成功读/写的服务端视图（CAS 基线）
  const loadSeq = useRef(0); // 过期响应守卫：慢响应不得覆盖新状态
  const savingRef = useRef(false); // 同一 tick 双击的防重复提交兜底

  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    setPhase("loading");
    setStatus("idle");
    setConflicts([]);
    api
      .getEnvSettings()
      .then((v) => {
        if (seq !== loadSeq.current) return;
        if (!v) {
          setPhase("loadFailed"); // 网络失败/非 2xx：行保持锁定，仅提供手动重试
          return;
        }
        if (v.status === "corrupt") {
          // settings.json 损坏：锁写保草稿不可能（无值可编辑），进入只读态；
          // 恢复流程在设置页顶部横幅（SettingsCorruptBanner）。
          baseEntries.current = {};
          setRows([]);
          setPhase("corrupt");
          return;
        }
        baseEntries.current = v.entries;
        setRows(rowsFromEntries(v.entries));
        setPhase("ready");
      })
      .catch(() => {
        if (seq !== loadSeq.current) return;
        setPhase("loadFailed"); // 行保持锁定，仅提供手动重试
      });
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const dirty = buildUpdates(rows, baseEntries.current).length > 0;
  const canSave = phase === "ready" && !saving && dirty && status !== "conflict";
  const locked = phase !== "ready" || saving;

  const update = (i: number, patch: Partial<EnvRow>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const updateKey = (i: number, key: string) =>
    // 键名变化即时重算本地敏感预判（镜像后端正则）：普通行输入 FOO_TOKEN
    // 后 value 框切 password；DSN 形态的值同样触发。已有敏感行键名锁定。
    setRows((rs) =>
      rs.map((r, j) => {
        if (j !== i) return r;
        const sensitive = r.originKey !== null && r.sensitive ? true : sensitiveForKey(key, r.sensitive ? r.replaceDraft : r.value);
        return { ...r, key, sensitive };
      }),
    );
  const updatePlainValue = (i: number, value: string) =>
    setRows((rs) =>
      rs.map((r, j) => {
        if (j !== i) return r;
        // 普通行值若成认证连接串形态 → 按敏感呈现（password），防肩窥
        const sensitive = r.originKey !== null && r.sensitive ? true : sensitiveForKey(r.key, value);
        return { ...r, value, sensitive };
      }),
    );
  const updateSecretDraft = (i: number, replaceDraft: string) => update(i, { replaceDraft });
  const remove = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));
  const add = () => setRows((rs) => [...rs, blankRow()]);

  async function save(overwrite = false) {
    // 加载未完成 / 保存中 / 无修改 一律拒绝提交（按钮 disabled 之外的第二道闸）；
    // 冲突未决时只有两个显式出口——env-conflict-overwrite（带 overwrite=true
    // 走 resolveConflicts 重建载荷）或 env-conflict-reload（丢弃草稿重拉）。
    if (savingRef.current || phase !== "ready" || !dirty) return;
    if (status === "conflict" && !overwrite) return;
    let ops = buildUpdates(rows, baseEntries.current);
    if (!ops.length) return;
    if (overwrite && conflicts.length) {
      // 用户显式选择「覆盖」：用服务端 conflicts 里的 current_version 重建载荷
      ops = resolveConflicts(ops, conflicts);
    }
    savingRef.current = true;
    setSaving(true);
    setStatus("saving");
    setFailDetail("");
    try {
      const r = await api.patchEnvSettings(ops);
      if (r.ok) {
        baseEntries.current = r.view.entries;
        setRows(rowsFromEntries(r.view.entries)); // 服务端权威视图（新 version token）
        setStatus("saved");
        setConflicts([]);
      } else if (r.error === "version_conflict") {
        // 另一窗口改了同键：全部未应用，草稿原样保留，等用户显式选择
        setConflicts(r.conflicts);
        setStatus("conflict");
      } else if (r.error === "settings_corrupt") {
        baseEntries.current = {};
        setRows([]);
        setPhase("corrupt");
      } else if (r.error === "invalid_request") {
        setFailDetail(r.detail);
        setStatus("invalid");
      } else if (r.error === "write_failed") {
        setFailDetail(r.detail);
        setStatus("saveFailed");
      } else {
        setStatus("saveFailedConn");
      }
    } catch {
      setStatus("saveFailedConn"); // 草稿不动
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  const statusError =
    phase === "loadFailed" ||
    status === "saveFailed" ||
    status === "saveFailedConn" ||
    status === "invalid";
  const statusText =
    phase === "loading"
      ? t("loading")
      : phase === "loadFailed"
        ? t("loadFailed")
        : status === "idle" || status === "conflict"
          ? "" // conflict 的说明与操作在专属区块（env-conflict），状态行不重复
          : status === "invalid"
            ? t("saveInvalid", { detail: failDetail })
            : status === "saveFailed"
              ? t("saveWriteFailed", { detail: failDetail })
              : t(status);
  const conflictKeys = conflicts.map((c) => c.key).join(", ");

  return (
    <div className="px-8 py-7" data-testid="env-vars">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">{t("description")}</p>
      {phase === "corrupt" && (
        <p data-testid="env-corrupt" role="alert" className="mt-4 max-w-2xl rounded border border-red/40 bg-red/[0.04] p-3 text-sm text-red">
          {t("corruptLine")}
        </p>
      )}
      <div className="mt-5 max-w-2xl space-y-1.5">
        {rows.map((r, i) => {
          const secretInput =
            r.sensitive && (r.originKey === null || r.replacing || r.replaceDraft !== "");
          return (
            <div key={r.id} className="flex items-center gap-2">
              <input
                data-testid={`env-key-${i}`}
                value={r.key}
                disabled={locked || (r.originKey !== null && r.sensitive)}
                title={r.originKey !== null && r.sensitive ? t("secretKeyLocked") : undefined}
                onChange={(e) => updateKey(i, e.target.value)}
                placeholder={t("namePlaceholder")}
                aria-label={t("namePlaceholder")}
                className="w-48 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt disabled:opacity-50"
              />
              {secretInput ? (
                <>
                  <input
                    data-testid={`env-value-${i}`}
                    type="password"
                    value={r.replaceDraft}
                    disabled={locked}
                    onChange={(e) => updateSecretDraft(i, e.target.value)}
                    placeholder={t("secretNewValue")}
                    aria-label={`${r.key || t("namePlaceholder")}: ${t("secretNewValue")}`}
                    autoComplete="off"
                    className="flex-1 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt disabled:opacity-50"
                  />
                  {r.originKey !== null && (
                    <button
                      data-testid={`env-replace-cancel-${i}`}
                      onClick={() => update(i, { replacing: false, replaceDraft: "" })}
                      disabled={locked}
                      title={t("cancelReplace")}
                      aria-label={t("cancelReplace")}
                      className="rounded p-1.5 text-faint hover:text-txt disabled:opacity-50"
                    >
                      <X size={14} />
                    </button>
                  )}
                </>
              ) : r.sensitive ? (
                <>
                  <span
                    data-testid={`env-secret-${i}`}
                    className="flex flex-1 items-center gap-2 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-muted"
                  >
                    <KeyRound size={13} className="shrink-0 text-faint" aria-hidden />
                    <span className="truncate">
                      {t("secretSet")}
                      {r.url?.host ? ` · ${r.url.scheme}://${r.url.host}` : ""}
                    </span>
                  </span>
                  <button
                    data-testid={`env-replace-${i}`}
                    onClick={() => update(i, { replacing: true })}
                    disabled={locked}
                    title={t("replace")}
                    className="shrink-0 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
                  >
                    {t("replace")}
                  </button>
                </>
              ) : (
                <input
                  data-testid={`env-value-${i}`}
                  value={r.value}
                  disabled={locked}
                  onChange={(e) => updatePlainValue(i, e.target.value)}
                  placeholder={t("valuePlaceholder")}
                  aria-label={`${r.key || t("namePlaceholder")}: ${t("valuePlaceholder")}`}
                  className="flex-1 rounded border border-line2 bg-card2 px-2 py-1 font-mono text-sm text-txt disabled:opacity-50"
                />
              )}
              <button
                data-testid={`env-remove-${i}`}
                onClick={() => remove(i)}
                disabled={locked}
                title={t("remove")}
                aria-label={`${t("remove")} ${r.key}`}
                className="rounded p-1.5 text-faint hover:text-red disabled:opacity-50"
              >
                <Trash2 size={14} />
              </button>
            </div>
          );
        })}
        <button
          data-testid="env-add"
          onClick={add}
          disabled={locked}
          className="flex items-center gap-1 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          <Plus size={12} /> {t("add")}
        </button>
        <p className="text-xs text-faint">{t("pathNote")}</p>
        {status === "conflict" && (
          <div data-testid="env-conflict" role="alert" className="max-w-2xl space-y-2 rounded border border-amber/50 bg-amber/[0.06] p-3 text-sm">
            <p className="font-medium text-txt">{t("conflict.title", { keys: conflictKeys })}</p>
            <p className="text-xs text-muted">{t("conflict.body")}</p>
            <div className="flex flex-wrap gap-2 pt-1">
              <button
                data-testid="env-conflict-overwrite"
                onClick={() => void save(true)}
                className="rounded bg-accent px-3 py-1 text-xs text-white"
              >
                {t("conflict.overwrite")}
              </button>
              <button
                data-testid="env-conflict-reload"
                onClick={load}
                className="rounded border border-line2 px-3 py-1 text-xs text-muted hover:text-txt"
              >
                {t("conflict.reload")}
              </button>
            </div>
          </div>
        )}
        <div className="flex items-center gap-3 pt-1">
          <button
            data-testid="env-save"
            onClick={() => void save()}
            disabled={!canSave}
            title={phase === "ready" && (!dirty || status === "conflict") ? (status === "conflict" ? t("conflict.title", { keys: conflictKeys }) : t("noChanges")) : undefined}
            className="rounded bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {t("save")}
          </button>
          {phase === "loadFailed" && (
            <button
              data-testid="env-retry"
              onClick={load}
              className="flex items-center gap-1 rounded border border-line2 px-2 py-1 text-xs text-muted hover:text-txt"
            >
              <RotateCw size={12} /> {t("retry")}
            </button>
          )}
          <p
            data-testid="env-status"
            role="status"
            className={`text-xs ${statusError ? "text-red" : "text-muted"}`}
          >
            {statusText}
          </p>
        </div>
      </div>
    </div>
  );
}
