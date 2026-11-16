"""E1b の4回目の修正（f55defe）の受け入れ試験。検証担当が追加した。
記録は docs/verification/E1b-verifier-4.md。

期待値は、test_e1b_crossings.py の参照（Ref: 植物は Δ = Ad − I を φ1 から、開ループは z − 1 = expm1(jωTs)
で組み、交差は代数と格子の和、閉ループは 6×6 の固有値）と、このファイルで足す経路で作る。`circuit_sim` の
関数（tuning の内部、loop_response など）を期待値の計算に使わない。
このファイルで足す経路:
  - 低い周波数の交差: Ref の格子の下端（1e-4·ω0）より下を、1e-30·ω0 から等比で調べ、brentq で詰める
    （積分がとても遅いとき、交差が 1e-9 Hz や 1e-16·ω0 に来る）。
  - 負荷抵抗の極限: Q = R·√(C/L) が 1e10 を超えると、交差の近くの植物の応答は R でほとんど変わらない
    （相対で 1/Q 程度）。同じ回路の R と、Q = 1e10 にした R で、理由と余裕が一致するはず（参照の位相の向きに
    頼らない）。
  - 条件つき安定: 連続の位相が −180° − 360k を横切る点で |L| > 1 なら、ゲインが下がると不安定になる。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。

許容差（測る前に決めた根拠）:
  - 余裕の一致 0.01°（PM_TOL）と、返したゲインの 目標 − 2e-6°（TARGET_TOL）は test_e1b_crossings.py と同じ。
  - 「満たす」は 目標 − 1e-6° 以上（D-057）。
  - 安定と言える半径は 1 − 1e-9 以下（D-057）。参照の固有値の丸め（1e-13 程度）は、その 1e4 分の 1 なので、
    参照の半径を同じ境目で比べてよい。
  - 極限の一致: 余裕 1e-3°。Q = 1e10 と 1e15 の違いが交差の近くの応答に与える差は相対 1e-10 程度で、余裕では
    1e-8° 程度。1e-3° はその 1e5 倍で緩く、360° の取り違えや別の候補（余裕が数度以上違う）は必ず見分ける。
"""
from __future__ import annotations

import importlib.util
import math
import re
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import brentq

from circuit_sim.tuning import GainDesignError, buck_voltage_mode_pid

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("e1b_crossings_reference", _HERE / "test_e1b_crossings.py")
crossings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crossings)
Ref, _design = crossings.Ref, crossings._design
PM_TOL, TARGET_TOL = crossings.PM_TOL, crossings.TARGET_TOL
MEETS_TOL = 1e-6          # D-057
STABILITY_EPS = 1e-9      # D-057
LIMIT_PM_TOL = 1e-3


def _low_crossings(ref, w_top):
    """Ref の格子の下端より下の交差 [(ω, −1 からの角度)]（位相は −90° の近くなので主値でよい）。"""
    w = np.geomspace(1e-30 * ref.w0, w_top, 6000)
    h = np.log(np.abs(ref(w)))
    out = []
    for i in np.nonzero((h[:-1] > 0) != (h[1:] > 0))[0]:
        def f(lw):
            return math.log(abs(ref(np.array([math.exp(lw)]))[0]))
        a, b = math.log(w[i]), math.log(w[i + 1])
        if f(a) * f(b) <= 0:
            wc = math.exp(brentq(f, a, b, xtol=1e-14))
            v = ref(np.array([wc]))[0]
            out.append((wc, 180.0 - abs(math.degrees(math.atan2(v.imag, v.real)))))
    return out


def _min_margin(ref):
    """参照の余裕の最小（Ref.margins と、その下の低い周波数の交差）。"""
    lows = [m for _, m in _low_crossings(ref, 1e-4 * ref.w0)]
    ms = ref.margins()
    return min([min(m[1], m[2]) for m in ms] + lows) if ms or lows else math.inf


def _says_meets(err):
    return "は満たす）" in str(err)


# ------------------------------------------------------------------ 1. 負荷抵抗がとても大きい

