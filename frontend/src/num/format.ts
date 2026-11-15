// 数値の表示（画面仕様 2.1）。数値 ＋ 改行されない空白 ＋ 接頭辞 ＋ 単位。例: 47 µH、100 kHz、-1.5 A、0 V。
// 桁は、10 進の文字列（toExponential）から作る。2 進の値を 10 のべきで割って作ると、最後の桁がずれるため。

export const NBSP = " ";
export const DASH = "—";

/** 接頭辞（10 の指数 → 表示の文字）。表示のマイクロは U+00B5。 */
const PREFIX_BY_EXP: Record<number, string> = {
  [-15]: "f",
  [-12]: "p",
  [-9]: "n",
  [-6]: "µ",
  [-3]: "m",
  0: "",
  3: "k",
  6: "M",
  9: "G",
};

/** 仕様JSONの単位 → 画面の単位（2.1 の表）。 */
export function displayUnit(unit: string): string {
  if (unit.toLowerCase() === "ohm" || unit === "Ω" || unit === "Ω") return "Ω";
  if (unit.toLowerCase() === "hz") return "Hz";
  return unit;
}

export type Digits = { mode: "shortest"; max: number } | { mode: "fixed"; digits: number };

/** 仕様パネルの値: 有効数字9桁までで、元の値に戻せる最も短い表記。 */
export const PANEL_DIGITS: Digits = { mode: "shortest", max: 9 };
/** 回路図の値: 有効数字4桁まで（丸めたときは ≈ を付ける）。 */
export const SCHEMATIC_DIGITS: Digits = { mode: "shortest", max: 4 };
/** スコープの読み取り: 有効数字6桁で固定。末尾の 0 も書く。 */
export const READOUT_DIGITS: Digits = { mode: "fixed", digits: 6 };

export interface Formatted {
  /** 表示する文字列（数値と単位の間は U+00A0） */
  text: string;
  /** 数値の部分 */
  number: string;
  /** 接頭辞と単位（画面の書き方） */
  unit: string;
  /** 元の値に戻せない桁まで丸めた */
  rounded: boolean;
}

interface Decimal {
  negative: boolean;
  digits: string; // 先頭が 0 でない数字の並び（0 のときは "0"）
  exponent: number; // 先頭の数字の位（d.ddd × 10^exponent）
}

function decimalOf(value: number, significant: number): Decimal {
  const s = Math.abs(value).toExponential(significant - 1);
  const [mantissa, exp] = s.split("e");
  return { negative: value < 0, digits: mantissa.replace(".", ""), exponent: Number(exp) };
}

function trimZeros(digits: string): string {
  const trimmed = digits.replace(/0+$/, "");
  return trimmed === "" ? "0" : trimmed;
}

/** 数字の並びを、小数点の位置 point（先頭からの桁数）で区切って書く。 */
function placePoint(digits: string, point: number): string {
  if (point <= 0) return "0." + "0".repeat(-point) + digits;
  if (point >= digits.length) return digits + "0".repeat(point - digits.length);
  return digits.slice(0, point) + "." + digits.slice(point);
}

function roundTrips(d: Decimal, value: number): boolean {
  return Number(`${d.negative ? "-" : ""}${d.digits[0]}.${d.digits.slice(1) || "0"}e${d.exponent}`) === value;
}

/** 有効数字の決め方に従って、値を10進に直す。 */
function choose(value: number, digits: Digits): { d: Decimal; rounded: boolean } {
  if (digits.mode === "fixed") {
    const d = decimalOf(value, digits.digits);
    return { d, rounded: !roundTrips(d, value) };
  }
  for (let p = 1; p <= digits.max; p += 1) {
    const d = decimalOf(value, p);
    if (roundTrips(d, value)) return { d: { ...d, digits: trimZeros(d.digits) }, rounded: false };
  }
  const d = decimalOf(value, digits.max);
  return { d: { ...d, digits: trimZeros(d.digits) }, rounded: true };
}

