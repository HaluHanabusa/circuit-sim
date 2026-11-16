"""E1g の独立検証の受け入れ試験が共通で使う回路と道具（試験そのものではない）。

回路は検証役が自分で組む（実装の試験の回路を写さない）。テンプレートの仕様も、サンプルのファイルを読まずに
ここで作る。
"""
from __future__ import annotations

import contextlib
import importlib
import math
import time

import numpy as np

from circuit_sim import DC, Command, Component, Netlist, Pwm, Sine, Spec, Step
from circuit_sim.control import Pid, PidController

sim_module = importlib.import_module("circuit_sim.simulate")     # 名前 simulate は関数を指すので


def comp(cid, typ, a, b, value=None, **kw):
    return Component(id=cid, type=typ, nodes=(a, b), value=value, **kw)


def vsrc(cid, a, b, waveform):
    return Component(id=cid, type="V", nodes=(a, b), waveform=waveform)


def isrc(cid, a, b, waveform):
    return Component(id=cid, type="I", nodes=(a, b), waveform=waveform)


def net(*comps):
    return Netlist(components=list(comps))


def rc(waveform=None, r=1e3, c=1e-6, ic=0.0):
    return net(vsrc("V1", "in", "0", waveform or Step(final=1.0)), comp("R1", "R", "in", "out", r),
               comp("C1", "C", "out", "0", c, ic=ic))


def rlc(r, ind=1e-3, c=10e-6, waveform=None, il=0.0, vc=0.0):
    return net(vsrc("V1", "in", "0", waveform or Step(final=10.0)), comp("R1", "R", "in", "a", r),
               comp("L1", "L", "a", "out", ind, ic=il), comp("C1", "C", "out", "0", c, ic=vc))


def ladder(n, waveform=None):
    """L − R − C のはしご n 段（未知数 3n + 2）。"""
    comps = [vsrc("V1", "n0", "0", waveform or Step(final=1.0))]
    for j in range(n):
        comps.append(comp(f"L{j}", "L", f"n{j}", f"m{j}", 1e-3 * (1 + 0.1 * (j % 7))))
        comps.append(comp(f"R{j}", "R", f"m{j}", f"n{j + 1}", 0.5))
        comps.append(comp(f"C{j}", "C", f"n{j + 1}", "0", 1e-6 * (1 + 0.05 * (j % 5))))
    comps.append(comp("RL", "R", f"n{n}", "0", 50.0))
    return net(*comps)


def buck_diode(r=6.0, ind=47e-6, c=100e-6, vc0=0.0, vin=24.0):
    """スイッチ S1 と還流ダイオード D1 の降圧（開ループで使う）。"""
    return net(vsrc("V1", "in", "0", DC(value=vin)), comp("S1", "S", "in", "sw", gate="g"),
               comp("D1", "D", "0", "sw"), comp("L1", "L", "sw", "out", ind),
               comp("C1", "C", "out", "0", c, ic=vc0), comp("R1", "R", "out", "0", r))


def gate(duty=0.5, frequency=1e5, carrier="sawtooth"):
    return [Pwm(name="g", frequency=frequency, duty=duty, carrier=carrier)]


def rectifier():
    """三相のダイオード整流（スイッチなし、ダイオード 6 個、正弦波の電源）。"""
    comps = []
    for j, ph in enumerate("abc"):
        wave = Sine(amplitude=100.0, frequency=50.0, phase_deg=-120.0 * j)
        comps.append(vsrc(f"V{ph}", f"s{ph}", "n", wave))
        comps.append(comp(f"L{ph}", "L", f"s{ph}", ph, 1e-3))
        comps.append(comp(f"Dp{ph}", "D", ph, "p"))
        comps.append(comp(f"Dn{ph}", "D", "0", ph))
    comps += [comp("Rn", "R", "n", "0", 1e6), comp("C1", "C", "p", "0", 470e-6),
              comp("R1", "R", "p", "0", 20.0)]
    return net(*comps)


# 降圧コンバータの基準の値（サンプルと同じ値を、ここに書く）
VIN, L_BUCK, C_BUCK, R_BUCK, R_ON, T_CTRL = 24.0, 47e-6, 100e-6, 6.0, 1e-3, 1e-5


def avg_buck(lower=0.0, upper=1.0):
    """降圧コンバータの状態平均化モデルを手で組んだもの。lower / upper = None で、電源の上下限を外す。"""
    return net(vsrc("Vleg", "sw_avg", "0", Command(command="duty", gain=VIN, lower=lower, upper=upper)),
               comp("Rleg", "R", "sw_avg", "sw", R_ON), comp("L1", "L", "sw", "out", L_BUCK),
               comp("C1", "C", "out", "0", C_BUCK), comp("R1", "R", "out", "0", R_BUCK))


def pid_controller(kp, ki, kd, lower=-math.inf, upper=math.inf, vref=12.0, t_filter=2.5e-6):
    pid = Pid(kp, ki, kd, period=T_CTRL, t_filter=t_filter, lower=lower, upper=upper)
    return PidController(pid, "v(out)", "duty", vref)


def q(value, unit, source="手入力"):
    return {"value": value, "unit": unit, "source": source}


