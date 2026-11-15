// 画面の好み（言語、テーマ、仕切りの位置、たたんだ状態、お知らせの帯の開閉）を覚える（app-design 6章）。
// ブラウザ（localStorage）と、アプリ本体の設定ファイル（/api/settings）の両方に書く。ブラウザの保存はポートごとに
// 分かれるので、ポートが変わっても次の起動で戻るように、設定ファイルにも書く（D-050）。
// 名前は boot.js と同じ。boot.js が、最初の描画の前に設定ファイルの値をブラウザの保存へ写す。
//
// 設定ファイルを読めていない間（別のサイトのリンクから開いて、ページに data-prefs が無いとき）は、設定ファイルへ
// 書かない（既定の値で、利用者の好みを上書きしない）。つながったら読み（syncServerPrefs）、それから書く。
// 書けなかったとき（アプリ本体が断った・失敗した）は、自動の送り直しを止めて知らせる（onPrefsFailure）。
// 次に利用者が好みを変えたときに、もう一度送る。
import { api, HttpError } from "./api";
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
/** 設定ファイルの好みを読めているか（アプリ本体がページに data-prefs を書いたか、あとで読んだか） */
let serverKnown = typeof document !== "undefined" && document.documentElement.hasAttribute("data-prefs");
/** 書けなかった後、利用者が次に変えるまで、自動では送らない */
let blocked = false;

export interface PrefsFailure {
  /** 設定の場所（アプリ本体が返したもの。無ければ空） */
  path: string;
  /** 内部の文（OS の理由など）。お知らせの「詳細」に出す */
  details: string;
}
let failureHandler: ((failure: PrefsFailure) => void) | null = null;

/** 設定ファイルに書けなかったときに呼ぶものを決める（App がお知らせを出す）。 */
export function onPrefsFailure(handler: ((failure: PrefsFailure) => void) | null): void {
  failureHandler = handler;
}

export function serverPrefsKnown(): boolean {
  return serverKnown;
}

/**
 * ページが設定ファイルの好みを持たずに開いたとき（data-prefs が無い）、アプリ本体から読んでブラウザの保存へ写す。
 * 読めたら、その値を返す（言語とテーマを当て直すため）。もう読めていれば、読めなければ null。
 */
export async function syncServerPrefs(): Promise<Record<string, string> | null> {
  if (serverKnown) return null;
  let settings: Record<string, string>;
  try {
    settings = (await api.settings()).settings;
  } catch {
    return null;
  }
  for (const [name, value] of Object.entries(settings)) {
    if (!SERVER_PREFS.has(name) || pending.has(name)) continue; // 読む前に利用者が変えた値が新しい
    try {
      window.localStorage.setItem(PREFIX + name, value);
    } catch {
      // 保存できない設定のブラウザ
    }
  }
  serverKnown = true;
  return settings;
}

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
    timer = window.setTimeout(() => void flushPrefs(true), SAVE_DELAY_MS);
  }
}

/**
 * まだ設定ファイルに書けていない好みを送る。
 * - 設定ファイルを読めていなければ送らない（読んでから。syncServerPrefs）。
 * - つながっていなければ、次の機会（再接続）まで持っておく。
 * - アプリ本体が断った・失敗したら、持っておき、知らせ、利用者が次に変えるまで自動では送らない。
 * byUser: 利用者が好みを変えたことによる送信（書けなかった後でも送る）。
 */
export async function flushPrefs(byUser = false): Promise<void> {
  if (pending.size === 0 || !serverKnown || (blocked && !byUser)) return;
  const batch = Object.fromEntries(pending);
  pending.clear();
  const keep = () => {
    for (const [name, value] of Object.entries(batch)) if (!pending.has(name)) pending.set(name, value);
  };
  try {
    await api.putSettings(batch);
    blocked = false;
  } catch (exc) {
    keep(); // つながっていない・合言葉が無いときは、再接続の後に送る
    if (exc instanceof HttpError) {
      blocked = true;
      const body = exc.body ?? {};
      const path = typeof body.path === "string" ? body.path : "";
      const reason = typeof body.reason === "string" ? body.reason : "";
      failureHandler?.({ path, details: [`HTTP ${exc.status}`, reason].filter(Boolean).join(": ") });
    }
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
