"""画面の文言と色の直書きの試験（A0 の受け入れ基準6、画面仕様 AC-10-3、AC-9-1、design-system 10章の4・5）。

Node.js が無くても動く（画面のソースを文字列として調べる）。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
I18N = SRC / "i18n"
TOOL = ROOT / "frontend" / "tools" / "strings_from_spec.py"

JA = json.loads((I18N / "ja.json").read_text(encoding="utf-8"))
EN = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
# 日英で同じ文言でよいもの（記号、名前、差し込むだけのもの。10.4 の「変わらない」もの）
SAME_IN_BOTH = {
    "app.name", "lang.ja", "lang.en", "notice.count", "num.interp", "schem.source.step", "schem.source.step.t",
    "scope.compare.diff.item", "scope.csv", "scope.table.delta",
}


def load_tool():
    spec = importlib.util.spec_from_file_location("strings_from_spec", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sources(*suffixes: str) -> list[Path]:
    return sorted(
        p for p in SRC.rglob("*")
        if p.suffix in suffixes and ".test." not in p.name and p.parent != I18N
    )


def without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", text)


def test_dictionaries_match_the_screen_spec_tables():
    """辞書は画面仕様の文言の表から作り、表と食い違わない（表に無い文字列を画面に出さない。0.2）。"""
    tool = load_tool()
    ja, en = tool.dictionaries(tool.SPEC.read_text(encoding="utf-8"))
    assert JA == ja
    assert EN == en


def test_japanese_and_english_have_the_same_keys():
    """AC-10-3: 日英の辞書のキーが完全に一致し、どちらも空でない。"""
    assert set(JA) == set(EN)
    assert all(JA[k].strip() and EN[k].strip() for k in JA)


def test_english_has_no_japanese_and_japanese_is_translated():
    """AC-10-3: 英語の表示に日本語の文字が無い。日本語の辞書に、英語のまま残った文言が無い。"""
    assert [k for k, v in EN.items() if JAPANESE.search(v) and k != "lang.ja"] == []
    same = {k for k in JA if JA[k] == EN[k]}
    assert same == SAME_IN_BOTH


def test_every_notice_has_what_and_fix():
    """AC-9-1: 付録Bのすべてのお知らせが「何が起きたか」と「どう直すか」を持つ。"""
    catalog = (SRC / "notices.ts").read_text(encoding="utf-8")
    codes = re.findall(r'"(N-[A-Z-]+)":', catalog)
    assert len(codes) == 33
    for code in codes:
        assert JA.get(f"notice.{code}.what") and EN.get(f"notice.{code}.what"), code
        if code == "N-INVALID":  # どう直すかは、入力に合わせて 2.3 の文言から選ぶ（付録B）
            continue
        assert JA.get(f"notice.{code}.fix") and EN.get(f"notice.{code}.fix"), code


@pytest.mark.parametrize("path", sources(".ts", ".tsx"), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_hard_coded_screen_text(path: Path):
    """design-system 10章の5: 画面の文字列は辞書から引く（コードに日本語の文字列も、画面に出す英語の文も無い）。"""
    code = without_comments(path.read_text(encoding="utf-8"))
    assert not JAPANESE.search(code), JAPANESE.search(code)
    if path.suffix == ".tsx":
        # JSX の中に直に書いた文字（>Open< のような）
        texts = [m.group(1).strip() for m in re.finditer(r"(?<!=)>([^<>{}]+)</?[A-Za-z]", code)]
        texts = [t for t in texts if re.search(r"[A-Za-z]", t) and not re.search(r"[()=;:?&|]", t)]
        assert texts == []
        # 読み上げやツールチップに使う属性に、直に書いた文字
        attrs = re.findall(r'\b(?:aria-label|title|placeholder|alt)="([^"]*)"', code)
        assert attrs == []


def test_message_keys_used_in_code_exist():
    """コードが t("…") で引くキーが、辞書にある（綴りの誤りで、利用者にキーの名前が出ない）。"""
    for path in sources(".ts", ".tsx"):
        for key in re.findall(r'\bt\(\s*"([\w.\-]+)"', path.read_text(encoding="utf-8")):
            assert key in JA, f"{path.name}: {key}"


COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(|"
                   r":\s*(?:white|black|red|blue|green|gray|grey|yellow|orange)\b")


@pytest.mark.parametrize(
    "path",
    [p for p in sources(".ts", ".tsx", ".css") if p.name != "tokens.css"] + [ROOT / "frontend" / "index.html"],
    ids=lambda p: p.name,
)
def test_no_hard_coded_colors(path: Path):
    """design-system 10章の4: 部品の中に色の値を直接書かない（tokens.css だけが値を持つ）。"""
    code = without_comments(path.read_text(encoding="utf-8"))
    code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
    assert COLOR.findall(code) == []
