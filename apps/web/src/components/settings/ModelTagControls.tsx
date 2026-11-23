"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ModelProtocol, ModelTag, ModelTagRecommendation } from "@/lib/types";
import { CODEX_CAPS } from "./modelConfigShared";

const TAGS = ["default", "codex"] as const;
const CAP_LABEL = { xhigh: "thinkingXhigh", max: "thinkingMax", ultra: "thinkingUltra" } as const;

export function ModelTagPicker({ model, tag, recommendation, onChange }: {
  model: string;
  tag?: ModelTag;
  recommendation?: ModelTagRecommendation;
  onChange: (tag?: ModelTag) => void;
}) {
  const t = useTranslations("settings.model.form.modelTag");
  const menuId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<Array<HTMLButtonElement | null>>([]);
  const [focused, setFocused] = useState<number | null>(null);
  const selected = tag ? "codex" : "default";
  const selectedIndex = TAGS.indexOf(selected);
  const open = focused !== null;
  const label = (value: (typeof TAGS)[number]) => value === "codex" ? "Codex" : t("default");

  useEffect(() => {
    if (focused !== null) items.current[focused]?.focus();
  }, [focused]);

  function closeAndFocusTrigger() {
    setFocused(null);
    trigger.current?.focus();
  }

  return (
    <div
      className="relative shrink-0"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setFocused(null);
      }}
    >
      <button
        ref={trigger}
        type="button"
        aria-label={t("pickerAria", { model, tag: label(selected) })}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        data-model-tag-picker
        onMouseDown={(e) => { if (open) e.preventDefault(); }}
        onClick={() => setFocused(open ? null : selectedIndex)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setFocused(e.key === "ArrowDown" ? 0 : TAGS.length - 1);
          }
        }}
        className="flex items-center gap-1.5 rounded-md border border-line2 bg-card px-2 py-1 text-xs font-medium text-muted hover:border-line hover:bg-card2 hover:text-txt"
      >
        {label(selected)}
        <ChevronDown aria-hidden="true" className="h-3 w-3 shrink-0 opacity-70" />
      </button>
      {open && (
        <div
          role="menu"
          id={menuId}
          aria-label={t("pickerAria", { model, tag: label(selected) })}
          onKeyDown={(e) => {
            switch (e.key) {
              case "Escape":
                e.preventDefault();
                e.stopPropagation();
                closeAndFocusTrigger();
                break;
              case "ArrowUp":
              case "ArrowDown":
                e.preventDefault();
                setFocused((index) => index === null ? null : 1 - index);
                break;
              case "Home":
              case "End":
                e.preventDefault();
                setFocused(e.key === "Home" ? 0 : TAGS.length - 1);
                break;
            }
          }}
          className="absolute right-0 top-full z-20 mt-1 w-36 rounded-lg border border-line bg-card py-1 shadow-xl"
        >
          {TAGS.map((value, index) => (
            <button
              key={value}
              ref={(el) => { items.current[index] = el; }}
              type="button"
              role="menuitemradio"
              aria-checked={selected === value}
              tabIndex={-1}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(value === "codex" ? tag ?? recommendation ?? { tag: "codex" } : undefined);
                closeAndFocusTrigger();
              }}
              className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-xs text-muted hover:bg-card2 hover:text-txt focus:bg-card2 focus:text-txt"
            >
              <Check aria-hidden="true" className={`h-3 w-3 shrink-0 ${selected === value ? "" : "invisible"}`} />
              {label(value)}
              {value === "codex" && recommendation && (
                <span className="ml-auto text-[10px] text-violet">{t("recommended")}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function CodexModelParameters({ tag, protocol, onChange }: {
  tag: ModelTag;
  protocol: ModelProtocol;
  onChange: (tag: ModelTag) => void;
}) {
  const t = useTranslations("settings.model.form.modelTag");
  const tc = useTranslations("chat.model");
  const anthropicField = tag.anthropic_effort_field ?? "output_config";
  const fieldName = protocol === "openai-responses" ? "reasoning.effort"
    : protocol === "anthropic" && anthropicField === "output_config" ? "output_config.effort"
    : "reasoning_effort";

  return (
    <details className="group px-3 pb-1.5 pl-8" data-codex-parameters>
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-2 gap-y-0.5 rounded text-[11px] text-muted hover:text-txt [&::-webkit-details-marker]:hidden">
        <ChevronDown aria-hidden="true" className="h-3 w-3 shrink-0 -rotate-90 group-open:rotate-0" />
        {t("parameters")}
        <code className="min-w-0 break-all text-faint">{fieldName}</code>
      </summary>
      <div className="mt-1.5 flex flex-wrap gap-2">
        <label className="min-w-0 flex-1 basis-40 text-[11px] text-muted">
          {t("maxEffort")}
          <select
            className="field mt-0.5 w-full"
            value={tag.max_effort ?? ""}
            onChange={(e) => onChange({ ...tag, max_effort: CODEX_CAPS.find((cap) => cap === e.target.value) })}
          >
            <option value="">{t("unknownCap")}</option>
            {CODEX_CAPS.map((cap) => <option key={cap} value={cap}>{tc(CAP_LABEL[cap])}</option>)}
          </select>
        </label>
        {protocol === "anthropic" && (
          <label className="min-w-0 flex-1 basis-40 text-[11px] text-muted">
            {t("anthropicField")}
            <select
              className="field mt-0.5 w-full"
              value={anthropicField}
              onChange={(e) => onChange({
                ...tag,
                anthropic_effort_field: e.target.value === "reasoning_effort" ? "reasoning_effort" : "output_config",
              })}
            >
              <option value="output_config">output_config.effort</option>
              <option value="reasoning_effort">reasoning_effort</option>
            </select>
          </label>
        )}
      </div>
    </details>
  );
}
