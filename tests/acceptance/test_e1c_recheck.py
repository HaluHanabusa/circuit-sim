"""E1c の修正（118789c）の再検証の受け入れ試験。検証担当が、実装者の試験とは別に用意した。

記録は docs/verification/E1c-verifier-2.md。前回の試験は test_e1c_followups.py（変えていない）。
期待値は、このファイルに書いた解析解と、回路の素子の値から自分で計算した時定数で作る。
`circuit_sim` の関数を期待値の計算に使わない。
製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest

from circuit_sim import Netlist, Pwm, Spec, expand, simulate

TOL_ANALYTIC = 0.01           # 解析解との差（roadmap.md の既存の基準）
H_SW = 50e-9                  # 100 kHz、1周期200分割の既定の刻み


def product_bug(severity: str, *lines: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"製品の不具合({severity}): " + "".join(lines))


def netlist(*components: dict, ground: str = "0") -> Netlist:
    return Netlist.model_validate({"ground": ground, "components": list(components)})


def comp(cid: str, kind: str, a: str, b: str, value: float | None = None, **kw) -> dict:
    d = {"id": cid, "type": kind, "nodes": [a, b], **kw}
    if value is not None:
        d["value"] = value
    return d


def buck(L: float, load: float, r_off: float | None = None, csw: float | None = None) -> Netlist:
    """降圧（出力は 12 V から始める）。csw はスイッチ節点の容量。"""
    kw = {} if r_off is None else {"r_off": r_off}
    parts = [comp("V1", "V", "in", "0", waveform={"kind": "dc", "value": 24.0}),
             comp("S1", "S", "in", "sw", gate="g", **kw), comp("D1", "D", "0", "sw", **kw),
             comp("L1", "L", "sw", "out", L), comp("C1", "C", "out", "0", 100e-6, ic=12.0),
             comp("R1", "R", "out", "0", load)]
    if csw:
        parts.append(comp("Csw", "C", "sw", "0", csw))
    return netlist(*parts)


GATE = [Pwm(name="g", frequency=1e5, duty=0.5)]


def oscillation_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if "回路の振動" in n]


# ================================================================ |λ| で比べる: 逆向きの崩れが無いこと

@pytest.mark.parametrize("L", [47e-6, 470e-6, 2.5e-3, 10e-3])
@pytest.mark.parametrize("r_off", [1e4, 1e5, 1e6, 1e9])
@pytest.mark.parametrize("load", [6.0, 100.0, 1e4])
def test_fake_off_resistance_modes_never_give_an_oscillation_note(L, r_off, load):
    """スイッチ節点に容量が無ければ、オフ・オフの区間の成分は L / r_off（実数、見かけの成分）だけで、
    振動は無い。|λ| で比べても、振動の注意は出ない（48 回路。実測 0 件）。"""
    res = simulate(buck(L, load, r_off), 2e-4, gates=GATE)
    assert oscillation_notes(res.notes) == []


@pytest.mark.parametrize("L,csw,r_off", [(47e-6, 1e-15, 1e6), (47e-6, 1e-12, 1e4), (10e-3, 1e-13, 1e6)])
def test_off_state_ringing_with_low_q_is_noted(L, csw, r_off):
    """オフ・オフの L とスイッチ節点の容量の振動で、Q = (r_off/2)·√(csw/L) が 0.7〜2.3（減衰が強いが振動する）
    でも、刻みより速いなら注意が出る。周期 2π√(L·csw) は 1.4 ns〜6.3 ns で、h = 50 ns の1周期 1 点未満。"""
    q = r_off / 2 * math.sqrt(csw / L)
    assert 0.7 < q < 2.5
    res = simulate(buck(L, 1e4, r_off, csw), 2e-4, gates=GATE)
    assert oscillation_notes(res.notes)


# ================================================================ 上限を超える刻みのときの注意: 作る素子

def two_branch_rc(tf: float, ts: float) -> Netlist:
    return netlist(comp("V1", "V", "in", "0", waveform={"kind": "step", "final": 1.0}),
                   comp("R1", "R", "in", "a", 100.0), comp("C1", "C", "a", "0", tf / 100.0),
                   comp("R2", "R", "in", "b", 1e4), comp("C2", "C", "b", "0", ts / 1e4))


def test_unreachable_step_note_names_only_the_fast_branch():
    """速い枝 R1·C1 = 1 ns、遅い枝 R2·C2 = 0.1 s、h = 0.1 ms、10 s。1 ns を見る刻みは 1e11 ステップで
    上限を超える。注意は速い枝の素子（C1, R1）だけを挙げ、遅い枝（C2, R2）は挙げない。"""
    res = simulate(two_branch_rc(1e-9, 0.1), 10.0, 1e-4)
    notes = [n for n in res.notes if "最も速い時定数" in n]
    assert len(notes) == 1
    named = re.search(r"この成分（(.*) が作る成分）", notes[0]).group(1).split(", ")
    assert sorted(named) == ["C1", "R1"]


def test_three_phase_note_names_the_inductors_and_the_ground_resistor():
    """三相ブリッジを 1 MΩ（Rg）で接地すると、3つの L の和の電流が Rg を通る成分ができる。
    時定数は (L/3) / Rg = 1 mH / 3 / 1 MΩ = 3.33e-10 s。注意はその素子（La, Lb, Lc, Rg）を挙げる。"""
    parts = []
    for k, ph in enumerate("abc"):
        parts += [comp(f"V{ph}", "V", f"s{ph}", "N",
                       waveform={"kind": "sine", "amplitude": 100.0, "frequency": 50.0,
                                 "phase_deg": -120.0 * k}),
                  comp(f"L{ph}", "L", f"s{ph}", ph, 1e-3),
                  comp(f"D{ph}1", "D", ph, "P"), comp(f"D{ph}2", "D", "M", ph)]
    parts += [comp("RL", "R", "P", "M", 10.0), comp("Rg", "R", "M", "N", 1e6)]
    res = simulate(netlist(*parts, ground="N"), 0.04, 1e-5)
    notes = [n for n in res.notes if "最も速い時定数" in n]
    assert len(notes) == 1 and "3.33e-10 s" in notes[0]
    named = re.search(r"この成分（(.*) が作る成分）", notes[0]).group(1).split(", ")
    assert sorted(named) == ["La", "Lb", "Lc", "Rg"]


# ================================================================ テンプレートの位相の注意

def rlc_error(R: float, simulation: dict) -> tuple[float, list[str]]:
    """v(out) の最大誤差（入力 10 V に対する比）と注意。解析解は不足減衰の直列RLCのステップ応答。"""
    ex = expand(Spec.model_validate({
        "schema_version": "0.1", "template": "rlc_series", "simulation": simulation,
        "plant": {"Vin": {"value": 10, "unit": "V"}, "R": {"value": R, "unit": "ohm"},
                  "L": {"value": 1e-3, "unit": "H"}, "C": {"value": 10e-6, "unit": "F"}}}))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    a, w0 = R / 2e-3, 1e4
    wd = math.sqrt(w0 ** 2 - a ** 2)
    t = res.t
    exact = 10.0 * (1 - np.exp(-a * t) * (np.cos(wd * t) + a / wd * np.sin(wd * t)))
    return float(np.max(np.abs(res["v(out)"] - exact)) / 10.0), ex.notes + res.notes


@pytest.mark.parametrize("R,t_end", [(0.01, 1.0), (0.1, 5.0), (1.0, 5.0), (3.0, 20.0), (5.0, 20.0)])
def test_following_the_advice_once_reaches_one_percent(R, t_end):
    """前回の軽微2の直り: 勧めの刻みに1回従えば 1% 以内（前回 2.6〜191%）。"""
    _, notes = rlc_error(R, {"t_end": t_end})
    h = [float(m.group(1)) for n in notes if (m := re.search(r"simulation\.h を ([0-9.e+-]+) s 以下", n))]
    assert h
    again, _ = rlc_error(R, {"t_end": t_end, "h": h[0]})
    assert again < TOL_ANALYTIC


@product_bug(
    "軽微",
    "減衰が1刻みより速い（α·h = 5）振動について、テンプレートの注意が",
    "『振動の波形が入力の約 166% ずれる見込み』と書くが、実際の誤差は 0.036%（R = 10 Ω、t_end = 20 s）。",
    "見積もりは振動が残り続ける前提で、ずれが頭打ちになる時刻（2/d = 37 µs）が最初の刻み（1 ms）より",
    "前でも、その時刻の値を使う。R = 5 Ω で 187%（実際 0.52%）",
)
@pytest.mark.parametrize("R", [10.0, 5.0])
def test_estimated_error_is_not_overstated_tenfold(R):
    error, notes = rlc_error(R, {"t_end": 20.0})
    claimed = [float(m.group(1)) / 100 for n in notes if (m := re.search(r"約 ([0-9.]+)% ずれる", n))]
    assert all(c <= 10 * max(error, 1e-3) for c in claimed), (claimed, error)


@product_bug(
    "軽微",
    "ζ = 0.71〜0.95（極は複素数だが減衰の方が速い）では、テンプレートが振動として扱わず、",
    "『速い成分（0.1 ms）と遅い成分（0.13 ms）が大きく離れている』と事実と違う理由で刻みを粗くし、",
    "誤差 1.0〜1.7% に精度の注意が出ない（ζ = 0.71・t_end = 3 s で 1.67%）。",
    "main ではさらに大きい（ζ = 0.75・2 s で 4.8%）",
)
@pytest.mark.parametrize("zeta,t_end", [(0.71, 3.0), (0.75, 2.0), (0.8, 3.0)])
def test_heavily_damped_complex_poles_are_accurate_or_noted(zeta, t_end):
    R = 2 * zeta * math.sqrt(1e-3 / 10e-6)
    error, notes = rlc_error(R, {"t_end": t_end})
    assert error < TOL_ANALYTIC or any("精度" in n or "正しく出ない" in n for n in notes)


# ================================================================ 遮断の注意（前回の軽微3の広がり）

@product_bug(
    "軽微",
    "r_off を 10〜100 kΩ にした降圧で L が大きい（2.5〜10 mH）と、ダイオードが電流 0 で普通に切れた後も",
    "L の電流（0.25 mA。S1 の r_off を通る漏れ）が r_off を通って流れ続ける（時定数 L/r_off = 0.1〜1 µs で、",
    "刻みの2〜20倍）。これを『電流が1刻みで消えた。サージ電圧になる』と知らせる。48 回路のうち 9 回路。",
    "cc85eda から同じ",
)
def test_leakage_through_low_off_resistance_is_not_an_interruption():
    res = simulate(buck(10e-3, 6.0, 1e5), 2e-4, gates=GATE)
    assert [n for n in res.notes if "還流経路" in n] == []   # E1g（D-063）: 「行き場」→「還流経路」
