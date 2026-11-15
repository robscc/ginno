// composerAttachments 单测：MIME 分流 / 图片读取与压缩 / 文档上传。
// node 环境无 FileReader/Image/document —— 全部用 vi.stubGlobal 造可控桩；
// uploadFile/debugLog 来自 @/lib/runtime，用 vi.mock 桩掉以控制成败。
// 只桩依赖，不 mock 被测模块自身。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { uploadFile, debugLog } = vi.hoisted(() => ({
  uploadFile: vi.fn(),
  debugLog: vi.fn(async () => {}),
}));
vi.mock("@/lib/runtime", () => ({ uploadFile, debugLog }));

import { isImageFile, readImage, uploadDoc } from "@/lib/composerAttachments";

/** 被测代码只读 file.name/type/size，_plain object 即可，无需真 File。 */
function mkFile(name: string, type: string, size = 10): File {
  return { name, type, size } as unknown as File;
}

// ---- FileReader 桩：行为由 readerState 驱动 ----
const readerState: { result?: string | null; fail?: boolean } = {};
class FakeFileReader {
  result: string | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readAsDataURL() {
    if (readerState.fail) {
      this.onerror?.();
      return;
    }
    this.result = readerState.result ?? null;
    this.onload?.();
  }
}

// ---- Image 桩：src 赋值即触发 onload/onerror，尺寸由 imageState 驱动 ----
const imageState = { width: 0, height: 0, fail: false };
class FakeImage {
  width = 0;
  height = 0;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(_v: string) {
    this.width = imageState.width;
    this.height = imageState.height;
    if (imageState.fail) this.onerror?.();
    else this.onload?.();
  }
}

// ---- canvas / document 桩 ----
const drawImage = vi.fn();
const toDataURL = vi.fn(() => "data:image/jpeg;base64,RECODED");
const getContext = vi.fn((): { drawImage: typeof drawImage } | null => ({ drawImage }));
const canvas = { width: 0, height: 0, getContext, toDataURL };
const createElement = vi.fn(() => canvas);

const PNG_URL = "data:image/png;base64,AAAA";

beforeEach(() => {
  vi.clearAllMocks();
  uploadFile.mockReset();
  readerState.result = PNG_URL;
  readerState.fail = false;
  imageState.width = 0;
  imageState.height = 0;
  imageState.fail = false;
  canvas.width = 0;
  canvas.height = 0;
  vi.stubGlobal("FileReader", FakeFileReader);
  vi.stubGlobal("Image", FakeImage);
  vi.stubGlobal("document", { createElement });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isImageFile MIME 分流", () => {
  it("image/* 走 base64 多模态路径", () => {
    for (const t of ["image/png", "image/jpeg", "image/webp", "image/svg+xml", "image/gif"]) {
      expect(isImageFile(mkFile("a", t)), t).toBe(true);
    }
  });

  it("非 image 一律走 sidecar 上传路径", () => {
    for (const t of ["application/pdf", "text/plain", "application/octet-stream", "video/mp4", ""]) {
      expect(isImageFile(mkFile("a", t)), t).toBe(false);
    }
  });

  it("quirk：大小写敏感，IMAGE/PNG 不被识别（MIME 规范要求小写，固化现状）", () => {
    expect(isImageFile(mkFile("a", "IMAGE/PNG"))).toBe(false);
    expect(isImageFile(mkFile("a", "image"))).toBe(false); // 无斜杠也不算
  });
});

describe("readImage 小图直读（≤400KB）", () => {
  it("合法 data URL → 拆出裸 base64/mediaType/preview/name", async () => {
    const out = await readImage(mkFile("pic.png", "image/png", 100));
    expect(out).toEqual({ data: "AAAA", mediaType: "image/png", preview: PNG_URL, name: "pic.png" });
    expect(createElement).not.toHaveBeenCalled(); // 未走 canvas
  });

  it("边界：恰好 400_000 字节仍直读，400_001 进压缩路径", async () => {
    await readImage(mkFile("a.png", "image/png", 400_000));
    expect(createElement).not.toHaveBeenCalled();
    imageState.width = 800;
    imageState.height = 600;
    await readImage(mkFile("b.png", "image/png", 400_001));
    expect(createElement).toHaveBeenCalledWith("canvas");
  });

  it("结果不是 data URL → null（调用方负责滤掉）", async () => {
    for (const bad of ["not-a-data-url", "data:image/png,AAAA", "data:;base64,AAAA"]) {
      readerState.result = bad;
      await expect(readImage(mkFile("a.png", "image/png")), bad).resolves.toBeNull();
    }
  });

  it("quirk：带额外参数的 data URL（charset）正则不认 → null，固化现状", async () => {
    readerState.result = "data:image/png;charset=utf-8;base64,AAAA";
    await expect(readImage(mkFile("a.png", "image/png"))).resolves.toBeNull();
  });

  it("reader.result 为 null/空串 → String(x || \"\") 兜底后仍解析失败 → null", async () => {
    readerState.result = null;
    await expect(readImage(mkFile("a.png", "image/png"))).resolves.toBeNull();
    readerState.result = "";
    await expect(readImage(mkFile("a.png", "image/png"))).resolves.toBeNull();
  });

  it("FileReader 本身失败 → null，不 reject", async () => {
    readerState.fail = true;
    await expect(readImage(mkFile("a.png", "image/png"))).resolves.toBeNull();
  });
});

