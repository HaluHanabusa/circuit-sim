"""設計トークンの試験（design-system.md 10章の 1〜3）。Node.js が無くても動く。

1. CSS のカスタムプロパティの値（frontend/src/styles/tokens.css）が、2.1・2.2・3.1 の表と一致する。
2. 2.3 の表の組み合わせのコントラスト比を式から計算し直し、基準（4.5 または 3）以上で、表の値と一致する。
3. 3.2 の a〜d を、両テーマの8色・全28組で計算し直し、基準以上である。
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "frontend" / "src" / "styles" / "tokens.css"
DESIGN = ROOT / "docs" / "design" / "design-system.md"
THEMES = ("light", "dark")
STATES = ("error", "missing", "warning", "ok")

# Machado・Oliveira・Fernandes（2009）の行列、強さ 1.0（線形 RGB に掛ける）
PROTAN = ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998))
DEUTAN = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))


def css_tokens() -> dict[str, dict[str, str]]:
    text = TOKENS.read_text(encoding="utf-8")
    blocks = {
        "light": re.search(r':root\[data-theme="light"\]\s*\{(.*?)\n\}', text, re.S).group(1),
        "dark": re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', text, re.S).group(1),
    }
    return {
        theme: {m.group(1): m.group(2).lower() for m in re.finditer(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", body)}
        for theme, body in blocks.items()
    }


def section(title: str) -> str:
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index(title)
    end = text.find("\n### ", start + len(title))
    return text[start:end if end >= 0 else None]


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s\-:|]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]


def documented_tokens() -> dict[str, dict[str, str]]:
    doc = {"light": {}, "dark": {}}
    for row in table_rows(section("### 2.1 役割と値")):
        if row[0].startswith("`") and row[2].startswith("`#"):
            name = row[0].strip("`")
            doc["light"][name], doc["dark"][name] = row[2].strip("`").lower(), row[3].strip("`").lower()
    for row in table_rows(section("### 2.2 状態の色")):
        state = row[0].split("`")[1]
        for part, cell in zip(("fg", "bg", "border"), row[3:6], strict=True):
            light, dark = (c.strip().strip("`").lower() for c in cell.split("/"))
            doc["light"][f"{state}-{part}"], doc["dark"][f"{state}-{part}"] = light, dark
    for row in table_rows(section("### 3.1 並びと値")):
        name = row[4].strip("`")
        doc["light"][name], doc["dark"][name] = row[2].strip("`").lower(), row[3].strip("`").lower()
    return doc


def linear(hex_color: str) -> tuple[float, float, float]:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)


def luminance(rgb: tuple[float, float, float]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def ratio_of(a: tuple, b: tuple) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def contrast(a: str, b: str) -> float:
    return ratio_of(linear(a), linear(b))


def encode(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def simulate(rgb: tuple, matrix) -> tuple[float, float, float]:
    """色覚の型の見え方。線形 RGB に行列を掛け、0〜1 に収め、8 ビットの sRGB の色に丸める（3.2 の表の値は、
    変換した色を 8 ビットの色として扱った値。丸めないと、暗の「赤」の1型が 2.99 になる）。"""
    mixed = (min(1.0, max(0.0, sum(m * c for m, c in zip(row, rgb, strict=True)))) for row in matrix)
    return linear("#" + "".join(f"{round(encode(c) * 255):02x}" for c in mixed))


def oklab(rgb: tuple) -> tuple[float, float, float]:
    r, g, b = rgb
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (v ** (1 / 3) for v in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def distance(a: tuple, b: tuple) -> float:
    return 100 * sum((x - y) ** 2 for x, y in zip(oklab(a), oklab(b), strict=True)) ** 0.5


TOKENS_CSS = css_tokens()


def test_css_values_match_design_system_tables():
    """10章の1: 2.1・2.2・3.1 の表の全部の値が、CSS に同じ値で入っている。"""
    doc = documented_tokens()
    assert len(doc["light"]) == 17 + 12 + 8
    for theme in THEMES:
        for name, value in doc[theme].items():
            assert TOKENS_CSS[theme].get(name) == value, f"{theme} {name}"


def contrast_rows():
    rows = []
    for row in table_rows(section("### 2.3 コントラストの根拠")):
        names = re.findall(r"`([\w-]+)`", row[0])
        if not names or not re.match(r"^[\d.]+$", row[1]):
            continue
        rows.append((row[0], names, float(row[1]), row[2], row[3]))
    return rows


@pytest.mark.parametrize(("label", "names", "threshold", "light", "dark"), contrast_rows())
def test_contrast_pairs_meet_the_threshold(label, names, threshold, light, dark):
    """10章の2: 2.3 の表の組み合わせを計算し直し、基準以上で、表の値（小数2桁）と一致する。"""
    for theme, documented in zip(THEMES, (light, dark), strict=True):
        tokens = TOKENS_CSS[theme]
        if names == ["fg", "bg-selected"]:  # 状態の fg / bg-selected（最小の組）
            values = [min(contrast(tokens[f"{s}-fg"], tokens["bg-selected"]) for s in STATES)]
        elif len(names) == 3:  # `x-fg` / `bg-panel` ・ `x-bg`
            values = [contrast(tokens[names[0]], tokens[names[1]]), contrast(tokens[names[0]], tokens[names[2]])]
        else:
            values = [contrast(tokens[names[0]], tokens[names[1]])]
        expected = [float(v) for v in re.findall(r"\d+\.\d+", documented)]
        for value, want in zip(values, expected, strict=True):
            assert value >= threshold, f"{label} {theme}: {value:.2f}"
            assert abs(value - want) <= 0.011, f"{label} {theme}: 計算 {value:.3f}、表 {want}"


@pytest.mark.parametrize("theme", THEMES)
def test_wave_colors_are_distinguishable(theme):
    """10章の3: 3.2 の a〜d を、8色・全28組で計算し直す。"""
    tokens = TOKENS_CSS[theme]
    plot = linear(tokens["bg-plot"])
    waves = [linear(tokens[f"wave-{i}"]) for i in range(1, 9)]
    for i, wave in enumerate(waves, start=1):
        assert ratio_of(wave, plot) >= 3, f"a: wave-{i}"
        for matrix in (PROTAN, DEUTAN):
            assert ratio_of(simulate(wave, matrix), simulate(plot, matrix)) >= 3, f"b: wave-{i}"
    worst_c = worst_d = float("inf")
    for a, b in itertools.combinations(waves, 2):
        worst_c = min(worst_c, *(distance(simulate(a, m), simulate(b, m)) for m in (PROTAN, DEUTAN)))
        worst_d = min(worst_d, distance(a, b))
    assert worst_c >= 8, f"c: {worst_c:.1f}"
    assert worst_d >= 15, f"d: {worst_d:.1f}"
    # 3.2 の表の最小値（1〜8番、28組）とほぼ一致する（色覚の変換の細かな違いで、暗の c は 10.4 になる）
    documented = {"light": (8.3, 16.4), "dark": (10.5, 18.5)}[theme]
    assert abs(worst_c - documented[0]) <= 0.15 and abs(worst_d - documented[1]) <= 0.15, (worst_c, worst_d)


def test_shadow_and_overlay_values():
    """2.1 の影と覆い（rgba で書く値）。"""
    text = TOKENS.read_text(encoding="utf-8")
    assert "--shadow-raised: 0 4px 12px rgba(0, 0, 0, 0.12);" in text
    assert "--shadow-raised: 0 4px 12px rgba(0, 0, 0, 0.5);" in text
    assert text.count("--overlay-dialog: rgba(0, 0, 0, 0.4);") == 2
