// 部品の試験（数値の入力欄 AC-2-4・AC-2-7、切り替え 6.2、お知らせ 9章、辞書の引き方 10.3）。
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, translate, type Lang } from "../i18n";
import type { Notice } from "../notices";
import { ELLIPSIS, fitMiddle } from "./Header";
import { NumberField } from "./NumberField";
import { NoticeBar } from "./NoticeBar";
import { Toggle } from "./Toggle";
import { Tooltip } from "./Tooltip";

const wrap = (ui: React.ReactElement, lang: Lang = "ja") => render(<I18nProvider lang={lang}>{ui}</I18nProvider>);

function editField(original: number | null, unit = "ohm", onCommit = vi.fn()) {
  wrap(
    <NumberField
      field={{ unit, positive: true }}
      value={original}
      original={original}
      label="R"
      onCommit={onCommit}
      testId="f"
    />,
  );
  fireEvent.click(screen.getByTestId("f"));
  return { input: screen.getByTestId("f") as HTMLInputElement, interp: screen.getByTestId("f-interp"), onCommit };
}

describe("数値の入力欄（2.2）", () => {
  it("1文字打つごとに解釈が変わり、100 ms 以内（AC-2-4）", () => {
    const { input, interp } = editField(1);
    const steps: [string, string][] = [
      ["4", "= 4 Ω"],
      ["47", "= 47 Ω"],
      ["47k", "= 47 kΩ（4.7e4 Ω）"],
      ["47kΩ", "= 47 kΩ（4.7e4 Ω）"],
    ];
    for (const [text, expected] of steps) {
      const started = performance.now();
      fireEvent.change(input, { target: { value: text } });
      expect(interp.textContent).toContain(expected);
      expect(performance.now() - started).toBeLessThan(100);
    }
  });

  it("1 Ω の欄に 1M と打つと、確定する前に 1 MΩ と倍率が注意の記号つきで出る。1m でも出て、2 で消える（AC-2-7）", () => {
    const { input, interp } = editField(1);
    fireEvent.change(input, { target: { value: "1M" } });
    expect(interp.textContent).toContain("= 1 MΩ（1e6 Ω）");
    expect(interp.textContent).toContain("変更前の 1,000,000 倍");
    expect(interp.querySelector(".num-interp-ratio svg")).not.toBeNull();
    fireEvent.change(input, { target: { value: "1m" } });
    expect(interp.textContent).toContain("変更前の 1/1,000");
    fireEvent.change(input, { target: { value: "2" } });
    expect(interp.textContent).not.toContain("変更前");
  });

  it("開いたときに値が無かった項目と、0 だった項目には倍率を出さない", () => {
    const { input, interp } = editField(null);
    fireEvent.change(input, { target: { value: "1M" } });
    expect(interp.textContent).not.toContain("変更前");
  });

  it("読めない入力は確定しない（値は変わらない。AC-2-2）、読める入力は基本単位の値で確定する", () => {
    const { input, interp, onCommit } = editField(1);
    fireEvent.change(input, { target: { value: "4k7" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).not.toHaveBeenCalled();
    expect(interp.textContent).toContain("「4k7」の書き方は使えません。4.7k と入力してください。");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    fireEvent.change(input, { target: { value: "4.7k" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).toHaveBeenCalledWith(4700);
  });

  it.each([
    ["1,000", "コンマは使えません。1000 または 1k のように入力してください。"],
    ["47uF", "単位が違います。この欄の単位は Ω です（入力された単位: F）。"],
    ["47U", "「U」は読めません。"],
    ["abc", "「abc」は数値として読めません。"],
    ["-5", "0 より大きい値が必要です。"],
    ["inf", "有限の値を入力してください。"],
  ])("%s は、それぞれの文言でエラーになる（AC-2-2）", (text, message) => {
    const { input, interp } = editField(1);
    fireEvent.change(input, { target: { value: text } });
    expect(interp.textContent).toContain(message);
  });

  it("Esc で取り消す", () => {
    const { input, onCommit } = editField(1);
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCommit).not.toHaveBeenCalled();
    expect(screen.getByTestId("f").textContent).toBe("1 Ω");
  });
});

describe("切り替え（6.2）", () => {
  it("左右の矢印キーで選択肢を移る。role=radiogroup", () => {
    const onChange = vi.fn();
    wrap(
      <Toggle
        label="テーマ"
        value="light"
        options={[
          { value: "light", label: "明" },
          { value: "dark", label: "暗" },
        ]}
        onChange={onChange}
      />,
    );
    const group = screen.getByRole("radiogroup");
    expect(group.getAttribute("aria-label")).toBe("テーマ");
    const light = screen.getByRole("radio", { name: "明" });
    expect(light.getAttribute("aria-checked")).toBe("true");
    expect(light.tabIndex).toBe(0);
    expect(screen.getByRole("radio", { name: "暗" }).tabIndex).toBe(-1);
    fireEvent.keyDown(light, { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("dark");
  });
});

describe("お知らせ（9章）", () => {
  const notices: Notice[] = [
    { id: 1, code: "N-CONN-RESTART", params: {}, time: "14:02:11" },
    { id: 2, code: "N-OPEN-SYNTAX", params: { file: "a.json", line: 3, col: 5 }, details: "Expecting value", time: "14:03:00" },
  ];

  it("空のとき、件数（全部 0）と「お知らせはありません」（9.3）", () => {
    wrap(<NoticeBar notices={[]} open={false} onToggle={() => undefined} onDismiss={() => undefined} />);
    expect(screen.getByTestId("notices-counts").textContent).toBe("エラー 0不足 0注意 0");
    expect(screen.getByTestId("notices-latest").textContent).toBe("お知らせはありません");
  });

  it("たたんだ状態でも、段階ごとの件数が記号と言葉と数で見え、いちばん重い最新の1件が出る（AC-9-5）", () => {
    wrap(<NoticeBar notices={notices} open={false} onToggle={() => undefined} onDismiss={() => undefined} />);
    const counts = screen.getByTestId("notices-counts");
    expect(counts.textContent).toBe("エラー 1不足 0注意 1");
    expect(counts.querySelectorAll("svg").length).toBe(3);
    expect(screen.getByTestId("notices-latest").textContent).toBe(
      "a.json を JSON として読めません（3 行目、5 文字目）。",
    );
  });

  it("開くと エラー → 注意 の順に並び、内部の文は「詳細」の中だけ（AC-9-6）。矢印と Delete で操作できる（AC-9-7）", () => {
    const onDismiss = vi.fn();
    wrap(<NoticeBar notices={notices} open onToggle={() => undefined} onDismiss={onDismiss} />);
    const rows = screen.getAllByTestId("notice-row");
    expect(rows.map((r) => r.dataset.code)).toEqual(["N-OPEN-SYNTAX", "N-CONN-RESTART"]);
    expect(rows[0].textContent).not.toContain("Expecting value");
    fireEvent.click(screen.getByRole("button", { name: "詳細" }));
    expect(rows[0].textContent).toContain("Expecting value");
    act(() => rows[0].focus());
    fireEvent.keyDown(rows[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[1]);
    fireEvent.keyDown(rows[1], { key: "Delete" });
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("言語を切り替えると、出ているお知らせの文も切り替わる（AC-10-8）", () => {
    const { rerender } = wrap(
      <NoticeBar notices={notices} open onToggle={() => undefined} onDismiss={() => undefined} />,
    );
    rerender(
      <I18nProvider lang="en">
        <NoticeBar notices={notices} open onToggle={() => undefined} onDismiss={() => undefined} />
      </I18nProvider>,
    );
    expect(screen.getAllByTestId("notice-row")[0].textContent).toContain("a.json isn't valid JSON (line 3, column 5).");
  });
});

describe("お知らせを閉じた後のフォーカス（AC-1-5）", () => {
  function Bar({ initial }: { initial: Notice[] }) {
    const [list, setList] = useState(initial);
    return (
      <NoticeBar
        notices={list}
        open
        onToggle={() => undefined}
        onDismiss={(id) => setList((l) => l.filter((n) => n.id !== id))}
      />
    );
  }
  const three: Notice[] = [1, 2, 3].map((id) => ({
    id,
    code: "N-OPEN-SYNTAX",
    params: { file: `${id}.json`, line: 1, col: 1 },
    time: "10:00:00",
  }));
  const frame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

  it("次の行、無ければ前の行、1件も残らなければ帯の見出しに置く（body に落とさない）", async () => {
    wrap(<Bar initial={three} />);
    // 並びは新しい順: 3, 2, 1
    let rows = screen.getAllByTestId("notice-row");
    act(() => rows[0].focus());
    fireEvent.keyDown(rows[0], { key: "Delete" });
    await frame();
    rows = screen.getAllByTestId("notice-row");
    expect(document.activeElement).toBe(rows[0]);
    expect(rows[0].textContent).toContain("2.json");
    act(() => rows[1].focus());
    fireEvent.keyDown(rows[1], { key: "Delete" });
    await frame();
    rows = screen.getAllByTestId("notice-row");
    expect(document.activeElement).toBe(rows[0]);
    fireEvent.click(screen.getByTestId("notice-dismiss"));
    await frame();
    expect(screen.queryAllByTestId("notice-row")).toHaveLength(0);
    expect(document.activeElement).toBe(screen.getByTestId("notices-toggle"));
  });
});

describe("ファイル名の中ほどの省略（AC-1-1、design-system 4.2）", () => {
  // 1 文字 = 幅 1 として、幅 budget に収まるか
  const within = (budget: number) => (text: string) => [...text].length <= budget;

  it("収まるときは省略しない", () => {
    expect(fitMiddle("rlc_series.json", within(40))).toBe("rlc_series.json");
  });
  it("収まらないときは中ほどを省略し、拡張子を必ず残す。前後を同じくらい残す", () => {
    const name = "buck_converter_rev12_final.json";
    const fitted = fitMiddle(name, within(20));
    expect([...fitted].length).toBe(20);
    expect(fitted.endsWith(".json")).toBe(true);
    expect(fitted).toContain(ELLIPSIS);
    expect(fitted.startsWith("buck_co")).toBe(true);
    expect(fitted).toBe("buck_co" + ELLIPSIS + "2_final.json");
  });
  it("とても狭くても「…」と拡張子は残す。日本語の名前も文字の途中で切らない", () => {
    expect(fitMiddle("buck_converter_rev12_final.json", within(3))).toBe(`${ELLIPSIS}.json`);
    const jp = "降圧コンバータ_試作3回目_最終確認.json";
    const fitted = fitMiddle(jp, within(12));
    expect(fitted).toBe("降圧コ" + ELLIPSIS + "終確認.json");
  });
  it("拡張子の無い名前（長すぎる「拡張子」は拡張子とみなさない）", () => {
    expect(fitMiddle("abcdefghijklmnopqrstuvwxyz", within(9))).toBe("abcd" + ELLIPSIS + "wxyz");
    expect(fitMiddle("a.verylongsuffix", within(8))).toBe("a.ve" + ELLIPSIS + "fix");
  });
});

describe("辞書（10.3）", () => {
  it("差し込む値と、辞書のキーを差し込んだ値を訳す", () => {
    expect(translate("ja", "notice.count", { level: { key: "notice.level.error" }, n: 2 })).toBe("エラー 2");
    expect(translate("en", "notice.count", { level: { key: "notice.level.error" }, n: 2 })).toBe("Error 2");
  });
  it("{mod} は OS の修飾キー", () => {
    expect(translate("en", "header.open.tip")).toMatch(/^Open a spec JSON \((Ctrl|⌘)\+O\)$/);
  });
});

describe("ツールチップ（design-system 6.7、A0 の UX 確認の改善2）", () => {
  function tip() {
    render(
      <Tooltip text="tip">
        <button type="button">b</button>
      </Tooltip>,
    );
    return { button: screen.getByRole("button"), tooltip: screen.getByRole("tooltip", { hidden: true }) };
  }

  it.each([
    ["Enter", "Enter"],
    ["スペース", " "],
  ])("キーボードのフォーカスで出て、%s で押すと消え、フォーカスが残っていても出し直さない", (_name, key) => {
    vi.useFakeTimers();
    try {
      const { button, tooltip } = tip();
      act(() => button.focus());
      expect(tooltip.hidden).toBe(false);
      fireEvent.keyDown(button, { key });
      expect(tooltip.hidden).toBe(true);
      expect(document.activeElement).toBe(button);
      fireEvent.pointerEnter(button);
      act(() => void vi.advanceTimersByTime(1000));
      expect(tooltip.hidden).toBe(true);
      // 次にフォーカスが来たら、また出る
      act(() => button.blur());
      act(() => button.focus());
      expect(tooltip.hidden).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ポインタで押すと消え、離れて乗り直すまで出し直さない", () => {
    vi.useFakeTimers();
    try {
      const { button, tooltip } = tip();
      fireEvent.pointerEnter(button);
      act(() => void vi.advanceTimersByTime(600));
      expect(tooltip.hidden).toBe(false);
      fireEvent.pointerDown(button);
      expect(tooltip.hidden).toBe(true);
      fireEvent.pointerEnter(button);
      act(() => void vi.advanceTimersByTime(600));
      expect(tooltip.hidden).toBe(true);
      fireEvent.pointerLeave(button);
      fireEvent.pointerEnter(button);
      act(() => void vi.advanceTimersByTime(600));
      expect(tooltip.hidden).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ほかのキー（Tab、矢印）では消えない", () => {
    const { button, tooltip } = tip();
    act(() => button.focus());
    fireEvent.keyDown(button, { key: "ArrowDown" });
    expect(tooltip.hidden).toBe(false);
  });
});
