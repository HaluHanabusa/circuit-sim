// 作業画面の全体（画面仕様 1章）。つながっているかの確かめ（D8）、開く処理の入口、ドロップ、ショートカット、
// お知らせ、言語とテーマを持つ。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Unauthorized, type Doc, type SampleInfo } from "./api";
import { Header } from "./components/Header";
import { Frame } from "./components/Frame";
import { Icon } from "./components/Icon";
import { NoticeBar } from "./components/NoticeBar";
import { StartScreen } from "./components/StartScreen";
import { I18nProvider, translate, type Lang } from "./i18n";
import { clockTime, entry, whatKey, type Notice, type NoticeData } from "./notices";
import { openSample, readSpecFile, type OpenResult } from "./open";
import { hasModifier } from "./platform";
import {
  applyLang,
  applyTheme,
  flushPrefs,
  initialLang,
  initialTheme,
  onPrefsFailure,
  readFlag,
  serverPrefsKnown,
  syncServerPrefs,
  writePref,
  type Theme,
} from "./prefs";

/** 2秒ごとに確かめる。応答が無いとみなすまで 2秒（api.HEALTH_TIMEOUT_MS）。切断に 5秒以内に気づく（D8） */
export const HEALTH_INTERVAL_MS = 2000;
/** 待ちの表示は、始まってから 200 ms たってから出す（design-system 9章） */
export const WAIT_SHOW_MS = 200;

/** unauthorized: アプリ本体は動いているが、このページは合言葉のクッキーを持っていない（D-051） */
type Conn = "connecting" | "connected" | "disconnected" | "unauthorized";

