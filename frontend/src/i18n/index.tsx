// 文言の辞書（画面仕様の文言の表から作る。frontend/tools/strings_from_spec.py）。
// 画面の文字列は、すべてここから引く。辞書に無いキーは、もう一方の言語の文言を出す（キーの名前を出さない。10.3）。
import { createContext, useContext, type ReactNode } from "react";
import en from "./en.json";
import ja from "./ja.json";
import { modifierKey } from "../platform";

export type Lang = "ja" | "en";
export type MessageKey = keyof typeof ja;
/** 差し込む値。文字列・数のほか、辞書のキー（言語を切り替えると訳し直す）も入れられる。 */
export type Param = string | number | { key: MessageKey; params?: Params };
export type Params = Record<string, Param>;

const TABLES: Record<Lang, Record<string, string>> = { ja, en };

export function hasKey(key: string): key is MessageKey {
  return Object.prototype.hasOwnProperty.call(ja, key) || Object.prototype.hasOwnProperty.call(en, key);
}

export function translate(lang: Lang, key: MessageKey, params: Params = {}): string {
  const other: Lang = lang === "ja" ? "en" : "ja";
  const template = TABLES[lang][key] ?? TABLES[other][key] ?? "";
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
    if (name === "mod" && !(name in params)) return modifierKey();
    const value = params[name];
    if (value === undefined) return whole;
    if (typeof value === "object") return translate(lang, value.key, value.params);
    return String(value);
  });
}

export type Translate = (key: MessageKey, params?: Params) => string;

interface I18n {
  lang: Lang;
  t: Translate;
}

const I18nContext = createContext<I18n>({ lang: "ja", t: (key, params) => translate("ja", key, params) });

export function I18nProvider({ lang, children }: { lang: Lang; children: ReactNode }) {
  const t: Translate = (key, params) => translate(lang, key, params);
  return <I18nContext.Provider value={{ lang, t }}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  return useContext(I18nContext);
}
