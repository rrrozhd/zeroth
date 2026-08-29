import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/app/components/AppShell";

// Server component. Renders the html/body document and the client `AppShell`
// wrapping every route's `{children}`. Under `output: "export"` the shell is a
// "use client" boundary (per Next 16 docs, "Context providers" pattern): the
// server layout prerenders to static HTML and the shell hydrates on the client,
// where it reads runtime config from localStorage and fetches data.
export const metadata: Metadata = {
  title: "Zeroth Console",
  description: "Operate and author Zeroth multi-agent apps",
  icons: {
    icon: "/console/zeroth-mark.png",
    apple: "/console/zeroth-mark.png",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