def test_large_load_reproduction_succeeds_with_55_deg_and_radius_0975():
    """前回までの不具合の再現例（R = 1e11 Ω、植物の零点が z = −1 から 1.7e-13）: 成功し、余裕 55.000°、
    極の半径 0.975。参照の交差は1つで、その余裕も 55°。"""
    plant, f_ctrl = (24.0, 47e-6, 100e-6, 1e11), 200e3
    g = buck_voltage_mode_pid(*plant, f_ctrl)
    ref = Ref(g, *plant, f_ctrl)
    ms = ref.margins()
    assert len(ms) == 1 and ms[0][0] == pytest.approx(g.f_cross, rel=1e-6)
    assert g.phase_margin_deg == pytest.approx(55.0, abs=1e-6)
    assert ms[0][1] == pytest.approx(55.0, abs=TARGET_TOL) and ms[0][2] == pytest.approx(55.0, abs=TARGET_TOL)
    assert g.pole_radius == pytest.approx(ref.radius(), rel=1e-9)
    assert round(g.pole_radius, 3) == 0.975 and ref.radius() <= 1.0 - STABILITY_EPS


CIRCUITS = [(24.0, 22e-6, 100e-6, 100e3), (24.0, 47e-6, 100e-6, 200e3), (12.0, 4.7e-6, 22e-6, 1e6)]


def _result(v_in, ind, cap, r, f_ctrl, **opts):
    best, err = _design((v_in, ind, cap, r), f_ctrl, **opts)
    return (err.reason if err else "ok"), best, err


@pytest.mark.parametrize("r_load", [1e7, 1e9, 1e11, 1e13, 1e15])
@pytest.mark.parametrize("circuit", CIRCUITS, ids=lambda c: f"L{c[1]:g}-f{c[3]:g}")
def test_large_load_matches_its_high_q_limit_and_the_reference(circuit, r_load):
    """R = 1e7〜1e15 Ω: 結果（理由と余裕）は、同じ回路で Q = 1e10 にしたときと一致する（極限）。
    返したゲインは参照で余裕が目標以上・半径 1 − 1e-9 以下、最良の候補の余裕は参照と合う（Q < 1e13 の範囲。
    参照の格子が共振を分けられる）。"""
    v_in, ind, cap, f_ctrl = circuit
    r_limit = 1e10 / math.sqrt(cap / ind)
    reason, best, err = _result(v_in, ind, cap, r_load, f_ctrl)
    reason0, best0, _ = _result(v_in, ind, cap, r_limit, f_ctrl)
    assert reason == reason0
    assert best.phase_margin_deg == pytest.approx(best0.phase_margin_deg, abs=LIMIT_PM_TOL)
    if r_load * math.sqrt(cap / ind) < 1e13:
        ref = Ref(best, v_in, ind, cap, r_load, f_ctrl)
        pm = _min_margin(ref)
        assert best.phase_margin_deg == pytest.approx(pm, abs=PM_TOL)
        if err is None:
            assert pm >= 55.0 - TARGET_TOL and ref.radius() <= 1.0 - STABILITY_EPS


@pytest.mark.parametrize("r_load", [1e17, 1e20])
@pytest.mark.parametrize("circuit", [CIRCUITS[0], CIRCUITS[2]], ids=lambda c: f"L{c[1]:g}-f{c[3]:g}")
def test_huge_load_matches_its_high_q_limit_and_never_says_meets_falsely(circuit, r_load):
    v_in, ind, cap, f_ctrl = circuit
    r_limit = 1e10 / math.sqrt(cap / ind)
    reason, best, err = _result(v_in, ind, cap, r_load, f_ctrl)
    reason0, best0, _ = _result(v_in, ind, cap, r_limit, f_ctrl)
    if err is not None and _says_meets(err):
        # 「満たす」と書くなら、−1 からの角度（位相の向きに頼らない）も目標以上で、閉ループは安定のはず
        ref = Ref(best, v_in, ind, cap, r_load, f_ctrl)
        assert min(m[2] for m in ref.margins()) >= 55.0 - TARGET_TOL and ref.radius() < 1.0
    assert reason == reason0
    assert best.phase_margin_deg == pytest.approx(best0.phase_margin_deg, abs=LIMIT_PM_TOL)


# ------------------------------------------------------------------ 2. PID の零点の重根の近く

SLOW_DOUBLE = ((77.13396730251614, 2.115134199875259e-05, 1.2061149392104576e-05, 16.567279975990026),
               9094234.619934775,
               {"zero_ratio": 3.299958554608792e-07, "zero_damping": 1.0, "filter_ratio": 0.0,
                "phase_margin_deg": 90.67692513462626, "f_cross_max": 26706929.13121755})


