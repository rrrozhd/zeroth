import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/app/components/Nav";

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
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-zinc-50 text-zinc-900 dark:bg-black dark:text-zinc-100">
        <Nav />
        <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
