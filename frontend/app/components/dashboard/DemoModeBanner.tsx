"use client";

interface DemoModeBannerProps {
  show: boolean;
}

/**
 * Persistent notice shown while the backend runs in offline demo mode
 * (MASG_DEMO_MODE=1, reported by /api/opportunities/status → demo_mode).
 * Not dismissible: every number on the page is synthetic while it shows.
 */
export function DemoModeBanner({ show }: DemoModeBannerProps) {
  if (!show) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-2 px-4 py-2.5 border"
      style={{
        background: "var(--warn-soft)",
        borderColor: "var(--warn-border)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <span
        className="w-2 h-2 rounded-full shrink-0"
        style={{ background: "var(--warn)" }}
        aria-hidden="true"
      />
      <span
        className="font-mono uppercase tracking-wider font-semibold whitespace-nowrap"
        style={{ fontSize: "10px", color: "var(--warn-strong)" }}
      >
        Demo mode
      </span>
      <span
        className="font-mono"
        style={{ fontSize: "10px", color: "var(--warn-strong)" }}
      >
        — synthetic example data. Not live market prices; no external requests, alerts or orders.
      </span>
    </div>
  );
}
