// 画面仕様 2章の例の表を、そのまま試験にする（AC-2-1、AC-2-2、AC-2-3、AC-2-6）。
import { describe, expect, it } from "vitest";
import {
  NBSP,
  PANEL_DIGITS,
  READOUT_DIGITS,
  SCHEMATIC_DIGITS,
  formatBase,
  formatCount,
  formatElapsed,
  formatQuantity,
  formatRatio,
} from "./format";
import { parseQuantity, type FieldSpec } from "./parse";

const H: FieldSpec = { unit: "H", positive: true };
const show = (v: number, unit = "H") => formatQuantity(v, unit, PANEL_DIGITS).text;
const value = (text: string, field: FieldSpec = H) => {
  const r = parseQuantity(text, field);
  if (r.kind !== "ok") throw new Error(`${text}: ${JSON.stringify(r)}`);
  return r.value;
};
const error = (text: string, field: FieldSpec = H) => {
  const r = parseQuantity(text, field);
  if (r.kind !== "error") throw new Error(`${text}: ${JSON.stringify(r)}`);
  return r.error;
};

describe("2.2 の例（単位が H の欄）AC-2-1", () => {
  it.each(["47u", "47 µH", "47uH", "47μH", "４７ｕ", "4.7e-5", "0.047m", "47e-6 H"])("%s → 47 µH", (t) => {
    expect(show(value(t))).toBe(`47${NBSP}µH`);
    expect(value(t)).toBe(4.7e-5);
  });
  it("1m → 1 mH", () => expect(show(value("1m"))).toBe(`1${NBSP}mH`));
  it.each(["1M", "1meg", "1MEG", "1e3k"])("%s → 1 MH（解釈に 1e6 H）", (t) => {
    expect(show(value(t))).toBe(`1${NBSP}MH`);
    expect(formatBase(value(t), "H")).toBe(`1e6${NBSP}H`);
  });
  it.each(["2.2K", "2.2k"])("%s → 2.2 kH", (t) => expect(show(value(t))).toBe(`2.2${NBSP}kH`));
  it.each(["+5", "5.", "5 H"])("%s → 5 H", (t) => expect(show(value(t))).toBe(`5${NBSP}H`));
  it.each(["-5", "0"])("%s → 0 より大きい値が必要", (t) => expect(error(t)).toEqual({ code: "positive" }));
  it("空欄 → 不足（値なし）", () => {
    expect(parseQuantity("", H)).toEqual({ kind: "empty" });
    expect(parseQuantity("   ", H)).toEqual({ kind: "empty" });
  });
  it("47uF → 単位が違う", () => expect(error("47uF")).toEqual({ code: "unit", unit: "H", given: "F" }));
  it.each(["47U", "1h"])("%s → 読めない", (t) => expect(error(t).code).toBe("suffix"));
  it("1,000 と 4k7 → それぞれの直し方", () => {
    expect(error("1,000")).toEqual({ code: "comma" });
    expect(error("4k7")).toEqual({ code: "rkm", text: "4k7", suggest: "4.7k" });
  });
  it(".5 と 4.7E-5 も読める", () => {
    expect(value(".5")).toBe(0.5);
    expect(value("4.7E-5")).toBe(4.7e-5);
  });
  it("全角の符号と小数点、U+2212 の負の符号も読む", () => {
    expect(value("－５", { unit: "V", positive: false })).toBe(-5);
    expect(value("−5", { unit: "V", positive: false })).toBe(-5);
    expect(value("４．７ｕ")).toBe(4.7e-6);
  });
});

describe("2.2 の「読めないもの」AC-2-2", () => {
  it.each([
    ["1,000", H, { code: "comma" }],
    ["4,7", H, { code: "comma" }],
    ["4k7", H, { code: "rkm", text: "4k7", suggest: "4.7k" }],
    ["6K34", H, { code: "rkm", text: "6K34", suggest: "6.34K" }],
    ["47uF", H, { code: "unit", unit: "H", given: "F" }],
    ["47U", H, { code: "suffix", suffix: "U", unit: "H" }],
    ["5v", { unit: "V", positive: false }, { code: "suffix", suffix: "v", unit: "V" }],
    ["2a", { unit: "A", positive: false }, { code: "suffix", suffix: "a", unit: "A" }],
    ["1e", H, { code: "parse", text: "1e" }],
    ["abc", H, { code: "parse", text: "abc" }],
    ["inf", H, { code: "finite" }],
    ["nan", H, { code: "finite" }],
  ] as const)("%s", (text, field, expected) => {
    expect(parseQuantity(text, field)).toEqual({ kind: "error", error: expected });
  });
  it("大きすぎて有限でない値", () => expect(error("1e999")).toEqual({ code: "finite" }));
});

describe("単位の書き方（2.2）", () => {
  const R: FieldSpec = { unit: "ohm", positive: true };
  const f: FieldSpec = { unit: "Hz", positive: true };
  it("ohm と Hz は大文字小文字を区別しない", () => {
    for (const t of ["1kohm", "1k Ohm", "1kOHM", "1kΩ", "1kΩ"]) expect(value(t, R)).toBe(1000);
    for (const t of ["100kHz", "100khz", "100 kHZ"]) expect(value(t, f)).toBe(1e5);
  });
  it("1 MΩ、1e6 Ω（2.2 の解釈の例）", () => {
    expect(show(value("1M", R), "ohm")).toBe(`1${NBSP}MΩ`);
    expect(formatBase(1e6, "ohm")).toBe(`1e6${NBSP}Ω`);
  });
  it("s の欄の ms と、ほかの量の単位", () => {
    expect(value("5ms", { unit: "s", positive: true })).toBe(5e-3);
    expect(error("5 Hz", { unit: "s", positive: true })).toEqual({ code: "unit", unit: "s", given: "Hz" });
    expect(error("1 Hz")).toEqual({ code: "unit", unit: "H", given: "Hz" });
  });
});

