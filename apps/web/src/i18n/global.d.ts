/**
 * next-intl 类型增强：Locale 收紧为 'en' | 'zh-CN'；Messages 取 en catalog
 * （source 语言，权威）。useTranslations 的 namespace/key 拼错会直接编译失败。
 *
 * 新增域文件时的三处同步（保持 en/zh-CN 成对）：
 *   1. messages/{en,zh-CN}/<domain>.json
 *   2. 本文件的 Messages intersection 追加一行
 *   3. provider.tsx 的 import 与 merge 各加一行
 */
import type { Locale } from "./config";

type CoreMessages = typeof import("../../messages/en/core.json");
type SettingsMessages = typeof import("../../messages/en/settings.json");
type ToolMessages = typeof import("../../messages/en/tool.json");
type ChatMessages = typeof import("../../messages/en/chat.json");
type ComposerMessages = typeof import("../../messages/en/composer.json");
type UiMessages = typeof import("../../messages/en/ui.json");
type ConnMessages = typeof import("../../messages/en/conn.json");
type KbMessages = typeof import("../../messages/en/kb.json");
type RightMessages = typeof import("../../messages/en/right.json");
type CodeMessages = typeof import("../../messages/en/code.json");
type ShellMessages = typeof import("../../messages/en/shell.json");
type StreamMessages = typeof import("../../messages/en/stream.json");
type SummaryMessages = typeof import("../../messages/en/summary.json");
type PinMessages = typeof import("../../messages/en/pin.json");
type ToolsMessages = typeof import("../../messages/en/tools.json");
type WfMessages = typeof import("../../messages/en/wf.json");
type ExtMessages = typeof import("../../messages/en/ext.json");
type SchedMessages = typeof import("../../messages/en/sched.json");
type GoalMessages = typeof import("../../messages/en/goal.json");

type AppMessages =
  CoreMessages &
  SettingsMessages &
  ToolMessages &
  ChatMessages &
  ComposerMessages &
  UiMessages &
  ConnMessages &
  KbMessages &
  RightMessages &
  CodeMessages &
  ShellMessages &
  StreamMessages &
  SummaryMessages &
  PinMessages &
  ToolsMessages &
  ExtMessages &
  WfMessages &
  SchedMessages &
  GoalMessages;

declare module "next-intl" {
  interface AppConfig {
    Locale: Locale;
    Messages: AppMessages;
  }
}
