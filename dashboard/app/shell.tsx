"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { API_URL, DEMO_USERS, call, getDemoUser, listApprovals, setDemoUser, type Approval } from "../lib/api";

const sections = [
  { title: "Policy", links: [{ href: "/", label: "Resolved view" }, { href: "/hierarchy", label: "Hierarchy" }] },
  { title: "Agents", links: [{ href: "/agents", label: "Collection" }, { href: "/publish", label: "Publish pipeline" }] },
  { title: "Runs", links: [{ href: "/traces", label: "Trace viewer" }, { href: "/approvals", label: "Approvals" }, { href: "/analytics", label: "Analytics" }] },
];

const isActive = (pathname: string, href: string) => href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [user, setUser] = useState<string | null>(null);
  const [pending, setPending] = useState<number | null>(null);

  useEffect(() => {
    setUser(getDemoUser());
    call<{ approvals?: Approval[] }>(listApprovals("pending")).then(outcome => {
      if (outcome.ok) setPending(outcome.data.approvals?.length ?? 0);
    });
  }, [pathname]);

  const current = DEMO_USERS.find(item => item.id === user);
  return <>
    <header className="topbar">
      <Link className="topbar-brand" href="/" aria-label="ReAgent home"><Image src="/reagent.svg" alt="" width={26} height={26} priority /><span>ReAgent</span></Link>
      <span className="topbar-env" title="Every request goes to this self-hosted runtime">{API_URL.replace(/^https?:\/\//, "")}</span>
      <label className="identity">
        <span className="identity-label">Log in as</span>
        <select aria-label="Log in as" value={user ?? ""} onChange={event => { setUser(event.target.value); setDemoUser(event.target.value); }}>
          {user === null && <option value="">…</option>}
          {DEMO_USERS.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}
        </select>
        {current && <span className="identity-team">{current.team ?? "all teams"}</span>}
      </label>
    </header>
    <div className="shell">
      <nav className="rail" aria-label="Main navigation">
        {sections.map(section => <div className="rail-section" key={section.title}>
          <div className="rail-title">{section.title}</div>
          {section.links.map(link => <Link key={link.href} href={link.href} className={isActive(pathname, link.href) ? "active" : undefined} aria-current={isActive(pathname, link.href) ? "page" : undefined}>
            {link.label}
            {link.href === "/approvals" && pending ? <span className="rail-count" aria-label={`${pending} pending`}>{pending}</span> : null}
          </Link>)}
        </div>)}
        <div className="rail-foot">
          <Link href="/privacy" className={pathname === "/privacy" ? "active" : undefined}>Privacy &amp; retention</Link>
          <span>Self-hosted · traces kept 30 days</span>
        </div>
      </nav>
      <main id="content">{children}</main>
    </div>
  </>;
}
