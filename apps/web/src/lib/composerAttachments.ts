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
 *  the same diagnostic. */
export async function uploadDoc(sessionId: string, file: File): Promise<ComposerFile> {
  const r = await uploadFile(sessionId, file);
  void debugLog({ where: "addFiles:upload-resp", name: file.name, ok: r?.ok, hasFile: !!r?.file, error: r?.error });
  if (r.ok && r.file) {
    const entry = r.file;
    return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
  }
  throw new Error(r.error || "upload failed");
}