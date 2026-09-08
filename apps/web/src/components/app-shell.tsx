"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Bell, BookOpenText, ChevronRight, LayoutDashboard, Settings, Target } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

const navigation = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/opportunities", label: "Opportunities", icon: Target },
  { href: "/research", label: "Research", icon: BookOpenText },
  { href: "/watchlist", label: "Watchlist", icon: Bell },
  { href: "/settings", label: "Settings", icon: Settings }
];

export function AppShell({ children }: Readonly<{ children: ReactNode }>): ReactNode {
  const pathname = usePathname();

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside className="fixed inset-y-0 left-0 hidden w-60 border-r border-line bg-panel lg:block">
        <div className="flex h-16 items-center gap-3 border-b border-line px-5">
          <div className="grid h-7 w-7 place-items-center border border-accent/70 bg-accent/10 font-mono text-xs text-accent">I</div>
          <div>
            <p className="text-sm font-semibold tracking-wide">INFLECTOR</p>
            <p className="text-xs text-muted">Research workstation</p>
          </div>
        </div>
        <nav aria-label="Primary navigation" className="p-3">
          {navigation.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                className={cn(
                  "mb-1 flex h-9 items-center gap-3 rounded-sm px-3 text-sm text-muted hover:bg-raised hover:text-ink",
                  active && "border border-line bg-raised text-ink"
                )}
                href={href}
                key={href}
              >
                <Icon aria-hidden="true" size={16} />
                {label}
                {active && <ChevronRight aria-hidden="true" className="ml-auto text-accent" size={14} />}
              </Link>
            );
          })}
        </nav>
      </aside>
      <div className="lg:pl-60">
        <header className="sticky top-0 z-10 flex h-16 items-center justify-between border-b border-line bg-canvas/95 px-5 backdrop-blur">
          <div className="flex items-center gap-3 lg:hidden"><span className="font-semibold">INFLECTOR</span><span className="text-xs text-muted">Research workstation</span></div>
          <div className="hidden lg:block"><p className="text-xs uppercase tracking-[0.18em] text-muted">Indian equity intelligence</p></div>
          <div aria-label="Data status" className="flex items-center gap-2 text-xs text-muted"><span className="h-2 w-2 rounded-full bg-accent" aria-hidden="true" />Identity universe · local development</div>
        </header>
        <main className="mx-auto max-w-[1600px] p-5 lg:p-7" id="main-content">{children}</main>
      </div>
    </div>
  );
}
