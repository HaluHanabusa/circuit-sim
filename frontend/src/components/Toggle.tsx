// 切り替え（design-system 6.2）。2つの選択肢を横に並べた1つの枠。role="radiogroup"。左右の矢印キーで切り替わる。
import { useRef } from "react";

interface Option<T extends string> {
  value: T;
  label: string;
  /** その選択肢の言語（言語の切り替えで、どちらの言語で表示していても読めるように） */
  lang?: string;
}

interface Props<T extends string> {
  label: string;
  value: T;
  options: readonly Option<T>[];
  onChange: (value: T) => void;
  testId?: string;
}

export function Toggle<T extends string>({ label, value, options, onChange, testId }: Props<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (index: number) => {
    const next = (index + options.length) % options.length;
    onChange(options[next].value);
    refs.current[next]?.focus();
  };
  return (
    <div className="toggle" role="radiogroup" aria-label={label} data-testid={testId}>
      {options.map((option, index) => {
        const checked = option.value === value;
        return (
          <button
            key={option.value}
            ref={(el) => {
              refs.current[index] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            lang={option.lang}
            className="toggle-option"
            onClick={() => onChange(option.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                e.preventDefault();
                move(index + 1);
              } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                e.preventDefault();
                move(index - 1);
              }
            }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
