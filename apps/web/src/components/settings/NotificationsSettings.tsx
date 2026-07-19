"use client";

import { useState } from "react";

export function NotificationsSettings() {
  const [n, setN] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    try {
      return localStorage.getItem("ginno-notify") !== "0";
    } catch {
      return true;
    }
  });

  function toggle() {
    const v = !n;
    setN(v);
    try {
      localStorage.setItem("ginno-notify", v ? "1" : "0");
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">通知</h2>
      <p className="mt-1 text-sm text-muted">任务完成 / 权限请求时的提醒（本地偏好）。</p>
      <label className="mt-4 flex items-center gap-2 text-sm text-txt">
        <input type="checkbox" checked={n} onChange={toggle} /> 启用桌面提醒
      </label>
    </div>
  );
}
