"""E1a（スイッチング回路エンジン）の受け入れ試験。検証担当Bが、実装者のテストとは別に用意した。

対象は `docs/plan/E1a-switching.md` の基準 1〜8、15、16 と、仕様に無い角。
テスト名の c01〜c16 が基準の番号、x は仕様に無い角。記録は docs/verification/E1a-B.md。

期待値の作り方（実装と独立にするための決まり）:
- 平均やリプルは、このファイルに書いた回路理論の式（降圧・昇圧の連続導通と不連続導通、整流）で作る。
- 波形そのものは、降圧コンバータの区分線形モデルを別経路で解いた参照（`buck_reference`）と比べる。
  状態方程式を手で立て、回路状態ごとに行列指数関数で厳密に進める。修正節点解析も台形則も使わない。
- `circuit_sim` の関数や行列を期待値の計算に使わない。

製品側の不具合で落ちる試験は xfail(strict=True) にしてある。直ったら XPASS で落ちるので、印を外す。
"""
import hashlib
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm
from scipy.optimize import brentq

from circuit_sim import Netlist, Pwm, simulate

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- 基準の回路（仕様の表のとおり）
VIN, L, C, FSW, N = 24.0, 47e-6, 100e-6, 1e5, 200
TS = 1.0 / FSW
H = TS / N
R_CCM, R_DCM, DUTY = 6.0, 100.0, 0.5
R_ON, R_OFF = 1e-3, 1e6          # スイッチとダイオードの既定のオン抵抗・オフ抵抗

# 仕様に無い角の許容差（測る前に決めた）
TOL_AVG = 0.02       # 平均値と理論式の差。基準3と同じ根拠（ダイオードが切れる時刻が最大1刻みずれる）
TOL_WAVE = 1e-3      # 波形と参照の差（電流は計算中の最大電流、電圧は入力電圧に対する比）。
#                      参照はダイオードの切り替え時刻を刻みの1/10で決めるので、理論上の差は
#                      傾き × 刻み / 10 = 2.5 mA 程度（起動時の最大電流 18 A の 0.014%）。0.1% は7倍の余裕
EXACT = 1e-9         # 丸め誤差しか残らないはずの量


# ---------------------------------------------------------------- 回路を組む小道具

def netlist(*components: dict) -> Netlist:
    return Netlist.model_validate({"components": list(components)})


def vsrc(name: str, a: str, b: str, **waveform) -> dict:
    return {"id": name, "type": "V", "nodes": [a, b], "waveform": waveform}


def res_(name: str, a: str, b: str, value: float) -> dict:
    return {"id": name, "type": "R", "nodes": [a, b], "value": value}


def ind(name: str, a: str, b: str, value: float, ic: float = 0.0) -> dict:
    return {"id": name, "type": "L", "nodes": [a, b], "value": value, "ic": ic}


def cap(name: str, a: str, b: str, value: float, ic: float = 0.0) -> dict:
    return {"id": name, "type": "C", "nodes": [a, b], "value": value, "ic": ic}


def diode(name: str, anode: str, cathode: str) -> dict:
    return {"id": name, "type": "D", "nodes": [anode, cathode]}


def switch(name: str, a: str, b: str, gate: str = "q1") -> dict:
    return {"id": name, "type": "S", "nodes": [a, b], "gate": gate}


def buck(load: float, inductance: float = L, v0: float = 0.0, i0: float = 0.0, source: dict | None = None):
    """降圧コンバータ: 電源(in) - スイッチ - (sw) - L - (out) - C と負荷。ダイオードは 基準 → sw。"""
    return netlist(
        vsrc("V1", "in", "0", **(source or {"kind": "dc", "value": VIN})),
        switch("S1", "in", "sw"), diode("D1", "0", "sw"),
        ind("L1", "sw", "out", inductance, ic=i0), cap("C1", "out", "0", C, ic=v0),
        res_("RL", "out", "0", load),
    )


def pwm(duty: float | str = DUTY, **kwargs) -> list[Pwm]:
    return [Pwm(name="q1", frequency=FSW, duty=duty, **kwargs)]


def period_averages(y: np.ndarray, n_periods: int, n: int = N) -> np.ndarray:
    """最後の n_periods 周期の、周期ごとの平均（周期の頭から頭まで、台形則）。"""
    last = len(y) - 1
    out = []
    for j in range(n_periods, 0, -1):
        seg = y[last - j * n: last - (j - 1) * n + 1]
        out.append((seg[:-1].sum() + seg[1:].sum()) / (2 * n))
    return np.array(out)


