// 重连退避契约测试(2026-11-18 每秒重连风暴修复)。
// 用法: node --test packages/extension/reconnect.test.mjs
//
// background.js 是 MV3 service worker(非 ES module,顶层即 chrome.* 调用),
// 不能直接 import——这里从源码提取 nextReconnectDelayMs 及常量定义求值,
// 源码级钉住:①指数退避 1s→…→60s 封顶 ②±20% 抖动 ③让位档位(attempt=5)
// 意味着 ≥32s 的让位间隔。函数或常量被误删/改坏,本测试转红。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const src = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "src", "background.js"), "utf8");

function extract(name) {
  const consts = src.match(/^const (RECONNECT_\w+|STABLE_CONN_S) = .*$/gm) || [];
  const fn = src.match(
    new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"))?.[0];
  if (!fn) throw new Error(`cannot extract ${name} from background.js`);
  // eslint-disable-next-line no-new-func
  const factory = new Function(`${consts.join("\n")}\n${fn}\nreturn ${name};`);
  return factory();
}

const delay = extract("nextReconnectDelayMs");

test("attempt=0 → 1s 档(±20% 抖动内)", () => {
  for (const r of [0, 0.25, 0.5, 0.75, 1]) {
    const d = delay(0, r);
    assert.ok(d >= 800 && d <= 1200, `attempt0 rand=${r} got ${d}`);
  }
});

test("指数递增且 60s 封顶(抖动后 ≤72s)", () => {
  let prev = 0;
  for (let a = 0; a <= 12; a++) {
    const d = delay(a, 0.5);           // 中位抖动(×1.0)
    assert.ok(d >= prev, `attempt=${a} delay=${d} not monotonic`);
    assert.ok(d <= 60000, `attempt=${a} delay=${d} exceeds cap`);
    prev = d;
  }
  assert.equal(delay(6, 0.5), 60000);
  assert.equal(delay(12, 0.5), 60000);
  assert.ok(delay(12, 1) <= 72000);    // +20% 抖动上限
});

test("让位档位 attempt=5 → ≥25.6s(抖动下限),远大于旧 1s 硬重试", () => {
  for (const r of [0, 0.5, 1]) {
    const d = delay(5, r);
    assert.ok(d >= 25600, `yield delay ${d} < 25.6s`);
  }
});

test("源码契约:onopen 不再重置 reconnectAttempt;replaced 抬档;存活 30s 规则在场", () => {
  assert.ok(!/onopen[\s\S]{0,220}?reconnectAttempt\s*=\s*0/.test(src),
    "onopen 不得重置退避档位(2026-11-18 根因)");
  assert.match(src, /method === "replaced"/);
  assert.match(src, /replacedByServer = true/);
  assert.match(src, /STABLE_CONN_S \* 1000/);
  assert.match(src, /S\.connectedAt = Date\.now\(\)/);
});
