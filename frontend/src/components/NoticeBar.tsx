// お知らせの帯（画面仕様 9章、design-system 6.6）。画面の下端に幅いっぱい。たたむと高さ 32。
// 見出しには段階ごとの件数（記号＋言葉＋数。0 件も出す）。たたんだ状態では、いちばん重い段階の最新の1件を1行で出す。
import { useRef, useState } from "react";
import { useI18n } from "../i18n";
import { entry, fixKey, LEVELS, sortNotices, whatKey, type Notice } from "../notices";
import { Icon } from "./Icon";
import { LevelWord } from "./NoticeText";
import { Tooltip } from "./Tooltip";

const LEVEL_ICON = { error: "error", missing: "missing", warning: "warning" } as const;

interface Props {
  notices: Notice[];
  open: boolean;
  onToggle: () => void;
  onDismiss: (id: number) => void;
}

export function NoticeBar({ notices, open, onToggle, onDismiss }: Props) {
  const { t } = useI18n();
  const sorted = sortNotices(notices);
  const rows = useRef<(HTMLDivElement | null)[]>([]);
  const toggle = useRef<HTMLButtonElement>(null);
  const [details, setDetails] = useState<Set<number>>(new Set());
  const counts = Object.fromEntries(LEVELS.map((l) => [l, notices.filter((n) => entry(n.code).level === l).length]));
  const top = sorted[0];

  const focusRow = (index: number) => {
    const n = sorted.length;
    if (n) rows.current[Math.max(0, Math.min(n - 1, index))]?.focus();
  };

  // 閉じた後のフォーカス: 次の行、無ければ前の行、1件も残らなければ帯の見出し（AC-1-5。body に落とさない）
  const dismiss = (id: number, index: number) => {
    const remaining = sorted.length - 1;
    onDismiss(id);
    requestAnimationFrame(() => {
      if (remaining > 0) rows.current[Math.min(index, remaining - 1)]?.focus();
      else toggle.current?.focus();
    });
  };

  return (
    <section className={`notices${open ? " is-open" : ""}`} aria-label={t("notice.title")} data-testid="notices">
      <div className="notices-head">
        <button
          ref={toggle}
          type="button"
          className="notices-toggle"
          aria-expanded={open}
          onClick={onToggle}
          data-testid="notices-toggle"
        >
          <span className="notices-title">{t("notice.title")}</span>
          <span className="notices-counts" data-testid="notices-counts">
            {LEVELS.map((level, i) => (
              <span key={level} className={`notices-count${counts[level] ? "" : " is-zero"}`}>
                {i > 0 && <span className="sep" aria-hidden="true" />}
                <span className={counts[level] ? `state-${level}` : undefined}>
                  <Icon name={LEVEL_ICON[level]} />
                </span>
                {t("notice.count", { level: { key: `notice.level.${level}` }, n: counts[level] })}
              </span>
            ))}
          </span>
          {!open && (
            <span className="notices-latest" data-testid="notices-latest">
              {top ? t(whatKey(top), top.params) : t("notice.empty")}
            </span>
          )}
        </button>
        <button
          type="button"
          className="btn btn-tool btn-icon"
          aria-label={open ? t("layout.collapse") : t("layout.expand")}
          aria-expanded={open}
          tabIndex={-1}
          onClick={onToggle}
        >
          <Icon name={open ? "chevronDown" : "chevronUp"} />
        </button>
      </div>
      {open && (
        <div className="notices-list" role="list" data-testid="notices-list">
          {sorted.length === 0 && <p className="notices-empty">{t("notice.empty")}</p>}
          {sorted.map((n, index) => {
            const e = entry(n.code);
            const shown = details.has(n.id);
            return (
              <div
                key={n.id}
                ref={(el) => {
                  rows.current[index] = el;
                }}
                role="listitem"
                className="notice-row"
                tabIndex={index === 0 ? 0 : -1}
                data-code={n.code}
                data-testid="notice-row"
                onKeyDown={(ev) => {
                  if (ev.target !== ev.currentTarget) return;
                  if (ev.key === "ArrowDown") {
                    ev.preventDefault();
                    focusRow(index + 1);
                  } else if (ev.key === "ArrowUp") {
                    ev.preventDefault();
                    focusRow(index - 1);
                  } else if (ev.key === "Delete" && e.dismissible) {
                    ev.preventDefault();
                    dismiss(n.id, index);
                  }
                }}
              >
                <span className="notice-level">
                  <LevelWord notice={n} />
                </span>
                <span className="notice-main">
                  <span className="notice-what">{t(whatKey(n), n.params)}</span>{" "}
                  <span className="notice-fix">{t(fixKey(n), n.fixParams ?? n.params)}</span>
                  {n.items && n.items.length > 0 && (
                    <ul className="notice-items">
                      {n.items.map((item, i) => (
                        <li key={i}>
                          <code>{item.path}</code>: {t(item.problem)}
                        </li>
                      ))}
                      {n.more ? <li>{t("schema.more", { n: n.more })}</li> : null}
                    </ul>
                  )}
                  {shown && n.details && (
                    <span className="notice-details">
                      <pre>{n.details}</pre>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => void navigator.clipboard?.writeText(n.details ?? "")}
                      >
                        {t("notice.details.copy")}
                      </button>
                    </span>
                  )}
                </span>
                <span className="notice-actions">
                  {n.details && (
                    <button
                      type="button"
                      className="link"
                      aria-expanded={shown}
                      onClick={() =>
                        setDetails((prev) => {
                          const next = new Set(prev);
                          if (next.has(n.id)) next.delete(n.id);
                          else next.add(n.id);
                          return next;
                        })
                      }
                    >
                      {t("notice.details")}
                    </button>
                  )}
                  {n.time && <span className="notice-time">{n.time}</span>}
                  {e.dismissible && (
                    <Tooltip text={t("notice.dismiss")}>
                      <button
                        type="button"
                        className="btn btn-tool btn-icon"
                        aria-label={t("notice.dismiss")}
                        onClick={() => dismiss(n.id, index)}
                        data-testid="notice-dismiss"
                      >
                        <Icon name="close" />
                      </button>
                    </Tooltip>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
