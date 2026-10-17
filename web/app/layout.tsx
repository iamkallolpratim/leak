import type { Metadata } from "next";
import { Nav } from "@/components/nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Leak",
  description: "Find recurring charges, hidden fees and forgotten subscriptions — locally.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <Nav />
        <main className="mx-auto max-w-6xl px-4 py-6 sm:py-8">{children}</main>
      </body>
    </html>
  );
}
