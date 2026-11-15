// お知らせ1件の文（何が起きたか ＋ どう直すか ＋ 箇所の一覧）。起動直後の画面のエラーと、お知らせの行が使う。
import { useI18n } from "../i18n";
import { entry, fixKey, whatKey, type NoticeData } from "../notices";
import { Icon } from "./Icon";

const LEVEL_ICON = { error: "error", missing: "missing", warning: "warning" } as const;

export function LevelWord({ notice }: { notice: NoticeData }) {
  const { t } = useI18n();
  const level = entry(notice.code).level;
  return (
    <span className={`state-word state-${level}`}>
      <Icon name={LEVEL_ICON[level]} />
      {t(`notice.level.${level}`)}
    </span>
  );
}

export function NoticeText({ notice, withLevel = true }: { notice: NoticeData; withLevel?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="notice-text" data-code={notice.code}>
      {withLevel && <LevelWord notice={notice} />}
      <span className="notice-body">
        <span className="notice-what">{t(whatKey(notice), notice.params)}</span>{" "}
        <span className="notice-fix">{t(fixKey(notice), notice.fixParams ?? notice.params)}</span>
        {notice.items && notice.items.length > 0 && (
          <ul className="notice-items">
            {notice.items.map((item, i) => (
              <li key={i}>
                <code>{item.path}</code>: {t(item.problem)}
              </li>
            ))}
            {notice.more ? <li>{t("schema.more", { n: notice.more })}</li> : null}
          </ul>
        )}
      </span>
    </div>
  );
}
