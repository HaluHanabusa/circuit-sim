// アプリ本体（ローカルのサーバー）との通信。相手はこのPCの中のアプリ本体だけ（同じ場所の /api/）。
// 合言葉はクッキー（HttpOnly）で送られる。画面のコードは合言葉を持たない（D-051）。
import type { NoticeData } from "./notices";

export interface Doc {
  file: string;
  template: string;
  text: string;
  sample: string | null;
}

export interface SampleInfo {
  id: string;
  file: string;
  template: string;
  name: string;
  desc: string;
}

export type OpenResponse = { ok: true; doc: Doc } | { ok: false; notice: NoticeData };

/** 応答が無いとみなすまでの時間。つながっているかの確かめ（D8）が 5 秒以内に切断に気づくように短くする。 */
export const HEALTH_TIMEOUT_MS = 2000;

/** アプリ本体が合言葉を確かめられなかった（401）。ターミナルに出ている URL から開き直してもらう。 */
export class Unauthorized extends Error {}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 10000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      credentials: "same-origin",
      headers: init.body ? { "Content-Type": "application/json" } : undefined,
    });
    if (response.status === 401) throw new Unauthorized(`HTTP ${response.status}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export const api = {
  health: () => request<{ instance: string; version: string }>("/api/health", {}, HEALTH_TIMEOUT_MS),
  samples: () => request<{ samples: SampleInfo[] }>("/api/samples"),
  openSample: (sample: string) =>
    request<OpenResponse>("/api/open", { method: "POST", body: JSON.stringify({ sample }) }),
  openText: (file: string, text: string) =>
    request<OpenResponse>("/api/open", { method: "POST", body: JSON.stringify({ file, text }) }),
  session: () => request<{ doc: Doc | null; dirty: boolean }>("/api/session"),
  putSession: (doc: Doc | null, dirty: boolean) =>
    request<{ doc: Doc | null }>("/api/session", { method: "PUT", body: JSON.stringify({ doc, dirty }) }),
  putSettings: (settings: Record<string, string | null>) =>
    request<{ settings: Record<string, string> }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ settings }),
    }),
};
