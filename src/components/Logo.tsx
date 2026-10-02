import React from "react";

/** The ACELO monogram: a white "A" on brand red. Also the favicon. */
export function LogoMark({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="shrink-0">
      <rect width="32" height="32" rx="8" className="fill-brand-500" />
      <path d="M16 7.5 L24.5 24.5 H20.4 L18.7 21 H13.3 L11.6 24.5 H7.5 Z M14.7 18 H17.3 L16 15.2 Z" fill="#FFFFFF" />
    </svg>
  );
}

/** Mark plus the ACELO wordmark. */
export default function Logo({ caption }: { caption?: string }) {
  return (
    <span className="flex items-center gap-2.5">
      <LogoMark />
      <span className="leading-none">
        <span className="block text-[17px] font-bold tracking-[0.14em] text-ink">ACELO</span>
        {caption && <span className="mt-1 block text-[10px] font-medium uppercase tracking-wider text-ink-faint">{caption}</span>}
      </span>
    </span>
  );
}
