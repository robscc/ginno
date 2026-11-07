import { describe, expect, it, vi } from "vitest";
import { createToolsWithUiStore, type ToolsWithUiMap } from "./mcpApps";

const MAP: ToolsWithUiMap = {
  mcp_qr_show_qr: { server: "qr", tool: "mcp_qr_show_qr", resourceUri: "ui://qr/main" },
};

function makeStore(opts: { staleMs?: number } = {}) {
  let t = 0;
  const fetcher = vi.fn(async () => ({ ...MAP }));
  const store = createToolsWithUiStore(fetcher, { staleMs: opts.staleMs ?? 5 * 60_000, now: () => t });
  const advance = (ms: number) => {
    t += ms;
  };
  return { store, fetcher, advance };
}

describe("createToolsWithUiStore（tools-with-ui 清单缓存）", () => {
  it("首次 get 拉取并缓存；新鲜期内并发 get 单飞去重", async () => {
    const { store, fetcher } = makeStore();
    const [a, b] = await Promise.all([store.get(), store.get()]);
    expect(fetcher).toHaveBeenCalledTimes(1); // 单飞
    expect(a).toEqual(MAP);
    expect(b).toEqual(MAP);
    await store.get();
    expect(fetcher).toHaveBeenCalledTimes(1); // 缓存命中
  });

  it("peek/lookup：拉取前 null；拉取后命中 graph 全名", async () => {
    const { store } = makeStore();
    expect(store.peek()).toBeNull();
    expect(store.lookup("mcp_qr_show_qr")).toBeNull();
    await store.get();
    expect(store.peek()).toEqual(MAP);
    expect(store.lookup("mcp_qr_show_qr")?.server).toBe("qr");
    expect(store.lookup("mcp_nope_x")).toBeNull();
  });

  it("过期（staleMs）后 get 重新拉取", async () => {
    const { store, fetcher, advance } = makeStore({ staleMs: 1_000 });
    await store.get();
    advance(999);
    await store.get();
    expect(fetcher).toHaveBeenCalledTimes(1);
    advance(1);
    await store.get();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("refresh 强制重拉；同刻多次 refresh 共享同一 in-flight", async () => {
    const { store, fetcher } = makeStore();
    await store.get();
    await Promise.all([store.refresh(), store.refresh()]);
    expect(fetcher).toHaveBeenCalledTimes(2); // get 1 次 + refresh 单飞 1 次
  });

  it("拉取失败：错误传播且不污染旧缓存", async () => {
    let t = 0;
    let fail = false;
    const fetcher = vi.fn(async () => {
      if (fail) throw new Error("sidecar down");
      return { ...MAP };
    });
    const store = createToolsWithUiStore(fetcher, { now: () => t });
    await store.get();
    fail = true;
    await expect(store.refresh()).rejects.toThrow("sidecar down");
    expect(store.peek()).toEqual(MAP); // 旧清单保留：app 卡照常渲染
    expect(store.lookup("mcp_qr_show_qr")).not.toBeNull();
  });

  it("fetcher 返回非对象：按空清单容错（不炸渲染链）", async () => {
    const fetcher = vi.fn(async () => null as unknown as ToolsWithUiMap);
    const store = createToolsWithUiStore(fetcher);
    expect(await store.get()).toEqual({});
  });
});
