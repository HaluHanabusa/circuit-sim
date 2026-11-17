"""E1b の7回目の修正（45d89cf）の受け入れ試験。検証担当が追加した。
記録は docs/verification/E1b-verifier-7.md。

確かめること: 零点の減衰 zero_damping の下限 1e-6（D-066。両端を含む範囲 [1e-6, 2]）、下限の付近で
PID の零点のすぐ横の交差が余裕の最小になるときの丸め（6回目の軽微1）、組み合わせの範囲の外の今の値の
書き方（6回目の軽微2）。

期待値の作り方（`circuit_sim` の関数を期待値の計算に使わない）:
  - 150 桁の参照の値（REF_FLOOR。このファイルに数字で書いてある）。6回目の参照（test_e1b_noload_and_
    combination_range.py の説明と同じ）で、作業ツリーの外で求めた。回路ごとに、目標を動かして「零点の横の
    交差が余裕の最小になる」境目を探し、その前後の目標で照合した。
  - 範囲: 決定記録 D-066 の文から書き写した（実装の定数を読まない）。

許容差（測る前に決めた根拠）:
  - 「満たす」は 目標 − 1e-6°、安定は半径 1 − 1e-9（D-057）。
  - 150 桁の値との余裕の一致 1e-7°: 判定の余白 1e-6° の 1/10。D-062 は余白が丸めより十分大きいことを
    求めるので、丸めがこれを超えれば D-066 の根拠（丸めは余白の 1/100 程度）が崩れる。半径は 1e-12。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest

from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

MEETS_TOL = 1e-6          # D-057
STABILITY_EPS = 1e-9      # D-057
FLOOR = 1e-6              # D-066
FLOOR_PM_TOL = 1e-7
REF_RADIUS_TOL = 1e-12


def _run(plant, f_ctrl, **opts):
    try:
        return "ok", buck_voltage_mode_pid(*plant, f_ctrl, **opts), ""
    except GainDesignError as e:
        return e.reason, e.best, str(e)


BASE = ((24.0, 22e-6, 100e-6, 6.0), 200e3)


@pytest.mark.parametrize("value", [FLOOR, np.float64(FLOOR), 1.0000000000000002e-6, 1e-5], ids=repr)
def test_zero_damping_floor_is_inclusive(value):
    result, _, _ = _run(*BASE, zero_damping=value)
    assert result != "invalid_argument"


@pytest.mark.parametrize("value", [math.nextafter(FLOOR, 0.0), 5e-7, 1e-10, 5e-324, 0.0], ids=repr)
def test_zero_damping_below_the_floor_is_refused_naming_it(value):
    result, g, msg = _run(*BASE, zero_damping=value)
    assert result == "invalid_argument" and g is None
    assert "zero_damping" in msg and "1e-06" in msg


# （回路、制御周波数、設計の引数、結果、150 桁の参照の余裕 [deg]、150 桁の参照の半径 − 1）。どれも ζz = 1e-6
# （下限ちょうど）。目標を、零点の横の交差が余裕の最小になる境目の前後に置いた（前は「満たす」、後は
# margin_below_target）。f_ctrl/f0 は 6.6e8 と 7.5e7。
_A = ((2694.8017090698213, 0.0005090499188395726, 8.26424942741055, 0.00010490964950782425),
      1627099760.6608832, {"zero_ratio": 1.0, "zero_damping": 1e-06, "filter_ratio": 0.25})
_B = ((0.00012370242242529947, 0.3845539330824293, 1.0037011902046282e-07, 192.25611460404235),
      61000049334.709724, {"zero_ratio": 0.44854667100041934, "zero_damping": 1e-06, "filter_ratio": 0.25})
REF_FLOOR = [
    (_A, 53.98852339433547, "no_stability_margin", 53.98852305392481, -9.520408428729748e-15),
    (_A, 53.988524094335475, "margin_below_target", 53.98852145684596, -9.520408428729278e-15),
    (_B, 53.3232590718025, "no_stability_margin", 53.32325858442011, -4.768961826140795e-14),
    (_B, 53.3232597718025, "margin_below_target", 53.32325750491855, -4.768961845189186e-14),
]


@pytest.mark.parametrize("case", REF_FLOOR, ids=["A-meets", "A-short", "B-meets", "B-short"])
def test_crossing_beside_the_zero_at_the_floor_agrees_with_150_digit_reference(case):
    """零点の横の交差の余裕は 150 桁の値と 1e-7° で合い、理由と「満たす / 足りない」と半径の言葉が
    参照と合う。"""
    (plant, f_ctrl, opts), target, expected, ref_pm, ref_r = case
    result, g, msg = _run(plant, f_ctrl, phase_margin_deg=target, **opts)
    assert result == expected
    assert g.phase_margin_deg == pytest.approx(ref_pm, abs=FLOOR_PM_TOL)
    assert g.pole_radius - 1.0 == pytest.approx(ref_r, abs=REF_RADIUS_TOL)
    assert ("は満たす）" in msg) == (ref_pm >= target - MEETS_TOL)
    assert ("足りない）" in msg) == (ref_pm < target - MEETS_TOL)
    assert ("位相余裕は目標を満たすが" in msg) == (ref_pm >= target - MEETS_TOL)
    assert ("安定の余裕が無い）" in msg) == (abs(ref_r) < STABILITY_EPS)


@pytest.mark.parametrize("plant_f", [
    ((24.0, 1e-6, 1e-6, math.nextafter(1e-3, 0.0)), 20e6),       # √(L/C) = 1 なので Q = R
    ((24.0, 1e-6, 1e-6, 0.999999e-3), 20e6),
], ids=["Q-1ulp", "Q-1e-6"])
def test_q_just_below_the_limit_is_shown_below_it(plant_f):
    """6回目の軽微2: 境目のすぐ外の今の値を、境目と見分けられる桁まで書く。"""
    plant, f = plant_f
    result, _, msg = _run(plant, f)
    assert result == "invalid_argument"
    m = re.search(r"は ([\d.e+\-]+) 以上にする（現在 ([\d.e+\-]+)", msg)
    assert m and float(m.group(1)) == 1e-3
    assert float(m.group(2)) < float(m.group(1))


def test_ratio_just_above_the_limit_is_shown_above_it():
    f = 1e11 / (2.0 * math.pi)
    while 2.0 * math.pi * f <= 1e11:
        f = math.nextafter(f, math.inf)
    result, _, msg = _run((24.0, 1.0, 1.0, 6.0), f)
    assert result == "invalid_argument"
    m = re.search(r"は ([\d.e+\-]+) 以下にする（現在 ([\d.e+\-]+)", msg)
    assert m and float(m.group(1)) == 1e11
    assert float(m.group(2)) > float(m.group(1))
