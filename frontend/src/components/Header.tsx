// ヘッダー（画面仕様 1.2）。1行。① アプリ名 ② 開いている仕様 ③ ファイルの操作 ‥余白‥ ④ 実行と状態 ⑤ 言語とテーマ。
import type { Doc, SampleInfo } from "../api";
import { useI18n, type Lang, type MessageKey } from "../i18n";
import type { Theme } from "../prefs";
import { Icon } from "./Icon";
import { MenuButton } from "./Menu";
import { Toggle } from "./Toggle";
import { Tooltip } from "./Tooltip";

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
  onLang: (lang: Lang) => void;
  onTheme: (theme: Theme) => void;
}

/** ファイル名は中ほどを省略し、拡張子を残す（design-system 4.2）。 */
export function middleEllipsis(name: string, max = 40): string {
  if (name.length <= max) return name;
  const dot = name.lastIndexOf(".");
  const tail = dot > 0 && name.length - dot <= 8 ? name.slice(dot - 6) : name.slice(-10);
  return `${name.slice(0, max - tail.length - 1)}…${tail}`;
}

export function Header(props: Props) {
  const { t } = useI18n();
  const { doc, samples, connected } = props;
  const sample = doc?.sample ? samples?.find((s) => s.id === doc.sample) : undefined;
  const templateKey = `template.${doc?.template}.name` as MessageKey;
  return (
    <header className="header" data-testid="header">
      <span className="header-app" data-testid="header-app">
        {t("app.name")}
      </span>
      <span className="header-file" data-testid="header-file">
        {doc ? (
          <>
            <Tooltip text={doc.file}>
              <span className="header-file-name" tabIndex={-1}>
                {middleEllipsis(doc.file)}
              </span>
            </Tooltip>
            <span className="sep" aria-hidden="true" />
            <span className="header-file-template">
              {sample ? t("header.file.sample", { name: { key: sample.name as MessageKey } }) : t(templateKey)}
            </span>
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
