"use client";

import { InstinctsPage } from "@/components/instincts/InstinctsPage";

/** 本能（instincts）管理页（docs/instincts-design.md §11，P0 只读+增删改）。
 *  入口在侧边栏 footer「本能」（BETA）。 */
export default function InstinctsRoutePage() {
  return (
    <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
      <InstinctsPage />
    </div>
  );
}
