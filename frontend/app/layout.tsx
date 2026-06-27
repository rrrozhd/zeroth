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
        <Header />
        <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
