"use client";

// useComposerFiles — composer 的文件/图片挂载管线（自 ChatStream.tsx 机械拆出，
// 行为零变化）：图片 base64 读取、文档上传、native 路径挂载、回执按 slot 归还
// （landFile）与表格类自动预览（maybeAutoPreview）。

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { debugLog, attachFilePath } from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import {
  readImage,
  uploadDoc,
  isImageFile,
  MAX_UPLOAD_BYTES,
  UploadError,
  uploadFailureNotice,
} from "@/lib/composerAttachments";
import { TABLE_KINDS, type Attachment, type FileAttachment } from "./streamCore";
import { HOME_SLOT } from "./useChatStreamEngine";
import type { SessionMeta } from "@/lib/types";
import type { ResolvedMention } from "./commandMenu";

interface ComposerFilesDeps {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  setAttachments: (v: Attachment[] | ((p: Attachment[]) => Attachment[])) => void;
  setFileAttachments: (v: FileAttachment[] | ((p: FileAttachment[]) => FileAttachment[])) => void;
  // engine 持有的共享 ref（回执落 slot / 首页延后上传缓冲）。engine 依赖
  // uploadOneDoc/attachOne，而它们又要读 engine 的 ref——经这个 holder 打破
  // 环：主文件在 engine hook 返回后立即填充，本 hook 的函数只在调用期读取
  // （用户交互 / effect 期），与拆分前提升函数声明的 late-binding 等价。
  refs: {
    current: {
      curSessionIdRef: { current: string | null };
      draftCacheRef: {
        current: Record<
          string,
          { input: string; attachments: Attachment[]; files?: FileAttachment[]; mentions?: ResolvedMention[] }
        >;
      };
      pendingDocsRef: { current: Array<{ file: File; tmpId: string }> };
      pendingPathsRef: { current: Array<{ path: string; tmpId: string }> };
    } | null;
  };
}

