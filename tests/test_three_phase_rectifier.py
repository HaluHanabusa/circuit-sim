"""三相ダイオード整流器（E1c 範囲1）。

回路: 星形の三相電源（中性点）- 交流側の直列インダクタ Ls - ダイオード6個のブリッジ - 直流側の負荷 R。
直流側（または中性点）は 1 MΩ で基準節点につなぐ（インダクタだけで外とつながる部分を作らないため）。

期待値（この中で式から作る）:
  理想の整流の平均 Vd0 = 3√3·Vm / π（Vm は相電圧の振幅）。
  転流の重なりによる降下 3ωLs·Id / π と、導通する2個のダイオードのオン抵抗の降下 2·r_on·Id を引く。
  Id = Vd / R なので Vd = Vd0 / (1 + (3ωLs / π + 2·r_on) / R)。
許容 2%（仕様の基準1）。式は直流電流を一定とみなしたもので、抵抗負荷では電流の6倍周波の脈動ぶん
ずれる。この条件（ωLs = 0.31 Ω、R = 10 Ω）では、転流の補正そのものが 3% あるので、
補正を入れない式とは 2% を超えて食い違う（下の試験で確かめる）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from circuit_sim import DC, CircuitError, Component, Netlist, Sine, simulate

F, LS, RL, RG, H, R_ON = 50.0, 1e-3, 10.0, 1e6, 1e-5, 1e-3
N = round(1 / (F * H))        # 電源の1周期の点数


def bridge(vm: float, ics=(0.0, 0.0, 0.0), grounded: str = "neutral", cdc: float | None = None,
           vc0: float = 0.0) -> tuple[Netlist, str]:
    """grounded="neutral": 中性点を接地し、直流側の負極を 1 MΩ で接地。"dc": その逆。"""
    neutral, neg = ("0", "n") if grounded == "neutral" else ("N", "0")
    comps = []
    for k, ph in enumerate("abc"):
        comps += [
            Component(id=f"V{ph}", type="V", nodes=(f"s{ph}", neutral),
                      waveform=Sine(amplitude=vm, frequency=F, phase_deg=-120.0 * k)),
            Component(id=f"L{ph}", type="L", nodes=(f"s{ph}", ph), value=LS, ic=float(ics[k])),
        ]
    for name, (anode, cathode) in {"D1": ("a", "p"), "D3": ("b", "p"), "D5": ("c", "p"),
                                   "D4": (neg, "a"), "D6": (neg, "b"), "D2": (neg, "c")}.items():
        comps.append(Component(id=name, type="D", nodes=(anode, cathode)))
    comps.append(Component(id="RL", type="R", nodes=("p", neg), value=RL))
    if cdc is not None:
        comps.append(Component(id="Cdc", type="C", nodes=("p", neg), value=cdc, ic=vc0))
    comps.append(Component(id="Rg", type="R", nodes=(neg, "0") if neg != "0" else (neutral, "0"), value=RG))
    return Netlist(components=comps), neg


def expected_dc(vm: float) -> float:
    vd0 = 3 * math.sqrt(3) * vm / math.pi
    return vd0 / (1 + (3 * 2 * math.pi * F * LS / math.pi + 2 * R_ON) / RL)


def last_period_mean(y: np.ndarray) -> float:
    seg = y[-N - 1:]
    return float((seg[:-1].sum() + seg[1:].sum()) / (2 * N))


def cases() -> list[tuple[str, float, tuple[float, float, float]]]:
    """150例: 接地の2通り × 振幅15通り（10〜400 V）× 初期値5通り（ゼロ1、乱数4）。乱数はシード固定。"""
    rng = np.random.default_rng(20261115)
    out = []
    for grounded in ("neutral", "dc"):
        for vm in np.geomspace(10.0, 400.0, 15):
            peak = 2 * vm / RL
            out.append((grounded, float(vm), (0.0, 0.0, 0.0)))
            out += [(grounded, float(vm), tuple(rng.uniform(-peak, peak, 3))) for _ in range(4)]
    return out


def test_dc_average_matches_theory_for_150_cases():
    """基準1: 初期値ゼロと乱数の初期値の150例で、エラーにならず、直流側の平均が理論値と 2% 以内。

    定常の区間: 計算は3周期（60 ms）。過渡の時定数は Ls / R 程度（0.1 ms）なので、最後の1周期で平均を取る。
    """
    worst = 0.0
    for grounded, vm, ics in cases():
        net, neg = bridge(vm, ics, grounded)
        res = simulate(net, 3 / F, H)
        error = abs(last_period_mean(res.v("p", neg)) - expected_dc(vm)) / expected_dc(vm)
        worst = max(worst, error)
        assert error < 0.02, f"{grounded}, Vm={vm:.4g}, ic={ics}: {error:.2%}"
    assert worst < 0.02


def test_check_detects_missing_commutation_drop():
    """試験の実効性: 転流の降下を入れない理想の式とは 2% を超えて食い違う。"""
    net, neg = bridge(100.0)
    mean = last_period_mean(simulate(net, 3 / F, H).v("p", neg))
    ideal = 3 * math.sqrt(3) * 100.0 / math.pi
    assert abs(mean - ideal) / ideal > 0.02


def rounding_band(x0: np.ndarray, n_nodes: int) -> float:
    """丸め誤差とみなす幅（仕様: 節点電圧の最大の 1e-8 倍、下限 1e-9 V）。"""
    return max(1e-9, 1e-8 * float(np.abs(x0[:n_nodes]).max()))


@pytest.mark.parametrize("vm", [48.0, 400.0])
def test_zero_initial_currents_are_rescued_at_start(vm):
    """t = 0 でインダクタ電流がすべて 0 だと、どのダイオードも電流 0 で、電圧は丸め誤差だけになる。
    オンにしてもオフにしても判定と食い違って落ち着かないので、違反が丸め誤差の幅の中の状態を採る（救済）。
    採った状態は、その幅の中で物理的に正しい（オンなら逆電圧が幅以下、オフなら順電圧が幅以下）。"""
    net, _ = bridge(vm)
    res = simulate(net, 2e-3, H)
    assert res.stats["rescues_at_start"] == 1
    n_nodes = sum(name.startswith("v(") for name in res.unknowns)
    x0, code0 = res.x[0], int(res.sample_state[0])
    band = rounding_band(x0, n_nodes)
    for j, cid in enumerate(res.devices):
        anode, cathode = net.component(cid).nodes
        vd = (0.0 if anode == "0" else x0[res.unknowns.index(f"v({anode})")]) - (
            0.0 if cathode == "0" else x0[res.unknowns.index(f"v({cathode})")])
        assert (vd >= -band) if (code0 >> j) & 1 else (vd <= band), f"{cid}: {vd} V（幅 {band} V）"
    # 初期値は整合の計算（擬似逆行列）を通るので、0 からのずれは丸め誤差だけ
    assert np.all(np.abs(x0[[res.unknowns.index(f"i(L{p})") for p in "abc"]]) < 1e-9)
    # 1 ms 後には、いちばん高い相と低い相のダイオードが導通している
    k = round(1e-3 / H)
    assert sum(res[f"on({d})"][k] for d in ("D1", "D3", "D5")) >= 1
    assert sum(res[f"on({d})"][k] for d in ("D4", "D6", "D2")) >= 1


def test_no_rescue_when_the_start_is_not_a_tie():
    """初期電流がある（電流 0 の引き分けでない）ときは、救済は働かない。"""
    net, _ = bridge(400.0, (5.0, -2.0, -3.0))
    assert simulate(net, 2e-3, H).stats["rescues_at_start"] == 0


def test_a_real_violation_is_not_rescued(monkeypatch):
    """丸め誤差より大きい違反は救済しない（幅を広げすぎる誤りを落とす）。

    10 V の部分（節点電圧の最大を決める）の横に、1 mV の電源 - ダイオード - 抵抗。t = 0 ではダイオードを
    オンにする必要がある（順電圧 1 mV。幅は 1e-8 × 10 V = 1e-7 V）。反転の上限を 0 回にすると、
    落ち着かないまま違反 1 mV が残り、幅を超えるのでエラーになる。"""
    import importlib
    sim = importlib.import_module("circuit_sim.simulate")
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=10.0)),
        Component(id="R1", type="R", nodes=("a", "0"), value=1e3),
        Component(id="V2", type="V", nodes=("b", "0"), waveform=DC(value=1e-3)),
        Component(id="D1", type="D", nodes=("b", "c")),
        Component(id="R2", type="R", nodes=("c", "0"), value=1e3),
    ])
    assert simulate(net, 1e-3, H)["on(D1)"][0] == 1.0                    # 対照: 普通はオンで始まる
    monkeypatch.setattr(sim, "_flip_limit", lambda n_diodes: 0)
    with pytest.raises(CircuitError, match="時刻 t=0 でダイオードの状態が"):
        simulate(net, 1e-3, H)


def test_states_are_continuous_and_reproducible():
    """インダクタ電流は切り替えをまたいでも、傾き × 刻みを超えて跳ばない。2回の計算はビット一致。"""
    net, _ = bridge(200.0, (3.0, -1.0, -2.0))
    a = simulate(net, 2 / F, H)
    b = simulate(net, 2 / F, H)
    assert np.array_equal(a.x, b.x)
    i_a = a["i(La)"]
    slope_bound = 2 * 200.0 * math.sqrt(3) / LS * H            # |v_L| ≤ 線間電圧の振幅の2倍
    assert np.max(np.abs(np.diff(i_a[1:]))) < slope_bound


def test_with_dc_capacitor_and_random_initial_values():
    """直流側にコンデンサ（ランダムな初期電圧）を足しても、エラーにならずに計算できる。"""
    rng = np.random.default_rng(7)
    for grounded in ("neutral", "dc"):
        for vm in (10.0, 60.0, 400.0):
            ics = tuple(rng.uniform(-vm / 5, vm / 5, 3))
            net, neg = bridge(vm, ics, grounded, cdc=1e-3, vc0=float(rng.uniform(0, 2 * vm)))
            res = simulate(net, 0.04, H)
            assert np.isfinite(res.x).all()
            # 定常に近づくと、直流電圧は線間電圧の振幅 √3·Vm を超えない（1% の余裕）
            assert res.v("p", neg)[-N:].max() < math.sqrt(3) * vm * 1.01 + max(0.0, res.v("p", neg)[0])


def test_floating_dc_side_error_names_the_inductors_and_the_fix():
    """中性点だけを接地し、直流側をどこにも接地しないと、直流側はインダクタだけで外とつながる。
    原因と直し方をエラーに書く。"""
    net, _ = bridge(48.0)
    net = Netlist(components=[c for c in net.components if c.id != "Rg"])
    with pytest.raises(CircuitError) as exc:
        simulate(net, 0.01, H)
    text = str(exc.value)
    assert "La, Lb, Lc だけで回路の残りとつながって" in text
    assert "大きな抵抗" in text and "基準節点" in text
    assert "ダイオードだけで閉じたループ" not in text


def twelve_pulse(vm: float) -> Netlist:
    """12パルス整流: 30° ずらした2組の三相（相電圧の振幅 vm、Ls 1 mH）とブリッジ2つを直列にして負荷 10 Ω。
    1組目の中性点を接地し、2組目の中性点と、2つのブリッジの間の節点を 1 MΩ で接地する。"""
    comps = []
    for side, (neutral, top, bot, shift) in {"y": ("0", "P", "M", 0.0), "d": ("Nd", "M", "Q", 30.0)}.items():
        for k, ph in enumerate("abc"):
            node = f"{side}{ph}"
            comps += [
                Component(id=f"V{node}", type="V", nodes=(f"s{node}", neutral),
                          waveform=Sine(amplitude=vm, frequency=F, phase_deg=shift - 120.0 * k)),
                Component(id=f"L{node}", type="L", nodes=(f"s{node}", node), value=LS),
                Component(id=f"D{node}1", type="D", nodes=(node, top)),
                Component(id=f"D{node}2", type="D", nodes=(bot, node)),
            ]
    comps += [Component(id="RL", type="R", nodes=("P", "Q"), value=RL),
              Component(id="Rg1", type="R", nodes=("M", "0"), value=RG),
              Component(id="Rg2", type="R", nodes=("Nd", "0"), value=RG)]
    return Netlist(components=comps)


@pytest.mark.parametrize("vm", [1e3, 1e4])
def test_rescue_at_start_for_high_voltage_twelve_pulse(vm):
    """電圧が高いと、t = 0 の丸め誤差が下限の 1e-9 V を超える。

    幅の比（節点電圧の 1e-8 倍）で救済できること（比を 1e-13 にすると、この試験は落ちる）。"""
    net = twelve_pulse(vm)
    res = simulate(net, 2e-3, H)
    assert res.stats["rescues_at_start"] == 1
    n_nodes = sum(name.startswith("v(") for name in res.unknowns)
    x0, code0 = res.x[0], int(res.sample_state[0])
    band = rounding_band(x0, n_nodes)
    for j, cid in enumerate(res.devices):
        anode, cathode = net.component(cid).nodes
        vd = (0.0 if anode == "0" else x0[res.unknowns.index(f"v({anode})")]) - (
            0.0 if cathode == "0" else x0[res.unknowns.index(f"v({cathode})")])
        assert (vd >= -band) if (code0 >> j) & 1 else (vd <= band), f"{cid}: {vd} V（幅 {band} V）"


def test_fast_mode_from_the_grounding_resistor_names_its_elements_instead_of_an_impossible_step():
    """1 MΩ の接地抵抗と3つの Ls が作る成分（時定数 L / (3·Rg) = 0.33 ns）。刻みで見るにはステップ数が上限を
    超えるので、h を細かくするよう勧めるのではなく、成分を作る素子を挙げ、
    見る必要が無ければ対処が要らないと書く。"""
    net, _ = bridge(100.0)
    res = simulate(net, 3 / F, H)
    fast = [n for n in res.notes if "最も速い時定数" in n]
    assert len(fast) == 1 and "3.33e-10 s" in fast[0]
    assert "La, Lb, Lc, Rg" in fast[0] and "上限" in fast[0] and "対処は要らない" in fast[0]
    assert "この成分を見るには h を" not in fast[0]                    # 実行できない直し方を勧めない
    # 勧める h で足りる場合は、従来どおり h を勧める（1 ms の RC を h = 10 ms で）
    rc = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])
    assert "この成分を見るには h を 0.0001 s 以下にする" in simulate(rc, 1.0, 1e-2).notes[0]
