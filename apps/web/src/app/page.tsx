import { ChatPanel } from "@/components/ChatPanel";

export default function Page() {
  return (
    <main className="flex h-screen flex-col">
      <header className="border-b border-black/10 px-4 py-2 text-sm font-medium">
        Ginno
      </header>
      <ChatPanel />
    </main>
  );
}
