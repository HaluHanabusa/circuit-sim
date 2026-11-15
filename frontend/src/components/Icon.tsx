// アイコン（design-system 6.11）。16px 四方、線 1.5px、塗りなし、色は currentColor。自作（外部から読み込まない）。
import type { ReactElement } from "react";

const PATHS: Record<string, ReactElement> = {
  run: <path d="M5 3.5v9l7-4.5z" strokeLinejoin="round" />,
  stop: <rect x="4" y="4" width="8" height="8" rx="0.5" />,
  open: <path d="M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3.5H3A1 1 0 0 0 2 4.5z" />,
  save: (
    <>
      <path d="M8 2.5v7M5 6.5l3 3 3-3" />
      <path d="M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10" />
    </>
  ),
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  chevronUp: <path d="M4 10l4-4 4 4" />,
  chevronDown: <path d="M4 6l4 4 4-4" />,
  chevronLeft: <path d="M10 4l-4 4 4 4" />,
  chevronRight: <path d="M6 4l4 4-4 4" />,
  error: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M5.75 5.75l4.5 4.5M10.25 5.75l-4.5 4.5" />
    </>
  ),
  missing: <circle cx="8" cy="8" r="6" strokeDasharray="2.2 1.9" />,
  warning: (
    <>
      <path d="M8 2.2L14.2 13H1.8z" strokeLinejoin="round" />
      <path d="M8 6.5v3.2M8 11.2v0.3" />
    </>
  ),
  ok: <path d="M3 8.5l3.2 3L13 4.5" />,
  spinner: <path d="M8 2a6 6 0 1 1-6 6" />,
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      className={`icon${className ? ` ${className}` : ""}`}
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
