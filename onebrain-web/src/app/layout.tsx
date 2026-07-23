import type { Metadata } from "next";
import type { ReactNode } from "react";
import { DEFAULT_LOCALE } from "@/lib/i18n";
import "./globals.css";

export const metadata: Metadata = {
  title: "OneBrain",
  description: "OneBrain web console",
};

// The server-rendered default is the platform's primary language (German).
// LocaleProvider updates <html lang> on the client once the account default or a
// per-user choice is known, so the attribute always reflects the active language.
export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html data-brand="assad-dar" lang={DEFAULT_LOCALE}>
      <body>{children}</body>
    </html>
  );
}
