/* Quick-fix D6 回归：主题初始默认跟随 prefers-color-scheme。
 * node 环境无 DOM——matchMedia 用最小 fake（{matches}）模拟，组件侧
 * （AppShell/GeneralSettings 的接线）照仓库惯例做源码断言。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  THEME_KEY,
  initialTheme,
  resolveInitialTheme,
  systemPrefersLight,
} from "@/lib/themeInit";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveInitialTheme 裁决优先级：本地手动选择 > 系统偏好 > dark", () => {
  it("无本地选择 + 系统浅色 → light（新默认）", () => {
    expect(resolveInitialTheme(null, true)).toBe("light");
  });

  it("无本地选择 + 系统深色 → dark", () => {
    expect(resolveInitialTheme(null, false)).toBe("dark");
    expect(resolveInitialTheme(undefined, false)).toBe("dark");
  });

  it("本地选择永远赢（哪怕与系统相反）", () => {
    expect(resolveInitialTheme("light", false)).toBe("light");
    expect(resolveInitialTheme("dark", true)).toBe("dark");
  });

  it("matchMedia 不可用（null）→ 维持 legacy 默认 dark", () => {
    expect(resolveInitialTheme(null, null)).toBe("dark");
  });

  it("脏值视为无选择，交给系统偏好", () => {
    expect(resolveInitialTheme("blue", true)).toBe("light");
    expect(resolveInitialTheme("", false)).toBe("dark");
  });
});

describe("systemPrefersLight：matchMedia fake 模拟", () => {
  it("matches=true/false 正确透传", () => {
    expect(systemPrefersLight({ matches: true })).toBe(true);
    expect(systemPrefersLight({ matches: false })).toBe(false);
  });

  it("缺 matchMedia / 缺 matches → null（不瞎猜）", () => {
    expect(systemPrefersLight(null)).toBeNull();
    expect(systemPrefersLight(undefined)).toBeNull();
    expect(systemPrefersLight({} as { matches: boolean })).toBeNull();
  });
});

describe("initialTheme：window.matchMedia + localStorage 双桩", () => {
  const stubEnv = (opts: {
    stored: string | null;
    prefersLight?: boolean;
    matchMediaThrows?: boolean;
    localStorageThrows?: boolean;
  }) => {
    vi.stubGlobal(
      "window",
      opts.matchMediaThrows
        ? { matchMedia: () => { throw new Error("boom"); } }
        : {
            matchMedia: () => ({
              matches: opts.prefersLight ?? false,
            }),
          },
    );
    vi.stubGlobal(
      "localStorage",
      opts.localStorageThrows
        ? {
            getItem: () => { throw new Error("quota"); },
          }
        : {
            getItem: (k: string) => (k === THEME_KEY ? opts.stored : null),
          },
    );
  };

  it("无本地选择 + 系统浅色 → light", () => {
    stubEnv({ stored: null, prefersLight: true });
    expect(initialTheme()).toBe("light");
  });

  it("无本地选择 + 系统深色 → dark", () => {
    stubEnv({ stored: null, prefersLight: false });
    expect(initialTheme()).toBe("dark");
  });

  it("本地选择覆盖系统偏好", () => {
    stubEnv({ stored: "dark", prefersLight: true });
    expect(initialTheme()).toBe("dark");
  });

  it("matchMedia 抛异常 → dark（不崩、不瞎猜）", () => {
    stubEnv({ stored: null, matchMediaThrows: true });
    expect(initialTheme()).toBe("dark");
  });

  it("localStorage 抛异常 → 仍按系统偏好裁决", () => {
    stubEnv({ stored: null, prefersLight: true, localStorageThrows: true });
    expect(initialTheme()).toBe("light");
  });
});

describe("组件接线（源码断言）：初始化不落盘，手动切换才持久化", () => {
  const appShell = readFileSync(
    fileURLToPath(new URL("../components/shell/AppShell.tsx", import.meta.url)),
    "utf8",
  );
  const general = readFileSync(
    fileURLToPath(
      new URL("../components/settings/GeneralSettings.tsx", import.meta.url),
    ),
    "utf8",
  );

  it("AppShell 挂载用 initialTheme() 且 persist=false（系统默认不写成手动选择）", () => {
    expect(appShell).toContain("applyTheme(initialTheme(), false)");
  });

  it("GeneralSettings 初始化同样 initialTheme + persist=false", () => {
    expect(general).toMatch(/const t = initialTheme\(\);\s*setTheme\(t\);\s*applyTheme\(t, false\);/);
  });

  it("手动切换路径保持默认持久化（setThemeAndApply 里 applyTheme 不带 false）", () => {
    const m = general.match(/function setThemeAndApply\(t: string\) \{[\s\S]*?\}/);
    expect(m, "未找到 setThemeAndApply").toBeTruthy();
    expect(m![0]).toContain("applyTheme(t)");
    expect(m![0]).not.toContain("applyTheme(t, false)");
  });

  it("applyTheme 签名 persist 默认 true（既有手动调用零改动）", () => {
    expect(general).toMatch(/export function applyTheme\(t: string, persist = true\)/);
  });
});
