// メニュー（design-system 6.8）。開いた元の部品の下に出す。上下の矢印キーで項目を移り、Enter で選ぶ。
// Esc か外側のクリックで閉じ、フォーカスは元の部品に戻る。項目を選んだときも元の部品に戻し、選んだ処理が移す。
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";

export interface MenuItem {
  id: string;
  label: string;
}

interface Props {
  label: ReactNode;
  items: readonly MenuItem[];
  disabled?: boolean;
  onSelect: (id: string) => void;
  testId?: string;
}

export function MenuButton({ label, items, disabled, onSelect, testId }: Props) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const button = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const root = useRef<HTMLDivElement>(null);

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) button.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    itemRefs.current[0]?.focus();
    const onPointer = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", onPointer, true);
    return () => window.removeEventListener("pointerdown", onPointer, true);
  }, [open]);

  const focusItem = (index: number) => {
    const n = items.length;
    itemRefs.current[(index + n) % n]?.focus();
  };

  return (
    <div className="menu-root" ref={root}>
      <button
        ref={button}
        type="button"
        className="btn btn-secondary"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-disabled={disabled || undefined}
        data-testid={testId}
        onClick={() => !disabled && setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (!disabled && e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span>{label}</span>
        <Icon name="chevronDown" />
      </button>
      {open && (
        <div className="menu" role="menu" id={menuId}>
          {items.map((item, index) => (
            <button
              key={item.id}
              ref={(el) => {
                itemRefs.current[index] = el;
              }}
              type="button"
              role="menuitem"
              className="menu-item"
              tabIndex={-1}
              onClick={() => {
                // 元の部品へ戻してから選ぶ（選んだ結果がフォーカスを動かさなければ、元の部品に残る。
                // サンプルを開けたときは、開いた結果の「実行」へ移る。3.4）
                close(true);
                onSelect(item.id);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  focusItem(index + 1);
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  focusItem(index - 1);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation();
                  close(true);
                } else if (e.key === "Tab") {
                  close(false);
                }
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