export function App() {
  const [lang, setLang] = useState<Lang>(initialLang);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [conn, setConn] = useState<Conn>("connecting");
  const [slowConnect, setSlowConnect] = useState(false);
  const [doc, setDoc] = useState<Doc | null>(null);
  const [samples, setSamples] = useState<SampleInfo[] | null>(null);
  const [samplesFailed, setSamplesFailed] = useState(false);
  const [samplesSlow, setSamplesSlow] = useState(false);
  const [openError, setOpenError] = useState<{ notice: NoticeData; file?: string } | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  // 帯の開閉。覚えるのは利用者が押したときだけ（layout.noticesOpen）。0 件のときはたたむ。エラーの出来事では
  // 自動で開くが、覚えない（9.3、9.4）
  const [noticesOpen, setNoticesOpen] = useState(false);
  const [focusRun, setFocusRun] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [announce, setAnnounce] = useState("");
  const instance = useRef<string | null>(null);
  const docRef = useRef<Doc | null>(null);
  const nextId = useRef(1);
  const fileInput = useRef<HTMLInputElement>(null);
  const chooseRef = useRef<HTMLButtonElement | null>(null);
  const runRef = useRef<HTMLButtonElement | null>(null);
  const noticeCount = useRef(0);
  const connected = conn === "connected";
  docRef.current = doc;

  useEffect(() => {
    const before = noticeCount.current;
    noticeCount.current = notices.length;
    if (notices.length === 0) setNoticesOpen(false);
    else if (before === 0) setNoticesOpen((open) => open || readFlag("layout.noticesOpen"));
  }, [notices.length]);

  const toggleNotices = () => {
    const next = !noticesOpen;
    setNoticesOpen(next);
    writePref("layout.noticesOpen", next ? "1" : "0");
  };

  // 開けた後は「実行」にフォーカスを置く（3.4、4.3。不足・エラーの行へ置くのは A1）
  useEffect(() => {
    if (focusRun) runRef.current?.focus();
  }, [focusRun]);

  const addNotice = useCallback((data: NoticeData) => {
    const e = entry(data.code);
    const notice: Notice = { ...data, id: nextId.current++, time: e.kind === "event" ? clockTime(new Date()) : undefined };
    setNotices((list) => [...list, notice]);
    if (e.level === "error" && e.kind === "event") {
      // 新しいエラーの出来事は、帯を自動で開いて、読み上げにも伝える（9.4）
      setNoticesOpen(true);
      setAnnounce(translate(initialLang(), whatKey(notice), notice.params)); // 表示中の言語（<html lang>）
    }
  }, []);

  // 画面の好みを設定ファイルに書けなかった: お知らせで知らせる（同じお知らせが出ていれば足さない）
  useEffect(() => {
    onPrefsFailure(({ path, details }) => {
      setNotices((list) =>
        list.some((n) => n.code === "N-PREFS-SAVE")
          ? list
          : [...list, { code: "N-PREFS-SAVE", params: { path }, details, id: nextId.current++, time: clockTime(new Date()) }],
      );
      setAnnounce(translate(initialLang(), "notice.N-PREFS-SAVE.what", {}));
    });
    return () => onPrefsFailure(null);
  }, []);

  // ---------------------------------------------------------------- つながっているかの確かめ（D8、1.3）

  useEffect(() => {
    const timer = window.setTimeout(() => setSlowConnect(true), WAIT_SHOW_MS);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const check = async () => {
      try {
        const health = await api.health();
        if (stopped) return;
        const before = instance.current;
        if (!serverPrefsKnown()) {
          // ページが設定ファイルの好みを持たずに開いた（別のサイトのリンクから来て、クッキーが付かなかった）:
          // 読んでから書く（既定の値で上書きしない）。言語とテーマは読んだ値を当て直す
          const prefs = await syncServerPrefs();
          if (stopped) return;
          if (prefs?.lang === "ja" || prefs?.lang === "en") {
            applyLang(prefs.lang);
            setLang(prefs.lang);
          }
          if (prefs?.theme === "light" || prefs?.theme === "dark") {
            applyTheme(prefs.theme);
            setTheme(prefs.theme);
          }
        }
        void flushPrefs(); // つながっていない間に変えた好みを、設定ファイルへ
        if (before === null) {
          // 最初につながった: アプリ本体が持っている作業の状態を取り戻す（再読み込みで作業を失わせない。1.3）
          const session = await api.session();
          if (!stopped && session.doc) setDoc(session.doc);
        } else if (before !== health.instance && docRef.current) {
          // アプリ本体が起動し直されていた: 画面が持っている仕様を送り直す。結果は消えている（N-CONN-RESTART）
          await api.putSession(docRef.current, false);
          addNotice({ code: "N-CONN-RESTART", params: {} });
        }
        instance.current = health.instance;
        if (!stopped) setConn("connected");
      } catch (exc) {
        if (!stopped) setConn(exc instanceof Unauthorized ? "unauthorized" : "disconnected");
      }
      if (!stopped) timer = window.setTimeout(check, HEALTH_INTERVAL_MS);
    };
    void check();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [addNotice]);

  // ---------------------------------------------------------------- サンプルの一覧（D1、3.3）

  const loadSamples = useCallback(async () => {
    setSamplesFailed(false);
    setSamplesSlow(false);
    const slow = window.setTimeout(() => setSamplesSlow(true), WAIT_SHOW_MS);
    try {
      const response = await api.samples();
      setSamples(response.samples);
    } catch {
      setSamplesFailed(true);
    } finally {
      window.clearTimeout(slow);
    }
  }, []);

  useEffect(() => {
    if (connected && samples === null && !samplesFailed) void loadSamples();
  }, [connected, samples, samplesFailed, loadSamples]);

  // ---------------------------------------------------------------- 開く（入口は1つ。4.2）

  const finishOpen = useCallback(
    (result: OpenResult) => {
      if (result.ok) {
        setDoc(result.doc);
        setOpenError(null);
        setNotices((list) => list.filter((n) => !n.code.startsWith("N-OPEN-") && n.code !== "N-CONN-RESTART"));
        setFocusRun((n) => n + 1);
        return;
      }
      if (docRef.current) addNotice(result.notice);
      else setOpenError({ notice: result.notice, file: result.file });
    },
    [addNotice],
  );

  const runOpen = useCallback(
    async (label: string | null, work: () => Promise<OpenResult>) => {
      setOpenError(null);
      // 次に開く操作をすると、前の「開けなかった」は消える（B.1）
      setNotices((list) => list.filter((n) => !n.code.startsWith("N-OPEN-")));
      const slow = window.setTimeout(() => setOpening(label), WAIT_SHOW_MS);
      try {
        finishOpen(await work());
      } catch (exc) {
        finishOpen({ ok: false, notice: { code: "N-OPEN-READ", params: { file: label ?? "" }, details: String(exc) } });
      } finally {
        window.clearTimeout(slow);
        setOpening(null);
      }
    },
    [finishOpen],
  );

  const openFiles = useCallback(
    (files: File[]) => {
      if (!connected || files.length === 0) return;
      void runOpen(files.length === 1 ? files[0].name : null, () => readSpecFile(files));
    },
    [connected, runOpen],
  );

  const chooseFile = useCallback(() => {
    if (connected) fileInput.current?.click();
  }, [connected]);

  const openSampleById = useCallback(
    (id: string) => {
      if (!connected) return;
      const sample = samples?.find((s) => s.id === id);
      void runOpen(sample?.file ?? id, () => openSample(id));
    },
    [connected, samples, runOpen],
  );

  // ---------------------------------------------------------------- ショートカット（1.4、付録C）

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!hasModifier(e) || e.altKey || e.shiftKey) return;
      const key = e.key.toLowerCase();
      if (key === "o") {
        e.preventDefault();
        chooseFile();
      } else if (key === "s") {
        e.preventDefault(); // ブラウザの「ページを保存」を出さない。仕様の保存は A1
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chooseFile]);

  // ---------------------------------------------------------------- ドロップ（ウィンドウのどこでも。4.2）

  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      setDragging(true);
    };
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = connected ? "copy" : "none";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return; // ファイルでないもの（文字列など）は何も起こさない
      e.preventDefault();
      depth = 0;
      setDragging(false);
      openFiles(Array.from(e.dataTransfer?.files ?? []));
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [connected, openFiles]);

  // ---------------------------------------------------------------- 言語とテーマ（10章）

  const changeLang = (next: Lang) => {
    applyLang(next);
    setLang(next);
  };
  const changeTheme = (next: Theme) => {
    applyTheme(next);
    setTheme(next);
  };

  const t = (key: Parameters<typeof translate>[1], params?: Parameters<typeof translate>[2]) =>
    translate(lang, key, params);

  const noticeBar = (
    <NoticeBar
      notices={notices}
      open={noticesOpen}
      onToggle={toggleNotices}
      onDismiss={(id) => {
        // 最後の1件を閉じたら、同じ描画で帯をたたむ（0 件の帯を一瞬でも開いて見せない。9.3）
        const remaining = notices.filter((n) => n.id !== id);
        setNotices(remaining);
        if (remaining.length === 0) setNoticesOpen(false);
      }}
    />
  );

  return (
    <I18nProvider lang={lang}>
      <div className={`app${conn === "disconnected" ? " is-disconnected" : ""}`} data-conn={conn}>
        {conn === "disconnected" && (
          <div className="conn-banner" role="alert" data-testid="conn-banner">
            <span className="state-word state-error">
              <Icon name="error" />
              {t("notice.level.error")}
            </span>
            <span>{t("app.disconnected")}</span>
          </div>
        )}
        <Header
          doc={doc}
          samples={samples}
          connected={connected}
          lang={lang}
          theme={theme}
          onOpen={chooseFile}
          onSample={openSampleById}
          onSave={() => undefined}
          onRun={() => undefined}
          runRef={runRef}
          onLang={changeLang}
          onTheme={changeTheme}
        />
        {conn === "connecting" ? (
          <main className="connecting" data-testid="connecting">
            {slowConnect && <p>{t("app.connecting")}</p>}
          </main>
        ) : conn === "unauthorized" ? (
          <main className="connecting" data-testid="unauthorized">
            <p className="state-word state-error" role="alert">
              <Icon name="error" />
              {t("app.unauthorized")}
            </p>
          </main>
        ) : doc ? (
          <>
            <Frame doc={doc} connected={connected} onRun={() => undefined} />
            {noticeBar}
          </>
        ) : (
          <>
          <StartScreen
            samples={samples}
            samplesFailed={samplesFailed}
            samplesSlow={samplesSlow}
            dragging={dragging}
            connected={connected}
            openError={openError}
            opening={opening}
            onChoose={chooseFile}
            onSample={openSampleById}
            onRetrySamples={() => void loadSamples()}
            chooseRef={chooseRef}
          />
          {/* 起動直後の画面には帯を置かない（1.3）。ただし、仕様を開く前に起きた出来事（好みを保存できなかった）は、
              ここで知らせる */}
          {notices.length > 0 && noticeBar}
          </>
        )}
        {doc && dragging && (
          <div className="drop-overlay" data-testid="drop-overlay">
            <div className="drop-overlay-frame">{t("open.drop.overlay")}</div>
          </div>
        )}
        <input
          ref={fileInput}
          type="file"
          accept=".json,application/json"
          className="visually-hidden"
          tabIndex={-1}
          aria-hidden="true"
          data-testid="file-input"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            openFiles(files);
          }}
        />
        <div className="visually-hidden" aria-live="polite" data-testid="announce">
          {announce}
        </div>
      </div>
    </I18nProvider>
  );
}
