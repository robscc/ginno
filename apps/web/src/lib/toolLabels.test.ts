// toolLabels 单测：工具名 → 本地化标签的解析链
//（用户覆盖 > 内置 catalog > MCP 自动识别 > 原名兜底）。
//
// 打桩口径（对齐 desktopNotifyGate.test.ts 范式）：
// - JSON catalog 用真实文件（测的就是真实映射，不 mock）；
// - 只桩 ./runtime 的 getSettings，精确控制 settings 返回值/异常；
// - 模块级缓存（_catalog/_user/_labels/_loaded）无重置导出，每个用例
//   vi.resetModules() + 动态 import 拿全新实例，避免用例间串状态。

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Locale } from "@/i18n/config";

const { getSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(async () => ({}) as Record<string, unknown>),
}));
vi.mock("./runtime", () => ({ getSettings }));

async function fresh() {
  vi.resetModules();
  return import("@/lib/toolLabels");
}

beforeEach(() => {
  getSettings.mockClear();
  getSettings.mockResolvedValue({});
});

describe("初始状态与内置默认", () => {
  it("未加载时 isLabelsLoaded 为 false，但英文默认标签已可用", async () => {
    const m = await fresh();
    expect(m.isLabelsLoaded()).toBe(false);
    expect(m.toolLabel("read_file")).toBe("Reading file");
    expect(m.toolLabel("bash")).toBe("Running command");
  });

  it("DEFAULT_TOOL_LABELS 是英文 catalog 的完整拷贝", async () => {
    const m = await fresh();
    expect(m.DEFAULT_TOOL_LABELS).toEqual({
      read_file: "Reading file",
      write_file: "Writing file",
      edit_file: "Editing file",
      glob_files: "Searching files",
      grep_files: "Searching content",
      bash: "Running command",
      delegate_agent: "Delegating to external agent",
      parse_document: "Parsing document",
      generate_image: "Generating image",
    });
  });

  it("未知工具原样返回；空名返回空串", async () => {
    const m = await fresh();
    expect(m.toolLabel("no_such_tool")).toBe("no_such_tool");
    expect(m.toolLabelOptions("no_such_tool")).toEqual(["no_such_tool"]);
    expect(m.toolLabel("")).toBe("");
  });
});

describe("locale 切换", () => {
  it("zh-CN 切到中文 catalog（含 MCP 模板）", async () => {
    const m = await fresh();
    m.setToolLabelsLocale("zh-CN");
    expect(m.toolLabel("read_file")).toBe("读取文件中");
    expect(m.toolLabel("delegate_agent")).toBe("转交外部代理中");
    expect(m.toolLabel("generate_image")).toBe("生成图片中");
    expect(m.toolLabel("mcp_trending_list")).toBe("调用 MCP：trending");
  });

  it("未知 locale 回落英文 catalog", async () => {
    const m = await fresh();
    m.setToolLabelsLocale("fr" as unknown as Locale);
    expect(m.toolLabel("read_file")).toBe("Reading file");
    expect(m.toolLabel("mcp_x_y")).toBe("Calling MCP: x");
  });

  it("切换 locale 后用户覆盖保持优先，其余键跟随 catalog", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { read_file: "我的读取" } });
    await m.loadToolLabels();
    m.setToolLabelsLocale("zh-CN");
    expect(m.toolLabel("read_file")).toBe("我的读取");
    expect(m.toolLabel("write_file")).toBe("写文件中");
    m.setToolLabelsLocale("en");
    expect(m.toolLabel("read_file")).toBe("我的读取");
    expect(m.toolLabel("write_file")).toBe("Writing file");
  });
});