def test_double_zero_with_a_very_slow_integral_keeps_the_low_frequency_crossing():
    """実装担当の照合で見つかった例（ζz = 1、zero_ratio 3.3e-7）: 参照は 2.0054e-9 Hz に交差を見つけ、
    その余裕は 90.00007°（目標 90.68° に 0.68° 足りない）。最良の候補の余裕はそれと合い、「足りない」と書き
    「満たす」と書かない。半径は 1 + 3e-15 で、安定とも不安定とも言わない。"""
    plant, f_ctrl, opts = SLOW_DOUBLE
    best, err = _design(plant, f_ctrl, **opts)
    assert err is not None and err.reason != "invalid_argument"
    ref = Ref(best, *plant, f_ctrl)
    lows = _low_crossings(ref, 1e-4 * ref.w0)
    assert len(lows) == 1 and lows[0][0] / (2 * math.pi) == pytest.approx(2.0054e-9, rel=1e-3)
    assert lows[0][1] == pytest.approx(90.00007, abs=1e-5)
    assert best.phase_margin_deg == pytest.approx(_min_margin(ref), abs=1e-6)
    text = str(err)
    assert "足りない" in text and not _says_meets(err)
    assert abs(ref.radius() - 1.0) < STABILITY_EPS
    assert "安定の余裕が無い" in text and "1 未満で安定" not in text


@pytest.mark.parametrize("zeta", [1.0 - 1e-12, 1.0, 1.0 + 1e-12, 1.0 + 1e-6, 1.5, 2.0])
@pytest.mark.parametrize("kappa", [1e-4, 0.3, 1.0])
def test_double_and_real_zeros_return_true_designs(kappa, zeta):
    """零点の重根の近く（ζz = 1 ± 1e-12 など）と2つの実根（ζz ≦ 2）、比 κ ≦ 1: 返したゲインは参照（低い
    周波数の交差を含む）で余裕が目標以上・半径 1 − 1e-9 以下。失敗なら、最良の候補の余裕が参照と合い、
    「満たす」は D-057 の判定どおり。"""
    plant, f_ctrl = (24.0, 22e-6, 100e-6, 6.0), 100e3
    best, err = _design(plant, f_ctrl, zero_ratio=kappa, zero_damping=zeta)
    ref = Ref(best, *plant, f_ctrl)
    pm = _min_margin(ref)
    assert best.phase_margin_deg == pytest.approx(pm, abs=PM_TOL)
    if err is None:
        assert pm >= 55.0 - TARGET_TOL and ref.radius() <= 1.0 - STABILITY_EPS
    else:
        assert _says_meets(err) == (pm >= 55.0 - MEETS_TOL)


@pytest.mark.parametrize("opts", [{"zero_ratio": 1.0, "zero_damping": 1.0},
                                  {"zero_ratio": 1.0, "zero_damping": 2.0},
                                  {"zero_ratio": 0.5, "zero_damping": 2.0}])
def test_range_ends_of_d057_give_designs_that_are_not_conditionally_stable(opts):
    """D-057 の範囲の端（κ = 1 と ζz = 1・2）: 設計でき（零点を共振に置くと位相の進みが小さいので目標 30°）、
    連続の位相が −180° − 360k を横切る点で |L| > 1 になるところが無い（条件つき安定でない。κ > 1 を断る理由と
    同じ物差し）。"""
    plant, f_ctrl = (24.0, 47e-6, 100e-6, 6.0), 100e3
    g = buck_voltage_mode_pid(*plant, f_ctrl, phase_margin_deg=30.0, **opts)
    ref = Ref(g, *plant, f_ctrl)
    w, val, ph = ref._phase_grid(1e-4 * ref.w0, 0.9999 * math.pi * f_ctrl)
    band = np.floor((ph + math.pi) / (2 * math.pi))
    cross = np.nonzero(band[1:] != band[:-1])[0]
    assert all(min(abs(val[i]), abs(val[i + 1])) < 1.0 for i in cross)
    assert _min_margin(ref) >= 30.0 - TARGET_TOL and ref.radius() <= 1.0 - STABILITY_EPS


# ------------------------------------------------------------------ 3. 「満たす」と安定の判定（D-057）