def buck_dict(fidelity="switching", plant=None, control=None, sim=None, drop=()):
    p = {"Vin": q(24, "V"), "L": q(47, "uH"), "C": q(100, "uF"), "R_load": q(6, "ohm")}
    c = {"f_sw": q(100, "kHz"), "f_ctrl": q(100, "kHz"), "Vref": q(12, "V"), "Kp": q(0.0117, "1/V"),
         "Ki": q(147, "1/(V·s)"), "Kd": q(2.74e-6, "s/V"), "Tf": q(2.5, "us"), "d_min": q(0, "1"),
         "d_max": q(0.9, "1")}
    p.update(plant or {})
    c.update(control or {})
    for key in drop:
        p.pop(key, None)
        c.pop(key, None)
    return {"schema_version": "0.2", "template": "buck_voltage_mode", "plant": p, "control": c,
            "simulation": {"fidelity": fidelity, **(sim or {})}}


def buck_spec(*args, **kwargs):
    return Spec.model_validate(buck_dict(*args, **kwargs))


RC_PLANT = {"Vin": q(5, "V"), "R": q(1, "kohm"), "C": q(1, "uF")}
RLC_PLANT = {"Vin": q(10, "V"), "R": q(5, "ohm"), "L": q(1, "mH"), "C": q(10, "uF")}


def t0_spec(template, plant, sim=None):
    return Spec.model_validate({"template": template, "plant": plant, "simulation": sim or {}})


def signature(res) -> list[bytes]:
    """結果の全部（時刻、変数、信号、状態、数え、注意、信号の情報）を、ビット単位で比べられる形にする。"""
    parts = [res.t.tobytes(), res.x.tobytes()]
    for name, values in res.signals.items():
        parts.append(name.encode() + np.asarray(values).tobytes())
    if res.sample_state is not None:
        parts.append(res.sample_state.tobytes())
    parts.append(repr(sorted(res.stats.items())).encode())
    parts.append("\n".join(str(n) for n in res.notes).encode())
    parts.append(repr(list(res.signal_info.items())).encode())
    return parts


@contextlib.contextmanager
def chunk_steps(steps: int | None = None, setup_block: int | None = None):
    """区切りのステップ数を固定する（steps を大きくすれば「区切らない」）。

    steps が None なら、区切りは既定のまま（時間で決まる）。setup_block は、準備と後処理の配列を埋める
    単位の行数（None なら既定のまま）。"""
    names = ("CHUNK_STEPS_FIRST", "CHUNK_STEPS_MIN", "CHUNK_STEPS_MAX", "SETUP_BLOCK")
    saved = {name: getattr(sim_module, name) for name in names}
    try:
        if steps is not None:
            sim_module.CHUNK_STEPS_FIRST = sim_module.CHUNK_STEPS_MIN = sim_module.CHUNK_STEPS_MAX = steps
        if setup_block is not None:
            sim_module.SETUP_BLOCK = setup_block
        yield
    finally:
        for name, value in saved.items():
            setattr(sim_module, name, value)


UNCHUNKED = 1 << 60


class Lookup:
    """測定値によらず、制御周期ごとに決まった値を出す制御器（開ループ）。"""

    inputs = ()

    def __init__(self, values, output="u", period=1e-3):
        self.values, self.outputs, self.period = list(values), (output,), float(period)

    def reset(self):
        self.j = 0
        return [self.values[0]]

    def step(self, t, measured):
        self.j += 1
        return [self.values[min(self.j, len(self.values) - 1)]]


class Feedback:
    """u[j+1] = gain·測定値[j]（上下限なし）。gain と回路の利得の積が 1 を超えると発散する。"""

    def __init__(self, gain, measure="v(out)", output="u", period=1e-3, first=0.1):
        self.gain, self.inputs, self.outputs = gain, (measure,), (output,)
        self.period, self.first = period, first

    def reset(self):
        self.seen = []
        return [self.first]

    def step(self, t, measured):
        self.seen.append(float(measured[0]))
        return [self.gain * float(measured[0])]


def watch_with_work(run, controller, period, t_end, delay=0.0):
    """進み具合と、そのとき済みうる計算の上限を記録する。

    戻り値: （[(進み具合, 上限)]、知らせの間隔の最大 [s]）。

    済んだ計算は、制御器が呼ばれた時刻で外から測る。エンジンは、制御周期 [t, t + period] を計算し終えてから
    時刻 t の測定で制御器を呼び、続けて次の周期を計算する。だから、制御器が最後に時刻 t で呼ばれた後に
    済みうるのは t + 2·period まで（まだ呼ばれていなければ、最初の 1 周期まで）。
    delay [s] を渡すと、制御器の演算のたびに、その時間だけ待つ（演算に時間のかかる制御器の代わり）。
    """
    last = {"t": -period}
    original = controller.step

    def step(t, measured):
        last["t"] = t
        if delay:
            time.sleep(delay)
        return original(t, measured)

    controller.step = step
    seen, stamps = [], []
    start = time.perf_counter()

    def progress(fraction):
        stamps.append(time.perf_counter())
        seen.append((fraction, min(1.0, (last["t"] + 2 * period) / t_end)))

    try:
        run(progress)
    finally:
        controller.step = original
    gaps = np.diff([start] + stamps + [time.perf_counter()])
    return seen, float(gaps.max())


def check_progress_against_work(seen, ahead=0.05):
    """進み具合が 0 から 1 へ単調に増え、済みうる計算の割合を大きく超えない（最後の 1 を除く）。

    ahead は、許す先行（準備 0.02 と後処理 0.01 の割り当てと、丸めの分）。
    """
    values = [fraction for fraction, _ in seen]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b > a for a, b in zip(values, values[1:], strict=False)), values
    for fraction, done in seen[:-1]:
        # 時間ステップが済みうる前に、後処理の割り当て（0.99〜1）に入らない
        assert done >= 1.0 or fraction < 0.99, (fraction, done)
        assert fraction <= done + ahead, (fraction, done)
