/**
 * Composer attachment helpers — shared by the main chat composer
 * (ChatStream.tsx) and the floating quick-chat window (PinStream.tsx).
 *
 * Moved verbatim out of ChatStream.tsx (steer-queue-shared-brief §3.2):
 * the image read/compress path, the document upload call, and the MIME split.
 * Behaviour and thresholds are UNCHANGED — readImage's 400KB / 1600px / JPEG
 * 0.85 numbers are load-bearing: the checkpointer rewrites the whole session
 * file on every step, so embedded images must stay small.
 */
import { uploadFile, debugLog } from "@/lib/runtime";
import type { FileOpErrorCode } from "@/lib/api/workspace";

/**
 * 上传上限，与 runtime `api/files.py` 的 `UPLOAD_MAX_BYTES`（100MB）保持
 * 同步——前端在发起上传前先本地预检，超限文件不必白传一遍再被后端拒；
 * 后端响应的 `code:"file_too_large"` 仍是权威兜底（两处常量若漂移，
 * 兜底路径会接住）。改后端常量时务必同步这里。
 */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

/** 上传失败的可见错误：携带后端的稳定错误码（如超限），调用方据此选文案。 */
export class UploadError extends Error {
  readonly code?: FileOpErrorCode;
  constructor(message: string, code?: FileOpErrorCode) {
    super(message);
    this.name = "UploadError";
    this.code = code;
  }
}

/** 人类可读的文件大小（错误文案用）：1 位小数的 MB / 整数 KB / B。 */
export function fmtUploadSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** 上传上限的展示文案（如 "100 MB"）——从 MAX_UPLOAD_BYTES 派生，不硬编码。 */
export function fmtUploadLimit(): string {
  return fmtUploadSize(MAX_UPLOAD_BYTES);
}

/** 上传失败的呈现决定（纯函数，node 环境直测）：该用哪个文案 key、带哪些
 *  参数。超限拒绝（code=file_too_large 或本地预检）→ tooLarge（文件名+
 *  大小+上限）；其它失败 → failed（带后端 error 文本）。UI 层把它交给
 *  pushNotice 弹可见错误——修复「>100MB 上传无声失败」（R3 巡检 Q1）。
 *  size 仅 attach-path 场景可缺省（前端无本地 File 对象；超限但拿不到
 *  大小时降级 failed 分支展示后端 error 文本，不产残缺的 tooLarge 文案）。 */
export function uploadFailureNotice(
  e: unknown,
  name: string,
  size?: number,
): { key: "tooLarge" | "failed"; params: Record<string, string> } {
  if (e instanceof UploadError && e.code === "file_too_large") {
    if (size == null) {
      return { key: "failed", params: { name, error: e.message || "file too large" } };
    }
    return { key: "tooLarge", params: { name, size: fmtUploadSize(size), max: fmtUploadLimit() } };
  }
  const detail = e instanceof Error && e.message ? e.message : String(e || "unknown");
  return { key: "failed", params: { name, error: detail } };
}

/** One image the user attached. `data` is the bare base64 payload (no data-url
 *  prefix) that the model-facing frame carries; `preview` is the full data URL
 *  used for local display. */
export interface ComposerImage {
  data: string;
  mediaType: string;
  preview: string;
  name: string;
}

/** Non-image attachment: uploaded to the sidecar, referenced by registry id. */
export interface ComposerFile {
  id: string;
  name: string;
  path: string;
  kind: string; // spreadsheet | table | document | presentation | pdf | …
  uploading?: boolean;
}

/** MIME split: `image/*` keeps the base64 → multimodal path, everything else
 *  is uploaded to the sidecar and attached by registry ref. */
export function isImageFile(file: File): boolean {
  return file.type.startsWith("image/");
}

/**
 * Read an image file as a data URL. Files over ~400KB are re-encoded through a
 * canvas (max 1600px, JPEG 0.85) — the checkpointer rewrites the whole session
 * file on every step, so keeping embedded images small matters.
 *
 * Resolves to null when the FileReader itself fails (the caller filters nulls
 * out) — this is the pre-existing behaviour and is relied on by both windows.
 */
export function readImage(file: File): Promise<ComposerImage | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const src = String(reader.result || "");
      const finish = (url: string) => {
        const m = /^data:([^;]+);base64,(.*)$/.exec(url);
        resolve(m ? { data: m[2], mediaType: m[1], preview: url, name: file.name } : null);
      };
      if (file.size <= 400_000) return finish(src);
      const img = new Image();
      img.onload = () => {
        const MAX = 1600;
        const scale = Math.min(1, MAX / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) return finish(src);
        ctx.drawImage(img, 0, 0, w, h);
        finish(canvas.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = () => finish(src);
      img.src = src;
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

/** Upload a document to the session and return the registered entry. Throws on
 *  failure (transport error or a non-ok server response); the caller decides
 *  how to surface it. The response telemetry lives here so both windows emit
 *  the same diagnostic. Server rejections throw UploadError carrying the
 *  backend's stable `code` (e.g. "file_too_large") so callers can tell a
 *  size-limit rejection apart from other failures. */
export async function uploadDoc(sessionId: string, file: File): Promise<ComposerFile> {
  const r = await uploadFile(sessionId, file);
  void debugLog({ where: "addFiles:upload-resp", name: file.name, ok: r?.ok, hasFile: !!r?.file, error: r?.error });
  if (r.ok && r.file) {
    const entry = r.file;
    return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
  }
  throw new UploadError(r.error || "upload failed", r.code);
}