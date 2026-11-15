// 全体の枠の、仕様を開いているときの配置（画面仕様 1.2）。左に仕様パネル、中央の上に回路図、下にスコープ。
// 仕切りの位置と、たたんだ状態はブラウザに覚えさせる（AC-1-6）。各領域の中身は A1 で入る。
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Doc } from "../api";
import { useI18n, type MessageKey } from "../i18n";
import { readFlag, readNumber, writePref } from "../prefs";
import { Icon } from "./Icon";
import { Splitter } from "./Splitter";
import { Tooltip } from "./Tooltip";

export const SPEC_WIDTH = { initial: 360, min: 300, max: 560 };
export const SCHEMATIC_HEIGHT = { initial: 180, min: 140, maxRatio: 0.5 };
export const SCOPE_MIN_HEIGHT = 280;

interface Props {
  doc: Doc;
  connected: boolean;
  onRun: () => void;
}

function CollapseButton({ collapsed, onClick, dir }: { collapsed: boolean; onClick: () => void; dir: "x" | "y" }) {
  const { t } = useI18n();
  const label = collapsed ? t("layout.expand") : t("layout.collapse");
  const icon = dir === "x" ? (collapsed ? "chevronRight" : "chevronLeft") : collapsed ? "chevronDown" : "chevronUp";
  return (
    <Tooltip text={label}>
      <button
        type="button"
        className="btn btn-tool btn-icon"
        aria-label={label}
        aria-expanded={!collapsed}
        onClick={onClick}
      >
        <Icon name={icon} />
      </button>
    </Tooltip>
  );
}

export function Frame({ doc, connected, onRun }: Props) {
  const { t } = useI18n();
  const [specWidth, setSpecWidth] = useState(() => readNumber("layout.specWidth", SPEC_WIDTH.initial));
  const [schemHeight, setSchemHeight] = useState(() => readNumber("layout.schemHeight", SCHEMATIC_HEIGHT.initial));
  const [specCollapsed, setSpecCollapsed] = useState(() => readFlag("layout.specCollapsed"));
  const [schemCollapsed, setSchemCollapsed] = useState(() => readFlag("layout.schemCollapsed"));
  const center = useRef<HTMLDivElement>(null);
  const [centerHeight, setCenterHeight] = useState(0);

  useLayoutEffect(() => {
    const el = center.current;
    if (!el) return;
    const measure = () => setCenterHeight(el.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => writePref("layout.specWidth", String(specWidth)), [specWidth]);
  useEffect(() => writePref("layout.schemHeight", String(schemHeight)), [schemHeight]);
  useEffect(() => writePref("layout.specCollapsed", specCollapsed ? "1" : "0"), [specCollapsed]);
  useEffect(() => writePref("layout.schemCollapsed", schemCollapsed ? "1" : "0"), [schemCollapsed]);

  // 回路図の高さの上限は、中央の列の 50%。スコープの最小の高さ 280 も守る
  const schemMax = Math.max(
    SCHEMATIC_HEIGHT.min,
    Math.min(centerHeight * SCHEMATIC_HEIGHT.maxRatio, centerHeight - SCOPE_MIN_HEIGHT - 1),
  );
  const width = Math.min(SPEC_WIDTH.max, Math.max(SPEC_WIDTH.min, specWidth));
  const height = centerHeight ? Math.min(schemMax, Math.max(SCHEMATIC_HEIGHT.min, schemHeight)) : schemHeight;
  const templateName = t(`template.${doc.template}.name` as MessageKey);

  return (
    <main className="frame" data-testid="frame">
      {specCollapsed ? (
        <aside className="spec-rail" data-testid="spec-panel" data-collapsed="true">
          <CollapseButton collapsed dir="x" onClick={() => setSpecCollapsed(false)} />
          <span className="spec-rail-title">{t("spec.title")}</span>
        </aside>
      ) : (
        <aside className="spec-panel" style={{ width }} data-testid="spec-panel" data-collapsed="false">
          <div className="section-head">
            <h2 className="section-title">{t("spec.title")}</h2>
            <CollapseButton collapsed={false} dir="x" onClick={() => setSpecCollapsed(true)} />
          </div>
          <div className="spec-template">
            <span className="spec-template-label">{t("spec.template")}</span>
            <span className="spec-template-name">{templateName}</span>
            <code className="spec-template-id">{doc.template}</code>
          </div>
        </aside>
      )}
      {!specCollapsed && (
        <Splitter
          orientation="vertical"
          label={t("layout.resize")}
          value={width}
          min={SPEC_WIDTH.min}
          max={SPEC_WIDTH.max}
          onChange={setSpecWidth}
          onReset={() => setSpecWidth(SPEC_WIDTH.initial)}
          testId="splitter-spec"
        />
      )}
      <div className="center" ref={center}>
        <section
          className={`schematic${schemCollapsed ? " is-collapsed" : ""}`}
          style={schemCollapsed ? undefined : { height }}
          aria-label={t("schem.title")}
          data-testid="schematic"
          data-collapsed={schemCollapsed}
        >
          <span className="corner-title">{t("schem.title")}</span>
          <span className="corner-action">
            <CollapseButton collapsed={schemCollapsed} dir="y" onClick={() => setSchemCollapsed((v) => !v)} />
          </span>
        </section>
        {!schemCollapsed && (
          <Splitter
            orientation="horizontal"
            label={t("layout.resize.height")}
            value={height}
            min={SCHEMATIC_HEIGHT.min}
            max={schemMax}
            onChange={setSchemHeight}
            onReset={() => setSchemHeight(SCHEMATIC_HEIGHT.initial)}
            testId="splitter-schematic"
          />
        )}
        <section className="scope" aria-label={t("scope.title")} data-testid="scope">
          <div className="scope-empty" data-testid="scope-empty">
            <p className="scope-empty-title">{t("scope.empty.title")}</p>
            <p className="scope-empty-hint">{t("scope.empty.hint")}</p>
            <button
              type="button"
              className="btn btn-secondary"
              aria-disabled={!connected || undefined}
              onClick={() => connected && onRun()}
            >
              <Icon name="run" />
              <span>{t("run.button")}</span>
            </button>
          </div>
        </section>
      </div>
    </main>
  );
}
