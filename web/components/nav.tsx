"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getSession, type Session } from "@/lib/api";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Findings" },
  { href: "/approvals/", label: "Approvals" },
  { href: "/runs/", label: "Agent runs" },
  { href: "/llm/", label: "What the LLM saw" },
];

export function Nav() {
  const path = usePathname();
  const [s, setS] = useState<Session | null>(null);
  useEffect(() => { getSession().then(setS, () => {}); }, []);
  return (
    <header className="sticky top-0 z-30 border-b bg-background">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">₹</span>
          Leak
        </Link>
        <nav className="flex flex-wrap gap-1 text-sm">
          {links.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href.replace(/\/$/, ""));
            return (
              <Link key={l.href} href={l.href}
                className={cn("rounded-md px-2.5 py-1.5 hover:bg-muted", active ? "bg-muted font-medium" : "text-muted-foreground")}>
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
          {s?.demo && <span className="rounded-full bg-warning/25 px-2 py-0.5 font-medium text-foreground">Demo data</span>}
          <span title="Everything stays on this machine">🔒 local only</span>
        </div>
      </div>
    </header>
  );
}