# 6回目の検証で例を取り直した: 前の例（crossings.UNSTABLE_CASE、目標 24.361861916713153°）は、6回目の修正
# （d9f98d7）で丸めの向きが変わり、余裕が目標を 1.4e-14° 上回るようになって、前提（目標を下回る）が崩れた。
# 下の2つは 6回目の修正で目標を 3.0e-12°・6.8e-13° 下回る候補（どちらも no_stability_margin。150 桁の参照でも
# 余裕は目標 − 1e-11° 以内、半径は 1 − 7.2e-11・1 − 4.0e-12 で、「満たす」「安定の余裕が無い」が正しい）。
# 目標をはっきり（5e-7°）下回る候補の文は、丸めの向きに頼らない形で
# test_e1b_noload_and_combination_range.py で確かめる。
WITHIN_ROUNDING = [
    ((42.30613676782444, 1.8287100897010294e-11, 5.3595205566901816e-05, 1.050159124468598e-06),
     66785873.205888286, {"zero_ratio": 5.175900446436682e-07, "filter_ratio": 0.0007893269265332699,
      "phase_margin_deg": 26.420576153971524}),
    ((0.0033965286230973665, 3.4609161273413534e-06, 0.008424209268930087, 0.00033423609517650867),
     28636.478574244513, {"zero_ratio": 5.527456417738679e-07, "zero_damping": 1.1734567796051698,
                          "filter_ratio": 0.0, "phase_margin_deg": 41.114588074864535}),
]


@pytest.mark.parametrize("case", WITHIN_ROUNDING, ids=["3.0e-12", "6.8e-13"])
def test_candidate_within_1e6_deg_of_the_target_is_said_to_meet_it(case):
    """余裕が目標を丸めの分だけ下回る候補は「満たす」と書き、「足りない」と書かない（「目標まで
    0.0000000000001° 足りない」も書かない）。本文の「位相余裕は目標を満たすが」とも食い違わない。"""
    plant, f_ctrl, opts = case
    best, err = _design(plant, f_ctrl, **opts)
    assert 0.0 < opts["phase_margin_deg"] - best.phase_margin_deg < MEETS_TOL
    text = str(err)
    assert _says_meets(err) and "足りない" not in text and "位相余裕は目標を満たすが" in text


def test_genuine_small_shortfall_is_written_with_a_nonzero_digit():
    """本当に 0.0023° 足りない候補: 「足りない」と書き、不足を 0 でない桁まで書く（参照の余裕とも合う）。"""
    plant, f_ctrl = (24.0, 470e-6, 10e-6, 15.793045736999451), 15e3
    opts = {"zero_damping": 0.1653846153846154, "filter_ratio": 0.0, "phase_margin_deg": 30.0}
    best, err = _design(plant, f_ctrl, **opts)
    assert best.phase_margin_deg == pytest.approx(_min_margin(Ref(best, *plant, f_ctrl)), abs=PM_TOL)
    m = re.search(r"目標まで ([\d.]+)° 足りない", str(err))
    assert m and float(m.group(1)) > 0.0
    assert float(m.group(1)) == pytest.approx(30.0 - best.phase_margin_deg, abs=1e-3)
    assert not _says_meets(err)


@pytest.mark.parametrize("plant,f_ctrl", [((24.0, 22e-6, 100e-6, 6.0), 100e3),
                                          ((24.0, 4.7e-6, 1e-3, 1000.0), 1e6)])
def test_radius_within_eps_of_one_is_refused_as_no_stability_margin(plant, f_ctrl):
    """zero_ratio = 1e-9 と 1e-5（積分がとても遅い）: 参照の半径も 1 との差が 1e-9 未満。理由は
    no_stability_margin で、「安定」とも「不安定」とも書かない（3回目の軽微4）。"""
    for kappa in (1e-9, 1e-5):
        best, err = _design(plant, f_ctrl, zero_ratio=kappa)
        assert err is not None and err.reason == "no_stability_margin"
        ref = Ref(best, *plant, f_ctrl)
        assert abs(ref.radius() - 1.0) < STABILITY_EPS
        text = str(err)
        assert "安定の余裕が無い" in text and "1 未満で安定" not in text and "1 以上で不安定" not in text


