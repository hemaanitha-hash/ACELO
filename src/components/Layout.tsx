import React, { useState } from "react";
import Sidebar from "./Sidebar";
import Topbar from "./Topbar";

interface LayoutProps {
  pageName: string;
  children: React.ReactNode;
  onRefresh?: () => void;
}

export default function Layout({ pageName, children, onRefresh }: LayoutProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar
          pageName={pageName}
          onMenuClick={() => setSidebarOpen(true)}
          onRefresh={onRefresh}
        />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1240px] px-4 py-6 sm:px-6 sm:py-8 lg:px-10">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
