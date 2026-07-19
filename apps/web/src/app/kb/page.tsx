"use client";

import { BookOpen } from "lucide-react";

export default function KnowledgeBasePage() {
  return (
    <div className="flex min-w-0 flex-1 flex-col items-center justify-center px-8 text-center">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-violet/15 text-violet">
        <BookOpen className="h-6 w-6" />
      </div>
      <h2 className="text-lg font-semibold text-txt">Knowledge Base</h2>
      <p className="mt-1 max-w-md text-sm text-faint">
        Your Obsidian vault is indexed via the configured MCP server. Search & management UI
        arrives in Phase F.
      </p>
    </div>
  );
}