export function useComposerFiles({
  g,
  session,
  setAttachments,
  setFileAttachments,
  refs,
}: ComposerFilesDeps) {
  // 错误文案走 composer 域 catalog（异步回调里闭包引用当次渲染的 t，
  // locale 切换后下一次上传即用新语言）。
  const t = useTranslations("composer.attach");
  // Images currently being read/compressed by readImage. The Attachment only
  // reaches `attachments` when that async read resolves, so a send fired in the
  // meantime would silently drop the picture. This counter gates the send.
  const [imagesReading, setImagesReading] = useState(0);

  // 上传/挂载是异步的：等待期间用户可能已切走会话。完成回执必须落回文件
  // 所属的那个 slot——用户还在就更新 state，切走了就写它的草稿，切回来时
  // 芯片已是就绪态。直接 setFileAttachments 会把文件串进当前会话（或让
  // 旧会话草稿里的芯片永远停在「上传中」）。
  function landFile(slot: string, fn: (a: FileAttachment[]) => FileAttachment[]) {
    const r = refs.current!;
    const cur = r.curSessionIdRef.current ?? HOME_SLOT;
    if (cur === slot) {
      setFileAttachments(fn);
      return;
    }
    const d = r.draftCacheRef.current[slot];
    if (d) d.files = fn(d.files ?? []);
  }
  // 表格类文件落盘后自动开预览——但仅当用户还在文件所属的会话里；切走后
  // 弹预览只会打断别的会话。
  function maybeAutoPreview(slot: string, entry: { id: string; name: string; path: string; kind: string }) {
    if (TABLE_KINDS.has(entry.kind) && ((refs.current!.curSessionIdRef.current) ?? HOME_SLOT) === slot) {
      g.openPreview({ id: entry.id, name: entry.name, path: entry.path, kind: entry.kind });
    }
  }

  // 上传失败的可见反馈（R3 巡检 Q1）：超限拒绝与其它失败分别给明确 toast，
  // 文件名+大小+上限都要呈现——此前 catch 只 debugLog + 芯片静默移除，用户
  // 以为没拖上。dedupeKey 防同一文件反复拖入时堆叠多条；呈现决策抽在
  // composerAttachments.uploadFailureNotice（纯函数，vitest 直测）。
  function notifyUploadFailure(name: string, size: number, e: unknown) {
    const { key, params } = uploadFailureNotice(e, name, size);
    pushNotice("error", t(key, params), {
      dedupeKey: `upload-${key}:${name}`,
    });
  }

  async function uploadOneDoc(sid: string, f: File, tmpId: string): Promise<FileAttachment | null> {
    try {
      // The upload + response telemetry live in the shared composerAttachments
      // module (used by the floating window too); it throws on failure.
      const entry = await uploadDoc(sid, f);
      landFile(sid, (a) =>
        a.map((x) =>
          x.id === tmpId
            ? { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind }
            : x,
        ),
      );
      // spreadsheets/tables auto-open the preview on drop
      maybeAutoPreview(sid, entry);
      g.reloadArtifacts();
      return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
    } catch (e) {
      void debugLog({ where: "addFiles:upload-error", name: f.name, error: String(e) });
      landFile(sid, (a) => a.filter((x) => x.id !== tmpId));
      notifyUploadFailure(f.name, f.size, e);
      return null;
    }
  }

  async function addFiles(files: FileList | File[] | null) {
    // [DEBUG] telemetry for WKWebView drag & drop diagnosis
    void debugLog({
      where: "addFiles:enter",
      hasSession: !!session,
      count: files?.length ?? 0,
      files: Array.from(files ?? []).map((f) => ({ name: f.name, type: f.type, size: f.size })),
    });
    if (!files?.length) return;
    const sid = session?.id ?? null;
    const list = Array.from(files);
    // Images keep the base64 → multimodal path; everything else is uploaded
    // to the sidecar and attached by registry ref (docs §7.2).
    const images = list.filter(isImageFile);
    const docs = list.filter((f) => !isImageFile(f));
    void debugLog({ where: "addFiles:split", images: images.length, docs: docs.length });
    if (images.length) {
      // Bracket the async read so send() can gate on it: until this resolves,
      // `attachments` does not contain these pictures and sending would drop
      // them silently (see imagesReading).
      setImagesReading((n) => n + 1);
      try {
        const items = await Promise.all(images.map(readImage));
        const ok = items.filter((x): x is Attachment => !!x);
        if (ok.length) setAttachments((a) => [...a, ...ok]);
      } finally {
        setImagesReading((n) => Math.max(0, n - 1));
      }
    }
    for (const f of docs) {
      // 本地预检（Q1）：超限文件不必白传一遍再被后端拒——先给可见报错，
      // 不放乐观芯片也不发请求。上限常量与 runtime 同步（见
      // composerAttachments.MAX_UPLOAD_BYTES 的注释）；后端 code 兜底漂移。
      if (f.size > MAX_UPLOAD_BYTES) {
        notifyUploadFailure(f.name, f.size, new UploadError("file too large", "file_too_large"));
        continue;
      }
      // optimistic chip (uploading state) so the user gets instant feedback
      const tmpId = `up-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFileAttachments((a) => [
        ...a,
        { id: tmpId, name: f.name, path: "", kind: "", uploading: true },
      ]);
      if (!sid) {
        refs.current!.pendingDocsRef.current.push({ file: f, tmpId });
        continue;
      }
      await uploadOneDoc(sid, f, tmpId);
    }
  }

  // Native OS file-drop bridge for the desktop app. WKWebView never fires the
  // HTML5 onDrop for Finder drags, so the Tauri shell handles the drop and
  // forwards the native paths here (lib.rs → window.eval). Browsers use the
  // HTML5 path above instead; this is a no-op there (nothing calls it).
  useEffect(() => {
    (window as unknown as { __ginnoFileDrop?: (p: string[]) => void }).__ginnoFileDrop = (
      paths: string[],
    ) => void attachPaths(paths);
    return () => {
      delete (window as unknown as { __ginnoFileDrop?: unknown }).__ginnoFileDrop;
    };
  });

  async function attachOne(sid: string, p: string, tmpId: string): Promise<FileAttachment | null> {
    const name = p.split("/").pop() || p;
    try {
      const r = await attachFilePath(sid, p);
      void debugLog({ where: "attachPaths:resp", name, ok: r?.ok, error: r?.error });
      if (r.ok && r.file) {
        const entry = r.file;
        landFile(sid, (a) =>
          a.map((x) =>
            x.id === tmpId
              ? { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind }
              : x,
          ),
        );
        maybeAutoPreview(sid, entry);
        g.reloadArtifacts();
        return { id: entry.id, name: entry.name, path: entry.path, kind: entry.kind };
      } else {
        landFile(sid, (a) => a.filter((x) => x.id !== tmpId));
        return null;
      }
    } catch (e) {
      void debugLog({ where: "attachPaths:error", name, error: String(e) });
      landFile(sid, (a) => a.filter((x) => x.id !== tmpId));
      return null;
    }
  }

  async function attachPaths(paths: string[]) {
    void debugLog({ where: "attachPaths", paths });
    if (!paths?.length) return;
    const sid = session?.id ?? null;
    for (const p of paths) {
      const name = p.split("/").pop() || p;
      const tmpId = `path-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setFileAttachments((a) => [...a, { id: tmpId, name, path: p, kind: "", uploading: true }]);
      if (!sid) {
        refs.current!.pendingPathsRef.current.push({ path: p, tmpId });
        continue;
      }
      await attachOne(sid, p, tmpId);
    }
  }

  return { imagesReading, addFiles, attachPaths, uploadOneDoc, attachOne };
}
