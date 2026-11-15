// 起動直後の画面（画面仕様 3章）。仕様を開いていないときだけ出る。幅 640 の列。
import { useEffect, useRef, useState } from "react";
import type { SampleInfo } from "../api";
import { useI18n, type MessageKey } from "../i18n";
import type { NoticeData } from "../notices";
import { Icon } from "./Icon";
import { NoticeText } from "./NoticeText";

interface Props {
  samples: SampleInfo[] | null;
  samplesFailed: boolean;
  samplesSlow: boolean;
  dragging: boolean;
  connected: boolean;
  openError: { notice: NoticeData; file?: string } | null;
  opening: string | null;
  onChoose: () => void;
  onSample: (id: string) => void;
  onRetrySamples: () => void;
  chooseRef: React.RefObject<HTMLButtonElement | null>;
}

export function StartScreen(props: Props) {
  const { t } = useI18n();
  const rows = useRef<(HTMLButtonElement | null)[]>([]);
  const { samples, samplesFailed, samplesSlow, connected } = props;
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    // 最初のフォーカスは「ファイルを選ぶ…」（3.2）
    if (!focused && connected) {
      props.chooseRef.current?.focus();
      setFocused(true);
    }
  }, [connected, focused, props.chooseRef]);

  const move = (index: number) => {
    const n = samples?.length ?? 0;
    if (n) rows.current[(index + n) % n]?.focus();
  };

  return (
    <main className="start" data-testid="start">
      <div className="start-column">
        <h1 className="start-title">{t("start.open.title")}</h1>
        <div className={`drop${props.dragging ? " is-active" : ""}`} data-testid="drop-zone">
          {props.opening ? (
            <span className="drop-text" data-testid="opening">
              {t("open.loading", { file: props.opening })}
            </span>
          ) : (
            <>
              <span className="drop-text">{props.dragging ? t("start.drop.active") : t("start.drop")}</span>
              <span className="drop-or">
                <span>{t("start.or")}</span>
                <button
                  ref={props.chooseRef}
                  type="button"
                  className="btn btn-secondary"
                  aria-disabled={!connected || undefined}
                  onClick={() => connected && props.onChoose()}
                  data-testid="choose-file"
                >
                  {t("start.choose")}
                </button>
              </span>
            </>
          )}
        </div>
        <p className="start-privacy">{t("start.privacy")}</p>
        {props.openError && (
          <div className="start-error" role="alert" data-testid="open-error">
            {props.openError.file && (
              <p className="start-error-title">{t("open.error.title", { file: props.openError.file })}</p>
            )}
            <NoticeText notice={props.openError.notice} />
          </div>
        )}

        <h2 className="start-title start-title-samples">{t("start.samples.title")}</h2>
        <div className="samples" data-testid="samples">
          {samples ? (
            <div className="sample-list" role="list">
              {samples.map((s, index) => (
                <div role="listitem" key={s.id}>
                  <button
                    ref={(el) => {
                      rows.current[index] = el;
                    }}
                    type="button"
                    className="sample-row"
                    aria-disabled={!connected || undefined}
                    data-testid={`sample-${s.id}`}
                    onClick={() => connected && props.onSample(s.id)}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowDown") {
                        e.preventDefault();
                        move(index + 1);
                      } else if (e.key === "ArrowUp") {
                        e.preventDefault();
                        move(index - 1);
                      }
                    }}
                  >
                    <span className="sample-name">{t(s.name as MessageKey)}</span>
                    <span className="sample-desc">{t(s.desc as MessageKey)}</span>
                  </button>
                </div>
              ))}
            </div>
          ) : samplesFailed ? (
            <div className="samples-error" role="alert" data-testid="samples-error">
              <span className="state-word state-error">
                <Icon name="error" />
                {t("start.samples.error")}
              </span>
              <button type="button" className="btn btn-secondary" onClick={props.onRetrySamples}>
                {t("common.retry")}
              </button>
            </div>
          ) : samplesSlow ? (
            <p className="samples-loading" data-testid="samples-loading">
              {t("start.samples.loading")}
            </p>
          ) : null}
        </div>
      </div>
    </main>
  );
}