describe("表示（2.1）", () => {
  it("接頭辞の選び方と 0、負の値", () => {
    expect(show(1e-3)).toBe(`1${NBSP}mH`);
    expect(formatQuantity(1e5, "Hz").text).toBe(`100${NBSP}kHz`);
    expect(formatQuantity(-1.5, "A").text).toBe(`-1.5${NBSP}A`);
    expect(formatQuantity(0, "V").text).toBe(`0${NBSP}V`);
    expect(formatQuantity(1.5e-18, "F").text).toBe(`1.5e-18${NBSP}F`);
    expect(formatQuantity(2.5e12, "Hz").text).toBe(`2.5e12${NBSP}Hz`);
  });
  it("仕様パネル: 有効数字9桁まで、末尾の 0 を書かない", () => {
    expect(show(4.7e-5)).toBe(`47${NBSP}µH`);
    const v = formatQuantity(1e-3 / Math.PI, "H", PANEL_DIGITS);
    expect(v.text).toBe(`318.309886${NBSP}µH`);
    expect(v.rounded).toBe(true);
    expect(formatQuantity(3.18309886e-3, "H", PANEL_DIGITS).text).toBe(`3.18309886${NBSP}mH`);
  });
  it("回路図: 有効数字4桁まで、丸めた印", () => {
    const v = formatQuantity(3.18309886e-3, "H", SCHEMATIC_DIGITS);
    expect(v.text).toBe(`3.183${NBSP}mH`);
    expect(v.rounded).toBe(true);
    expect(formatQuantity(1e-3, "H", SCHEMATIC_DIGITS).rounded).toBe(false);
  });
  it("スコープの読み取り: 有効数字6桁で固定、末尾の 0 も書く", () => {
    expect(formatQuantity(12, "V", READOUT_DIGITS).text).toBe(`12.0000${NBSP}V`);
    expect(formatQuantity(0, "V", READOUT_DIGITS).text).toBe(`0.00000${NBSP}V`);
    expect(formatQuantity(999.9999, "V", READOUT_DIGITS).text).toBe(`1.00000${NBSP}kV`);
  });
  it("単位の無い量は数値だけ", () => {
    expect(formatQuantity(0.5, "").text).toBe("0.5");
    expect(formatQuantity(-0.25, "").text).toBe("-0.25");
  });
  it("有限でない値は —", () => {
    expect(formatQuantity(Number.NaN, "V").text).toBe("—");
    expect(formatQuantity(Number.POSITIVE_INFINITY, "V").text).toBe("—");
  });
  it("個数、所要時間、倍率", () => {
    expect(formatCount(3183)).toBe("3,183");
    expect(formatCount(1048576)).toBe("1,048,576");
    expect(formatElapsed(0.123)).toBe(`0.12${NBSP}s`);
    expect(formatElapsed(3.41)).toBe(`3.4${NBSP}s`);
    expect(formatElapsed(72.4)).toBe(`72${NBSP}s`);
    expect(formatRatio(1e6)).toBe("1,000,000");
    expect(formatRatio(1000)).toBe("1,000");
  });
  it("数値と単位の間は改行されない空白（AC-2-6）", () => {
    for (const v of [1, 47e-6, 1e6, -3.3]) expect(formatQuantity(v, "V").text).toContain(NBSP);
    expect(formatQuantity(47e-6, "H").text).not.toContain(" ");
  });
});

describe("表示した値を貼り付けると同じ値に戻る（AC-2-3）", () => {
  const units = ["V", "A", "ohm", "H", "F", "s", "Hz"];
  const values = [
    1, 10, 47e-6, 4.7e-5, 1e-3, 3.3e-3, 100e3, 2.2e3, 1e6, 1e-12, 6.8e-9, 12, 0.5, 999, 1000, 1e-15, 999e9,
    1.5e-18, 3.14159265e-6, 24, 6, 100e-6,
  ];
  for (const unit of units) {
    it(unit, () => {
      for (const v of values) {
        const shown = formatQuantity(v, unit, PANEL_DIGITS);
        expect(shown.rounded).toBe(false);
        expect(value(shown.text, { unit, positive: true })).toBe(v);
      }
    });
  }
  it("乱数の値（9桁で表せるもの）", () => {
    let seed = 12345;
    const random = (n: number) => {
      seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
      return seed % n;
    };
    for (let i = 0; i < 2000; i += 1) {
      let digits = String(1 + random(9));
      for (let k = random(9); k > 0; k -= 1) digits += String(random(10));
      const v = Number(`${digits}e${random(26) - 15 - digits.length}`);
      const shown = formatQuantity(v, "F", PANEL_DIGITS);
      expect(value(shown.text, { unit: "F", positive: true })).toBe(v);
    }
  });
});
