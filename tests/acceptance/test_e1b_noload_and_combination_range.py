"""E1b の6回目の修正（d9f98d7）の受け入れ試験。検証担当が追加した。
記録は docs/verification/E1b-verifier-6.md。

確かめること: 無負荷 r_load = math.inf（D-065）、組み合わせの範囲（Q = R·√(C/L) ≥ 1e-3、f_ctrl/f0 ≤ 1e11）の
境目と断るときの文、Vin・時間・インピーダンスの尺度を変えても結果が変わらないこと（無次元化の効き目）、範囲の
端での 150 桁の参照との一致、丸めで目標をわずかに下回る候補の文。

期待値の作り方（`circuit_sim` の関数を期待値の計算に使わない）:
  - 150 桁の参照の値（REF_*。このファイルに数字で書いてある）。検証担当が作業ツリーの外で mpmath により
    求めた。5回目の参照（test_e1b_extreme_inputs.py の説明と同じ: 3×3 の拡大行列の指数関数、開ループの多項式、
    |L|² − 1 の実根、根ごとの偏角の和、特性多項式の根）に、r_load = inf を R = 1e80 で計算することを足した
    （1e120 で計算したものとの差は、40 通りで余裕 0、半径 2e-74）。
  - 物理の相似則（実装の式の写しではない）:
      (a) 制御対象は Vin に比例するので、ループは Vin·Kp、Vin·Ki、Vin·Kd だけで決まる。
      (b) 時間の尺度: L、C を k 倍、f_ctrl と f_cross_max を 1/k 倍にすると、Q と f_ctrl/f0 は同じで、
          周波数応答は周波数の軸が 1/k 倍になるだけ。余裕と半径は同じ、Ki は 1/k 倍、Kd は k 倍。
      (c) インピーダンスの尺度: L、R を a 倍、C を 1/a 倍にすると、Vout/D = Vin/(s²LC + sL/R + 1) は同じ。
  - 範囲: 決定記録 D-059（個々の値）と D-065（組み合わせ、inf）の文から書き写した（実装の定数を読まない）。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。

許容差（測る前に決めた根拠）:
  - 「満たす」は 目標 − 1e-6° 以上、安定は半径 1 − 1e-9 以下（D-057）。
  - 150 桁の値との一致: 余裕 1e-6°（D-057 の「満たす」の許し）、半径 1e-12（ε = 1e-9 の 1/1000。D-062 は
    ε が丸めより大きいことを求めるので、丸めがこれを超えれば ε の根拠が崩れる）。
  - 尺度の相似: 余裕 1e-9°、半径 1e-12、ゲイン相対 1e-9。尺度だけを変えた回路の違いは入力の丸め（相対 1e-16）
    だけなので、これより大きい差は、尺度によって丸めが増える（5回目の重大と同じ種類）ことを意味する。
  - 無負荷と大きい R: Q = 1e10 との差は、制御対象の応答の R による差（相対 1/Q）から余裕で 1e-8° 程度。
    許し 1e-6°（「満たす」の判定が入れ替わらない大きさ）。R = 1e30 との差は 1e-9°。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest

from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

MEETS_TOL = 1e-6          # D-057
STABILITY_EPS = 1e-9      # D-057
REF_PM_TOL = 1e-6
REF_RADIUS_TOL = 1e-12
SCALE_PM_TOL, SCALE_RADIUS_TOL, SCALE_GAIN_REL = 1e-9, 1e-12, 1e-9
Q_MIN, RATIO_MAX = 1e-3, 1e11          # D-065


def _run(plant, f_ctrl, **opts):
    """（結果の名前、ゲインか最良の候補、文）。想定外の例外はそのまま上げる。"""
    try:
        return "ok", buck_voltage_mode_pid(*plant, f_ctrl, **opts), ""
    except GainDesignError as e:
        return e.reason, e.best, str(e)


# ---------------------------------------------------------------------------------------------------
# 1. 無負荷 r_load = inf（D-065）
# ---------------------------------------------------------------------------------------------------

NOLOAD_CIRCUITS = [
    ((24.0, 22e-6, 100e-6), 200e3, {}),
    ((24.0, 47e-6, 100e-6), 40e3, {}),
    ((12.0, 4.7e-6, 22e-6), 1e6, {}),
    ((48.0, 10e-6, 470e-6), 500e3, {"phase_margin_deg": 45.0, "filter_ratio": 0.0}),
    ((5.0, 1e-6, 10e-6), 2e6, {"zero_ratio": 0.2, "zero_damping": 2.0, "phase_margin_deg": 60.0}),
    ((24.0, 22e-6, 100e-6), 200e3, {"f_cross_max": 6e3, "phase_margin_deg": 50.0}),
    ((1e9, 1e-12, 1e6), 1e12, {"zero_ratio": 1.0, "zero_damping": 2.0, "filter_ratio": 0.0}),
]


@pytest.mark.parametrize("value", [math.inf, float("inf"), np.inf, np.float64("inf"), np.float32("inf"),
                                   np.longdouble("inf")], ids=lambda v: type(v).__name__)
def test_no_load_is_accepted_as_infinity_in_every_numeric_form(value):
    plant, f_ctrl, opts = NOLOAD_CIRCUITS[0]
    result, g, _ = _run((*plant, value), f_ctrl, **opts)
    result0, g0, _ = _run((*plant, math.inf), f_ctrl, **opts)
    assert result == result0 == "ok"
    assert g == g0


@pytest.mark.parametrize("circuit", NOLOAD_CIRCUITS, ids=lambda c: f"L{c[0][1]:g}-f{c[1]:g}-{len(c[2])}")
def test_no_load_matches_the_large_load_limit(circuit):
    """inf と、Q = 1e10・1e16 の R と、R = 1e30 で、理由、「満たす」の文、余裕、半径が一致する。"""
    (v_in, ind, cap), f_ctrl, opts = circuit
    z0 = math.sqrt(ind / cap)
    result, g, msg = _run((v_in, ind, cap, math.inf), f_ctrl, **opts)
    assert result != "invalid_argument"
    for r, pm_tol in ((1e30, 1e-9), (1e16 * z0, 1e-9), (1e10 * z0, 1e-6)):
        if not 1e-6 <= r <= 1e30:
            continue
        result_r, g_r, msg_r = _run((v_in, ind, cap, r), f_ctrl, **opts)
        assert result_r == result
        assert (g is None) == (g_r is None)
        if g is not None:
            assert g.phase_margin_deg == pytest.approx(g_r.phase_margin_deg, abs=pm_tol)
            assert g.pole_radius == pytest.approx(g_r.pole_radius, abs=1e-10)
            assert ("は満たす）" in msg) == ("は満たす）" in msg_r)


@pytest.mark.parametrize("value", [-math.inf, math.nan, 1e31, math.nextafter(1e30, math.inf), 10**400,
                                   10**31], ids=repr)
def test_other_infinite_or_too_large_loads_are_still_refused(value):
    """inf のほかは D-059 のまま: −inf、NaN、1e30 Ω より大きい有限の値、倍精度にできない整数は断る。"""
    result, g, msg = _run((24.0, 22e-6, 100e-6, value), 200e3)
    assert result == "invalid_argument" and g is None
    assert "r_load" in msg and "math.inf" in msg          # 無負荷の書き方を文で示す


# （回路、制御周波数、設計の引数、結果、150 桁の参照の余裕 [deg]、150 桁の参照の半径 − 1）
REF_NOLOAD = [
    ((1.1212190215790915e-06, 1.184500234522089e-10, 0.4579727069422118, math.inf), 6461028802.367421,
     {"zero_ratio": 9.086138443743859e-05, "zero_damping": 0.31808124867452037, "filter_ratio": 0.05,
      "phase_margin_deg": 28.15388392209532}, "ok", 28.153883922095353, -1.0134988265050608e-09),
    ((0.4380711728843579, 33.91230896958526, 4.106929355696576e-07, math.inf), 78366.48013316862,
     {"zero_ratio": 3.5966462956133764e-05, "zero_damping": 1.0, "phase_margin_deg": 1.0},
     "no_stability_margin", 0.9999999999999674, -9.969649660412987e-10),
    ((31573.41514404352, 1.8613933066547327e-06, 5415.795323035799, math.inf), 37.05123852347625,
     {"zero_damping": 0.6081777256278372, "filter_ratio": 1000.0}, "margin_not_reached",
     -58.489642358702646, 0.0764993296596845),
]


# ---------------------------------------------------------------------------------------------------
# 2. 範囲の端と、組み合わせの範囲の境目の近くでの、150 桁の参照との一致
# ---------------------------------------------------------------------------------------------------

REF_EXTREME = [
    # Vin が範囲の上の端の近く（2.2e8 V）、f_ctrl/f0 = 5.9e8、半径 1 − 1.03e-9 で返す
    ((222929161.51014268, 171.2240885456152, 1.8772272520054358e-06, 255.78880093649025), 5211391340.219237,
     {"zero_damping": 0.19300487140296424, "filter_ratio": 25.09632298550964}, "ok",
     54.99999999999998, -1.0328531632191568e-09),
    # Q = 1.009e-3（組み合わせの範囲の下の境目の近く）、Vin 1e7 V
    ((10063816.187910631, 0.3289904791046857, 1.8251002750281826e-05, 0.13549635851512634),
     175394.08902860308,
     {"zero_ratio": 1.0, "zero_damping": 1.0167567991571747, "filter_ratio": 0.0}, "ok",
     55.00000000000013, -0.0012637200436181198),
    ((56920288.25511372, 7.877777975449459e-07, 0.0017519787239821394, 2.154187012711639e-05),
     316116546.23518264,
     {"zero_ratio": 0.0004539968065674162, "zero_damping": 0.0013852812064571176}, "ok",
     55.00000000000004, -7.635863901143256e-09),
    # Q = 1.03e-3 で、別の交差の余裕が目標に届かない
    ((36.23199828067549, 2.358963148562926e-06, 1.405629920202053, 1.329444652936287e-06),
     1338.7376247505244,
     {}, "margin_below_target", 53.6944178738239, -0.03240504371592412),
    # f_ctrl/f0 = 4.3e9、2.9e9、1.9e9（組み合わせの範囲の上の境目に近い側）
    ((126611.62675675008, 0.019237486913489504, 40.57363023494657, 4.6976636415694265e+29),
     778933683.3395424,
     {"zero_ratio": 1.0, "zero_damping": 0.832296380930362, "filter_ratio": 0.1,
      "phase_margin_deg": 33.46935208831961}, "ok", 33.46935208831959, -1.2094319055422902e-09),
    ((30.360298309377708, 0.017468099414773553, 0.0019244261437961004, 0.023744500797251877),
     79559816227.15738,
     {"zero_ratio": 1.0, "zero_damping": 0.48571869379008786}, "ok",
     55.00000000000001, -1.0529728365631576e-09),
    ((126050.37567874644, 0.03702589346242061, 15199.572080584097, 1.108141706559441e+21),
     12522124.340761222,
     {"zero_ratio": 1.0, "filter_ratio": 1.0, "phase_margin_deg": 1.0}, "ok",
     0.9999999999999853, -1.0098916737915555e-09),
    # L、C がとても小さく、f_ctrl 2.5e10 Hz
    ((22.490984242903046, 7.221960056142492e-11, 1.7079294338464192e-10, 0.010013719109036742),
     25008964072.808167,
     {"zero_ratio": 6.023747916594829e-06, "zero_damping": 1.132776130164266,
      "phase_margin_deg": 42.77176233787499}, "ok", 42.771762337875, -1.0001262529286834e-09),
    # 安定の余裕が無いので断る（参照の半径 1 − 9.99e-10。境目のすぐ内側）
    ((1639.2585432937951, 6.0227568007259575e-09, 257966.58980076556, 10.67252426391341),
     3810271343.9848466,
     {"phase_margin_deg": 0.001}, "no_stability_margin", 0.0010000000000534577, -9.987481131562594e-10),
]


def _check_against_reference(case):
    plant, f_ctrl, opts, expected, ref_pm, ref_r = case
    target = opts.get("phase_margin_deg", 55.0)
    result, g, msg = _run(plant, f_ctrl, **opts)
    assert result == expected
    assert g.phase_margin_deg == pytest.approx(ref_pm, abs=REF_PM_TOL)
    assert g.pole_radius - 1.0 == pytest.approx(ref_r, abs=REF_RADIUS_TOL)
    if result == "ok":
        assert ref_pm >= target - MEETS_TOL and ref_r <= -STABILITY_EPS
        assert g.kp >= 0.0 and g.kd >= 0.0
    else:
        assert ("は満たす）" in msg) == (ref_pm >= target - MEETS_TOL)
        assert ("（1 未満で安定）" in msg) == (ref_r <= -STABILITY_EPS)
        assert ("安定の余裕が無い）" in msg) == (abs(ref_r) < STABILITY_EPS)
        assert ("（1 以上で不安定）" in msg) == (ref_r >= STABILITY_EPS)


@pytest.mark.parametrize("case", REF_NOLOAD, ids=lambda c: c[3])
def test_no_load_agrees_with_150_digit_reference(case):
    _check_against_reference(case)


@pytest.mark.parametrize("case", REF_EXTREME, ids=lambda c: f"Vin{c[0][0]:.2g}-{c[3]}")
def test_range_ends_agree_with_150_digit_reference(case):
    _check_against_reference(case)


# ---------------------------------------------------------------------------------------------------
# 3. 組み合わせの範囲の境目（D-065）
# ---------------------------------------------------------------------------------------------------

def test_q_boundary_is_inclusive_and_just_below_is_refused_naming_q():
    """L = C = 1e-6（√(L/C) = 1 ちょうど）で R = 1e-3 は Q = 1e-3 ちょうど: 受ける。そのすぐ下は断り、
    文に Q と組み合わせを書く。"""
    plant = (24.0, 1e-6, 1e-6)
    result, _, _ = _run((*plant, 1e-3), 20e6)
    assert result != "invalid_argument"
    result, g, msg = _run((*plant, math.nextafter(1e-3, 0.0)), 20e6)
    assert result == "invalid_argument" and g is None
    assert "組み合わせ" in msg and "Q" in msg and "f_ctrl/f0" not in msg


def test_ratio_boundary_is_inclusive_and_just_above_is_refused_naming_the_ratio():
    """L = C = 1（√(LC) = 1）で f_ctrl/f0 = 2π·f_ctrl。1e11 以下は受け、そのすぐ上は断る。"""
    f = 1e11 / (2.0 * math.pi)
    while 2.0 * math.pi * f > RATIO_MAX:
        f = math.nextafter(f, 0.0)
    result, _, _ = _run((24.0, 1.0, 1.0, 6.0), f)
    assert result != "invalid_argument"
    f_out = math.nextafter(f, math.inf)
    while 2.0 * math.pi * f_out <= RATIO_MAX:
        f_out = math.nextafter(f_out, math.inf)
    result, g, msg = _run((24.0, 1.0, 1.0, 6.0), f_out)
    assert result == "invalid_argument" and g is None
    assert "組み合わせ" in msg and "f_ctrl/f0" in msg and "Q =" not in msg


def test_both_combinations_outside_are_named_together():
    result, _, msg = _run((24.0, 1.0, 1.0, 1e-6), 1e12)
    assert result == "invalid_argument"
    assert "Q" in msg and "f_ctrl/f0" in msg


def test_round5_low_q_reproduction_is_refused_as_outside_the_combination_range():
    """5回目の軽微1の回路（Q = 4.5e-10）は、D-065 で組み合わせの範囲の外として断る
    （余裕を誤って返さない）。"""
    plant = (10.81936124982362, 0.12746071594513633, 5.631368828574676e-09, 2.1410342282585894e-06)
    opts = {"zero_ratio": 0.6474596869314334, "zero_damping": 1.569212487698608, "filter_ratio": 0.0,
            "phase_margin_deg": 22.628123695975038}
    result, g, msg = _run(plant, 20412490.22544481, **opts)
    assert result == "invalid_argument" and g is None and "Q" in msg


@pytest.mark.xfail(strict=True, reason="軽微（文の数字の書き方）: 組み合わせの範囲の外の値を 3 桁で書くので、"
                                       "境目のすぐ外では「0.001 以上にする（現在 0.001）」「1e+11 以下にする"
                                       "（現在 1e+11）」と、外れた値が境目と同じ数字に見える")
@pytest.mark.parametrize("plant_f", [((24.0, 1e-6, 1e-6, 0.9996e-3), 20e6),
                                     ((24.0, 1.0, 1.0, 6.0), 1.0004e11 / (2.0 * math.pi))],
                         ids=["Q", "ratio"])
def test_value_just_outside_the_combination_range_is_not_shown_as_the_limit(plant_f):
    plant, f = plant_f
    result, _, msg = _run(plant, f)
    assert result == "invalid_argument"
    m = re.search(r"は ([\d.e+\-]+) (以上|以下)にする（現在 ([\d.e+\-]+)", msg)
    assert m and float(m.group(3)) != float(m.group(1))


# ---------------------------------------------------------------------------------------------------
# 4. 尺度を変えても結果が変わらない（無次元化の効き目）
# ---------------------------------------------------------------------------------------------------

SCALE_CIRCUITS = [
    ((22e-6, 100e-6, 6.0), 200e3, {}),
    ((47e-6, 100e-6, 1e16), 40e3, {}),
    ((22e-6, 100e-6, 6.0), 200e3, {"zero_ratio": 1e-7, "phase_margin_deg": 55.04}),
    # 5回目の重大1の回路（Vin 4e5 V などで、安定の余裕が無い設計を成功として返した）
    ((0.16160871635527527, 0.4632826986189658, 1e30), 22803.275120932136,
     {"zero_ratio": 5.316531590111157e-07, "zero_damping": 0.814948799243281}),
    ((7.687175724559705e-08, 232.59255161739404, 30806.96514674552), 1060587.972794125,
     {"zero_ratio": 3.5986891371643046e-05, "zero_damping": 1.84838158173341}),
    ((0.0003486222953146516, 0.03046573234076631, 36.301777798882036), 3831461.8219012544,
     {"zero_ratio": 1.1490533679001927e-05, "zero_damping": 1.099325229684776}),
    ((1e-12, 1e6, 6.0), 1e12, {"zero_ratio": 1.0, "zero_damping": 2.0, "filter_ratio": 0.0}),
    ((2.358963148562926e-06, 1.405629920202053, 1.329444652936287e-06), 1338.7376247505244, {}),
    ((1.8613933066547327e-06, 5415.795323035799, math.inf), 37.05123852347625,
     {"zero_damping": 0.6081777256278372, "filter_ratio": 1000.0}),
]
_SCALE_IDS = [f"{i}-{c[1]:.3g}Hz" for i, c in enumerate(SCALE_CIRCUITS)]


def _same_design(a, b, ki_factor=1.0, kd_factor=1.0, f_factor=1.0):
    (ra, ga, _), (rb, gb, _) = a, b
    assert ra == rb
    assert (ga is None) == (gb is None)
    if ga is None:
        return
    assert ga.phase_margin_deg == pytest.approx(gb.phase_margin_deg, abs=SCALE_PM_TOL)
    assert ga.pole_radius == pytest.approx(gb.pole_radius, abs=SCALE_RADIUS_TOL)
    assert ga.kp == pytest.approx(gb.kp, rel=SCALE_GAIN_REL)
    assert ga.ki == pytest.approx(gb.ki * ki_factor, rel=SCALE_GAIN_REL)
    assert ga.kd == pytest.approx(gb.kd * kd_factor, rel=SCALE_GAIN_REL)
    assert ga.f_cross == pytest.approx(gb.f_cross * f_factor, rel=SCALE_GAIN_REL)


@pytest.mark.parametrize("circuit", SCALE_CIRCUITS, ids=_SCALE_IDS)
@pytest.mark.parametrize("v_in", [1e-6, 3.7e-3, 1.0, 24.0, 4.12e5, 1.6e7, 1e9])
def test_result_does_not_depend_on_vin(circuit, v_in):
    """理由、余裕、半径、交差周波数は Vin によらず、ゲインは 1/Vin に比例する。"""
    (ind, cap, r), f_ctrl, opts = circuit
    base = _run((1.0, ind, cap, r), f_ctrl, **opts)
    result, g, msg = _run((v_in, ind, cap, r), f_ctrl, **opts)
    if g is not None:
        g = type(g)(g.kp * v_in, g.ki * v_in, g.kd * v_in, g.t_filter, g.f_cross, g.phase_margin_deg,
                    g.pole_radius)
    _same_design((result, g, msg), base)


@pytest.mark.parametrize("circuit", SCALE_CIRCUITS, ids=_SCALE_IDS)
@pytest.mark.parametrize("k", [1e-6, 0.37, 1e5])
def test_result_does_not_depend_on_the_time_scale(circuit, k):
    (ind, cap, r), f_ctrl, opts = circuit
    if not (1e-12 <= ind * k <= 1e4 and 1e-15 <= cap * k <= 1e6 and 1e-3 <= f_ctrl / k <= 1e12):
        pytest.skip("尺度を変えた値が D-059 の範囲の外")
    scaled = dict(opts)
    if "f_cross_max" in scaled:
        scaled["f_cross_max"] /= k
    base = _run((24.0, ind, cap, r), f_ctrl, **opts)
    moved = _run((24.0, ind * k, cap * k, r), f_ctrl / k, **scaled)
    _same_design(moved, base, ki_factor=1.0 / k, kd_factor=k, f_factor=1.0 / k)


@pytest.mark.parametrize("circuit", SCALE_CIRCUITS, ids=_SCALE_IDS)
@pytest.mark.parametrize("a", [1e-4, 3.0, 1e3])
def test_result_does_not_depend_on_the_impedance_scale(circuit, a):
    (ind, cap, r), f_ctrl, opts = circuit
    if not (1e-12 <= ind * a <= 1e4 and 1e-15 <= cap / a <= 1e6 and (r == math.inf or 1e-6 <= r * a <= 1e30)):
        pytest.skip("尺度を変えた値が D-059 の範囲の外")
    base = _run((24.0, ind, cap, r), f_ctrl, **opts)
    moved = _run((24.0, ind * a, cap / a, r * a), f_ctrl, **opts)
    _same_design(moved, base)


# ---------------------------------------------------------------------------------------------------
# 5. 丸めで目標をわずかに下回る候補の文（D-057、軽微2 の書き方）
# ---------------------------------------------------------------------------------------------------

# （探索の上端 f_cross_max [Hz]、150 桁の参照の上端の余裕 [deg]、目標 [deg]）。
# 回路は (24 V, 22 µH, 100 µF, 6 Ω)、200 kHz。上端に向かって余裕が下がるので、目標を上端の余裕より
# 少し上（1e-6° 以内）に置くと、上端まで「満たす」区間が続く。2つ目は、余裕 63.44031032… をどの桁で
# 丸めても目標 63.4403104 以上に見えない例（書き方の最後の分かれ: 判定が「満たす」なので、目標と同じ
# 数字で書く）。
TOP_EDGE = [(1.5 / math.sqrt(22e-6 * 100e-6) / (2.0 * math.pi), 61.277059601632, 61.277059601632 + 5e-7),
            (4577.696121753016, 63.440310321624179736, 63.4403104)]


@pytest.mark.parametrize("case", TOP_EDGE, ids=["5e-7", "7.8e-8"])
def test_candidate_just_below_the_target_is_said_to_meet_it_without_a_smaller_number(case):
    """最良の候補（上端）の余裕は目標より小さいが 1e-6° 以内なので「満たす」と書き、「足りない」と書かず、
    書いた余裕の数字は書いた目標の数字より小さく見えない。目標はちょうどの値で書く。"""
    f_cross_max, ref_top, target = case
    result, g, msg = _run((24.0, 22e-6, 100e-6, 6.0), 200e3, f_cross_max=f_cross_max, phase_margin_deg=target)
    assert result == "crossover_at_search_edge"
    assert g.phase_margin_deg == pytest.approx(ref_top, abs=1e-9)
    assert 0.0 < target - ref_top < MEETS_TOL
    assert "は満たす）" in msg and "足りない" not in msg
    m = re.search(r"位相余裕 (-?[\d.]+)°（目標の (-?[\d.]+)° は満たす）", msg)
    assert m and float(m.group(1)) >= float(m.group(2))
    assert float(m.group(2)) == target


# ---------------------------------------------------------------------------------------------------
# 6. 零点の減衰がほぼ 0 で f_ctrl/f0 が大きいとき、零点のすぐ横の交差の余裕の丸め（軽微）
# ---------------------------------------------------------------------------------------------------

# 範囲の端の組（ζz = 5e-324、κ = 1、Q = 0.5、f_ctrl/f0 = 1e11 − 1e-1）。PID の零点が単位円の上にあり、
# そのすぐ横（共振 0.1591549430772 Hz）に交差がある。150 桁と 300 桁の参照で、その交差の余裕は目標を
# 1.2695e-5° 下回る（ほかの交差は目標 − 6e-14°、+142.5°）。閉ループの極の半径は 1 − 2.0e-21。
NEAR_NOTCH = ((24.0, 1e4, 1e-4, 5000.0), 15915494309.17362,
              {"zero_ratio": 1.0, "zero_damping": 5e-324, "filter_ratio": 0.0,
               "phase_margin_deg": 18.743734753558844})
NEAR_NOTCH_REF_SHORT = 1.2695081043290202e-05


def test_near_notch_candidate_is_refused_for_no_stability_margin():
    """断る理由の本体（安定の余裕が無い）は正しい: ゲインは返さない。"""
    plant, f_ctrl, opts = NEAR_NOTCH
    result, g, msg = _run(plant, f_ctrl, **opts)
    assert result in ("no_stability_margin", "margin_below_target") and "安定の余裕が無い" in msg


@pytest.mark.xfail(strict=True, reason="軽微: ζz ≲ 1e-10 かつ f_ctrl/f0 ≳ 1e9 で、零点のすぐ横の交差の余裕の"
                                       "丸めが最大 2.6e-5°（D-057 の許し 1e-6° より大きい）。断る候補の文で、"
                                       "目標を 1.27e-5° 下回るのに「位相余裕は目標を満たすが」"
                                       "「は満たす」と書く")
def test_near_notch_candidate_short_of_the_target_is_not_said_to_meet_it():
    plant, f_ctrl, opts = NEAR_NOTCH
    result, g, msg = _run(plant, f_ctrl, **opts)
    assert NEAR_NOTCH_REF_SHORT > MEETS_TOL
    assert "は満たす）" not in msg and "位相余裕は目標を満たすが" not in msg
