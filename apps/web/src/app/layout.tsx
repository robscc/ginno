import type { Metadata } from "next";
import "./globals.css";
import { GinnoProvider } from "@/lib/store";
import { AppShell } from "@/components/shell/AppShell";
import { I18nProvider } from "@/i18n/provider";

export const metadata: Metadata = {
  title: "GinnoWork",
  description: "Personal AI Agent workspace",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // i18n：SSG 构建期 lang 固定 en，client 挂载后由 I18nProvider 按 settings 校正
    <html lang="en">
      <body>
        <I18nProvider>
          <GinnoProvider>
            <AppShell>{children}</AppShell>
          </GinnoProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