describe("loadToolLabels / refreshToolLabels", () => {
  it("用户覆盖优先于内置默认，其余键保持默认；加载后 isLabelsLoaded 为 true", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { bash: "跑命令" } });
    await m.loadToolLabels();
    expect(m.isLabelsLoaded()).toBe(true);
    expect(m.toolLabel("bash")).toBe("跑命令");
    expect(m.toolLabel("read_file")).toBe("Reading file");
  });

  it("settings 无 tool_labels 键 → 纯内置默认", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ other_key: 1 });
    await m.loadToolLabels();
    expect(m.toolLabel("bash")).toBe("Running command");
  });

  it("tool_labels 为 null → 按空覆盖处理", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: null });
    await m.loadToolLabels();
    expect(m.toolLabel("edit_file")).toBe("Editing file");
  });

  it("getSettings 失败 → 保留内置默认并清掉旧覆盖（refresh 同路径）", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { bash: "自定义命令" } });
    await m.loadToolLabels();
    expect(m.toolLabel("bash")).toBe("自定义命令");
    getSettings.mockRejectedValue(new Error("sidecar down"));
    await m.refreshToolLabels();
    expect(m.toolLabel("bash")).toBe("Running command");
    expect(m.isLabelsLoaded()).toBe(true);
  });

  it("英文 UI 下旧版中文种子迁移为新默认（含多段值与首尾空白）", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({
      tool_labels: {
        read_file: "读取文件中",
        write_file: "写文件中|Saving file",
        edit_file: " 编辑文件中 ",
        bash: "我的命令", // 非种子值：用户真自定义，不动
      },
    });
    await m.loadToolLabels();
    expect(m.toolLabel("read_file")).toBe("Reading file");
    expect(m.toolLabelOptions("write_file")).toEqual(["Writing file", "Saving file"]);
    expect(m.toolLabel("edit_file")).toBe("Editing file");
    expect(m.toolLabel("bash")).toBe("我的命令");
  });

  it("zh-CN UI 下中文值不被误迁移（2026-11-04 实案回归）", async () => {
    const m = await fresh();
    m.setToolLabelsLocale("zh-CN");
    getSettings.mockResolvedValue({ tool_labels: { edit_file: "编辑文件中" } });
    await m.loadToolLabels();
    expect(m.toolLabel("edit_file")).toBe("编辑文件中");
  });

  it("refreshToolLabels 在设置保存后刷新缓存", async () => {
    const m = await fresh();
    await m.loadToolLabels();
    expect(m.toolLabel("bash")).toBe("Running command");
    getSettings.mockResolvedValue({ tool_labels: { bash: "跑命令" } });
    await m.refreshToolLabels();
    expect(m.toolLabel("bash")).toBe("跑命令");
  });
});

describe("toolLabelOptions / toolLabel", () => {
  it("多候选值按 | 拆分、去首尾空白、滤空段；toolLabel 取第一个", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { bash: " Running | Executing | " } });
    await m.loadToolLabels();
    expect(m.toolLabelOptions("bash")).toEqual(["Running", "Executing"]);
    expect(m.toolLabel("bash")).toBe("Running");
  });

  it("配置值全为空白段 → 回退工具原名", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { bash: " | " } });
    await m.loadToolLabels();
    expect(m.toolLabelOptions("bash")).toEqual(["bash"]);
  });

  it("MCP 自动识别：server 取第二段，工具名里的下划线不影响", async () => {
    const m = await fresh();
    expect(m.toolLabelOptions("mcp_trending_list")).toEqual(["Calling MCP: trending"]);
    expect(m.toolLabel("mcp_my_server_tool")).toBe("Calling MCP: my");
  });

  it("mcp_ 前缀但不足三段 → 原名兜底", async () => {
    const m = await fresh();
    expect(m.toolLabel("mcp_foo")).toBe("mcp_foo");
    expect(m.toolLabel("mcp_")).toBe("mcp_");
  });

  it("用户显式配置的 mcp_ 标签优先于自动识别", async () => {
    const m = await fresh();
    getSettings.mockResolvedValue({ tool_labels: { mcp_trending_list: "看趋势" } });
    await m.loadToolLabels();
    expect(m.toolLabel("mcp_trending_list")).toBe("看趋势");
  });
});
