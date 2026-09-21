import React from "react";
import { Menu, RefreshCw, Bell } from "lucide-react";

interface TopbarProps {
  pageName: string;
  onMenuClick: () => void;
  onRefresh?: () => void;
}

export default function Topbar({ pageName, onMenuClick, onRefresh }: TopbarProps) {
  return (
    <header className="flex h-16 items-center justify-between border-b border-panel-border bg-canvas/95 backdrop-blur px-4 sm:px-6">
      <div className="flex items-center gap-3 min-w-0">
        <button
          onClick={onMenuClick}
          className="rounded-sm p-1.5 text-ink-muted hover:text-ink hover:bg-panel-hover lg:hidden"
          aria-label="Open navigation"
        >
          <Menu size={18} />
        </button>
        <p className="truncate text-sm text-ink-muted">
          <span className="text-ink-faint">ACELO</span>
          <span className="mx-1.5 text-ink-faint">/</span>
          <span className="font-medium text-ink">{pageName}</span>
        </p>
      </div>
      <div className="flex items-center gap-1">
        <button
          onClick={onRefresh}
          className="rounded-sm p-2 text-ink-muted hover:text-ink hover:bg-panel-hover transition-colors"
          aria-label="Refresh"
        >
          <RefreshCw size={16} />
        </button>
        <button
          className="relative rounded-sm p-2 text-ink-muted hover:text-ink hover:bg-panel-hover transition-colors"
          aria-label="Notifications"
        >
          <Bell size={16} />
          <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-signal-medium" />
        </button>
        <span className="ml-2 flex h-8 w-8 items-center justify-center rounded-full bg-brand-500/15 border border-brand-500/25 text-xs font-semibold text-brand-300">
          SN
        </span>
      </div>
    </header>
  );
}
