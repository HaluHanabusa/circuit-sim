// 画面の好みを設定ファイルへ書く手順の試験（D-050、A0 のレビュー2回目の軽2・軽3）。
// prefs.ts はページを開いたときの状態（data-prefs の有無）を読むので、試験ごとに読み直す。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Prefs = typeof import("./prefs");

interface Call {
  method: string;
  body: unknown;
}

let calls: Call[] = [];
let reply: (method: string) => Promise<Response> = () => Promise.resolve(new Response("{}"));

async function load(withPrefs: boolean): Promise<Prefs> {
  vi.resetModules();
  window.localStorage.clear();
  if (withPrefs) document.documentElement.setAttribute("data-prefs", "{}");
  else document.documentElement.removeAttribute("data-prefs");
  return import("./prefs");
}

const puts = () => calls.filter((c) => c.method === "PUT");

beforeEach(() => {
  vi.useFakeTimers();
  calls = [];
  reply = () => Promise.resolve(new Response(JSON.stringify({ settings: {} })));
  vi.stubGlobal("fetch", (_url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    calls.push({ method, body: init.body ? JSON.parse(String(init.body)) : null });
    return reply(method);
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function settle(prefs: Prefs) {
  await vi.advanceTimersByTimeAsync(prefs.SAVE_DELAY_MS + 10);
}

describe("設定ファイルを読めていないとき（data-prefs が無いページ）", () => {
  it("書かない。読んだ後に、利用者が変えた値だけを書く", async () => {
    const prefs = await load(false);
    expect(prefs.serverPrefsKnown()).toBe(false);
    prefs.writePref("layout.specWidth", "400");
    await settle(prefs);
    await prefs.flushPrefs();
    expect(puts()).toEqual([]);

    reply = () =>
      Promise.resolve(new Response(JSON.stringify({ settings: { "layout.specWidth": "420", theme: "dark" } })));
    const read = await prefs.syncServerPrefs();
    expect(read).toEqual({ "layout.specWidth": "420", theme: "dark" });
    expect(prefs.serverPrefsKnown()).toBe(true);
    expect(window.localStorage.getItem("circuit-sim.theme")).toBe("dark");
    expect(window.localStorage.getItem("circuit-sim.layout.specWidth")).toBe("400"); // 読む前に変えた値が新しい

    reply = () => Promise.resolve(new Response(JSON.stringify({ settings: {} })));
    await prefs.flushPrefs();
    expect(puts().map((c) => c.body)).toEqual([{ settings: { "layout.specWidth": "400" } }]);
  });

  it("読めなければ、読めるまで書かない", async () => {
    const prefs = await load(false);
    reply = () => Promise.reject(new TypeError("Failed to fetch"));
    expect(await prefs.syncServerPrefs()).toBeNull();
    prefs.writePref("theme", "dark");
    await settle(prefs);
    expect(puts()).toEqual([]);
  });
});

describe("設定ファイルに書けなかったとき", () => {
  it("知らせて、自動の送り直しを止める。利用者が次に変えたら、まとめて送り直す", async () => {
    const prefs = await load(true);
    const failures: unknown[] = [];
    prefs.onPrefsFailure((f) => failures.push(f));
    reply = (method) =>
      Promise.resolve(
        method === "PUT"
          ? new Response(JSON.stringify({ detail: "x", path: "/cfg", reason: "Read-only file system" }), { status: 500 })
          : new Response("{}"),
      );
    prefs.writePref("theme", "dark");
    await settle(prefs);
    expect(puts()).toHaveLength(1);
    expect(failures).toEqual([{ path: "/cfg", details: "HTTP 500: Read-only file system" }]);

    for (let i = 0; i < 5; i++) await prefs.flushPrefs(); // 2 秒ごとの確かめ
    expect(puts()).toHaveLength(1);

    reply = () => Promise.resolve(new Response(JSON.stringify({ settings: {} })));
    prefs.writePref("lang", "en");
    await settle(prefs);
    expect(puts()).toHaveLength(2);
    expect(puts()[1].body).toEqual({ settings: { theme: "dark", lang: "en" } });
    expect(failures).toHaveLength(1);
  });

  it("つながっていないだけなら、知らせずに、次の確かめで送り直す", async () => {
    const prefs = await load(true);
    const failures: unknown[] = [];
    prefs.onPrefsFailure((f) => failures.push(f));
    reply = () => Promise.reject(new TypeError("Failed to fetch"));
    prefs.writePref("theme", "dark");
    await settle(prefs);
    reply = () => Promise.resolve(new Response(JSON.stringify({ settings: {} })));
    await prefs.flushPrefs();
    expect(puts()).toHaveLength(2);
    expect(puts()[1].body).toEqual({ settings: { theme: "dark" } });
    expect(failures).toEqual([]);
  });
});
