// 画面の好み（言語、テーマ、仕切りの位置、たたんだ状態）だけを、ブラウザに覚えさせる（app-design 6章）。
// 名前は boot.js と同じ（最初の描画の前に、言語とテーマを当てるため）。
import type { Lang } from "./i18n";

export type Theme = "light" | "dark";
const PREFIX = "circuit-sim.";

export function readPref(name: string): string | null {
  try {
    return window.localStorage.getItem(PREFIX + name);
  } catch {
    return null;
  }
}

export function writePref(name: string, value: string): void {
  try {
    window.localStorage.setItem(PREFIX + name, value);
  } catch {
    // 保存できない設定のブラウザでは、覚えないだけ（画面は動く）
  }
}

export function readNumber(name: string, fallback: number): number {
  const raw = readPref(name);
  const value = raw === null ? Number.NaN : Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

export function readFlag(name: string): boolean {
  return readPref(name) === "1";
}

/** boot.js が最初の描画の前に当てた値を読む（同じ決め方を二重に持たない）。 */
export function initialLang(): Lang {
  return document.documentElement.getAttribute("lang") === "en" ? "en" : "ja";
}

export function initialTheme(): Theme {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

export function applyLang(lang: Lang): void {
  document.documentElement.setAttribute("lang", lang);
  writePref("lang", lang);
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.style.colorScheme = theme;
  writePref("theme", theme);
}
