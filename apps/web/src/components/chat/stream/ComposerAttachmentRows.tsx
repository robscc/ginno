"use client";

// ComposerAttachmentRows — composer 内的图片与文件附件芯片两行（自
// ChatStream.tsx 机械拆出，渲染逐字一致）。

import { Loader2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { TABLE_KINDS, type Attachment, type FileAttachment } from "../streamCore";

export function ComposerAttachmentRows({
  attachments,
  fileAttachments,
  uploadingLabel,
  onRemoveImage,
  onRemoveFile,
}: {
  attachments: Attachment[];
  fileAttachments: FileAttachment[];
  /** 上传中芯片的说明文案（会话内即时上传 vs 首页随首条消息上传）。 */
  uploadingLabel: string;
  onRemoveImage: (i: number) => void;
  onRemoveFile: (i: number) => void;
}) {
  const tc = useTranslations("chat");
  return (
    <>
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {attachments.map((a, i) => (
            <div key={i} className="group relative">
              <img
                src={a.preview}
                alt={a.name}
                title={a.name}
                className="h-14 w-14 rounded-lg border border-line object-cover"
              />
              <button
                onClick={() => onRemoveImage(i)}
                aria-label={tc("composer.removeNamed", { name: a.name })}
                className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}
      {fileAttachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {fileAttachments.map((f, i) => (
            <div
              key={f.id}
              className="group relative flex items-center gap-1.5 rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-txt"
            >
              <span>{TABLE_KINDS.has(f.kind) ? "📊" : "📄"}</span>
              <span className="max-w-[180px] truncate" title={f.name}>
                {f.name}
              </span>
              {f.uploading && (
                <span className="text-faint">{uploadingLabel}</span>
              )}
              <button
                onClick={() => onRemoveFile(i)}
                aria-label={tc("composer.removeNamed", { name: f.name })}
                className="absolute -right-1.5 -top-1.5 flex h-[18px] w-[18px] items-center justify-center rounded-full bg-red text-white opacity-0 shadow transition-opacity group-hover:opacity-100"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// imagesReading 行（读取/压缩中的可见反馈）与本文件同属附件渲染，一并落这里。
export function ImagesReadingRow() {
  const tc = useTranslations("chat");
  return (
    <div className="mb-2 flex items-center gap-1.5 text-xs text-faint">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      <span>{tc("composer.processingImages")}</span>
    </div>
  );
}
