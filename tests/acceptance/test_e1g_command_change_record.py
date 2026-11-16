"""E1g の独立検証（基準6、D-048）: 指令で値が決まる電源の変わり目の記録。

確かめること（期待値は、区間ごとの厳密解を、この試験の中で式として書いたもの。製品の関数を使わない）:
  1. 指令が変わる格子点の代数的な変数（電源の電圧と電流）が、変わる直前の値になっている。
  2. 状態量（コンデンサ電圧）は連続で、記録の決まりを変えても積分は変わらない（2次の収束を保つ）。
  3. 制御器に渡る測定値は、記録と同じ（変わる直前の値）。代数的な変数を測る制御器で、指令の列が
     「直前を測る」モデルに合い、「直後を測る」モデルとは合わない。
  4. スイッチの切り替えと重なっても直前の値。時刻で決まる電源のステップは、これまでどおり
     変わった後の値（D-019）。

回路: 指令で値が決まる電源 V1（値 = G·u）→ R1 → C1。時定数 τ = R·C = 1 ms、制御周期 Tc = 1 ms。
指令 u は区分一定なので、区間 j（指令 u_j、区間の初めのコンデンサ電圧 v0）の厳密解は
    vC(t) = G·u_j + (v0 − G·u_j)·exp(−t/τ)、  i(V1)(t) = −(G·u_j − vC(t)) / R
（i(V1) は素子の中を in → 0 が正。電源が電流を送り出すと負）。

許容差（測る前に決めた根拠）:
  - 記録の値と厳密解の差: 台形則の誤差は、1 区間で約 (h/τ)²/12 × 変化の幅。h = Tc/100、変化の幅は最大
    G·|Δu| = 19 V（電流で 19 mA）なので、(0.01)²/12 × 19 mA ≈ 1.6e-7 A。許容差はその約 3 倍の 5e-7 A
    （電圧では 5e-4 V）。「直前」と「直後」の値の差は最小でも G·|Δu|/R = 7.5 mA で、許容差の 1 万倍以上。
    取り違えを見逃さない。
  - 収束の比: 2次なら 4。指令の変わり目の直後の減衰ステップ（後退オイラーを細かく刻んだもの）の分、
    粗い刻みでは少し下がる。受け入れ基準は 3.5 以上。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Command, Pwm, Step, simulate

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

R, C, G, TC = 1e3, 1e-6, 10.0, 1e-3
TAU = R * C
VALUES = [0.0, 1.0, 1.0, 0.25, -0.5, 0.75, 0.75, 0.75, 2.0, 0.1]     # 変わる所と変わらない所、符号の反転
T_END = len(VALUES) * TC
TOL_I = 5e-7      # A（上の根拠）
TOL_V = 5e-4      # V


def circuit(lower=None, upper=None):
    return hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=G, lower=lower, upper=upper)),
                  hp.comp("R1", "R", "in", "out", R), hp.comp("C1", "C", "out", "0", C))


def exact(t: np.ndarray, values=VALUES):
    """厳密な vC(t) と、各時刻の「変わる直前」の電源電圧。t は格子点（制御周期の整数分の1）。"""
    vc = np.empty_like(t)
    before = np.empty_like(t)
    v0 = 0.0
    eps = 1e-9 * TC
    for j, u in enumerate(values):
        t0 = j * TC
        sel = (t > t0 - eps) & (t <= t0 + TC + eps) if j else (t <= TC + eps)
        vc[sel] = G * u + (v0 - G * u) * np.exp(-(t[sel] - t0) / TAU)
        # 区間の中と、区間の終わりの点（次の変わり目の直前）
        inner = (t > t0 + eps) & (t <= t0 + TC + eps)
        before[inner] = G * u
        v0 = G * u + (v0 - G * u) * math.exp(-TC / TAU)
    before[0] = G * values[0]
    return vc, before


def changes(cmd: np.ndarray) -> np.ndarray:
    mask = np.zeros(len(cmd), dtype=bool)
    mask[1:] = cmd[1:] != cmd[:-1]
    return mask


def run(h, **kw):
    return simulate(circuit(), T_END, h, controllers=[hp.Lookup(VALUES, period=TC)], **kw)


def test_change_points_record_the_value_just_before_the_change():
    res = run(TC / 100)
    vc, before = exact(res.t)
    at = changes(res["cmd(u)"])
    assert np.flatnonzero(at).tolist() == [100, 300, 400, 500, 800, 900]      # 値が変わる制御時刻だけ
    i_before = -(before - vc) / R
    after = G * res["cmd(u)"]                          # その点から始まる区間の値（変わった後）
    i_after = -(after - vc) / R
    # 変わり目: 直前の値と合い、直後の値とは 7.5 mA 以上違う
    assert np.abs(res["i(V1)"][at] - i_before[at]).max() <= TOL_I
    assert np.abs(res["i(V1)"][at] - i_after[at]).min() >= 7.4e-3
    # 電源の電圧は、全格子点で「その点までの区間の指令 × G」（丸めの幅で）
    assert np.abs(res["v(in)"][1:] - G * res["cmd(u)"][:-1]).max() <= 1e-9
    # 変わり目でない点は、どちらで見ても同じ値
    assert np.abs(res["i(V1)"] - i_before).max() <= TOL_I


def test_state_is_continuous_and_every_point_satisfies_the_circuit_equation():
    res = run(TC / 100)
    vc, _ = exact(res.t)
    assert np.abs(res["v(out)"] - vc).max() <= TOL_V
    # 同じ時刻の記録どうしが回路の式を満たす（変わり目の点も、直前の値どうしで整合する）
    assert np.abs(res["v(in)"] - res["v(out)"] + R * res["i(V1)"]).max() <= 1e-9


def test_command_changes_are_counted_once_per_change():
    res = run(TC / 100)
    assert res.stats["command_changes"] == 6 and res.stats["state_changes"] == 0
    limited = simulate(circuit(lower=0.0, upper=1.0), T_END, TC / 100,
                       controllers=[hp.Lookup(VALUES, period=TC)])
    # 上下限で止めた後の値で数える（−0.5 は 0、2.0 は 1 になる。変わるのは 6 回:
    # 0 → 1、1 → 0.25、0.25 → 0、0 → 0.75、0.75 → 1、1 → 0.1）
    clipped = np.clip(VALUES, 0.0, 1.0)
    assert limited.stats["command_changes"] == int(np.count_nonzero(np.diff(clipped)))
    assert np.abs(limited["v(in)"][1:] - G * np.clip(limited["cmd(u)"][:-1], 0.0, 1.0)).max() <= 1e-9


@pytest.mark.parametrize("signal", ["v(out)", "i(V1)"])
def test_halving_the_step_keeps_second_order(signal):
    """刻みを半分にしていく誤差の比が 3.5 以上（基準6）。誤差は全格子点（変わり目を含む）での最大。"""
    errors = []
    for n in (20, 40, 80, 160):
        res = run(TC / n)
        vc, before = exact(res.t)
        reference = vc if signal == "v(out)" else -(before - vc) / R
        errors.append(float(np.abs(res[signal] - reference).max()))
    ratios = [a / b for a, b in zip(errors, errors[1:], strict=False)]
    assert all(r >= 3.5 for r in ratios), (errors, ratios)
    # 2次より速くは縮まない（誤差が丸めに埋もれていない）
    assert all(r <= 4.5 for r in ratios), (errors, ratios)
    # 絶対の大きさも、台形則の見積もり（(h/τ)²/12 × 19 V）の 3 倍以内
    scale = 19.0 if signal == "v(out)" else 19.0 / R
    assert errors[0] <= 3 * (TC / 20 / TAU) ** 2 / 12 * scale


# ------------------------------------------------------------------ 制御器が測る値

K_LOOP, I_REF, N_PERIODS = 40.0, 2e-3, 30


class CurrentLoop:
    """u[j+1] = K·(I_REF − i_out[j])。i_out は電源が送り出す電流（記録の i(V1) の符号を反転）。
    代数的な変数を測る制御器。"""

    inputs, outputs, period = ("i(V1)",), ("u",), TC

    def reset(self):
        self.seen = []
        return [0.3]

    def step(self, t, measured):
        self.seen.append(float(measured[0]))
        return [K_LOOP * (I_REF - (-float(measured[0])))]


def loop_model(measure: str) -> np.ndarray:
    """独立の離散モデル（区間ごとの厳密な指数関数）。

    measure = "before": 制御時刻の、指令が変わる直前の電流を測る。"after": 変わった直後の電流を測る。
    戻り値は、各制御周期で使われた指令。"""
    a = math.exp(-TC / TAU)
    used = [0.3]
    vc = 0.0
    for j in range(N_PERIODS):
        active = used[j]
        previous = used[j - 1] if j else active
        source = G * (previous if measure == "before" else active)
        used.append(K_LOOP * (I_REF - (source - vc) / R))
        vc = G * active + (vc - G * active) * a
    return np.array(used[:N_PERIODS])


def test_controller_is_given_the_value_before_the_change():
    """指令の列が「直前を測る」モデルに合う。

    2つのモデルの差は 0.136（指令の大きさ 0.3）で、取り違えると合わない。
    許容差: 刻み Tc/80 の積分の誤差が指令に入る分。見積もりは K·(h/τ)²/12 × 3 mA ≈ 1.6e-6、
    その 3 倍の 5e-6。"""
    ctl = CurrentLoop()
    n = 80
    res = simulate(circuit(), N_PERIODS * TC, TC / n, controllers=[ctl])
    used = res["cmd(u)"][::n][:N_PERIODS]
    before, after = loop_model("before"), loop_model("after")
    assert np.abs(before - after).max() > 0.1                    # この回路で、2つの決まりは見分けられる
    assert np.abs(used - before).max() <= 5e-6
    assert np.abs(used - after).max() > 0.1
    # 制御器に渡った値は、結果に記録された値とビット単位で同じ（control/interface.py の決まり）
    assert np.array_equal(np.array(ctl.seen[:N_PERIODS]), res["i(V1)"][::n][:N_PERIODS])


# ------------------------------------------------------------------ ほかの不連続と重なるとき

def test_switch_change_at_the_same_instant_also_keeps_the_values_before():
    """指令の変わり目と、スイッチの切り替えが同じ格子点（1 ms ごと）に重なる。

    どちらも変わる前の値（D-019、D-048）。"""
    netlist = hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=G)),
                     hp.comp("R1", "R", "in", "out", 100.0),
                     hp.comp("C1", "C", "out", "0", 1e-6), hp.comp("R2", "R", "out", "ld", 50.0),
                     hp.comp("S1", "S", "ld", "0", gate="g"))
    res = simulate(netlist, T_END, TC / 100, gates=[Pwm(name="g", frequency=1e3, duty=0.5)],
                   controllers=[hp.Lookup(VALUES, period=TC)])
    assert np.abs(res["v(in)"][1:] - G * res["cmd(u)"][:-1]).max() <= 1e-9
    # 電源の電流も、直前の電源電圧と整合する
    assert np.abs(res["v(in)"] - res["v(out)"] + 100.0 * res["i(V1)"]).max() <= 1e-9
    # スイッチの電流は、直前の導通状態で決まる値（切り替えの点で、切り替えた後の抵抗を使っていない）
    k = 100                                         # 1 ms: 周期の先頭でオンになる点。直前はオフ
    assert res["on(S1)"][k - 1] == 0 and res["on(S1)"][k] == 1
    assert abs(res.i("S1")[k]) < 1e-4               # オフ抵抗 1 MΩ を通る電流（20 V / 1 MΩ 以下）


def test_time_source_step_still_records_the_value_after_the_step():
    """時刻で決まる電源のステップは、これまでどおり変わった後の値を記録する（D-019。E1g で変えない）。"""
    res = simulate(hp.rc(Step(final=5.0, t_step=1e-3)), 3e-3, 1e-5)
    k = 100
    assert abs(res["v(in)"][k - 1]) <= 1e-12 and res["v(in)"][k] == pytest.approx(5.0, abs=1e-12)
    assert res["i(V1)"][k] == pytest.approx(-5.0 / R, rel=1e-9)     # 電流も、ステップした後の値
    # 制御器のある回路（核が別）でも同じ
    netlist = hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=G)),
                     hp.vsrc("V2", "b", "0", Step(final=3.0, t_step=2.5e-3)),
                     hp.comp("R1", "R", "in", "out", R), hp.comp("R2", "R", "b", "out", 2e3),
                     hp.comp("C1", "C", "out", "0", C))
    res = simulate(netlist, T_END, TC / 100, controllers=[hp.Lookup(VALUES, period=TC)])
    assert abs(res["v(b)"][249]) <= 1e-12 and res["v(b)"][250] == pytest.approx(3.0, abs=1e-12)
    # 指令の変わり目（ステップと重ならない点）は、直前の値
    at = changes(res["cmd(u)"])
    assert np.abs(res["v(in)"][at] - G * res["cmd(u)"][np.flatnonzero(at) - 1]).max() <= 1e-9


def test_commanded_current_source_records_the_value_before_the_change():
    """指令で値が決まる電流源でも同じ: 電源の端の電圧（代数的な変数）は、変わる直前の電流で決まる値。"""
    netlist = hp.net(hp.isrc("I1", "0", "a", Command(command="u", gain=1e-3)),
                     hp.comp("R1", "R", "a", "b", 1e3),
                     hp.comp("C1", "C", "b", "0", 1e-6), hp.comp("R2", "R", "b", "0", 5e3))
    res = simulate(netlist, T_END, TC / 100, controllers=[hp.Lookup(VALUES, period=TC)])
    cmd = res["cmd(u)"]
    # v(a) − v(b) = R1 × 電流源の電流。記録の時刻の電流は、その点までの区間の指令 × 1 mA
    drop = res["v(a)"] - res["v(b)"]
    assert np.abs(drop[1:] - 1e3 * 1e-3 * cmd[:-1]).max() <= 1e-9
    at = changes(cmd)
    assert np.abs(drop[at] - 1e3 * 1e-3 * cmd[at]).min() >= 0.7     # 変わった後の値とは 0.75 V 以上違う


def test_average_model_template_keeps_the_identities_at_every_point():
    """テンプレートの状態平均化モデル: v(sw_avg) は直前の指令 × Vin。

    v(sw) と i(Vleg) は、同じ時刻の値どうしで整合する。"""
    from circuit_sim import expand

    res = expand(hp.buck_spec("average", plant={"C_esr": hp.q(20, "mohm")},
                              control={"Vref_step": hp.q(10, "V"), "t_ref_step": hp.q(1, "ms")},
                              sim={"t_end": 2e-3})).run()
    cmd = res["cmd(duty)"]
    assert np.count_nonzero(changes(cmd)) > 150
    assert np.abs(res["v(sw_avg)"][1:] - hp.VIN * cmd[:-1]).max() <= 1e-9
    assert np.abs(res["v(sw)"] - (res["v(sw_avg)"] - hp.R_ON * res["i(L1)"])).max() <= 1e-9
    assert np.abs(res["i(Vleg)"] + res["i(L1)"]).max() <= 1e-9
    # 出力（ESR つき）: v(out) = v(cap) + ESR × (i(L1) − v(out)/R_load)。変わり目の点でも成り立つ
    i_c = res["i(L1)"] - res["v(out)"] / hp.R_BUCK
    assert np.abs(res["v(out)"] - res["v(cap)"] - 0.02 * i_c).max() <= 1e-9
