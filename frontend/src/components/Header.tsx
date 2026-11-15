// ヘッダー（画面仕様 1.2）。1行。① アプリ名 ② 開いている仕様 ③ ファイルの操作 ‥余白‥ ④ 実行と状態 ⑤ 言語とテーマ。
import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import type { Doc, SampleInfo } from "../api";
import { useI18n, type Lang, type MessageKey } from "../i18n";
import type { Theme } from "../prefs";
import { Icon } from "./Icon";
import { MenuButton } from "./Menu";
import { Toggle } from "./Toggle";
import { Tooltip } from "./Tooltip";

export const ELLIPSIS = "…";
/** 測った幅と描いた幅の差（字形の丸め）を見込む余白 */
const FIT_SLACK_PX = 2;

interface Props {
  doc: Doc | null;
  samples: SampleInfo[] | null;
  connected: boolean;
  lang: Lang;
  theme: Theme;
  onOpen: () => void;
  onSample: (id: string) => void;
  onSave: () => void;
  onRun: () => void;
  /** 開けた後にフォーカスを置く「実行」（3.4） */
  runRef?: RefObject<HTMLButtonElement | null>;
  onLang: (lang: Lang) => void;
  onTheme: (theme: Theme) => void;
}

/**
 * ファイル名の中ほどを省略して、fits（その文字列が収まるか）を満たす最も長い形にする（design-system 4.2）。
 * 拡張子（最後の . から。8 文字まで）は必ず残す。前と後ろの文字は同じくらい残す（前を 1 文字多く）。
 * 何も残せない幅でも「…」と拡張子は出す（拡張子を欠かさない）。
 */
export function fitMiddle(name: string, fits: (text: string) => boolean): string {
  if (fits(name)) return name;
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 && name.length - dot <= 8 ? name.slice(dot) : "";
  const stem = [...name.slice(0, name.length - ext.length)]; // サロゲートペアを分けない
  const candidate = (keep: number) => {
    const head = Math.ceil(keep / 2);
    const tail = keep - head;
    return stem.slice(0, head).join("") + ELLIPSIS + (tail ? stem.slice(stem.length - tail).join("") : "") + ext;
  };
  let lo = 0;
  let hi = stem.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (fits(candidate(mid))) lo = mid;
    else hi = mid - 1;
  }
  return candidate(lo);
}

/**
 * ②の名前を、②の最大幅（CSS の max-width）から区切りと表示名の幅を引いた残りに、幅で収める。
 * 幅は、名前と同じ書式の見えない部品（probe）に書いて測る。測れない環境（試験の jsdom）では省略しない。
 */
function useFittedName(
  name: string | null,
  box: RefObject<HTMLElement | null>,
  probe: RefObject<HTMLElement | null>,
  others: string,
): string | null {
  const [shown, setShown] = useState(name);
  useLayoutEffect(() => {
    const fit = () => {
      const el = box.current;
      const measure = probe.current;
      if (name === null || !el || !measure) {
        setShown(name);
        return;
      }
      const max = Number.parseFloat(getComputedStyle(el).maxWidth);
      let used = 0;
      for (const child of Array.from(el.children)) {
        if (child !== measure && !child.contains(measure) && !child.classList.contains("header-file-name-box")) {
          used += child.getBoundingClientRect().width;
        }
      }
      const budget = max - used - FIT_SLACK_PX;
      const width = (text: string) => {
        measure.textContent = text;
        return measure.getBoundingClientRect().width;
      };
      if (!Number.isFinite(budget) || width(name) === 0) {
        measure.textContent = "";
        setShown(name);
        return;
      }
      const fitted = fitMiddle(name, (text) => width(text) <= budget);
      measure.textContent = "";
      setShown(fitted);
    };
    fit();
    // 書体の読み込みが後から終わると幅が変わる
    let alive = true;
    void document.fonts?.ready.then(() => alive && fit());
    return () => {
      alive = false;
    };
  }, [name, box, probe, others]);
  return shown;
}

export function Header(props: Props) {
  const { t } = useI18n();
  const { doc, samples, connected } = props;
  const sample = doc?.sample ? samples?.find((s) => s.id === doc.sample) : undefined;
  const templateKey = `template.${doc?.template}.name` as MessageKey;
  const templateText = doc
    ? sample
      ? t("header.file.sample", { name: { key: sample.name as MessageKey } })
      : t(templateKey)
    : "";
  const fileBox = useRef<HTMLSpanElement>(null);
  const probe = useRef<HTMLSpanElement>(null);
  const shownName = useFittedName(doc?.file ?? null, fileBox, probe, templateText);
  return (
    <header className="header" data-testid="header">
      <span className="header-app" data-testid="header-app">
        {t("app.name")}
      </span>
      <span className="header-file" data-testid="header-file" ref={fileBox}>
        {doc ? (
          <>
            <span className="header-file-name-box">
              <Tooltip text={doc.file}>
                <span className="header-file-name" tabIndex={-1} data-testid="header-file-name">
                  {shownName ?? doc.file}
                </span>
              </Tooltip>
            </span>
            <span className="sep" aria-hidden="true" />
            <span className="header-file-template" data-testid="header-file-template">
              {templateText}
            </span>
            <span className="header-file-name header-file-probe" aria-hidden="true" ref={probe} />
          </>
        ) : (
          <span className="header-file-none">{t("header.file.none")}</span>
        )}
      </span>
      <span className="header-group" data-testid="header-files">
        <Tooltip text={t("header.open.tip")}>
          <button
            type="button"
            className="btn btn-secondary"
            aria-disabled={!connected || undefined}
            onClick={() => connected && props.onOpen()}
            data-testid="header-open"
          >
            <Icon name="open" />
            <span>{t("header.open")}</span>
          </button>
        </Tooltip>
        <MenuButton
          label={t("header.samples")}
          items={(samples ?? []).map((s) => ({ id: s.id, label: t(s.name as MessageKey) }))}
          disabled={!connected || !samples || samples.length === 0}
          onSelect={props.onSample}
          testId="header-samples"
        />
        <Tooltip text={t("header.save.tip")}>
          <button
            type="button"
            className="btn btn-secondary"
            aria-disabled={!connected || !doc || undefined}
            onClick={() => connected && doc && props.onSave()}
            data-testid="header-save"
          >
            <Icon name="save" />
            <span>{t("header.save")}</span>
          </button>
        </Tooltip>
      </span>
      <span className="header-spacer" />
      <span className="header-group header-run" data-testid="header-run">
        <Tooltip text={t("run.button.tip")}>
          <button
            ref={props.runRef}
            type="button"
            className="btn btn-primary btn-run"
            aria-disabled={!connected || !doc || undefined}
            onClick={() => connected && doc && props.onRun()}
            data-testid="run-button"
          >
            <Icon name="run" />
            <span>{t("run.button")}</span>
          </button>
        </Tooltip>
        <span className="run-status" data-testid="run-status">
          {doc ? t("run.status.never") : t("run.reason.nospec")}
        </span>
      </span>
      <span className="header-group" data-testid="header-prefs">
        <Toggle
          label={t("lang.label")}
          value={props.lang}
          options={[
            { value: "ja", label: t("lang.ja"), lang: "ja" },
            { value: "en", label: t("lang.en"), lang: "en" },
          ]}
          onChange={props.onLang}
          testId="toggle-lang"
        />
        <Toggle
          label={t("theme.label")}
          value={props.theme}
          options={[
            { value: "light", label: t("theme.light") },
            { value: "dark", label: t("theme.dark") },
          ]}
          onChange={props.onTheme}
          testId="toggle-theme"
        />
      </span>
    </header>
  );
}
