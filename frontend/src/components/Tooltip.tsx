// ツールチップ（design-system 6.7）。ポインタが乗って 500ms で出る。キーボードのフォーカスでも出る。Esc で消える。
// 押したら（ポインタでも、Enter・スペースでも）消え、次にフォーカスが来るまで（ポインタは、一度離れて乗り直すまで）
// 出し直さない（A0 の UX 確認の改善2。押した後も、結果の上に残り続けないように）。
// 中に押せるものを置かない。ここにしか無い情報を作らない（ショートカット、省略した全文、説明、名前だけ）。
import {
  cloneElement,
  useEffect,
  useId,
  useRef,
  useState,
  type MutableRefObject,
  type ReactElement,
  type Ref,
} from "react";

const DELAY_MS = 500;

interface Props {
  text: string;
  children: ReactElement<Record<string, unknown>>;
}

export function Tooltip({ text, children }: Props) {
  const id = useId();
  const [shown, setShown] = useState(false);
  const [place, setPlace] = useState<{ left: number; top: number } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const anchor = useRef<HTMLElement | null>(null);
  /** 押した後: フォーカスが離れるか、ポインタが離れるまで出さない */
  const pressed = useRef(false);

  const show = (now: boolean) => {
    window.clearTimeout(timer.current);
    if (pressed.current) return;
    const open = () => {
      const el = anchor.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      setPlace({ left: Math.max(4, Math.min(rect.left, window.innerWidth - 284)), top: rect.bottom + 6 });
      setShown(true);
    };
    if (now) open();
    else timer.current = window.setTimeout(open, DELAY_MS);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setShown(false);
  };
  const press = () => {
    pressed.current = true;
    hide();
  };

  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!shown) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [shown]);

  // 子の部品の ref と、同じ名前の操作の受け口も、そのまま呼ぶ
  const own = children.props as {
    ref?: Ref<HTMLElement>;
    onFocus?: (e: FocusEvent) => void;
    onBlur?: (e: FocusEvent) => void;
    onPointerDown?: (e: PointerEvent) => void;
    onPointerLeave?: (e: PointerEvent) => void;
    onKeyDown?: (e: KeyboardEvent) => void;
  };
  const child = cloneElement(children, {
    ref: (el: HTMLElement | null) => {
      anchor.current = el;
      if (typeof own.ref === "function") own.ref(el);
      else if (own.ref) (own.ref as MutableRefObject<HTMLElement | null>).current = el;
    },
    "aria-describedby": id,
    onPointerEnter: () => show(false),
    onPointerLeave: (e: PointerEvent) => {
      own.onPointerLeave?.(e);
      pressed.current = false; // 離れて乗り直したら出す（フォーカスでは、次に来るまで出ない）
      hide();
    },
    onFocus: (e: FocusEvent) => {
      own.onFocus?.(e);
      pressed.current = false; // 次にフォーカスが来た
      const target = e.target as HTMLElement;
      if (target.matches(":focus-visible")) show(true);
    },
    onBlur: (e: FocusEvent) => {
      own.onBlur?.(e);
      pressed.current = false;
      hide();
    },
    onPointerDown: (e: PointerEvent) => {
      own.onPointerDown?.(e);
      press();
    },
    onKeyDown: (e: KeyboardEvent) => {
      own.onKeyDown?.(e);
      if (e.key === "Enter" || e.key === " ") press();
    },
  });
  return (
    <>
      {child}
      <span id={id} role="tooltip" className="tooltip" hidden={!shown} style={place ?? undefined}>
        {text}
      </span>
    </>
  );
}