def test_slow_but_resolvable_integral_is_returned_as_stable():
    """zero_ratio = 1e-4、1 MHz: 参照の半径は 1 − 2.8e-9（境目 1 − 1e-9 より内側）。返し、余裕は目標以上。"""
    plant, f_ctrl = (24.0, 4.7e-6, 1e-3, 1000.0), 1e6
    g = buck_voltage_mode_pid(*plant, f_ctrl, zero_ratio=1e-4)
    ref = Ref(g, *plant, f_ctrl)
    assert ref.radius() <= 1.0 - STABILITY_EPS and 1.0 - ref.radius() == pytest.approx(2.8e-9, rel=0.05)
    assert _min_margin(ref) >= 55.0 - TARGET_TOL


def test_radius_shown_as_below_one_when_it_says_below_one():
    plant, f_ctrl = (24.0, 470e-6, 10e-6, 6.0), 15e3
    best, err = _design(plant, f_ctrl, zero_ratio=1e-4)
    assert err is not None and 1.0 - 1e-4 < Ref(best, *plant, f_ctrl).radius() <= 1.0 - STABILITY_EPS
    m = re.search(r"極の半径 ([\d.]+)（1 未満で安定）", str(err))
    assert m and float(m.group(1)) < 1.0


# ------------------------------------------------------------------ 4. 探索の上限の制限、numpy の数

@pytest.mark.parametrize("f_cross_max", [49997.5, 5e4, 1e9, 1e300])
def test_limited_upper_bound_is_recorded_in_the_result(f_cross_max):
    """f_cross_max がナイキスト（50 kHz）の 0.9999 倍より上: 返すゲインは制限の値を指定したときと同じで、
    結果に指定した値が残る（3回目の軽微3）。"""
    base = ((24.0, 22e-6, 100e-6, 6.0), 100e3)
    g = buck_voltage_mode_pid(*base[0], base[1], f_cross_max=f_cross_max)
    g0 = buck_voltage_mode_pid(*base[0], base[1], f_cross_max=0.9999 * 50e3)
    assert g == g0 and g.f_cross_max_limited_from == f_cross_max and g0.f_cross_max_limited_from is None
    assert g.f_search_max == pytest.approx(0.9999 * 50e3, rel=1e-12)


def test_limited_upper_bound_is_stated_in_the_failure():
    """失敗の文に、指定した上限と制限した後の上端を書く（「〜9999 Hz」の理由が分かる）。"""
    best, err = _design((24.0, 47e-6, 10e-6, 1000.0), 20e3, f_cross_max=1e9)
    text = str(err)
    assert "指定した上限 1 GHz を、ナイキスト周波数の手前の 9999 Hz に制限した" in text
    assert best.f_cross_max_limited_from == 1e9


def test_unlimited_upper_bound_is_not_recorded():
    _, err = _design((24.0, 47e-6, 10e-6, 1000.0), 20e3, f_cross_max=9000.0)
    assert "制限した" not in str(err)
    g = buck_voltage_mode_pid(24.0, 22e-6, 100e-6, 6.0, 100e3)
    assert g.f_cross_max_limited_from is None


@pytest.mark.parametrize("kind", [np.float64, np.float32, np.int64, np.int32])
def test_numpy_numbers_give_the_same_result_as_python_floats(kind):
    """numpy の数を受け、倍精度に直した値と同じ結果（3回目の軽微1）。整数の型は整数の値だけ。"""
    if kind in (np.int64, np.int32):
        args, same = (kind(24), 22e-6, 100e-6, kind(6), kind(100000)), (24.0, 22e-6, 100e-6, 6.0, 100e3)
        opts = {"phase_margin_deg": kind(50)}
    else:
        args = tuple(kind(x) for x in (24.0, 22e-6, 100e-6, 6.0, 100e3))
        same = tuple(float(x) for x in args)
        opts = {"zero_ratio": kind(0.5), "zero_damping": kind(0.3), "filter_ratio": kind(0.25),
                "phase_margin_deg": kind(50.0)}
    g = buck_voltage_mode_pid(*args, **opts)
    g0 = buck_voltage_mode_pid(*same, **{k: float(v) for k, v in opts.items()})
    assert g == g0 and all(type(x) is float for x in (g.kp, g.ki, g.kd, g.phase_margin_deg))


@pytest.mark.parametrize("value", [np.bool_(True), complex(24.0), np.complex128(24.0), np.array([24.0])])
def test_numpy_bool_complex_and_arrays_are_refused(value):
    with pytest.raises(GainDesignError) as info:
        buck_voltage_mode_pid(value, 22e-6, 100e-6, 6.0, 100e3)
    assert info.value.reason == "invalid_argument" and info.value.best is None
