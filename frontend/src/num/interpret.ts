// 入力をどう読んだかの表示（画面仕様 2.2「解釈をその場で見せる」）と、読めないときの文言（2.3）。
import type { MessageKey, Params } from "../i18n";
import { formatBase, formatQuantity, formatRatio, PANEL_DIGITS, displayUnit } from "./format";
import type { FieldSpec, ParseError, ParseResult } from "./parse";

/** 桁の取り違え（m と M、µ と m、k と M）は 1000 倍の単位で起きるので、しきい値を 1000 倍に置く。 */
export const BIG_CHANGE = 1000;

export interface Line {
  key: MessageKey;
  params: Params;
}

export interface Interpretation {
  /** = 47 µH（4.7e-5 H） */
  value?: Line;
  /** ⚠ 変更前の 1,000,000 倍 */
  ratio?: Line;
  /** 読めないときの文言 */
  error?: Line;
}

export function errorLine(error: ParseError): Line {
  switch (error.code) {
    case "parse":
      return { key: "num.err.parse", params: { text: error.text } };
    case "comma":
      return { key: "num.err.comma", params: {} };
    case "rkm":
      return { key: "num.err.rkm", params: { text: error.text, suggest: error.suggest } };
    case "unit":
      return { key: "num.err.unit", params: { unit: error.unit, given: error.given } };
    case "suffix":
      return { key: "num.err.suffix", params: { suffix: error.suffix, unit: error.unit } };
    case "positive":
      return { key: "num.err.positive", params: {} };
    case "finite":
      return { key: "num.err.finite", params: {} };
  }
}

/** 開いたときの値に対する倍率の注意。開いたときに値が無かった項目と、値が 0 だった項目には出さない。 */
export function ratioLine(value: number, original: number | null): Line | undefined {
  if (original === null || original === 0 || value === 0) return undefined;
  const ratio = Math.abs(value / original);
  if (ratio >= BIG_CHANGE) return { key: "num.interp.ratio.up", params: { ratio: formatRatio(ratio) } };
  if (ratio <= 1 / BIG_CHANGE) return { key: "num.interp.ratio.down", params: { ratio: formatRatio(1 / ratio) } };
  return undefined;
}

export function interpret(result: ParseResult, field: FieldSpec, original: number | null): Interpretation {
  if (result.kind === "empty") return {};
  if (result.kind === "error") return { error: errorLine(result.error) };
  const shown = formatQuantity(result.value, field.unit, PANEL_DIGITS);
  const base = displayUnit(field.unit);
  const hasPrefix = base !== "" && shown.unit !== base;
  const value: Line = hasPrefix
    ? { key: "num.interp.base", params: { value: shown.text, base: formatBase(result.value, field.unit) } }
    : { key: "num.interp", params: { value: shown.text } };
  return { value, ratio: ratioLine(result.value, original) };
}
