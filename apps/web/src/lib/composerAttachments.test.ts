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

import {
  isImageFile,
  readImage,
  svgIntrinsicSize,
  uploadDoc,
  MAX_UPLOAD_BYTES,
  UploadError,
  fmtUploadSize,
  fmtUploadLimit,
  uploadFailureNotice,
} from "@/lib/composerAttachments";

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
const fillRect = vi.fn();
const toDataURL = vi.fn(() => "data:image/jpeg;base64,RECODED");
const getContext = vi.fn(
  (): { drawImage: typeof drawImage; fillRect: typeof fillRect; fillStyle: string } | null => ({
    drawImage,
    fillRect,
    fillStyle: "",
  }),
);
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

  it("重编码前先铺白底：JPEG 无 alpha，透明区不再合成纯黑（D9）", async () => {
    imageState.width = 800;
    imageState.height = 600;
    await readImage(big());
    expect(fillRect).toHaveBeenCalledWith(0, 0, 800, 600);
    expect(getContext.mock.results[0].value.fillStyle).toBe("#ffffff");
  });
});

describe("readImage 无固有尺寸 SVG（viewBox 兜底，D9）", () => {
  // viewBox-only SVG（无 width/height 属性）——Image 报 0×0，侦察实测会被
  // 压成 200×150 微缩图；修后走 svgIntrinsicSize 解析宽高比再压缩。
  const svgUrl = (svg: string) => `data:image/svg+xml;base64,${btoa(svg)}`;
  const viewBoxOnly = svgUrl(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2000 1000"><rect width="2000" height="1000"/></svg>',
  );

  it("viewBox 2000×1000 + Image 报 0×0 → 按解析宽高缩到 1600×800", async () => {
    readerState.result = viewBoxOnly;
    imageState.width = 0;
    imageState.height = 0;
    const out = await readImage(mkFile("big.svg", "image/svg+xml", 500_000));
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(800);
    expect(drawImage.mock.calls[0].slice(1)).toEqual([0, 0, 1600, 800]);
    expect(out?.mediaType).toBe("image/jpeg");
  });

  it("Chrome 真实行为复刻：viewBox 2000×1000 的 SVG，Image 报按比例夹进 300×150 框的 300×150 → 仍按源码宽高缩到 1600×800", async () => {
    readerState.result = viewBoxOnly;
    imageState.width = 300; // 侦察实测：Chrome 对无尺寸 SVG 报 viewBox 比例 fit 默认框
    imageState.height = 150;
    await readImage(mkFile("big.svg", "image/svg+xml", 500_000));
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(800);
  });

  it("解析失败（无 viewBox 无数字宽高）→ 原样透传，绝不缩成微缩图", async () => {
    readerState.result = svgUrl('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>');
    imageState.width = 0;
    imageState.height = 0;
    const out = await readImage(mkFile("big.svg", "image/svg+xml", 500_000));
    expect(out?.mediaType).toBe("image/svg+xml");
    expect(createElement).not.toHaveBeenCalled();
  });

  it("带数字 width/height 属性的 SVG 优先取属性（即使 Image 报 0）", async () => {
    readerState.result = svgUrl(
      '<svg xmlns="http://www.w3.org/2000/svg" width="4000" height="1000" viewBox="0 0 4000 1000"/>',
    );
    imageState.width = 0;
    imageState.height = 0;
    await readImage(mkFile("big.svg", "image/svg+xml", 500_000));
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(400);
  });

  it("解析失败但 Image 报出有效尺寸（如带 % 宽高的 SVG）→ 用 Image 尺寸，不透传", async () => {
    readerState.result = svgUrl('<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="50%"/>');
    imageState.width = 800;
    imageState.height = 600;
    await readImage(mkFile("big.svg", "image/svg+xml", 500_000));
    expect(canvas.width).toBe(800);
    expect(canvas.height).toBe(600);
  });
});