/** 工学表記で書く。単位の無い量は数値だけ（接頭辞を付けない）。有限でない値は —。 */
export function formatQuantity(value: number, unit: string, digits: Digits = PANEL_DIGITS): Formatted {
  const shown = displayUnit(unit);
  if (!Number.isFinite(value)) return { text: DASH, number: DASH, unit: "", rounded: false };
  if (value === 0) {
    const zero = digits.mode === "fixed" ? placePoint("0".repeat(digits.digits), 1) : "0";
    return join(zero, shown, false);
  }
  const { d, rounded } = choose(value, digits);
  const sign = d.negative ? "-" : "";
  if (shown === "") {
    return join(sign + plainNumber(d, digits), "", rounded);
  }
  const eng = Math.floor(d.exponent / 3) * 3;
  if (!(eng in PREFIX_BY_EXP)) {
    // 接頭辞の範囲（f〜G）に収まらない値は、基本単位で指数表記にする（1.5e-18 F）
    return join(sign + exponential(d), shown, rounded);
  }
  const fixedLength = digits.mode === "fixed" ? digits.digits : d.digits.length;
  const body = placePoint(d.digits.padEnd(fixedLength, "0"), d.exponent - eng + 1);
  return join(sign + body, PREFIX_BY_EXP[eng] + shown, rounded);
}

function plainNumber(d: Decimal, digits: Digits): string {
  if (d.exponent < -4 || d.exponent >= 9) return exponential(d);
  const length = digits.mode === "fixed" ? digits.digits : d.digits.length;
  return placePoint(d.digits.padEnd(length, "0"), d.exponent + 1);
}

function exponential(d: Decimal): string {
  const mantissa = d.digits.length > 1 ? `${d.digits[0]}.${d.digits.slice(1)}` : d.digits;
  return `${mantissa}e${d.exponent}`;
}

function join(number: string, unit: string, rounded: boolean): Formatted {
  return { text: unit ? `${number}${NBSP}${unit}` : number, number, unit, rounded };
}

/** 基本単位での値（解釈の表示の括弧の中。4.7e-5 H、1e6 Ω）。 */
export function formatBase(value: number, unit: string): string {
  const shown = displayUnit(unit);
  const { d } = choose(value, { mode: "shortest", max: 17 });
  const text = (d.negative ? "-" : "") + exponential(d);
  return shown ? `${text}${NBSP}${shown}` : text;
}

/** 整数の個数（ステップ数、点数）。桁区切りのコンマを使う（3,183）。 */
export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return DASH;
  const sign = n < 0 ? "-" : "";
  return sign + String(Math.round(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 所要時間（計算にかかった実際の時間）。単位は s、有効数字2桁（0.12 s、3.4 s、72 s）。 */
export function formatElapsed(seconds: number): string {
  if (!Number.isFinite(seconds)) return DASH;
  if (seconds === 0) return `0${NBSP}s`;
  const d = decimalOf(seconds, 2);
  const point = d.exponent + 1;
  const body = point >= 2 ? String(Math.round(seconds)) : placePoint(d.digits, point);
  return `${body}${NBSP}s`;
}

export const APPROX = "≈";

/**
 * 倍率（変更前の {ratio} 倍）。有効数字3桁に丸め、桁区切りを付ける。丸めたときだけ前に ≈ を付ける
 * （1234 → ≈1,230、ちょうど 1000 → 1,000。画面仕様 2.2）。tolerance は、2 進の計算の誤差を「ちょうど」と
 * みなす相対の幅（999.9999999999999 は 1,000 で、≈ を付けない）。
 */
export function formatRatio(ratio: number, tolerance = 0): string {
  if (!Number.isFinite(ratio)) return DASH;
  const d = decimalOf(ratio, 3);
  const shown = Number(`${d.digits[0]}.${d.digits.slice(1)}e${d.exponent}`);
  const body = d.exponent >= 2 ? formatCount(shown) : placePoint(trimZeros(d.digits), d.exponent + 1);
  return Math.abs(shown - ratio) > tolerance * Math.abs(ratio) ? APPROX + body : body;
}
