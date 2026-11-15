// 仕切り（design-system 6.10、画面仕様 1.4）。ドラッグか、フォーカスして矢印キー（16px ずつ）で大きさを変える。
// ダブルクリック（またはフォーカスして Home）で既定の大きさに戻す。見た目 1px、つかめる範囲 8px。
import { useRef } from "react";

const STEP = 16;

interface Props {
  orientation: "vertical" | "horizontal";
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  onReset: () => void;
  testId?: string;
}

export function Splitter({ orientation, label, value, min, max, onChange, onReset, testId }: Props) {
  const drag = useRef<{ start: number; value: number } | null>(null);
  const clamp = (v: number) => Math.round(Math.min(max, Math.max(min, v)));
  const vertical = orientation === "vertical";
  return (
    <div
      className={`splitter splitter-${orientation}`}
      role="separator"
      aria-orientation={orientation}
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(max)}
      tabIndex={0}
      data-testid={testId}
      onPointerDown={(e) => {
        e.preventDefault();
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        drag.current = { start: vertical ? e.clientX : e.clientY, value };
      }}
      onPointerMove={(e) => {
        if (!drag.current) return;
        const delta = (vertical ? e.clientX : e.clientY) - drag.current.start;
        onChange(clamp(drag.current.value + delta));
      }}
      onPointerUp={(e) => {
        drag.current = null;
        (e.target as HTMLElement).releasePointerCapture?.(e.pointerId);
      }}
      onDoubleClick={onReset}
      onKeyDown={(e) => {
        const less = vertical ? "ArrowLeft" : "ArrowUp";
        const more = vertical ? "ArrowRight" : "ArrowDown";
        if (e.key === less || e.key === more) {
          e.preventDefault();
          onChange(clamp(value + (e.key === more ? STEP : -STEP)));
        } else if (e.key === "Home") {
          e.preventDefault();
          onReset();
        }
      }}
    />
  );
}
