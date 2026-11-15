// 画面の好み（言語、テーマ、仕切りの位置、たたんだ状態、お知らせの帯の開閉）を覚える（app-design 6章）。
// ブラウザ（localStorage）と、アプリ本体の設定ファイル（/api/settings）の両方に書く。ブラウザの保存はポートごとに
// 分かれるので、ポートが変わっても次の起動で戻るように、設定ファイルにも書く（D-050）。
// 名前は boot.js と同じ。boot.js が、最初の描画の前に設定ファイルの値をブラウザの保存へ写す。
import { api } from "./api";
import type { Lang } from "./i18n";

export type Theme = "light" | "dark";
const PREFIX = "circuit-sim.";
/** 設定ファイルにも書く名前（アプリ本体の config.py の PREFS と同じ） */
export const SERVER_PREFS = new Set([
  "lang",
  "theme",
  "layout.specWidth",
  "layout.schemHeight",
  "layout.specCollapsed",
  "layout.schemCollapsed",
  "layout.noticesOpen",
]);
/** 続けて変わる値（仕切りのドラッグ）をまとめて送るまでの時間 */
export const SAVE_DELAY_MS = 300;

const pending = new Map<string, string>();
let timer: number | undefined;

export function readPref(name: string): string | null {
  try {
    return window.localStorage.getItem(PREFIX + name);
  } catch {
    return null;
  }
}

export function writePref(name: string, value: string): void {
  let same = false;
  try {
    same = window.localStorage.getItem(PREFIX + name) === value;
    window.localStorage.setItem(PREFIX + name, value);
  } catch {
    // 保存できない設定のブラウザでも、設定ファイルには書く（画面は動く）
  }
  if (SERVER_PREFS.has(name) && !same) {
    pending.set(name, value);
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void flushPrefs(), SAVE_DELAY_MS);
  }
}

/** まだ設定ファイルに書けていない好みを送る。つながっていなければ、次の機会（再接続）まで持っておく。 */
export async function flushPrefs(): Promise<void> {
  if (pending.size === 0) return;
  const batch = Object.fromEntries(pending);
  pending.clear();
  try {
    await api.putSettings(batch);
  } catch {
    for (const [name, value] of Object.entries(batch)) if (!pending.has(name)) pending.set(name, value);
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
