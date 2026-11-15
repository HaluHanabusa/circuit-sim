// お知らせ（画面仕様 9章、付録B）。文面は辞書の notice.<番号>.what / .fix から、番号と差し込む値で作る。
// 言語を切り替えると、出ているお知らせも作り直される（AC-10-8）。
import type { MessageKey, Params } from "./i18n";

export type Level = "error" | "missing" | "warning";
/** 出来事（開けなかった、計算が失敗した…）と、状態（不足、入力エラー…。原因が無くなると消える）。 */
export type Kind = "event" | "state";

interface Entry {
  level: Level;
  kind: Kind;
  /** 利用者が閉じられる */
  dismissible: boolean;
}

const OPEN: Entry = { level: "error", kind: "event", dismissible: true };

export const CATALOG = {
  "N-OPEN-MULTI": OPEN,
  "N-OPEN-TYPE": OPEN,
  "N-OPEN-SIZE": OPEN,
  "N-OPEN-READ": OPEN,
  "N-OPEN-ENCODING": OPEN,
  "N-OPEN-SYNTAX": OPEN,
  "N-OPEN-VERSION": OPEN,
  "N-OPEN-SCHEMA": OPEN,
  "N-OPEN-TEMPLATE": OPEN,
  "N-MISSING": { level: "missing", kind: "state", dismissible: false },
  "N-INVALID": { level: "error", kind: "state", dismissible: false },
  "N-UNIT-WRONG": { level: "error", kind: "state", dismissible: false },
  "N-STEPS-LIMIT": { level: "error", kind: "state", dismissible: false },
  "N-STEP-COARSE": { level: "warning", kind: "state", dismissible: false },
  "N-BIG-CHANGE": { level: "warning", kind: "state", dismissible: true },
  "N-STALE": { level: "warning", kind: "state", dismissible: false },
  "N-UNIT-CONVERTED": { level: "warning", kind: "event", dismissible: true },
  "N-UNUSED": { level: "warning", kind: "event", dismissible: true },
  "N-SAVE-INVALID": { level: "warning", kind: "event", dismissible: true },
  "N-CONN-RESTART": { level: "warning", kind: "event", dismissible: true },
  "N-RUN-CIRCUIT": { level: "error", kind: "event", dismissible: true },
  "N-RUN-SWITCH": { level: "error", kind: "event", dismissible: true },
  "N-RUN-DIVERGED": { level: "error", kind: "event", dismissible: true },
  "N-RUN-STEPS": { level: "error", kind: "event", dismissible: true },
  "N-RUN-MEMORY": { level: "error", kind: "event", dismissible: true },
  "N-RUN-INTERNAL": { level: "error", kind: "event", dismissible: true },
  "N-RUN-NOTE-STEP": { level: "warning", kind: "event", dismissible: false },
  "N-RUN-NOTE-ROUND": { level: "warning", kind: "event", dismissible: false },
  "N-RUN-NOTE-OTHER": { level: "warning", kind: "event", dismissible: false },
  "N-DATA-FETCH": { level: "error", kind: "event", dismissible: true },
  "N-EXPORT-FAIL": { level: "error", kind: "event", dismissible: true },
  "N-PLOTS-FULL": { level: "warning", kind: "event", dismissible: true },
  "N-SIGNALS-FULL": { level: "warning", kind: "event", dismissible: true },
} as const satisfies Record<string, Entry>;

export type NoticeCode = keyof typeof CATALOG;
export const LEVELS: readonly Level[] = ["error", "missing", "warning"];

/** N-OPEN-SCHEMA の下に並べる箇所（{path}: {problem}）。problem は辞書の schema.* のキー。 */
export interface Place {
  path: string;
  problem: MessageKey;
}

export interface NoticeData {
  code: NoticeCode;
  params: Params;
  items?: Place[];
  more?: number;
  /** プログラムの内部の文。主文に出さず「詳細」の中だけに出す（9章、AC-9-6） */
  details?: string | null;
  /** どう直すかを、別の文言から選ぶもの（N-INVALID）。 */
  fixKey?: MessageKey;
  fixParams?: Params;
}

export interface Notice extends NoticeData {
  id: number;
  /** 起きた時刻（出来事だけ。24時間表記） */
  time?: string;
}

export function isNoticeCode(code: string): code is NoticeCode {
  return Object.prototype.hasOwnProperty.call(CATALOG, code);
}

export function entry(code: NoticeCode): Entry {
  return CATALOG[code];
}

export function whatKey(n: NoticeData): MessageKey {
  return `notice.${n.code}.what` as MessageKey;
}

export function fixKey(n: NoticeData): MessageKey {
  return n.fixKey ?? (`notice.${n.code}.fix` as MessageKey);
}

/** 並び順: エラー → 不足 → 注意。同じ段階の中は、出来事は新しい順、状態は入った順（仕様パネルの行の順）。 */
export function sortNotices(list: Notice[]): Notice[] {
  return [...list].sort((a, b) => {
    const la = LEVELS.indexOf(entry(a.code).level);
    const lb = LEVELS.indexOf(entry(b.code).level);
    if (la !== lb) return la - lb;
    const ka = entry(a.code).kind;
    const kb = entry(b.code).kind;
    if (ka !== kb) return ka === "event" ? -1 : 1;
    return ka === "event" ? b.id - a.id : a.id - b.id;
  });
}

export function clockTime(date: Date): string {
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
}
