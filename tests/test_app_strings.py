"""画面の文言と色の直書きの試験（A0 の受け入れ基準6、画面仕様 AC-10-3、AC-9-1、design-system 10章の4・5）。

Node.js が無くても動く（画面のソースを文字列として調べる）。
JSX は、正規表現ではなく小さな読み取り器（scan_jsx）で、タグ・属性・子の式・子の文字を分けて調べる
（正規表現では、括弧・?・: を含む文や、式の中の文字列、属性を式で書いた文字列を見逃した。A0 のレビューの中4、
検証の問題5）。子の式と、文字を出す属性（TEXT_ATTRS）の式では、そのまま画面に出る位置の文字列を探す
（式の頭、三項・&&・||・?? の枝、+ でつないだもの。displayed_literals）。
直書きを写しに1つずつ仕込み、すべて見つかることも確かめる（test_planted_*）。
見つけられないもの:
  - 変数や定数に入れた文字列（const label = "Open"、<Tooltip text={label}>）
  - 関数の引数に渡した文字列（{format("Open")}、{list.map(() => "Item")}）。t("…") のキーと見分けられないため
  - 配列・オブジェクトの中の文字列（{["Open"][0]}、{{ a: "Open" }.a}）
  - TEXT_ATTRS に無い名前の属性に渡した文字列（部品が独自の名前で文字を受け取るもの）
これらは、画面に出た文字を調べる試験（受け入れ試験の AC-10-3）で拾う。
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
    "app.name", "lang.ja", "lang.en", "notice.count", "num.interp", "schem.source.step",
    "schem.source.step.t", "scope.compare.diff.item", "scope.csv", "scope.table.delta",
    "schem.leg.average", "schem.step.to",  # 付録A.4（記号と式だけ: duty × Vin、→ {to}）
}

# CSS の色の名前（CSS Color 4 の名前の色 148）と、システム色。transparent と currentColor は使ってよい
NAMED_COLORS = frozenset("""
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood
cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod
darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon
darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray
dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green
greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon
lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
magenta
maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen
mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive
olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff
peru
pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen
seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle
tomato
turquoise violet wheat white whitesmoke yellow yellowgreen
""".split())
SYSTEM_COLORS = frozenset("""
accentcolor accentcolortext activetext buttonborder buttonface buttontext canvas canvastext field fieldtext
graytext highlight highlighttext linktext mark marktext selecteditem selecteditemtext visitedtext
activeborder activecaption appworkspace background buttonhighlight buttonshadow captiontext inactiveborder
inactivecaption inactivecaptiontext infobackground infotext menu menutext scrollbar threeddarkshadow
threedface
threedhighlight threedlightshadow threedshadow window windowframe windowtext
""".split())
COLOR_WORDS = NAMED_COLORS | SYSTEM_COLORS
# 文字列がそのまま色の名前か、を見るときは、ふつうの語でもある古いシステム色を除く（role="menu" など）
COMMON_WORDS = frozenset({"menu", "background", "window", "canvas", "field", "mark", "highlight",
                          "scrollbar"})
BARE_COLOR_WORDS = COLOR_WORDS - COMMON_WORDS
COLOR_WORD_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(COLOR_WORDS, key=len, reverse=True)) + r")(?![\w-])", re.I)
COLOR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}(?![\w-])|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(",
                              re.I)
# 文字を画面に出す属性（読み上げ、ツールチップ、入力の見本）。text は Tooltip の文
TEXT_ATTRS = frozenset({"aria-label", "aria-description", "aria-roledescription", "aria-valuetext",
                        "aria-placeholder", "title", "placeholder", "alt", "label", "text"})
# 色を取る属性（SVG と style の名前。JSX の書き方と CSS の書き方の両方）
COLOR_ATTRS = frozenset({"fill", "stroke", "color", "stopColor", "stop-color", "floodColor", "flood-color",
                         "lightingColor", "lighting-color"})
COLOR_KEYS = re.compile(r"^(?:color|background(?:Color)?|border(?:\w*Color)?|border\w*|outline(?:Color)?|"
                        r"fill|stroke|boxShadow|textShadow|stopColor|floodColor|lightingColor|caretColor|accentColor|"
                        r"textDecorationColor|columnRuleColor)$")
ENGLISH_WORD = re.compile(r"[A-Za-z]{2,}")


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
    return re.sub(r"(^|[^:\"'\\])//[^\n]*", r"\1", text)


# ------------------------------------------------------------------ JSX の読み取り


def string_end(code: str, i: int) -> int:
    """code[i] が引用符（' " `）の文字列の、終わりの次の位置。テンプレートの ${…} は入れ子を数える。"""
    quote = code[i]
    j = i + 1
    while j < len(code):
        c = code[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if quote == "`" and code.startswith("${", j):
            j = brace_end(code, j + 1)
            continue
        if c == "\n" and quote != "`":
            return j  # 閉じていない（読み取りを止めない）
        j += 1
    return j


def brace_end(code: str, i: int) -> int:
    """code[i] == "{" の、対応する "}" の次の位置（文字列の中の括弧は数えない）。"""
    depth = 0
    j = i
    while j < len(code):
        c = code[j]
        if c in "'\"`":
            j = string_end(code, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def string_literal(expr: str) -> str | None:
    """式が文字列の決まった値（"…"、'…'、`…`（${} なし））だけなら、その中身。"""
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] in "'\"`" and string_end(expr, 0) == len(expr):
        if expr[0] == "`" and "${" in expr:
            return re.sub(r"\$\{[^}]*\}", "", expr[1:-1]) or None
        return expr[1:-1]
    return None


def is_color(value: str) -> bool:
    v = value.strip()
    if v.lower() in ("none", "currentcolor", "transparent", "inherit", ""):
        return False
    return bool(COLOR_LITERAL_RE.search(v) or COLOR_WORD_RE.search(v))


def is_tag_start(code: str, i: int) -> bool:
    """code[i] == "<" が JSX のタグの始まりか（型の引数 Set<number> や比較 a < b ではない）。"""
    nxt = code[i + 1:i + 3]
    closing = nxt[:1] == "/" and (nxt[1:2].isalpha() or nxt[1:2] == ">")
    if not (nxt[:1].isalpha() or nxt[:1] == ">" or closing):
        return False
    before = code[:i].rstrip()
    return not before or not re.search(r"[\w$)\].]$", before)


def style_problems(expr: str) -> list[str]:
    """style={{ color: "white" }} の、色の値の直書き。"""
    found = []
    for m in re.finditer(r"([\w$]+)\s*:\s*(['\"`])((?:\\.|(?!\2).)*)\2", expr):
        if COLOR_KEYS.match(m.group(1)) and is_color(m.group(3)):
            found.append(f"style の {m.group(1)}: {m.group(3)!r}")
    return found


def scan_jsx(code: str) -> list[str]:
    """JSX の直書きを探す: 子の文字、子の式の文字列、文字を出す属性の値、色の属性と style の色。"""
    found: list[str] = []
    depth = 0  # 開いている要素の数（子の文字を集めるのは、要素の中だけ）
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if c in "'\"`" and depth == 0:
            i = string_end(code, i)
            continue
        if c == "<" and is_tag_start(code, i):
            i, kind = parse_tag(code, i, found)
            depth += {"open": 1, "close": -1}.get(kind, 0)
            depth = max(depth, 0)
            continue
        if depth > 0 and c == "{":
            end = brace_end(code, i)
            child_expression(code[i + 1:end - 1], found)
            i = end
            continue
        if depth > 0:
            j = i + 1  # 少なくとも1文字進める（タグの始まりでない < でも止まらない）
            while j < n and code[j] not in "<{":
                j += 1
            text = code[i:j]
            if ENGLISH_WORD.search(text) or JAPANESE.search(text):
                found.append(f"子の文字: {text.strip()!r}")
            i = j
            continue
        i += 1
    return found


# そのまま画面に出る位置の文字列の、前の記号（式の頭は ""）と、後ろの記号
DISPLAY_BEFORE = ("", "?", ":", "&&", "||", "??", "+", "(")
OPERATORS = re.compile(r"===|!==|==|!=|<=|>=|&&|\|\||\?\?|=>|[?:+\-*/%<>!=,;]")


def displayed_literals(expr: str) -> list[str]:
    """式の中で、そのまま画面に出る位置の文字列の中身（テンプレートの ${…} は外す）。

    出る位置: 式の頭、三項の枝（? の後、: の後）、&& || ?? の後、+ の前後、まとめの括弧 ( の後。
    出ない位置: 関数の引数（t("a.b")、Icon の名前）、比較（kind === "error"）、配列・オブジェクトの中、
    添え字（a["b"]）。"""
    found: list[str] = []
    stack: list[str] = []  # 開いている括弧の種類: call、group、array、object
    prev = ""  # 直前の記号（"" は式の頭か、まとめの括弧の直後）
    pending: tuple[str, str] | None = None  # 直前の文字列（中身、その前の記号）。後ろに + が来るかを見る
    i = 0
    n = len(expr)

    def settle(after: str) -> None:
        nonlocal pending
        if pending is not None:
            text, before = pending
            inside = not stack or stack[-1] == "group"
            comparing = before in ("===", "!==", "==", "!=") or after in ("===", "!==", "==", "!=")
            if inside and not comparing and (before in DISPLAY_BEFORE or after == "+"):
                found.append(text)
            pending = None

    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c in "'\"`":
            end = string_end(expr, i)
            settle("")
            body = expr[i + 1:end - 1]
            if c == "`":
                body = re.sub(r"\$\{[^}]*\}", "", body)
            pending = (body, prev)
            prev = "str"
            i = end
            continue
        if c in "([{":
            settle("")
            if c == "(":
                stack.append("call" if prev in ("name", ")", "]") else "group")
            else:
                stack.append("array" if c == "[" else "object")
            prev = "" if c == "(" and stack[-1] == "group" else c
            i += 1
            continue
        if c in ")]}":
            settle(c)
            if stack:
                stack.pop()
            prev = ")" if c == ")" else "]"
            i += 1
            continue
        m = OPERATORS.match(expr, i)
        if m:
            settle(m.group(0))
            prev = m.group(0)
            i = m.end()
            continue
        m = re.compile(r"[\w$.]+").match(expr, i)
        settle("")
        if m:
            prev = "name"
            i = m.end()
        else:
            prev = c
            i += 1
    settle("")
    return found


def has_words(text: str) -> bool:
    return bool(ENGLISH_WORD.search(text) or JAPANESE.search(text))


def child_expression(expr: str, found: list[str]) -> None:
    value = string_literal(expr)
    if value is not None:
        if has_words(value):
            found.append(f"子の式の文字列: {value!r}")
        return
    # 三項や && の枝、+ でつないだ文字列（cond ? "Yes" : "No"、cond && "Ready"、"Saved: " + name）
    for text in displayed_literals(expr):
        if has_words(text):
            found.append(f"子の式の文字列: {text!r}")
    found.extend(scan_jsx(expr))  # 式の中の JSX（cond && <span>Open</span>）


def parse_tag(code: str, i: int, found: list[str]) -> tuple[int, str]:
    """i は "<"。タグの終わりの次の位置と、種類（open、close、self）。属性を調べて found に足す。"""
    j = i + 1
    closing = code.startswith("/", j)
    if closing:
        j += 1
    m = re.compile(r"[\w.:-]*").match(code, j)
    j = m.end()
    while j < len(code):
        c = code[j]
        if c == ">":
            return j + 1, "close" if closing else "open"
        if code.startswith("/>", j):
            return j + 2, "self"
        if c == "{":  # {...props}
            end = brace_end(code, j)
            found.extend(scan_jsx(code[j + 1:end - 1]))
            j = end
            continue
        am = re.compile(r"([\w:-]+)\s*(=\s*)?").match(code, j)
        if not am or not am.group(1):
            j += 1
            continue
        name, has_value = am.group(1), bool(am.group(2))
        j = am.end()
        if not has_value:
            continue
        if code[j] in "'\"":
            end = string_end(code, j)
            value = code[j + 1:end - 1]
            j = end
            attribute(name, value, None, found)
        elif code[j] == "{":
            end = brace_end(code, j)
            expr = code[j + 1:end - 1]
            j = end
            attribute(name, string_literal(expr), expr, found)
    return j, "open"


def attribute(name: str, value: str | None, expr: str | None, found: list[str]) -> None:
    if name in TEXT_ATTRS and value is not None and has_words(value):
        found.append(f"属性 {name}: {value!r}")
    if name in TEXT_ATTRS and value is None and expr is not None:
        # 式で書いた文字（aria-label={open ? "Collapse" : "Expand"}、aria-label={"Open " + name}）
        found.extend(f"属性 {name}: {text!r}" for text in displayed_literals(expr) if has_words(text))
    if name in COLOR_ATTRS and value is not None and is_color(value):
        found.append(f"色の属性 {name}: {value!r}")
    if name == "style" and expr is not None:
        found.extend(style_problems(expr))
    if expr is not None and value is None:
        found.extend(scan_jsx(expr))  # 属性の式の中の JSX


def text_problems(code: str, suffix: str) -> list[str]:
    code = without_comments(code)
    found = [f"日本語: {m.group(0)!r}" for m in JAPANESE.finditer(code)][:1]
    if suffix == ".tsx":
        found += scan_jsx(code)
    return found


def color_problems(code: str, suffix: str) -> list[str]:
    """色の値の直書き。tokens.css だけが値を持つ（design-system 10章の4）。"""
    code = without_comments(code)
    found: list[str] = []
    if suffix in (".css", ".html"):
        code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
        for m in re.finditer(r"([\w-]+)\s*:\s*([^;{}<>]+)", code):
            value = re.sub(r"var\([^)]*\)", "", m.group(2))
            if m.group(1).lower() in ("content", "font-family", "font", "grid-template-areas"):
                continue
            if COLOR_LITERAL_RE.search(value) or COLOR_WORD_RE.search(value):
                found.append(f"{m.group(1)}: {m.group(2).strip()}")
        return found
    # TS / TSX: 色の値そのものの文字列（"#fff"、"rgb(…)"、"crimson"、"CanvasText"）
    for m in re.finditer(r"(['\"`])((?:\\.|(?!\1)[^\n])*)\1", code):
        text = m.group(2)
        if COLOR_LITERAL_RE.search(text) or text.strip().lower() in BARE_COLOR_WORDS:
            found.append(f"文字列の色: {text!r}")
    if suffix == ".tsx":
        found += [f for f in scan_jsx(code) if f.startswith(("色の属性", "style"))]
    # style オブジェクトを変数に置いたもの（const s = { color: "white" }）
    found += style_problems(code)
    return sorted(set(found))


# ------------------------------------------------------------------ 辞書


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


UNIT_AFTER_NUMBER = re.compile(r"\d[ \u00a0][fpnµμumkMG]?(?:Hz|V|A|H|F|s|Ω|B|W)(?![A-Za-z])")


def test_numbers_and_units_are_joined_by_no_break_space():
    """AC-2-6、design-system 4.2: 辞書の「数値 ＋ 空白 ＋ 単位」の空白は U+00A0（改行されない）。"""
    found = {}
    for lang, table in (("ja", JA), ("en", EN)):
        for key, text in table.items():
            bad = [m.group(0) for m in UNIT_AFTER_NUMBER.finditer(text) if "\u00a0" not in m.group(0)]
            if bad:
                found[f"{lang} {key}"] = bad
    assert found == {}
    assert "1.59\u00a0kHz" in EN["sample.rlc.desc"] and "1.59\u00a0kHz" in JA["sample.rlc.desc"]


def test_dictionary_tool_joins_units_but_leaves_words():
    tool = load_tool()
    assert tool.keep_with_unit("1.59 kHz, 47 µH, 10 MB, 0.12 s, 1 V/s") == \
        "1.59\u00a0kHz, 47\u00a0µH, 10\u00a0MB, 0.12\u00a0s, 1\u00a0V/s"
    assert tool.keep_with_unit("8 signals, 4 plots, 0 to 1, 3 Hzz") == "8 signals, 4 plots, 0 to 1, 3 Hzz"


def test_every_notice_has_what_and_fix():
    """AC-9-1: 付録Bのすべてのお知らせが「何が起きたか」と「どう直すか」を持つ。"""
    catalog = (SRC / "notices.ts").read_text(encoding="utf-8")
    codes = re.findall(r'"(N-[A-Z-]+)":', catalog)
    # 付録B の 33 件と、付録B の外の N-PREFS-SAVE（strings_from_spec.py の EXTRA）
    assert len(codes) == 33 + 1
    for code in codes:
        assert JA.get(f"notice.{code}.what") and EN.get(f"notice.{code}.what"), code
        if code == "N-INVALID":  # どう直すかは、入力に合わせて 2.3 の文言から選ぶ（付録B）
            continue
        assert JA.get(f"notice.{code}.fix") and EN.get(f"notice.{code}.fix"), code


def test_message_keys_used_in_code_exist():
    """コードが t("…") で引くキーが、辞書にある（綴りの誤りで、利用者にキーの名前が出ない）。"""
    for path in sources(".ts", ".tsx"):
        for key in re.findall(r'\bt\(\s*"([\w.\-]+)"', path.read_text(encoding="utf-8")):
            assert key in JA, f"{path.name}: {key}"


# ------------------------------------------------------------------ 直書き（いまのソース）


@pytest.mark.parametrize("path", sources(".ts", ".tsx"), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_hard_coded_screen_text(path: Path):
    """design-system 10章の5: 画面の文字列は辞書から引く（子の文字、子の式の文字列、文字を出す属性）。"""
    assert text_problems(path.read_text(encoding="utf-8"), path.suffix) == []


@pytest.mark.parametrize(
    "path",
    [p for p in sources(".ts", ".tsx", ".css") if p.name != "tokens.css"]
    + [ROOT / "frontend" / "index.html"],
    ids=lambda p: p.name,
)
def test_no_hard_coded_colors(path: Path):
    """design-system 10章の4: 部品の中に色の値を直接書かない（tokens.css だけが値を持つ）。"""
    assert color_problems(path.read_text(encoding="utf-8"), path.suffix) == []


# ------------------------------------------------------------------ 直書きを仕込んで、見つかることを確かめる


HOST = SRC / "components" / "Header.tsx"
ANCHOR = '<span className="header-spacer" />'
PLANTED_TEXT = [
    '<span>{"Open file"}</span>',
    "<span>{'Open'}</span>",
    "<span>{`Open the file`}</span>",
    '<button aria-label={"Dismiss"} />',
    '<button aria-label="Dismiss" />',
    "<span title={`Open a spec`} />",
    '<span title="Open file" />',
    '<input placeholder="Type here" />',
    '<img alt="logo" />',
    "<span>Error: cannot open</span>",
    "<span>Are you sure?</span>",
    "<span>Open (Ctrl+O)</span>",
    "<span>Open</span>",
    "<span>{connected ? \"Online\" : \"Offline\"}</span>",
    "<span>{connected && <b>Ready</b>}</span>",
    "<p>\n  Retry\n</p>",
    "<span>開く</span>",
    '<Tooltip text={"x"}><span aria-description={"Opens the file"} /></Tooltip>',
    # A0 のレビュー2回目の軽4
    '<Tooltip text="Open the file"><button /></Tooltip>',
    '<button aria-label={open ? "Collapse" : "Expand"} />',
    '<span>{"Saved: " + name}</span>',
    '<button aria-label={"Open " + name} />',
    '<span>{name + " saved"}</span>',
    '<span title={busy ? `Running ${n}` : "Idle"} />',
    '<span>{(open && "Collapse") || "Expand"}</span>',
    '<span>{label ?? "Untitled"}</span>',
]
PLANTED_COLOR = [
    ('<span style={{color:"red"}} />', ".tsx"),
    ('<span style={{ backgroundColor: "white" }} />', ".tsx"),
    ("<span style={{ borderColor: '#ff0000' }} />", ".tsx"),
    ('<path fill="black" />', ".tsx"),
    ('<path stroke={"crimson"} />', ".tsx"),
    ('<path fill="CanvasText" />', ".tsx"),
    ('<span style={{ color: "rgb(1, 2, 3)" }} />', ".tsx"),
    ('const c = "crimson";', ".ts"),
    ('const s = { color: "white" };', ".ts"),
    ('const c = "#abc";', ".ts"),
    (".x { color: white; }", ".css"),
    (".x { color: crimson; }", ".css"),
    (".x { background: CanvasText; }", ".css"),
    (".x { border: 1px solid rebeccapurple; }", ".css"),
    (".x { box-shadow: 0 0 0 1px #000; }", ".css"),
    (".x { --mine: hsl(0 0% 0%); }", ".css"),
    (".x { outline-color: Highlight; }", ".css"),
    ('<div style="color: red"></div>', ".html"),
]


def planted(snippet: str) -> str:
    code = HOST.read_text(encoding="utf-8")
    assert ANCHOR in code
    return code.replace(ANCHOR, ANCHOR + "\n      " + snippet, 1)


@pytest.mark.parametrize("snippet", PLANTED_TEXT, ids=repr)
def test_planted_text_is_found(snippet):
    """写し（Header.tsx）に文言の直書きを1つ仕込むと、見つかる。"""
    assert text_problems(HOST.read_text(encoding="utf-8"), ".tsx") == []
    assert text_problems(planted(snippet), ".tsx") != [], snippet


@pytest.mark.parametrize(("snippet", "suffix"), PLANTED_COLOR, ids=lambda v: repr(v))
def test_planted_color_is_found(snippet, suffix):
    """写しに色の直書きを1つ仕込むと、見つかる（.tsx は Header.tsx、ほかは app.css などの写し）。"""
    if suffix == ".tsx":
        code = planted(snippet)
    elif suffix == ".css":
        code = (SRC / "styles" / "app.css").read_text(encoding="utf-8") + "\n" + snippet + "\n"
    elif suffix == ".html":
        page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        code = page.replace("</body>", snippet + "</body>")
    else:
        code = (SRC / "prefs.ts").read_text(encoding="utf-8") + "\n" + snippet + "\n"
    assert color_problems(code, suffix) != [], snippet


@pytest.mark.parametrize("snippet", [
    '<span className="header-file-name" data-testid="x" />',
    "<span>{t(\"app.name\")}</span>",
    '<Icon name="error" />',
    "<span>{n.time}</span>",
    "<span>{open ? t(\"layout.collapse\") : t(\"layout.expand\")}</span>",
    "<path fill=\"none\" stroke=\"currentColor\" />",
    "<span>{\" \"}</span>",
    '<button aria-label={open ? t("layout.collapse") : t("layout.expand")} />',
    '<Tooltip text={t("header.open.tip", { mod: "Ctrl" })}><button /></Tooltip>',
    '<span>{kind === "error" ? <Icon name="error" /> : null}</span>',
    '<span>{n.time ?? ""}</span>',
    '<span title={name + "\u00a0"} />',
], ids=repr)
def test_allowed_forms_are_not_reported(snippet):
    """辞書から引く形、部品の名前、データの差し込み、currentColor は、直書きとして数えない。

    （誤検出しない）"""
    assert text_problems(planted(snippet), ".tsx") == []
    assert color_problems(planted(snippet), ".tsx") == []


@pytest.mark.parametrize("snippet", [
    '<Tooltip text={label}><button /></Tooltip>',
    "<span>{format(\"Open\")}</span>",
    "<span>{list.map(() => \"Item\")}</span>",
    "<span>{[\"Open\"][0]}</span>",
    '<Panel heading="Open" />',
], ids=repr)
def test_documented_gaps_are_not_found(snippet):
    """モジュールの説明に書いた「見つけられないもの」は、本当に見つけられない（説明と事実を合わせておく）。

    見つけられるようにしたら、説明とこの一覧から外す。"""
    assert text_problems(planted(snippet), ".tsx") == []
