import { describe, expect, it } from "vitest";
import { parseShellOutput } from "./toolOutput";

describe("parseShellOutput", () => {
  it("parses exit 0 with stdout and stderr sections", () => {
    const r = parseShellOutput("[exit 0]\nok line\n--- stderr ---\nwarn\n");
    expect(r?.ok).toBe(true);
    expect(r?.exitLine).toBe("exit 0");
    expect(r?.stdout).toBe("ok line"); // 不含 stderr 分隔行的前导换行
    expect(r?.stderr).toBe("warn\n");
  });

  it("flags non-zero exit as not ok", () => {
    expect(parseShellOutput("[exit 2]\nboom\n--- stderr ---\n")?.ok).toBe(false);
  });

  it("flags timeout as not ok even without stderr", () => {
    const r = parseShellOutput("[timeout after 30s — partial output kept]\npartial\n");
    expect(r?.ok).toBe(false);
    expect(r?.stderr).toBeNull();
    expect(r?.stdout).toBe("partial\n");
  });

  it("returns null for non-shell tool output (backward compatible)", () => {
    expect(parseShellOutput("wrote 3824 bytes to /tmp/x.json")).toBeNull();
    expect(parseShellOutput("[error] boom")).toBeNull(); // 无尾随换行，非完整首行
  });
});
