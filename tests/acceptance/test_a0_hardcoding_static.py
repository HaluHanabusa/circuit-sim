"""A0 の独立検証（verifier）: 文言と色の直書きの検出の補い（基準6、design-system 10章の4・5）。

記録は docs/verification/A0-verifier.md。Node.js もブラウザも要らない。
実装の試験（tests/test_app_strings.py）に直書きを仕込んだところ、次の形を見逃した（16 通り中 9 通り）:
JSX の式の中の文字列（{"Open"}）、変数に入れた文字列、属性を式で書いた文字列（aria-label={"Dismiss"}）、
括弧・疑問符・コロンを含む JSX の文、style の中の色の名前（color: "white"）、SVG の fill="black"、
短い一覧に無い色の名前（crimson）、システム色（CanvasText）。
文の直書きは、画面に出ている文字を調べる試験（test_a0_browser_checks.py の AC-10-3）でも拾う。
ここでは、ソースを読むだけで分かる形を拾う。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

# CSS の色の名前（CSS Color Module Level 4 の名前の色 148 と、システム色）。transparent と currentColor は除く
NAMED = """
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet
brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan
darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen
darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey
darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew
hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue
lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime
limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple
mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue
mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen
seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal
thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen accentcolor
accentcolortext activetext buttonborder buttonface buttontext canvas canvastext field fieldtext
graytext highlight highlighttext linktext mark marktext selecteditem selecteditemtext
visitedtext
""".split()
NAMED_RE = re.compile(r"(?<![\w-])(?:" + "|".join(NAMED) + r")(?![\w-])", re.I)
LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(")
COLOR_PROPS = (r"(?:color|background(?:-color)?|border(?:-[a-z]+)*|outline(?:-color)?|fill|stroke|box-shadow|"
               r"text-decoration(?:-color)?|caret-color|accent-color|column-rule(?:-color)?|stop-color|"
               r"text-shadow|flood-color|lighting-color)")


def files(*suffixes: str) -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in suffixes and ".test." not in p.name
                  and "i18n" not in p.parts)


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", text)


@pytest.mark.parametrize("path", [p for p in files(".css") if p.name != "tokens.css"], ids=lambda p: p.name)
def test_css_declarations_use_only_tokens(path: Path):
    """CSS の色の宣言の値に、色の名前・色の値を直接書かない（var(--…) だけ）。"""
    code = strip_comments(path.read_text(encoding="utf-8"))
    found = []
    for m in re.finditer(COLOR_PROPS + r"\s*:\s*([^;{}]+)", code, re.I):
        value = re.sub(r"var\([^)]*\)", "", m.group(1))
        if NAMED_RE.search(value) or LITERAL_RE.search(value):
            found.append(m.group(0).strip())
    assert found == []


@pytest.mark.parametrize("path", files(".tsx", ".ts"), ids=lambda p: p.relative_to(SRC).as_posix())
def test_components_write_no_color_values(path: Path):
    """部品の style や SVG の属性に、色の名前・色の値を直接書かない（currentColor と none だけ）。"""
    code = strip_comments(path.read_text(encoding="utf-8"))
    found = []
    # style={{ color: "white" }} のような、文字列で書いた色
    for m in re.finditer(r"\b(?:color|background(?:Color)?|border(?:Color)?|outline(?:Color)?|fill|stroke|"
                         r"boxShadow|textShadow|stopColor)\s*:\s*([\"'`])([^\"'`]*)\1", code):
        if NAMED_RE.search(m.group(2)) or LITERAL_RE.search(m.group(2)):
            found.append(m.group(0))
    # <rect fill="black" /> のような SVG の属性
    attr = r"\b(?:fill|stroke|stop-color|stopColor|color|floodColor|lightingColor)=\{?([\"'])([^\"']*)\1"
    for m in re.finditer(attr, code):
        value = m.group(2)
        if value not in ("none", "currentColor") and (NAMED_RE.search(value) or LITERAL_RE.search(value)):
            found.append(m.group(0))
    assert found == []


@pytest.mark.parametrize("path", files(".tsx"), ids=lambda p: p.relative_to(SRC).as_posix())
def test_jsx_has_no_literal_text_in_expressions_or_attributes(path: Path):
    """JSX の式（{"…"}）や、読み上げ・ツールチップの属性の式（aria-label={"…"}）に、文字列を直に書かない。"""
    code = strip_comments(path.read_text(encoding="utf-8"))
    found = []
    # 子として置いた文字列の式: >{"Open"}< や >{'…'}<、{`…`}
    for m in re.finditer(r"[>}]\s*\{\s*([\"'`])([^\"'`{}]*[A-Za-z]{2,}[^\"'`{}]*)\1\s*\}", code):
        found.append(m.group(0).strip())
    # 文字を出す属性を式で書いたもの
    for m in re.finditer(r"\b(?:aria-label|aria-description|aria-roledescription|aria-valuetext|title|"
                         r"placeholder|alt|label)=\{\s*([\"'`])([^\"'`]*[A-Za-z]{2,}[^\"'`]*)\1\s*\}", code):
        found.append(m.group(0))
    # 括弧・疑問符・コロンを含む、JSX に直に書いた英語の文（実装の試験はこの形を除いている）
    for m in re.finditer(r"(?<!=)>([^<>{}\n]*[A-Za-z]{2,}[^<>{}\n]*)</?[A-Za-z]", code):
        text = m.group(1).strip()
        if text and re.search(r"[A-Za-z]+\s+[A-Za-z]+|[A-Z][a-z]+[?:!.]", text):
            found.append(text)
    assert found == []
