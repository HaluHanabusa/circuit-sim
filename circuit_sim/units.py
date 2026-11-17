"""層Aの値に付いた単位を読み、SI単位の値に換算する。

層A（Spec）を読む側（テンプレートの展開）が使う。エンジンの核（mna / kernel / simulate）は
SI単位の値だけを受け取り、単位を知らない。

読める形: SI接頭語 ＋ 単位記号。大文字と小文字を区別する（m はミリ、M はメガ）。
  接頭語  : f p n u(µ, μ) m k M G
  単位記号: V（電圧）、A（電流）、ohm / Ohm / Ω（抵抗）、H（インダクタンス）、F（キャパシタンス）、
            Hz（周波数）、s（時間）、W（電力）
  接頭語を付けない単位（制御の値）: 1/V（比例ゲイン）、1/(V·s)（積分ゲイン）、s/V（微分ゲイン）、
            1（無次元。デューティなど）、%（無次元の 1/100）。
            空の文字列は読まない（単位の書き忘れと区別できない）
"""
from __future__ import annotations

from .messages import MESSAGES, CodedError, Message, Names

# 接頭語 → 10 の指数。µ は3通りの書き方を受け付ける（u、U+00B5 マイクロ記号、U+03BC ギリシャ文字ミュー）
PREFIX_EXPONENT = {
    "f": -15, "p": -12, "n": -9, "u": -6, "µ": -6, "μ": -6, "m": -3, "k": 3, "M": 6, "G": 9,
}

# 単位記号 → 次元。Ω は2通りの文字（U+03A9 ギリシャ文字オメガ、U+2126 オーム記号）を受け付ける
UNIT_DIMENSION = {
    "V": "voltage",
    "A": "current",
    "ohm": "resistance", "Ohm": "resistance", "Ω": "resistance", "Ω": "resistance",
    "H": "inductance",
    "F": "capacitance",
    "Hz": "frequency",
    "s": "time",
    "W": "power",
}

# 接頭語を付けない単位（完全に一致したときだけ読む）→（次元、10 の指数）。
# 制御のゲインは、デューティ（無次元）を電圧の偏差で割った量。· は U+00B7、* と空白も受け付ける
EXACT_UNITS = {
    "1/V": ("per_voltage", 0),
    "1/(V·s)": ("per_voltage_time", 0),
    "1/(V*s)": ("per_voltage_time", 0),
    "1/(V s)": ("per_voltage_time", 0),
    "s/V": ("time_per_voltage", 0),
    "1": ("ratio", 0), "%": ("ratio", -2),
}

# 次元 → SI単位の記号
SI_SYMBOL = {
    "voltage": "V",
    "current": "A",
    "resistance": "ohm",
    "inductance": "H",
    "capacitance": "F",
    "frequency": "Hz",
    "time": "s",
    "power": "W",
    "per_voltage": "1/V",
    "per_voltage_time": "1/(V·s)",
    "time_per_voltage": "s/V",
    "ratio": "1",
}
# 次元 → （日本語の呼び名、SI単位の記号）。呼び名は文面の辞書（messages の DIM_<次元>）から取る
DIMENSION_LABEL = {d: (MESSAGES[f"DIM_{d}"][0], symbol) for d, symbol in SI_SYMBOL.items()}


class UnitError(CodedError):
    """単位が読めない、または期待した次元と違う（番号 UNIT_*、UNITS_*）。"""


def parse_unit(text: str) -> tuple[str, int]:
    """単位の文字列を（次元、10 の指数）に分ける。読めなければ UnitError。

    例: "mH" → ("inductance", -3)、"kΩ" → ("resistance", 3)、"V" → ("voltage", 0)
    """
    unit = text.strip()
    if unit in EXACT_UNITS:
        return EXACT_UNITS[unit]
    if unit in UNIT_DIMENSION:
        return UNIT_DIMENSION[unit], 0
    for symbol, dimension in UNIT_DIMENSION.items():
        if unit.endswith(symbol) and unit[: -len(symbol)] in PREFIX_EXPONENT:
            return dimension, PREFIX_EXPONENT[unit[: -len(symbol)]]
    raise UnitError("UNIT_UNREADABLE", unit=text)


def _how_to_write(dimension: str) -> Message:
    """その次元の単位の書き方の案内。制御の値の単位（EXACT_UNITS）は接頭語を受け付けない。"""
    exact = [u for u, (d, _) in EXACT_UNITS.items() if d == dimension]
    if exact:
        return Message("HOW_EXACT", units=Names(exact))
    return Message("HOW_PREFIXED", si=SI_SYMBOL[dimension])


def to_si(value: float, unit: str, dimension: str) -> float:
    """値を SI単位に換算する。単位が読めない、または次元が dimension と違えば UnitError。"""
    expected = Message(f"DIM_{dimension}")
    try:
        found, exponent = parse_unit(unit)
    except UnitError:
        raise UnitError("UNIT_UNREADABLE_EXPECTED", unit=unit, expected=expected,
                        how=_how_to_write(dimension), dimension=dimension) from None
    if found != dimension:
        raise UnitError("UNIT_WRONG_DIMENSION", unit=unit, found=Message(f"DIM_{found}"), expected=expected,
                        how=_how_to_write(dimension), dimension=dimension)
    # 負の指数は割り算にする（10 の負のべきは2進数で正確に表せないので、掛けると最後の桁がずれる）
    return value * 10.0**exponent if exponent >= 0 else value / 10.0 ** (-exponent)