def on_steps_per_period(result, n: int = N) -> np.ndarray:
    gate = result["gate(q1)"][:-1]
    return gate[: len(gate) // n * n].reshape(-1, n).sum(axis=1)


# ---------------------------------------------------------------- 理論式（自分で導いたもの）

def buck_dcm_voltage(duty: float, load: float, inductance: float = L) -> float:
    """降圧・不連続導通の出力電圧。

    ピーク電流 ip = (Vin − Vo)·D·Ts / L、電流が0に戻るまでの時間は D2·Ts、D2 = D·(Vin − Vo) / Vo。
    インダクタ電流の平均 ip·(D + D2) / 2 が負荷電流 Vo / R に等しいことから、M = Vo / Vin について
    M² + (D² / K)·M − D² / K = 0、K = 2L / (R·Ts)。正の根は M = 2 / (1 + √(1 + 4K / D²))。
    """
    k = 2.0 * inductance / (load * TS)
    return VIN * 2.0 / (1.0 + math.sqrt(1.0 + 4.0 * k / duty**2))


def buck_reference(vin_of_step, n_steps: int, gate_on: np.ndarray, load: float, x0=(0.0, 0.0), sub: int = 10):
    """降圧コンバータの区分線形モデルの参照解。戻り値は格子点上の [iL, vC]。

    スイッチとダイオードを抵抗（オン 1 mΩ、オフ 1 MΩ）とみなすと、節点 sw の電流則から
      v_sw = (Vin / Rs − iL) / (1 / Rs + 1 / Rd)、L·iL' = v_sw − vC、C·vC' = iL − vC / R。
    回路状態ごとに係数が一定なので、刻みを sub 等分した小区間を行列指数関数で厳密に進める。
    ダイオードは小区間の境目で判定する（導通中に電流が負になったら切る。遮断中に順方向の電圧が出たら入れる）。
    """
    hs = H / sub
    table = {}
    for s_on in (False, True):
        for d_on in (False, True):
            rs, rd = (R_ON if s_on else R_OFF), (R_ON if d_on else R_OFF)
            g = 1.0 / rs + 1.0 / rd
            a, b = (1.0 / rs) / g, -1.0 / g                    # v_sw = a·Vin + b·iL
            m = np.zeros((3, 3))
            m[:2, :2] = [[b / L, -1.0 / L], [1.0 / C, -1.0 / (load * C)]]
            m[0, 2] = a / L
            e = expm(m * hs)
            table[s_on, d_on] = (e[:2, :2], e[:2, 2], a, b)
    x = np.array(x0, dtype=float)
    out = np.empty((n_steps + 1, 2))
    d_on = False
    for k in range(n_steps):
        out[k] = x
        s_on, vin = bool(gate_on[k]), vin_of_step(k)
        for j in range(sub + 1):
            for _ in range(2):                                   # 状態の判定（切り替わったら判定し直す）
                a, b = table[s_on, d_on][2:]
                v_sw = a * vin + b * x[0]
                if d_on and v_sw > 0.0:
                    d_on = False
                elif not d_on and v_sw < 0.0:
                    d_on = True
                else:
                    break
            if j < sub:
                p, q = table[s_on, d_on][:2]
                x = p @ x + q * vin
    out[n_steps] = x
    return out


class IntegralControl:
    """積分主体の電圧制御（基準16用に、この試験の中に書いたもの）。

    duty = 積分 + 比例。積分は 目標 / Vin から始める。既定のゲインは、積分 26 [1/(V·s)]、比例 0.005 [1/V]。
    """

    inputs = ["v(out)"]
    outputs = ["duty"]

    def __init__(self, target: float, ki: float = 26.0, kp: float = 0.005, period: float = TS):
        self.target, self.ki, self.kp, self.period = target, ki, kp, period

    def reset(self):
        self.integral = self.target / VIN
        return [self.integral]

    def step(self, t, measured):
        error = self.target - measured[0]
        self.integral = min(max(self.integral + self.ki * self.period * error, 0.0), 1.0)
        return [min(max(self.integral + self.kp * error, 0.0), 1.0)]


class ScheduledCommand:
    """決まった時刻以降に指令を切り替えるだけの制御器。測定値と呼ばれた時刻を覚えておく。"""

    inputs = ["v(out)"]
    outputs = ["d"]

    def __init__(self, period: float, before: float, after: float | None = None, t_change: float = math.inf):
        self.period, self.before, self.after, self.t_change = period, before, after, t_change
        self.calls: list[tuple[float, float]] = []

    def reset(self):
        self.calls = []
        return [self.before]

    def step(self, t, measured):
        self.calls.append((float(t), float(measured[0])))
        return [self.after if t >= self.t_change - 1e-12 else self.before]


# ---------------------------------------------------------------- 使い回す計算結果

@pytest.fixture(scope="module")
def ccm():
    """連続導通の定常。理論上の平均値から始めて 12 ms（LC の包絡の時定数 2RC = 1.2 ms の10倍）。"""
    return simulate(buck(R_CCM, v0=DUTY * VIN, i0=DUTY * VIN / R_CCM), 12e-3, gates=pwm())


@pytest.fixture(scope="module")
def dcm():
    """不連続導通の定常。理論上の出力電圧から始めて 6 ms。"""
    return simulate(buck(R_DCM, v0=buck_dcm_voltage(DUTY, R_DCM)), 6e-3, gates=pwm())


@pytest.fixture(scope="module")
def dcm_startup():
    """不連続導通の負荷で、初期値0から 3 ms（起動の過渡。連続導通と不連続導通の両方を通る）。"""
    return simulate(buck(R_DCM), 3e-3, gates=pwm())


def steady_average(result, name: str = "v(out)") -> float:
    """最後の100周期の平均。その前の100周期との差が 0.01% 以内（過渡が収まっている）ことも確かめる。"""
    both = period_averages(result[name], 200)
    assert abs(both[100:].mean() - both[:100].mean()) < 1e-4 * abs(both[100:].mean())
    return float(both[100:].mean())


# ================================================================ 基準1〜3: 平均とリプル

def test_c01_ccm_output_average(ccm):
    v_out = steady_average(ccm)
    assert v_out == pytest.approx(DUTY * VIN, rel=0.01)                 # 基準1（1%以内）
    # 理想値からのずれは導通損失で説明できる: 負荷電流 2 A × オン抵抗 1 mΩ = 2 mV（0.017%）
    assert v_out == pytest.approx(DUTY * VIN - (DUTY * VIN / R_CCM) * R_ON, rel=1e-3)


def test_c02_ccm_inductor_ripple(ccm):
    v_out = steady_average(ccm)
    current = ccm["i(L1)"][-N - 1:]
    ripple = current.max() - current.min()
    assert ripple == pytest.approx((VIN - v_out) * DUTY / (L * FSW), rel=0.02)     # 1.28 A
    assert steady_average(ccm, "i(L1)") == pytest.approx(v_out / R_CCM, rel=1e-3)  # 平均電流 = 負荷電流


def test_c03_dcm_output_average(dcm):
    expected = buck_dcm_voltage(DUTY, R_DCM)
    assert expected == pytest.approx(18.587, abs=1e-3)                  # 仕様の「約 18.6 V」
    assert steady_average(dcm) == pytest.approx(expected, rel=0.02)


@pytest.mark.parametrize("duty,load", [(0.2, 30.0), (0.35, 1000.0), (0.65, 100.0), (0.8, 6.0), (0.8, 1000.0)])
def test_x_buck_average_over_duty_and_load(duty, load):
    """デューティと負荷を変えても、連続導通（K ≥ 1 − D）なら D·Vin、不連続導通なら上の式に合う。"""
    continuous = 2.0 * L / (load * TS) >= 1.0 - duty
    expected = duty * VIN if continuous else buck_dcm_voltage(duty, load)
    result = simulate(buck(load, v0=expected, i0=expected / load if continuous else 0.0), 6e-3,
                      gates=pwm(duty))
    assert period_averages(result["v(out)"], 100).mean() == pytest.approx(expected, rel=TOL_AVG)


# ================================================================ 基準4: 不連続導通

def test_c04_dcm_current_does_not_go_negative(dcm, dcm_startup):
    peak = dcm["i(L1)"][-N - 1:].max()
    assert peak == pytest.approx((VIN - steady_average(dcm)) * DUTY * TS / L, rel=0.02)   # ピーク電流の理論値
    assert dcm["i(L1)"].min() >= -0.01 * peak
    assert dcm_startup["i(L1)"].min() >= -0.01 * peak                   # 起動の過渡でも


def test_c04_no_oscillation_while_switch_and_diode_are_off(dcm):
    """スイッチもダイオードもオフの区間では、インダクタに電圧がかからず、電流はオフ抵抗の漏れだけ。

    区間は理論から決める（製品の記録を使わない）: 電流が0に戻るのは周期の頭から (D + D2)·Ts、
    D2 = D·(Vin − Vo) / Vo。その刻みの次の点はまだ切り替えの途中（刻みの平均）なので、2点目以降を見る。
    許容差: v(sw) − v(out) は Vin の 0.1%、電流はピークの 0.1%、隣り合う点の差（振動）も Vin の 0.1%。
    """
    v_out = steady_average(dcm)
    d2_steps = DUTY * (VIN - v_out) / v_out * N
    first = math.floor(DUTY * N + d2_steps) + 2
    assert first < N - 20                                             # オフの区間が十分に長い
    last = len(dcm.t) - 1
    peak = dcm["i(L1)"][-N - 1:].max()
    for period in range(1, 51):
        k = np.arange(last - period * N + first, last - (period - 1) * N + 1)   # 次の周期の頭の点まで
        dv = dcm["v(sw)"][k] - dcm["v(out)"][k]
        assert np.max(np.abs(dv)) < 1e-3 * VIN
        assert np.max(np.abs(np.diff(dv))) < 1e-3 * VIN
        assert np.max(np.abs(dcm["i(L1)"][k])) < 1e-3 * peak


# ================================================================ 基準5: 整流

def test_c05_half_wave_rectifier():
    amplitude, f, load = 10.0, 50.0, 100.0
    result = simulate(netlist(vsrc("V1", "in", "0", kind="sine", amplitude=amplitude, frequency=f),
                              diode("D1", "in", "out"), res_("RL", "out", "0", load)), 3 / f, 1e-5)
    v_in = result["v(in)"]
    assert np.max(np.abs(v_in - amplitude * np.sin(2 * math.pi * f * result.t))) < EXACT * amplitude
    expected = np.maximum(v_in, 0.0) * load / (load + R_ON)
    assert np.max(np.abs(result["v(out)"] - expected)) < 0.01 * amplitude
    # 逆方向はオフ抵抗の漏れだけ（−A·R / (R + r_off) = −1 mV）
    assert result["v(out)"].min() == pytest.approx(-amplitude * load / (load + R_OFF), rel=1e-6)


def test_x_bridge_rectifier_and_diode_or():
    """ダイオードが複数ある回路。ブリッジ整流は |v|·R / (R + 2·r_on)、ダイオードORは高い方の電源。"""
    amplitude, f, load = 10.0, 50.0, 100.0
    bridge = simulate(netlist(vsrc("V1", "p", "n", kind="sine", amplitude=amplitude, frequency=f),
                              diode("D1", "p", "out"), diode("D2", "n", "out"),
                              diode("D3", "0", "p"), diode("D4", "0", "n"),
                              res_("RL", "out", "0", load)), 3 / f, 1e-5)
    expected = np.abs(bridge.v("p", "n")) * load / (load + 2 * R_ON)
    assert np.max(np.abs(bridge["v(out)"] - expected)) < 0.01 * amplitude

    either = simulate(netlist(vsrc("V1", "a", "0", kind="sine", amplitude=amplitude, frequency=f),
                              vsrc("V2", "b", "0", kind="dc", value=4.0),
                              diode("D1", "a", "out"), diode("D2", "b", "out"),
                              res_("RL", "out", "0", load)), 3 / f, 1e-5)
    expected = np.maximum(either["v(a)"], 4.0) * load / (load + R_ON)
    assert np.max(np.abs(either["v(out)"] - expected)) < 0.01 * amplitude


def test_x_peak_rectifier_with_smoothing_capacitor():
    """半波整流＋平滑コンデンサ。

    導通が終わる角は π − atan(ωRC)。その後は時定数 RC で放電し、正弦波と交わったところで再び導通する。
    """
    amplitude, f, load, c = 10.0, 50.0, 1000.0, 100e-6
    w = 2 * math.pi * f
    h = 2e-6
    result = simulate(netlist(vsrc("V1", "in", "0", kind="sine", amplitude=amplitude, frequency=f),
                              diode("D1", "in", "out"), cap("C1", "out", "0", c),
                              res_("RL", "out", "0", load)), 10 / f, h)
    theta_off = math.pi - math.atan(w * load * c)
    v_off = amplitude * math.sin(theta_off)
    theta_on = brentq(lambda th: amplitude * math.sin(th)
                      - v_off * math.exp(-(th + 2 * math.pi - theta_off) / (w * load * c)), 0.0, math.pi / 2)
    n = round(1 / f / h)
    v_last = result["v(out)"][-n - 1:]
    assert v_last.max() == pytest.approx(amplitude, rel=0.01)
    assert v_last.min() == pytest.approx(amplitude * math.sin(theta_on), abs=0.01 * amplitude)
    conduction = result["on(D1)"][-n - 1:-1].sum() / n * 2 * math.pi
    assert conduction == pytest.approx(theta_off - theta_on, abs=0.01 * 2 * math.pi)


# ================================================================ 基準6: 切り替え点での連続性

def element_law_residuals(result, load: float) -> tuple[np.ndarray, np.ndarray]:
    """各ステップで、状態量の変化が素子の式からどれだけ外れているか（電流 [A]、コンデンサ電圧 [V]）。

    インダクタ: L·Δi = h·vL。右辺は、後退オイラーなら終点の値、台形則なら始点と終点の平均。
    切り替えの点の記録は切り替え前の値なので、切り替えをまたぐステップで成り立つのは後退オイラーの形。
    どちらの形にも合わない変化（＝跳び）があれば、その大きさがそのまま残る。
    """
    i, v = result["i(L1)"], result["v(out)"]
    v_l = result["v(sw)"] - v
    i_c = i - v / load
    d_i, d_v = np.diff(i), np.diff(v)
    res_i = np.minimum(np.abs(d_i - H / L * v_l[1:]), np.abs(d_i - H / (2 * L) * (v_l[:-1] + v_l[1:])))
    res_v = np.minimum(np.abs(d_v - H / C * i_c[1:]), np.abs(d_v - H / (2 * C) * (i_c[:-1] + i_c[1:])))
    return res_i, res_v


@pytest.mark.parametrize("which,load", [("ccm", R_CCM), ("dcm", R_DCM), ("dcm_startup", R_DCM)])
def test_c06_states_are_continuous_across_switching(which, load, request):
    """コンデンサ電圧とインダクタ電流の跳びが、代表値の 0.1% 以内（切り替えの点を含む全ステップ）。

    代表値: 電流は定常のピーク電流、電圧は出力電圧の平均。
    """
    result = request.getfixturevalue(which)
    res_i, res_v = element_law_residuals(result, load)
    peak = request.getfixturevalue("ccm" if load == R_CCM else "dcm")["i(L1)"][-N - 1:].max()
    assert res_i.max() < 1e-3 * peak
    assert res_v.max() < 1e-3 * (DUTY * VIN)
    # 切り替えの点（周期の頭と、オフになる点）が確かに含まれている
    gate = result["gate(q1)"]
    assert np.count_nonzero(np.diff(gate)) >= 2 * (len(result.t) // N) - 2


def test_c06_check_detects_a_jump_of_a_tenth_of_a_percent(ccm):
    """上の測り方の実効性: 切り替えの点から先の電流に、ピークの 0.1% の跳びを足すと検出される。"""
    peak = ccm["i(L1)"].max()
    k = len(ccm.t) - 1 - 3 * N + round(DUTY * N)          # スイッチがオフになる格子点

    class Tampered:
        def __getitem__(self, name):
            y = ccm[name].copy()
            if name == "i(L1)":
                y[k + 1:] += 1.5e-3 * peak
            return y

    res_i, _ = element_law_residuals(Tampered(), R_CCM)
    assert res_i.max() > 1e-3 * peak
    assert int(res_i.argmax()) == k


# ================================================================ 基準7: 制御器の口

def test_c07_constant_controller_equals_fixed_duty():
    fixed = simulate(buck(R_CCM), 3e-3, gates=pwm(DUTY))
    controller = ScheduledCommand(TS, DUTY)
    controlled = simulate(buck(R_CCM), 3e-3, gates=pwm("d"), controllers=[controller])
    assert np.array_equal(fixed.x, controlled.x)
    assert np.array_equal(fixed["gate(q1)"], controlled["gate(q1)"])
    assert np.all(controlled["cmd(d)"] == DUTY)
    # 制御器は制御周期ごとに呼ばれ、渡される測定値は記録と同じ
    assert len(controller.calls) == 300
    for t, measured in controller.calls[::37]:
        assert measured == controlled["v(out)"][round(t / H)]


@pytest.mark.parametrize("periods_per_control", [1, 2, 4])
def test_c07_command_takes_effect_at_next_control_period(periods_per_control):
    """t = 1 ms の演算で指令を 0.5 → 0.25 に変えると、次の制御周期の頭から反映される（1サンプル遅れ）。"""
    control_period = periods_per_control * TS
    controller = ScheduledCommand(control_period, 0.5, 0.25, t_change=1e-3)
    result = simulate(buck(R_CCM), 2e-3, gates=pwm("d"), controllers=[controller])
    k_effect = round((1e-3 + control_period) / H)
    command = result["cmd(d)"]
    assert np.all(command[:k_effect] == 0.5)
    assert np.all(command[k_effect:] == 0.25)
    on_steps = on_steps_per_period(result)
    first_period = k_effect // N
    assert np.all(on_steps[:first_period] == 100)
    assert np.all(on_steps[first_period:] == 50)


def test_x_control_period_different_from_carrier_period():
    """制御周期が搬送波の周期の整数倍でなくても、指令は搬送波の周期の頭で取り込まれる。"""
    controller = ScheduledCommand(1.5 * TS, 0.5, 0.3, t_change=1e-4)
    result = simulate(buck(R_CCM), 5e-4, gates=pwm("d"), controllers=[controller])
    # 1e-4 s 以降で最初の演算は t = 1.05e-4 s（7回目）。指令は 1.2e-4 s から変わり、ちょうど周期の頭
    command = result["cmd(d)"]
    assert np.all(command[: round(1.2e-4 / H)] == 0.5)
    assert np.all(command[round(1.2e-4 / H):] == 0.3)
    on_steps = on_steps_per_period(result)
    assert np.all(on_steps[:12] == 100)
    assert np.all(on_steps[12:] == 60)


# ================================================================ 基準8: 再現性

def reproducibility_digest() -> str:
    sha = hashlib.sha256()
    runs = [
        (buck(R_CCM), {"gates": pwm()}),
        (buck(R_DCM), {"gates": pwm(0.37, carrier="triangle")}),
        (buck(R_CCM), {"gates": pwm("duty"), "controllers": [IntegralControl(12.03)]}),
    ]
    for net, kwargs in runs:
        result = simulate(net, 2e-3, **kwargs)
        sha.update(result.t.tobytes())
        sha.update(result.x.tobytes())
        for name in sorted(result.signals):
            sha.update(result.signals[name].tobytes())
    return sha.hexdigest()


def test_c08_same_input_gives_bitwise_identical_result():
    first = simulate(buck(R_DCM), 2e-3, gates=pwm())
    second = simulate(buck(R_DCM), 2e-3, gates=pwm())
    assert np.array_equal(first.x, second.x)
    assert first.signals.keys() == second.signals.keys()
    for name in first.signals:
        assert np.array_equal(first.signals[name], second.signals[name])


def test_c08_same_input_gives_bitwise_identical_result_across_processes():
    module = Path(__file__).stem
    code = (f"import sys; sys.path.insert(0, 'tests/acceptance'); import {module};"
            f" print({module}.reproducibility_digest())")
    digests = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                              text=True, check=True)
        digests.append(done.stdout.strip())
    assert len(digests[0]) == 64
    assert digests[0] == digests[1] == reproducibility_digest()


# ================================================================ 基準15: 大きいLでの減衰

def off_interval_deviation(result, inductance: float) -> tuple[float, float, float]:
    """スイッチもダイオードもオフの区間の v(sw) − v(out) を、Vin に対する比で3通りに測る。

    戻り値:
      文面どおり   … 区間に入って2点目以降の |v(sw) − v(out)| の最大
      4点目以降    … 同じものを4点目以降で
      厳密解との差 … 2点目以降で、回路そのものの応答を引いた残り
    オフの回路では、節点 sw の電流則から v(sw) − v(out) が時定数 2L / r_off で減衰する
    （L = 10 mH で 20 ns。刻み 50 ns に対して1刻みで e^-2.5 = 0.082 倍）。これは数値振動ではなく回路の応答。
    区間に入った最初の点は、切り替えの途中の刻み（電流を0まで運ぶ刻み）の結果なので、初期値として使う。
    """
    off = (result["on(S1)"] == 0) & (result["on(D1)"] == 0)
    deviation = (result["v(sw)"] - result["v(out)"]) / VIN
    steps = np.nonzero(off[:-1])[0]                       # 区間 k → k+1 が両方オフ
    decay = math.exp(-H / (2.0 * inductance / R_OFF))
    literal = later = versus_exact = 0.0
    for run in np.split(steps, np.nonzero(np.diff(steps) != 1)[0] + 1):
        samples = np.arange(run[0] + 1, run[-1] + 2)      # 両方オフの回路で進めた結果の点
        if len(samples) < 5:
            continue
        d = deviation[samples]
        literal = max(literal, float(np.abs(d[1:]).max()))
        later = max(later, float(np.abs(d[3:]).max()))
        exact = d[0] * decay ** np.arange(len(d))
        versus_exact = max(versus_exact, float(np.abs(d - exact)[1:].max()))
    assert literal > 0.0 or versus_exact >= 0.0
    return literal, later, versus_exact


@pytest.mark.parametrize("inductance", [47e-6, 470e-6, 2.5e-3, 10e-3])
@pytest.mark.parametrize("condition", ["spec", "scaled_load"])
def test_c15_no_residual_oscillation_for_large_inductance(inductance, condition):
    """基準15。条件は2通り:

    spec        … 仕様の文面どおり、基準回路（負荷 100 Ω）の L だけを変える。L ≥ 470 µH では定常が
                  連続導通になる（K = 2L / (R·Ts) ≥ 0.94 > 1 − D）ので、両方オフの区間は起動の過渡にだけ出る。
    scaled_load … 実装者の測り方。負荷を L に比例させて不連続導通を保つ。
    """
    load = R_DCM if condition == "spec" else R_DCM * inductance / L
    result = simulate(buck(load, inductance), 10e-3, gates=pwm())
    off = (result["on(S1)"] == 0) & (result["on(D1)"] == 0)
    assert np.count_nonzero(off) > 1000                   # 両方オフの区間が実際にある
    literal, later, versus_exact = off_interval_deviation(result, inductance)
    assert versus_exact < 0.005                           # 回路の応答を引いた残り（数値振動）は 0.5% 以内
    assert later < 0.005                                  # 4点目以降は、文面どおりでも 0.5% 以内
    if inductance <= 2.5e-3:
        assert literal < 0.005                            # 2.5 mH までは、2点目から文面どおり 0.5% 以内
    else:
        # 10 mH では、2〜3点目に回路そのものの応答（時定数 20 ns）が Vin の数%残る。厳密解でも同じ
        assert 0.005 < literal < 0.10


def test_c15_check_detects_missing_damping():
    """上の測り方の実効性: 減衰ステップを入れないと、残りが Vin の 0.5% を大きく超える。"""
    inductance = 2.5e-3
    result = simulate(buck(R_DCM * inductance / L, inductance), 4e-3, gates=pwm(), damping_steps=0)
    _, later, versus_exact = off_interval_deviation(result, inductance)
    assert versus_exact > 0.05
    assert later > 0.05


# ================================================================ 基準16: デューティの分解能

def closed_loop_swing(target: float, **control) -> tuple[float, float]:
    """積分主体の電圧制御で 40 ms 計算し、最後の 15 ms の出力の周期平均の（振れ幅 / 目標、平均）を返す。"""
    result = simulate(buck(R_CCM), 40e-3, gates=pwm("duty"), controllers=[IntegralControl(target, **control)])
    averages = period_averages(result["v(out)"], 1500)
    return float(np.ptp(averages)) / target, float(averages.mean())


@pytest.mark.parametrize("target", [12.03, 12.06])
def test_c16_closed_loop_swing_at_specified_targets(target):
    swing, mean = closed_loop_swing(target)
    assert swing < 0.005
    assert mean == pytest.approx(target, rel=1e-3)


@pytest.mark.xfail(strict=True, reason=(
    "製品の制約（軽微、E1a-B.md の問題1）: 目標がデューティの格子（24 V × n / 200 − 2 mV）から 6 mV 以内に"
    "あると、余りの持ち越しの周期が LC の共振（2.3 kHz）に乗り、出力の周期平均が振れ続ける。"
    "目標 11.99225 V で 1.11%（基準16の 0.5% の2.2倍）。基準16が指定する 12.03 V と 12.06 V では出ない"))
def test_c16_closed_loop_swing_near_duty_grid():
    """基準16の文面の外。目標を 0.25 mV 刻みで振って見つけた最悪の点でも、振れ幅が目標の 0.5% 以内。"""
    swing, _ = closed_loop_swing(11.99225)
    assert swing < 0.005


def test_x_carried_remainder_tracks_the_duty_command():
    """余りの持ち越し: j 周期目までのオンの合計が、j·duty·N から1ステップ以上ずれない。"""
    duty = 0.50335
    result = simulate(buck(R_CCM), 4e-3, gates=pwm(duty))
    on_steps = on_steps_per_period(result)
    assert set(np.unique(on_steps)) == {100.0, 101.0}
    cumulative = np.cumsum(on_steps)
    ideal = duty * N * np.arange(1, len(on_steps) + 1)
    assert np.max(np.abs(cumulative - ideal)) <= 1.0
    assert on_steps.mean() == pytest.approx(duty * N, abs=1.0 / len(on_steps))


# ================================================================ 仕様に無い角

@pytest.mark.parametrize("which,load", [("dcm_startup", R_DCM)])
def test_x_waveform_matches_piecewise_linear_reference(which, load, request):
    """波形そのものを、別経路の参照解と比べる（起動の過渡。連続導通から不連続導通へ移る）。"""
    result = request.getfixturevalue(which)
    n = len(result.t) - 1
    gate = (np.arange(n) % N) < round(DUTY * N)
    ref = buck_reference(lambda k: VIN, n, gate, load)
    assert np.max(np.abs(result["i(L1)"] - ref[:, 0])) < TOL_WAVE * np.abs(ref[:, 0]).max()
    assert np.max(np.abs(result["v(out)"] - ref[:, 1])) < TOL_WAVE * VIN


@pytest.mark.parametrize("phase", [0, 100, 37, 150], ids=["switch_on", "switch_off", "while_on", "while_off"])
def test_x_source_step_coinciding_with_switching(phase):
    """電源のステップ（24 → 30 V）が、スイッチの切り替えと同じ格子点に重なっても、参照解と合う。"""
    k_step = 60 * N + phase
    n = 120 * N
    source = {"kind": "step", "initial": VIN, "final": 30.0, "t_step": k_step * H}
    result = simulate(buck(R_DCM, source=source), n * H, gates=pwm())
    assert result.notes == []                             # 格子点上なので、丸めの注意は出ない
    gate = (np.arange(n) % N) < round(DUTY * N)
    ref = buck_reference(lambda k: 30.0 if k >= k_step else VIN, n, gate, R_DCM)
    assert np.max(np.abs(result["i(L1)"] - ref[:, 0])) < TOL_WAVE * np.abs(ref[:, 0]).max()
    assert np.max(np.abs(result["v(out)"] - ref[:, 1])) < TOL_WAVE * 30.0
    assert result["v(in)"][k_step - 1] == pytest.approx(VIN, rel=EXACT)
    assert result["v(in)"][k_step] == pytest.approx(30.0, rel=EXACT)      # 電源は、その点で切り替わった後の値


def test_x_duty_zero_and_one():
    """デューティ 0 は漏れ電流だけ、1 はスイッチが入ったまま（Vin·R / (R + r_on)）。切り替えは起きない。"""
    off = simulate(buck(R_CCM), 5e-3, gates=pwm(0.0))
    assert np.all(off["gate(q1)"] == 0)
    # オフのスイッチ 1 MΩ の先に、オフのダイオード 1 MΩ と負荷 6 Ω が並列
    leak = VIN / (R_OFF + 1.0 / (1.0 / R_OFF + 1.0 / R_CCM))
    assert off["v(out)"][-1] == pytest.approx(leak * (1.0 / (1.0 / R_OFF + 1.0 / R_CCM)), rel=1e-3)

    on = simulate(buck(R_CCM, v0=VIN, i0=VIN / R_CCM), 5e-3, gates=pwm(1.0))
    assert np.all(on["gate(q1)"] == 1)
    assert on["v(out)"][-1] == pytest.approx(VIN * R_CCM / (R_CCM + R_ON), rel=1e-4)


def boost(load: float, v_in: float, inductance: float, v0: float) -> Netlist:
    """昇圧コンバータ: 電源 - L - (x)、スイッチは x → 基準、ダイオードは x → out。"""
    return netlist(vsrc("V1", "in", "0", kind="dc", value=v_in), ind("L1", "in", "x", inductance),
                   switch("S1", "x", "0"), diode("D1", "x", "out"),
                   cap("C1", "out", "0", C, ic=v0), res_("RL", "out", "0", load))


@pytest.mark.parametrize("load", [20.0, 500.0], ids=["ccm", "dcm"])
def test_x_boost_converter(load):
    """別の回路（昇圧）。

    連続導通（K ≥ D(1 − D)²）なら Vin / (1 − D)、不連続導通なら Vin·(1 + √(1 + 4D² / K)) / 2。

    不連続導通の式: ピーク電流 Vin·D·Ts / L、ダイオードの導通時間 D2 = D·Vin / (Vo − Vin)、
    ダイオード電流の平均 ip·D2 / 2 = Vo / R から M² − M − D² / K = 0。
    """
    v_in, inductance, duty = 12.0, 100e-6, 0.5
    k = 2.0 * inductance / (load * TS)
    if k >= duty * (1 - duty) ** 2:
        expected = v_in / (1 - duty)
    else:
        expected = v_in * (1 + math.sqrt(1 + 4 * duty**2 / k)) / 2
    result = simulate(boost(load, v_in, inductance, expected), 20e-3, gates=pwm(duty))
    assert period_averages(result["v(out)"], 100).mean() == pytest.approx(expected, rel=TOL_AVG)
    current = result["i(L1)"][-N - 1:]
    assert current.max() - current.min() == pytest.approx(v_in * duty * TS / inductance, rel=TOL_AVG)
    assert result["i(L1)"].min() >= -1e-3


def test_x_recording_convention_at_switching_instants(ccm):
    """記録の決まり: スイッチが切り替わる格子点の回路の変数は、切り替わる前の値。gate / on は次の区間の値。

    スコープでは、ゲートの縁の点で v(sw) がまだ前の値を示し、次の点で変わる（縁が1刻みの傾きで描かれる）。
    """
    k_on = len(ccm.t) - 1 - 2 * N                         # 周期の頭（スイッチがオンになる点）
    k_off = k_on + round(DUTY * N)
    v_sw, gate = ccm["v(sw)"], ccm["gate(q1)"]
    assert (gate[k_on - 1], gate[k_on]) == (0.0, 1.0)
    assert v_sw[k_on] == pytest.approx(0.0, abs=0.01 * VIN)          # まだダイオードが導通している値
    assert v_sw[k_on + 1] == pytest.approx(VIN, rel=0.01)
    assert (gate[k_off - 1], gate[k_off]) == (1.0, 0.0)
    assert v_sw[k_off] == pytest.approx(VIN, rel=0.01)               # まだスイッチが入っている値
    assert v_sw[k_off + 1] == pytest.approx(0.0, abs=0.01 * VIN)
    # 素子の電流は、その点の値と同じ回路状態で計算される（貫通の跡が出ない）
    assert ccm.i("S1")[k_off] == pytest.approx(ccm["i(L1)"][k_off], rel=1e-3)
    assert np.max(np.abs(ccm.i("S1") + ccm.i("D1") - ccm["i(L1)"])) < EXACT * ccm["i(L1)"].max()
    assert v_sw.min() > -0.01 * VIN and v_sw.max() < 1.01 * VIN


def test_x_long_run_stays_periodic():
    """長時間（60万ステップ、3000周期）計算しても、定常の波形が周期ごとに同じ形を保つ。"""
    result = simulate(buck(R_CCM, v0=DUTY * VIN, i0=DUTY * VIN / R_CCM), 30e-3, gates=pwm())
    assert np.isfinite(result.x).all()
    last = result["i(L1)"][-N - 1:]
    earlier = result["i(L1)"][-1001 * N - 1: -1000 * N]
    assert np.max(np.abs(last - earlier)) < 1e-6 * last.max()        # 1000周期前と同じ形
    averages = period_averages(result["v(out)"], 1000)
    assert np.ptp(averages) < 1e-6 * VIN


@pytest.mark.xfail(strict=True, reason=(
    "製品の制約（軽微、E1a-B.md の問題2）: 還流の経路が無いままインダクタ電流が遮断されると、"
    "電流は1刻みで消え、v(sw) に刻みに依存する大きさの1点（L·Δi / h。この条件で 616 V、モデルの真値は"
    " 312 kV）が記録される。結果の notes に何も出ないので、スコープでは原因が分からない"))
def test_x_interrupted_inductor_current_is_reported():
    """出力を Vin より高く充電しておくと、オンの間に逆向きの電流が流れ、オフで行き場が無くなる。"""
    result = simulate(buck(R_CCM, v0=30.0), 1e-4, gates=pwm())
    assert result["i(L1)"].min() < -0.5                   # 逆向きに 0.6 A 流れてから遮断される
    assert result["v(sw)"].max() > 10 * VIN               # 入力電圧を大きく超える点が記録される
    assert result.notes != []                             # そのことが利用者に伝わる
