// 数値の入力欄（画面仕様 2.2、design-system 6.3）。表示と同じ書き方で入力でき、打つたびに「どう読んだか」を欄の
// 真下の小さな面に出す（フォーカスを奪わない）。読めない入力は確定しない（値は変わらない）。
// A0 では部品として用意する。仕様パネルの行（A1）が使う。
import { useId, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { formatQuantity, PANEL_DIGITS } from "../num/format";
import { interpret } from "../num/interpret";
import { parseQuantity, type FieldSpec } from "../num/parse";
import { Icon } from "./Icon";

interface Props {
  field: FieldSpec;
  value: number | null;
  /** 開いたときの値（倍率の注意の基準） */
  original: number | null;
  label: string;
  onCommit: (value: number | null) => void;
  testId?: string;
}

/** 編集を始めたときの欄の中: 丸めない（元の値に戻せる最も短い表記）。 */
function editText(value: number | null, unit: string): string {
  if (value === null) return "";
  return formatQuantity(value, unit, { mode: "shortest", max: 17 }).text;
}

export function NumberField({ field, value, original, label, onCommit, testId }: Props) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const interpId = useId();
  const editing = draft !== null;
  const result = useMemo(() => (draft === null ? null : parseQuantity(draft, field)), [draft, field]);
  const shown = result ? interpret(result, field, original) : {};

  const commit = () => {
    if (!result) return;
    if (result.kind === "error") return; // 読めない入力は確定しない。欄の下に理由が出たまま
    onCommit(result.kind === "ok" ? result.value : null);
    setDraft(null);
  };

  if (!editing) {
    const text = value === null ? "" : formatQuantity(value, field.unit, PANEL_DIGITS).text;
    return (
      <button
        type="button"
        className="num-cell"
        aria-label={label}
        data-testid={testId}
        onClick={() => {
          setDraft(editText(value, field.unit));
          requestAnimationFrame(() => input.current?.select());
        }}
      >
        {text}
      </button>
    );
  }
  return (
    <div className="num-field">
      <input
        ref={input}
        className={`num-input${shown.error ? " is-error" : ""}`}
        aria-label={label}
        aria-invalid={shown.error ? true : undefined}
        aria-describedby={interpId}
        autoFocus
        spellCheck={false}
        autoComplete="off"
        value={draft}
        data-testid={testId}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            setDraft(null);
          }
        }}
        onBlur={commit}
      />
      <div className="num-interp" id={interpId} role="status" data-testid={testId && `${testId}-interp`}>
        {shown.error && (
          <span className="num-interp-error">
            <Icon name="error" />
            {t(shown.error.key, shown.error.params)}
          </span>
        )}
        {shown.value && <span className="num-interp-value">{t(shown.value.key, shown.value.params)}</span>}
        {shown.ratio && (
          <span className="num-interp-ratio">
            <Icon name="warning" />
            {t(shown.ratio.key, shown.ratio.params)}
          </span>
        )}
      </div>
    </div>
  );
}