describe("readImage 大图压缩（>400KB → canvas JPEG 0.85）", () => {
  const big = () => mkFile("big.png", "image/png", 500_000);

  it("3200×1600 → 缩到 1600×800，drawImage/toDataURL 参数正确", async () => {
    imageState.width = 3200;
    imageState.height = 1600;
    const out = await readImage(big());
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(800);
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(drawImage.mock.calls[0].slice(1)).toEqual([0, 0, 1600, 800]);
    expect(toDataURL).toHaveBeenCalledWith("image/jpeg", 0.85);
    expect(out).toEqual({
      data: "RECODED",
      mediaType: "image/jpeg",
      preview: "data:image/jpeg;base64,RECODED",
      name: "big.png",
    });
  });

  it("小于 1600px 的图不放大：800×600 原尺寸重编码", async () => {
    imageState.width = 800;
    imageState.height = 600;
    await readImage(big());
    expect(canvas.width).toBe(800);
    expect(canvas.height).toBe(600);
    expect(drawImage.mock.calls[0].slice(1)).toEqual([0, 0, 800, 600]);
  });

  it("极端长条 10000×2 → 短边 Math.max(1,…) 保底为 1px", async () => {
    imageState.width = 10000;
    imageState.height = 2;
    await readImage(big());
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(1); // round(2*0.16)=0 → 保底 1
  });

  it("canvas 2d 上下文拿不到 → 退回原始 data URL，不抛错", async () => {
    imageState.width = 3200;
    imageState.height = 1600;
    getContext.mockReturnValueOnce(null);
    const out = await readImage(big());
    expect(out).toEqual({ data: "AAAA", mediaType: "image/png", preview: PNG_URL, name: "big.png" });
    expect(toDataURL).not.toHaveBeenCalled();
  });

  it("图片解码失败（img.onerror）→ 退回原始 data URL", async () => {
    imageState.fail = true;
    const out = await readImage(big());
    expect(out).toEqual({ data: "AAAA", mediaType: "image/png", preview: PNG_URL, name: "big.png" });
    expect(createElement).not.toHaveBeenCalled();
  });
});

describe("uploadDoc 文档上传", () => {
  const file = mkFile("report.pdf", "application/pdf", 2048);
  const entry = {
    id: "f1",
    name: "report.pdf",
    path: "/data/report.pdf",
    kind: "pdf",
    mime: "application/pdf", // 额外字段：返回值必须剥掉
    size: 2048,
  };

  it("成功：只回 {id,name,path,kind}，遥测记录 ok/hasFile", async () => {
    uploadFile.mockResolvedValue({ ok: true, file: entry });
    const out = await uploadDoc("s1", file);
    expect(out).toEqual({ id: "f1", name: "report.pdf", path: "/data/report.pdf", kind: "pdf" });
    expect(uploadFile).toHaveBeenCalledWith("s1", file);
    expect(debugLog).toHaveBeenCalledWith({
      where: "addFiles:upload-resp",
      name: "report.pdf",
      ok: true,
      hasFile: true,
      error: undefined,
    });
  });

  it("服务端报错：抛 Error(error)，遥测先于失败记录", async () => {
    uploadFile.mockResolvedValue({ ok: false, error: "磁盘满了" });
    await expect(uploadDoc("s1", file)).rejects.toThrow("磁盘满了");
    expect(debugLog).toHaveBeenCalledWith(
      expect.objectContaining({ ok: false, hasFile: false, error: "磁盘满了" }),
    );
  });

  it("ok=false 且无 error → 兜底文案 upload failed", async () => {
    uploadFile.mockResolvedValue({ ok: false });
    await expect(uploadDoc("s1", file)).rejects.toThrow("upload failed");
  });

  it("ok=true 但 file 缺失 → 同样兜底 upload failed", async () => {
    uploadFile.mockResolvedValue({ ok: true });
    await expect(uploadDoc("s1", file)).rejects.toThrow("upload failed");
    expect(debugLog).toHaveBeenCalledWith(expect.objectContaining({ ok: true, hasFile: false }));
  });

  it("error 为空串 → || 兜底 upload failed", async () => {
    uploadFile.mockResolvedValue({ ok: false, error: "" });
    await expect(uploadDoc("s1", file)).rejects.toThrow("upload failed");
  });

  it("传输层 reject：原样向上抛，遥测来不及记录", async () => {
    uploadFile.mockRejectedValue(new Error("network down"));
    await expect(uploadDoc("s1", file)).rejects.toThrow("network down");
    expect(debugLog).not.toHaveBeenCalled();
  });

  it("quirk：uploadFile  resolve null → r.ok 读属性抛 TypeError（非友好文案），固化现状", async () => {
    uploadFile.mockResolvedValue(null);
    await expect(uploadDoc("s1", file)).rejects.toThrow(TypeError);
    expect(debugLog).toHaveBeenCalledWith(expect.objectContaining({ ok: undefined, hasFile: false }));
  });
});
