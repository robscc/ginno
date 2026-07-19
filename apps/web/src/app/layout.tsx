import type { Metadata } from "next";
import "./globals.css";
import { GinnoProvider } from "@/lib/store";
import { AppShell } from "@/components/shell/AppShell";

export const metadata: Metadata = {
  title: "GinnoWork",
  description: "Personal AI Agent workspace",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <GinnoProvider>
          <AppShell>{children}</AppShell>
        </GinnoProvider>
      </body>
    </html>
  );
}
