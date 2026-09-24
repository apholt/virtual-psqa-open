import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { C } from "../theme";

interface Crumb {
  label: string;
  to?: string;
}

interface TopbarProps {
  /** When provided, replaces the app title with a breadcrumb trail. */
  breadcrumb?: Crumb[];
  /** Optional node pinned to the far right (e.g. a verdict badge). */
  right?: ReactNode;
}

const NAV = [
  { to: "/", label: "Dashboard" },
  { to: "/audit-logs", label: "Audit Logs" },
  { to: "/settings", label: "Settings" },
];

function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);
  return now.toLocaleString(undefined, {
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function Topbar({ breadcrumb, right }: TopbarProps) {
  const clock = useClock();
  const { pathname } = useLocation();

  const handleNavClick = (e: React.MouseEvent<HTMLAnchorElement>, to: string) => {
    if (to === "/" && pathname === "/") {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  };

  return (
    <div
      style={{
        background: "#fff",
        borderBottom: `0.5px solid ${C.border}`,
        padding: "10px 16px",
        display: "flex",
        alignItems: "center",
        gap: 10,
        position: "sticky",
        top: 0,
        zIndex: 30,
      }}
    >
      {breadcrumb ? (
        <span style={{ fontSize: 12, color: C.muted }}>
          {breadcrumb.map((c, i) => (
            <span key={i}>
              {i > 0 && <span style={{ margin: "0 4px" }}>›</span>}
              {c.to ? (
                <Link to={c.to} style={{ color: C.muted, textDecoration: "none" }}>
                  {c.label}
                </Link>
              ) : (
                <span style={{ color: C.text }}>{c.label}</span>
              )}
            </span>
          ))}
        </span>
      ) : (
        <>
          <svg
            width="17"
            height="17"
            viewBox="0 0 24 24"
            fill="none"
            stroke={C.muted}
            strokeWidth="2"
          >
            <rect x="3" y="3" width="7" height="7" />
            <rect x="14" y="3" width="7" height="7" />
            <rect x="3" y="14" width="7" height="7" />
            <rect x="14" y="14" width="7" height="7" />
          </svg>
          <Link to="/" style={{ fontSize: 14, fontWeight: 600, color: C.text, textDecoration: "none" }}>
            Virtual PSQA
          </Link>
        </>
      )}

      {/* nav links */}
      <nav style={{ display: "flex", gap: 14, marginLeft: 18 }}>
        {NAV.map((l) => {
          const active = l.to === "/" ? pathname === "/" : pathname.startsWith(l.to);

          return (
            <Link
              key={l.to}
              to={l.to}
              onClick={(e) => handleNavClick(e, l.to)}
              style={{
                fontSize: 12,
                textDecoration: "none",
                color: active ? C.text : C.muted,
                fontWeight: active ? 600 : 400,
              }}
            >
              {l.label}
            </Link>
          );
        })}
      </nav>

      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
        {right}
        <span style={{ fontSize: 12, color: C.muted }}>{clock}</span>
        <span className="live-dot" />
        <span style={{ fontSize: 11, color: C.muted }}>Live</span>
        <a
          href="/logout"
          title="Sign out"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 11,
            color: C.muted,
            textDecoration: "none",
            padding: "2px 7px",
            borderRadius: 4,
            border: `0.5px solid ${C.border}`,
            background: "#f8fafc",
            cursor: "pointer",
            marginLeft: 4,
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = C.text;
            (e.currentTarget as HTMLElement).style.borderColor = C.text;
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = C.muted;
            (e.currentTarget as HTMLElement).style.borderColor = C.border;
          }}
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
            <polyline points="16 17 21 12 16 7" />
            <line x1="21" y1="12" x2="9" y2="12" />
          </svg>
          Sign Out
        </a>
      </div>
    </div>
  );
}
