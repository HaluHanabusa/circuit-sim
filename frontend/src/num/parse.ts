// 数値の入力の読み取り（画面仕様 2.2）。表示と同じ書き方で入力できる。単位と接頭辞は省ける。
//   [符号] 数値 [e 指数] [空白] [接頭辞] [単位]
// 接頭辞と1文字の単位は大文字と小文字を区別する（m はミリ、M はメガ）。値は、10 進の文字列を組み立ててから
// Number() で読む（いちばん近い 2 進の値になる。10 のべきを掛けると最後の桁がずれることがある）。

import { displayUnit } from "./format";

export interface FieldSpec {
  /** 仕様JSONの単位（V、A、ohm、H、F、s、Hz、""） */
  unit: string;
  /** 0 より大きい値だけを受け付ける（付録A の範囲。v0.1 は「0 より大きい」と「有限」） */
  positive: boolean;
}

export type ParseError =
  | { code: "parse"; text: string }
  | { code: "comma" }
  | { code: "rkm"; text: string; suggest: string }
  | { code: "unit"; unit: string; given: string }
  | { code: "suffix"; suffix: string; unit: string }
  | { code: "positive" }
  | { code: "finite" };

export type ParseResult =
  | { kind: "empty" }
  | { kind: "ok"; value: number; prefixed: boolean }
  | { kind: "error"; error: ParseError };

/** 接頭辞 → 10 の指数。別の書き方: マイクロは u と μ、キロは K、メガは meg（大文字小文字を問わない）。 */
const PREFIX: Record<string, number> = {
  f: -15,
  p: -12,
  n: -9,
  "µ": -6,
  u: -6,
  m: -3,
  k: 3,
  K: 3,
  M: 6,
  G: 9,
};

/** 単位の書き方（区別しないもの以外は、大文字と小文字を区別する）。 */
const UNIT_FORMS: Record<string, RegExp> = {
  V: /V$/,
  A: /A$/,
  "Ω": /(Ω|ohm)$/i,
  H: /H$/,
  F: /F$/,
  s: /s$/,
  Hz: /hz$/i,
};

function prefixExponent(text: string): number | undefined {
  if (text === "") return 0;
  if (/^meg$/i.test(text)) return 6;
  return Object.prototype.hasOwnProperty.call(PREFIX, text) ? PREFIX[text] : undefined;
}

/** 全角の数字・英字・記号を半角に、マイクロとオームの別の文字を表示の文字に直す。 */
export function normalizeInput(text: string): string {
  let out = "";
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    if (code >= 0xff01 && code <= 0xff5e) out += String.fromCodePoint(code - 0xfee0);
    else if (code === 0x3000) out += " ";
    else if (code === 0x2212) out += "-";
    else if (code === 0x03bc) out += "µ";
    else if (code === 0x2126) out += "Ω";
    else out += ch;
  }
  return out;
}

const NUMBER = /^([+-]?)(\d+\.?\d*|\.\d+)(?:[eE]([+-]?\d+))?/;

export function parseQuantity(input: string, field: FieldSpec): ParseResult {
  const text = normalizeInput(input).trim();
  if (text === "") return { kind: "empty" };
  if (text.includes(",")) return fail({ code: "comma" });
  if (/^[+-]?(inf(inity)?|nan)$/i.test(text)) return fail({ code: "finite" });

  const m = NUMBER.exec(text);
  if (!m) return fail({ code: "parse", text: input.trim() });
  const [whole, sign, mantissa, exp] = m;
  const rest = text.slice(whole.length).replace(/\s+/g, "");

  // 数値の後ろが e だけ（1e、1e+）は、指数の書きかけで、数値として読めない
  if (/^[eE][+-]?$/.test(rest) || /^[eE][+-]?\d/.test(rest)) return fail({ code: "parse", text: input.trim() });

  // 接頭辞を小数点の代わりにする書き方（4k7、6K34）
  const rkm = /^([fpnµumkKMG])(\d+)$/.exec(rest);
  if (rkm && /^\d+$/.test(mantissa) && exp === undefined) {
    return fail({ code: "rkm", text: input.trim(), suggest: `${sign}${mantissa}.${rkm[2]}${rkm[1]}` });
  }

  const unit = displayUnit(field.unit);
  let prefixText = rest;
  const own = unit ? UNIT_FORMS[unit] : undefined;
  const ownMatch = own ? own.exec(rest) : null;
  if (ownMatch) prefixText = rest.slice(0, rest.length - ownMatch[0].length);
  const prefix = prefixExponent(prefixText);

  if (prefix === undefined) {
    // ほかの量の単位が書かれている（H の欄に 47uF）
    for (const [other, form] of Object.entries(UNIT_FORMS)) {
      if (other === unit) continue;
      const hit = form.exec(rest);
      if (hit && prefixExponent(rest.slice(0, rest.length - hit[0].length)) !== undefined) {
        return fail({ code: "unit", unit, given: hit[0] });
      }
    }
    return fail({ code: "suffix", suffix: rest, unit });
  }

  const exponent = (exp === undefined ? 0 : Number(exp)) + prefix;
  const value = Number(`${sign}${mantissa}e${exponent}`);
  if (!Number.isFinite(value)) return fail({ code: "finite" });
  if (field.positive && !(value > 0)) return fail({ code: "positive" });
  return { kind: "ok", value: value === 0 ? 0 : value, prefixed: prefixText !== "" };
}

function fail(error: ParseError): ParseResult {
  return { kind: "error", error };
}
