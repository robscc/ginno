"use client";

import { useCallback, useState } from "react";

export interface PreviewFile {
  id: string;
  name: string;
  path: string;
  kind?: string;
  mtime?: number;
}

export function usePreview() {
  const [previewFile, setPreviewFile] = useState<PreviewFile | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);

  const openPreview = useCallback((f: PreviewFile) => {
    setPreviewFile(f);
    setPreviewNonce((n) => n + 1);
  }, []);
  const closePreview = useCallback(() => setPreviewFile(null), []);
  // Only refetch when the invalidated file is the one being viewed.
  const notifyPreviewInvalidate = useCallback((fileId: string) => {
    setPreviewFile((cur) => {
      if (cur && cur.id === fileId) setPreviewNonce((n) => n + 1);
      return cur;
    });
  }, []);

  return { previewFile, previewNonce, openPreview, closePreview, notifyPreviewInvalidate };
}
