"""画面仕様（docs/design/screens-v0.1.md）の文言の表から、日英の辞書を作る・照らし合わせる。

画面の文言の出どころは画面仕様の表だけにする（表に無い文字列を画面に出さない。0.2）。
  python frontend/tools/strings_from_spec.py --write   表から辞書（src/i18n/ の ja.json、en.json）を作る
  python frontend/tools/strings_from_spec.py           表と辞書が食い違っていないかを確かめる（0 = 一致）

読む表は2種類。
  1. 見出しが「キー | 日本語 | English」の表。1行が1つのキー。
  2. 付録Bのお知らせの表（「番号」「何が起きたか（日 / 英）」「どう直すか（日 / 英）」の列）。
     キーは notice.<番号>.what と notice.<番号>.fix。日と英は「 / 」で分ける。
表の中の `…`（コードの書式）は、記号を外して文字だけにする。
数値と単位の間の空白は、改行されない空白（U+00A0）にする（design-system 4.2、AC-2-6。表には見えない文字を
書かせない）。
画面仕様の付録Aの表示名など、表の外にある文言は EXTRA に持つ（出どころを書いておく）。
標準ライブラリだけで動く。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "design" / "screens-v0.1.md"
DICT_DIR = ROOT / "frontend" / "src" / "i18n"

# 表の外にある文言（出どころ）。
EXTRA = {
    # 付録A.2、A.3 の「表示名」（ヘッダーの②に出すテンプレートの表示名。1.2）
    "template.rc_lowpass.name": ("RCローパス", "RC low-pass"),
    "template.rlc_series.name": ("直列RLC", "Series RLC"),
    # A0 の直し（D-051）: 合言葉のクッキーが無いページ（アプリ本体が 401 を返す）の案内。
    # 画面仕様の 1.3 の表への追記は ux-designer に依頼した（入ったら、ここから外す）
    "app.unauthorized": (
        "このページからはアプリ本体を使えません。"
        "circuit-sim を起動したターミナルに出ている URL から開き直してください。",
        "This page can't use the app. Open it again from the URL shown in the terminal where circuit-sim "
        "is running.",
    ),
    # A0 の直し（レビュー2回目の軽3）: 画面の好みを設定ファイルに書けなかった（PUT /api/settings が失敗）。
    # 付録B への追記は ux-designer に確認を頼む（入ったら、ここから外す）
    "notice.N-PREFS-SAVE.what": (
        "画面の好み（言語、テーマ、仕切りの位置）を、設定ファイルに保存できませんでした。"
        "この画面ではそのまま使えますが、次に起動したときは戻りません。",
        "Couldn't save your display preferences (language, theme, panel sizes) to the settings file. "
        "They still apply here, but won't come back the next time the app starts.",
    ),
    "notice.N-PREFS-SAVE.fix": (
        "設定の場所（{path}）に書き込めるか（フォルダの権限、空き容量）を確かめてください。"
        "直った後に好みを変えると、保存し直します。",
        "Check that the settings folder ({path}) can be written to (permissions, free space). "
        "Once it's fixed, change a preference to save them again.",
    ),
}

# 数値の後ろの空白で、その後ろが単位（接頭辞を含む）のもの。単位の後ろに英字が続くもの（8 signals）は除く
UNIT_SPACE = re.compile(r"(?<=\d) (?=[fpnµμumkMG]?(?:Hz|V|A|H|F|s|Ω|B|W)(?![A-Za-z]))")
NBSP = "\u00a0"


def keep_with_unit(text: str) -> str:
    """数値と単位の間の空白を、改行されない空白にする（47 µH、1.59 kHz、10 MB、0.12 s）。"""
    return UNIT_SPACE.sub(NBSP, text)

# 付録Bで、どう直すかが別の文言を指しているもの。
#   N-INVALID: 2.3 の該当する文言（num.err.*）か spec.sim.err.order を、差し込む値で選ぶ（辞書には持たない）
#   N-RUN-STEPS: N-STEPS-LIMIT と同じ
FIX_FROM_PARAM = {"N-INVALID"}
FIX_SAME_AS = {"N-RUN-STEPS": "N-STEPS-LIMIT"}


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _plain(text: str) -> str:
    return text.replace("`", "").strip()


def parse(markdown: str) -> dict[str, tuple[str, str]]:
    """画面仕様の表から {キー: (日本語, 英語)} を作る。同じキーが2回出たら ValueError。"""
    found: dict[str, tuple[str, str]] = {}

    def add(key: str, ja: str, en: str) -> None:
        if key in found and found[key] != (ja, en):
            raise ValueError(f"キー {key} が、違う文言で2回出ている")
        found[key] = (ja, en)

    header: list[str] | None = None
    notices: dict[str, tuple[str, str]] = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = cells
            continue
        if set(line.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        if header[:3] == ["キー", "日本語", "English"]:
            key = cells[0]
            if not (key.startswith("`") and key.endswith("`")):
                raise ValueError(f"キーの欄が読めない: {line}")
            add(key.strip("`"), _plain(cells[1]), _plain(cells[2]))
        elif header and header[0] == "番号" and "何が起きたか（日 / 英）" in header:
            code = cells[0]
            what = cells[header.index("何が起きたか（日 / 英）")]
            fix = cells[header.index("どう直すか（日 / 英）")]
            for part, text in (("what", what), ("fix", fix)):
                if part == "fix" and (code in FIX_FROM_PARAM or code in FIX_SAME_AS):
                    continue
                pieces = text.split(" / ")
                if len(pieces) != 2:
                    raise ValueError(f"{code} の {part} を日と英に分けられない: {text}")
                notices[f"notice.{code}.{part}"] = (_plain(pieces[0]), _plain(pieces[1]))
    for code, same in FIX_SAME_AS.items():
        notices[f"notice.{code}.fix"] = notices[f"notice.{same}.fix"]
    for key, pair in {**notices, **EXTRA}.items():
        add(key, *pair)
    return {key: (keep_with_unit(ja), keep_with_unit(en)) for key, (ja, en) in found.items()}


def dictionaries(markdown: str) -> tuple[dict[str, str], dict[str, str]]:
    table = parse(markdown)
    keys = sorted(table)
    return {k: table[k][0] for k in keys}, {k: table[k][1] for k in keys}


def main(argv: list[str]) -> int:
    ja, en = dictionaries(SPEC.read_text(encoding="utf-8"))
    files = {"ja": ja, "en": en}
    if "--write" in argv:
        for lang, table in files.items():
            text = json.dumps(table, ensure_ascii=False, indent=2) + "\n"
            (DICT_DIR / f"{lang}.json").write_text(text, encoding="utf-8")
        print(f"辞書を書き直しました（{len(ja)} 件）")
        return 0
    differ = []
    for lang, table in files.items():
        current = json.loads((DICT_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        if current != table:
            missing = sorted(set(table) - set(current))
            extra = sorted(set(current) - set(table))
            changed = sorted(k for k in set(table) & set(current) if table[k] != current[k])
            differ.append(f"{lang}: 足りない {missing[:5]} 余分 {extra[:5]} 違う {changed[:5]}")
    if differ:
        print("画面仕様の表と辞書が食い違っています。--write で書き直してください。\n" + "\n".join(differ))
        return 1
    print(f"辞書は画面仕様の表と一致しています（{len(ja)} 件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
