import React, { useState } from "react";
import { NavLink } from "react-router-dom";
import {
  LayoutGrid,
  Sparkles,
  SlidersHorizontal,
  FileText,
  ShieldCheck,
  PlayCircle,
  History,
  Settings,
  Database,
  ChevronDown,
  Check,
  X,
} from "lucide-react";

const navItems = [
  { to: "/", label: "Overview", icon: LayoutGrid, end: true },
  { to: "/agent", label: "AI Agent", icon: Sparkles },
  { to: "/optimizations", label: "Optimizations", icon: SlidersHorizontal },
  { to: "/recommendations", label: "Recommendations", icon: FileText },
  { to: "/approvals", label: "Approvals", icon: ShieldCheck, badge: 3 },
  { to: "/execution", label: "Execution", icon: PlayCircle },
  { to: "/history", label: "History", icon: History },
];

const platforms = [
  {
    name: "Databricks",
    workspace: "Production Lakehouse",
  },
  {
    name: "Microsoft Fabric",
    workspace: "Fabric Workspace",
  },
];

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

export default function Sidebar({ open, onClose }: SidebarProps) {
  const [platformOpen, setPlatformOpen] = useState(false);
  const [activePlatform, setActivePlatform] = useState(platforms[1]);

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-panel-border bg-canvas-raised transition-transform duration-200 lg:static lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-sm bg-brand-500/15 border border-brand-500/25">
              <svg viewBox="0 0 32 32" className="h-4 w-4">
                <path
                  d="M16 6 L25 24 H20.5 L16 14.5 L11.5 24 H7 Z"
                  fill="#C41E3A"
                />
              </svg>
            </span>

            <span className="text-[15px] font-semibold tracking-tight text-ink">
              ACELO
            </span>
          </div>

          <button
            onClick={onClose}
            className="rounded-sm p-1 text-ink-faint hover:text-ink lg:hidden"
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        {/* Platform / Environment Selector */}
        <div className="relative mx-3 mb-4">
          <button
            onClick={() => setPlatformOpen((v) => !v)}
            aria-haspopup="listbox"
            aria-expanded={platformOpen}
            className="flex w-full items-center justify-between rounded-sm border border-panel-border bg-panel px-3 py-2.5 text-left transition-colors hover:bg-panel-hover"
          >
            <div className="flex min-w-0 items-center gap-2.5">
              <Database
                size={15}
                className="shrink-0 text-ink-muted"
              />

              <div className="min-w-0">
                <span className="block truncate text-sm font-medium leading-tight text-ink">
                  {activePlatform.name}
                </span>

                <span className="mt-0.5 block truncate text-xs leading-tight text-ink-faint">
                  {activePlatform.workspace}
                </span>
              </div>
            </div>

            <ChevronDown
              size={14}
              className={`shrink-0 text-ink-faint transition-transform ${
                platformOpen ? "rotate-180" : ""
              }`}
            />
          </button>

          {platformOpen && (
            <ul
              role="listbox"
              className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-sm border border-panel-border bg-panel shadow-elevated"
            >
              {platforms.map((platform) => {
                const isActive =
                  activePlatform.name === platform.name;

                return (
                  <li key={platform.name}>
                    <button
                      role="option"
                      aria-selected={isActive}
                      onClick={() => {
                        setActivePlatform(platform);
                        setPlatformOpen(false);
                      }}
                      className={`flex w-full items-center justify-between gap-2 px-3 py-3 text-left transition-colors ${
                        isActive
                          ? "bg-panel-hover"
                          : "hover:bg-panel-hover"
                      }`}
                    >
                      <span className="min-w-0">
                        <span
                          className={`block truncate text-sm ${
                            isActive
                              ? "font-medium text-ink"
                              : "text-ink"
                          }`}
                        >
                          {platform.name}
                        </span>

                        <span className="mt-0.5 block truncate text-xs text-ink-faint">
                          {platform.workspace}
                        </span>
                      </span>

                      {isActive && (
                        <Check
                          size={15}
                          className="shrink-0 text-brand-500"
                        />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-0.5 px-3">
          {navItems.map(
            ({ to, label, icon: Icon, end, badge }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                onClick={onClose}
                className={({ isActive }) =>
                  `flex items-center justify-between rounded-sm px-3 py-2 text-sm transition-colors ${
                    isActive
                      ? "bg-brand-500/12 text-brand-300 border border-brand-500/20"
                      : "text-ink-muted hover:bg-panel-hover hover:text-ink border border-transparent"
                  }`
                }
              >
                <span className="flex items-center gap-2.5">
                  <Icon size={16} strokeWidth={1.75} />
                  {label}
                </span>

                {badge && (
                  <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-signal-medium px-1 text-[11px] font-semibold text-white">
                    {badge}
                  </span>
                )}
              </NavLink>
            )
          )}
        </nav>

        {/* Bottom Section */}
        <div className="mt-auto border-t border-panel-border px-3 py-4 space-y-0.5">
          <div className="flex items-center gap-2.5 rounded-sm px-3 py-2">
            <span className="h-1.5 w-1.5 rounded-full bg-brand-400" />

            <div className="min-w-0">
              <p className="text-[11px] text-ink-faint leading-none">
                Connected Platform
              </p>

              <p className="text-sm text-ink leading-tight mt-0.5">
                {activePlatform.name}
              </p>
            </div>
          </div>

          <NavLink
            to="/settings"
            onClick={onClose}
            className={({ isActive }) =>
              `flex items-center gap-2.5 rounded-sm px-3 py-2 text-sm transition-colors ${
                isActive
                  ? "text-ink bg-panel-hover"
                  : "text-ink-muted hover:bg-panel-hover hover:text-ink"
              }`
            }
          >
            <Settings size={16} strokeWidth={1.75} />
            Settings
          </NavLink>
        </div>
      </aside>
    </>
  );
}