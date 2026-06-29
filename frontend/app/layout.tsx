import type { Metadata } from "next";
import "./globals.css";
import { Header } from "@/app/components/AppShell";

// System font stack (no next/font/google) so static export builds offline / in
// CI without a network fetch.
export const metadata: Metadata = {
  title: "Zeroth Console",
  description: "Operate and author Zeroth multi-agent apps",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only rounded-md bg-accent px-3 py-1.5 text-sm text-accent-fg focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50"
        >
          Skip to main content
        </a>
        <Header />
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
          {children}
        </main>
      </body>
    </html>
  );
}