describe("svgIntrinsicSize 解析", () => {
  const url = (svg: string) => `data:image/svg+xml;base64,${btoa(svg)}`;

  it("width/height 数字属性 → [w, h]", () => {
    expect(svgIntrinsicSize(url('<svg width="800" height="600"/>'))).toEqual([800, 600]);
  });

  it("height 在前也能解析（属性顺序无关）", () => {
    expect(svgIntrinsicSize(url('<svg height="600" width="800"/>'))).toEqual([800, 600]);
  });

  it("仅 viewBox → 取 w/h", () => {
    expect(svgIntrinsicSize(url('<svg viewBox="0 0 1200 300"/>'))).toEqual([1200, 300]);
  });

  it("无可用信息 → null", () => {
    expect(svgIntrinsicSize(url('<svg viewBox="0 0 100 0"/>'))).toBeNull(); // h=0 无效
    expect(svgIntrinsicSize(url("<svg/>"))).toBeNull();
    expect(svgIntrinsicSize("data:image/svg+xml;base64,!!!not-base64!!!")).toBeNull();
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

describe("Q1 超限可见报错：UploadError / uploadFailureNotice", () => {
  const big = mkFile("big.bin", "application/octet-stream", 150 * 1024 * 1024);

  it("上限常量钉在 100MB（与 runtime files.py UPLOAD_MAX_BYTES 同步，漂移即红）", () => {
    expect(MAX_UPLOAD_BYTES).toBe(100 * 1024 * 1024);
    expect(fmtUploadLimit()).toBe("100.0 MB");
  });

  it("超限拒绝：uploadDoc 抛 UploadError 带 code=file_too_large；呈现决定走 tooLarge（名字+大小+上限）", async () => {
    uploadFile.mockResolvedValue({
      ok: false,
      code: "file_too_large",
      error: "File too large (max 100MB)",
    });
    const err = await uploadDoc("s1", big).catch((e) => e);
    expect(err).toBeInstanceOf(UploadError);
    expect(err.code).toBe("file_too_large");
    expect(uploadFailureNotice(err, "big.bin", big.size)).toEqual({
      key: "tooLarge",
      params: { name: "big.bin", size: "150.0 MB", max: "100.0 MB" },
    });
  });

  it("本地预检超限（不依赖后端）：手造 UploadError(code) 同样命中 tooLarge", () => {
    const n = uploadFailureNotice(new UploadError("file too large", "file_too_large"), "x.iso", 101 * 1024 * 1024);
    expect(n.key).toBe("tooLarge");
    expect(n.params.size).toBe("101.0 MB");
  });

  it("其它服务端失败：code 缺失 → failed 分支带后端 error 文本", async () => {
    uploadFile.mockResolvedValue({ ok: false, error: "磁盘满了" });
    const err = await uploadDoc("s1", big).catch((e) => e);
    expect(err).toBeInstanceOf(UploadError);
    expect(err.code).toBeUndefined();
    expect(uploadFailureNotice(err, "big.bin", big.size)).toEqual({
      key: "failed",
      params: { name: "big.bin", error: "磁盘满了" },
    });
  });

  it("传输层普通 Error（非 UploadError）→ failed 分支取 message", () => {
    expect(uploadFailureNotice(new Error("network down"), "a.pdf", 1024)).toEqual({
      key: "failed",
      params: { name: "a.pdf", error: "network down" },
    });
  });

  it("非 Error 异常（字符串/null）→ failed 分支 String() 兜底不抛", () => {
    expect(uploadFailureNotice("boom", "a.pdf", 1024)).toEqual({
      key: "failed",
      params: { name: "a.pdf", error: "boom" },
    });
    expect(uploadFailureNotice(null, "a.pdf", 0)).toEqual({
      key: "failed",
      params: { name: "a.pdf", error: "unknown" },
    });
  });

  it("fmtUploadSize 三档：B / KB / MB（1 位小数）", () => {
    expect(fmtUploadSize(0)).toBe("0 B");
    expect(fmtUploadSize(999)).toBe("999 B");
    expect(fmtUploadSize(1024)).toBe("1 KB");
    expect(fmtUploadSize(1536)).toBe("2 KB"); // round(1.5)=2
    expect(fmtUploadSize(1024 * 1024)).toBe("1.0 MB");
    expect(fmtUploadSize(1048576 * 105.5)).toBe("105.5 MB");
  });
});

describe("Q2 attach-path 超限：size 缺省时的降级", () => {
  it("超限但拿不到 size（attach-path 异常路径）→ 降级 failed 分支带后端 error 文本，不出残缺 tooLarge 文案", () => {
    expect(uploadFailureNotice(new UploadError("File too large (max 100MB)", "file_too_large"), "big.iso")).toEqual({
      key: "failed",
      params: { name: "big.iso", error: "File too large (max 100MB)" },
    });
  });

  it("attach-path 超限且后端带 size → tooLarge 文案（名字+大小+上限）", () => {
    expect(
      uploadFailureNotice(new UploadError("File too large (max 100MB)", "file_too_large"), "big.iso", 150 * 1024 * 1024),
    ).toEqual({
      key: "tooLarge",
      params: { name: "big.iso", size: "150.0 MB", max: "100.0 MB" },
    });
  });
});
