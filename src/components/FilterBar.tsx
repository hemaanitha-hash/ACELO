import React from "react";
import { Search } from "lucide-react";

interface FilterBarProps {
  tabs: string[];
  activeTab: string;
  onTabChange: (tab: string) => void;
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder?: string;
  trailing?: React.ReactNode;
}

export default function FilterBar({
  tabs,
  activeTab,
  onTabChange,
  searchValue,
  onSearchChange,
  searchPlaceholder = "Search...",
  trailing,
}: FilterBarProps) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex flex-wrap gap-1 rounded-sm border border-panel-border bg-canvas-raised p-1 w-fit">
        {tabs.map((tab) => (
          <button
            key={tab}
            onClick={() => onTabChange(tab)}
            className={`rounded-sm px-3 py-1.5 text-sm font-medium transition-colors ${
              activeTab === tab
                ? "bg-brand-500 text-white"
                : "text-ink-muted hover:text-ink"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <div className="relative">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
          />
          <input
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={searchPlaceholder}
            className="input-field pl-9 w-full sm:w-64"
          />
        </div>
        {trailing}
      </div>
    </div>
  );
}
