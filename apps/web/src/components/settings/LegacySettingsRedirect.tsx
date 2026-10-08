"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// 旧 /settings/{tab} URL 的客户端跳转兜底（静态导出无服务端 redirect）。
// 渲染一个极简骨架占位，挂载后立即 replace 到新路径。
export function LegacySettingsRedirect({ to }: { to: string }) {
  const router = useRouter();
  useEffect(() => {
    router.replace(to);
  }, [router, to]);
  return (
    <div className="min-w-0 flex-1 px-8 py-10">
      <div className="h-2 w-40 animate-pulse rounded bg-line" />
    </div>
  );
}
