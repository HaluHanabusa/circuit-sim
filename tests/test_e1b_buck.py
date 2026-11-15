"""E1b: 降圧コンバータのテンプレート buck_voltage_mode の試験（受け入れ基準3〜8と、ゲインの初期値）。

基準の回路: 同期整流、Vin 24 V、L 47 µH、C 100 µF、R_load 6 Ω、f_sw = f_ctrl = 100 kHz、Vref 12 V、
ゲインは examples/buck_voltage_mode.json（解析式の初期値を3桁に丸めたもの）。
周期平均は、搬送波の周期 T の区間 [jT, (j+1)T] を台形則で積分して T で割る（周期の整数倍の区間）。
"""
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import cont2discrete

from circuit_sim import Spec, expand
from circuit_sim.__main__ import main
from circuit_sim.templates import MissingValueError
from circuit_sim.tuning import buck_voltage_mode_pid
from circuit_sim.units import UnitError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "buck_voltage_mode.json"
VIN, L, C, R, VREF = 24.0, 47e-6, 100e-6, 6.0, 12.0
FSW = 100e3
T = 1.0 / FSW
T_END = 10e-3
T_STEP = 5e-3


def spec_dict(fidelity="switching", t_end=T_END, **changes) -> dict:
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    for path, value in changes.items():
        section, key = path.split("__")
        if value is None:
            d[section].pop(key, None)
        else:
            d[section][key] = value
    return d


def build(fidelity="switching", t_end=T_END, **changes):
    return expand(Spec.model_validate(spec_dict(fidelity, t_end, **changes)))


SCENARIOS = {
    "startup": {},
    "reference_step": {"control__Vref_step": {"value": 10, "unit": "V", "source": "手入力"},
                       "control__t_ref_step": {"value": 5, "unit": "ms", "source": "手入力"}},
    "load_step": {"plant__R_load_step": {"value": 3, "unit": "ohm", "source": "手入力"},
                  "plant__t_load_step": {"value": 5, "unit": "ms", "source": "手入力"}},
}


def period_averages(res) -> np.ndarray:
    steps = int(round(T / res.h))
    y = res["v(out)"]
    m = (len(y) - 1) // steps
    return np.array([np.trapezoid(y[j * steps: (j + 1) * steps + 1]) / steps for j in range(m)])


@pytest.fixture(scope="module")
def runs():
    """シナリオごとに、スイッチングモデルと平均モデルの結果と所要時間。"""
    out = {}
    for name, changes in SCENARIOS.items():
        for fidelity in ("switching", "average"):
            ex = build(fidelity, **changes)
            start = time.perf_counter()
            res = ex.run()
            out[name, fidelity] = (ex, res, time.perf_counter() - start)
    return out


# ------------------------------------------------------------- 基準3: 閉ループ（スイッチング）

def test_c3_closed_loop_switching_settles_to_reference(runs):
    """基準3: 起動から 5 ms 以降、周期平均の最後の 2 ms の平均が Vref と 0.2% 以内。

    5 ms 以降の周期平均の一つ一つも 0.2% 以内（整定している）であることも確かめる。
    """
    _, res, _ = runs["startup", "switching"]
    avg = period_averages(res)
    assert len(avg) == round(T_END / T)
    last = avg[-round(2e-3 / T):].mean()
    assert abs(last - VREF) <= 0.002 * VREF
    assert np.abs(avg[round(5e-3 / T):] - VREF).max() <= 0.002 * VREF


def test_c3_check_detects_wrong_reference(runs):
    """失敗すべきときに失敗する: Vref を1割ずらした値とは 0.2% を大きく超えて違う。"""
    _, res, _ = runs["startup", "switching"]
    last = period_averages(res)[-200:].mean()
    assert abs(last - 1.1 * VREF) > 10 * 0.002 * VREF


# ------------------------------------------------------------- 基準4: 平均とスイッチングの一致

@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_c4_average_model_matches_switching_period_average(runs, scenario):
    """基準4: スイッチングモデルの周期平均と平均モデル（の周期平均）の差が、全区間（最初の1周期を除く）で
    Vref の 2% 以内。"""
    _, sw, _ = runs[scenario, "switching"]
    _, av, _ = runs[scenario, "average"]
    ps, pa = period_averages(sw), period_averages(av)
    assert ps.shape == pa.shape
    assert np.abs(ps - pa)[1:].max() <= 0.02 * VREF


def test_c4_scenarios_really_step(runs):
    """段差のシナリオで、実際に指令値と負荷が変わっている（比べる区間に過渡がある）。"""
    _, ref, _ = runs["reference_step", "average"]
    assert abs(period_averages(ref)[-1] - 10.0) < 0.01
    _, load, _ = runs["load_step", "average"]
    avg = period_averages(load)
    k = round(T_STEP / T)
    assert VREF - avg[k: k + 100].min() > 0.3                  # 負荷が重くなって出力が下がる
    assert abs(load.i("R1")[-1] + load.i("R2")[-1] - VREF / 3.0) < 0.01 * VREF / 3.0


def test_c4_check_detects_wrong_average_model():
    """失敗すべきときに失敗する: L を1割ずらした平均モデルとの差が 2% を超えること。

    閉ループは差を小さくするので、ループを開いた起動（Kp = Ki = Kd = 0、d_min = 0.5 の固定デューティ）で
    確かめる（同じ条件で、正しい平均モデルとは 2% 以内）。
    """
    open_loop = {"control__Kp": {"value": 0, "unit": "1/V"}, "control__Ki": {"value": 0, "unit": "1/(V·s)"},
                 "control__Kd": {"value": 0, "unit": "s/V"}, "control__d_min": {"value": 0.5, "unit": "1"}}
    sw = period_averages(build("switching", 3e-3, **open_loop).run())
    av = period_averages(build("average", 3e-3, **open_loop).run())
    assert np.abs(sw - av)[1:].max() <= 0.02 * VREF
    bad = period_averages(build("average", 3e-3, plant__L={"value": 51.7, "unit": "uH"}, **open_loop).run())
    assert np.abs(sw - bad)[1:].max() > 0.02 * VREF


# ------------------------------------------------------------- 基準5: 平均モデルの速さ

def test_c5_average_model_uses_a_tenth_of_the_steps(runs):
    """基準5: 同じ 10 ms で、平均モデルのステップ数がスイッチングモデルの 1/10 以下。所要時間を記録する。"""
    _, sw, t_sw = runs["startup", "switching"]
    _, av, t_av = runs["startup", "average"]
    n_sw, n_av = len(sw.t) - 1, len(av.t) - 1
    assert sw.t[-1] == pytest.approx(T_END) and av.t[-1] == pytest.approx(T_END)
    assert n_av * 10 <= n_sw
    print(f"\n基準5: ステップ数 スイッチング {n_sw}、平均 {n_av}。所要時間 {t_sw:.3f} s、{t_av:.3f} s")
    for (name, fidelity), (_, res, seconds) in sorted(runs.items()):
        print(f"  {name} {fidelity}: {len(res.t) - 1} ステップ、{seconds:.3f} s")


# ------------------------------------------------------------- 基準6: 指令の遅れ

class Probe:
    """t_change 以降の測定から 0.3 を返す制御器（それ以前は 0.5）。測った値と時刻を覚える。"""

    def __init__(self, t_change):
        self.period, self.inputs, self.outputs = T, ("v(out)",), ("duty",)
        self.t_change = t_change
        self.seen = []

    def reset(self):
        self.seen = []
        return [0.5]

    def step(self, t, measured):
        self.seen.append((t, float(measured[0])))
        return [0.3 if t >= self.t_change - 1e-3 * T else 0.5]


@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_c6_command_is_used_from_the_next_control_period(fidelity):
    """基準6: 測定から指令の反映まで1制御周期（スイッチングでも平均でも）。

    t = 5T の測定から 0.3 を返すと、指令は 6T から変わる。スイッチングモデルではオンの幅が周期6から、
    平均モデルではレグの電圧 v(sw) が 6T から 0.3·Vin になる。測った値は、記録されたその時刻の値と同じ。
    """
    ex = build(fidelity, 10 * T)
    ex.controllers[0] = Probe(5 * T)
    res = ex.run()
    steps = round(T / res.h)
    cmd = res["cmd(duty)"]
    assert np.all(cmd[: 6 * steps] == 0.5) and np.all(cmd[6 * steps:] == 0.3)
    if fidelity == "switching":
        on = [int(res["gate(q1)"][p * steps: (p + 1) * steps].sum()) for p in range(10)]
        assert on == [100] * 6 + [60] * 4
    else:
        v_sw = res["v(sw)"]
        assert np.allclose(v_sw[: 6 * steps], 0.5 * VIN, rtol=0, atol=1e-9)
        assert np.allclose(v_sw[6 * steps: -1], 0.3 * VIN, rtol=0, atol=1e-9)
    for j, (t, v) in enumerate(ex.controllers[0].seen):
        assert t == pytest.approx(j * T, abs=1e-3 * res.h)
        assert v == res["v(out)"][j * steps]


# ------------------------------------------------------------- 基準7: 仕様の読み込み

def test_c7_example_reads_with_units_and_no_missing_values():
    spec = Spec.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))
    assert spec.schema_version == "0.2"
    ex = expand(spec)
    assert ex.fidelity == "switching"
    assert ex.h == pytest.approx(T / 200)                       # f_sw = 100 kHz が読まれている
    assert ex.gates[0].frequency == pytest.approx(FSW) and ex.gates[0].outputs == ["q1", "q2"]
    pid = ex.controllers[0].pid
    assert pid.period == pytest.approx(T)                       # f_ctrl = 100 kHz
    assert (pid.kp, pid.ki, pid.kd) == (0.0117, 147.0, 2.74e-6)
    assert pid.t_filter == pytest.approx(2.5e-6)                # Tf = 2.5 us
    assert (pid.limiter.lower, pid.limiter.upper) == (0.0, 0.9)
    assert any("f_sw: 100 kHz を 100000 Hz に換算した" in n for n in ex.notes)
    assert any("C_esr" in n for n in ex.notes)                  # 省略したことを知らせる
    assert [s.name for s in ex.signals if s.shown] == ["v(out)", "i(L1)"]


def test_c7_version_01_file_is_read_with_a_note():
    d = spec_dict()
    d["schema_version"] = "0.1"
    d["control"] = {"f_sw": 100e3, "f_ctrl": 100e3, "Vref": 12, "Kp": 0.0117, "Ki": 147, "Kd": 2.74e-6,
                    "Tf": 2.5e-6, "d_min": 0, "d_max": 0.9}
    spec = Spec.model_validate(d)
    assert spec.control["f_sw"].source == "記載なし" and spec.control["f_sw"].unit == ""
    ex = expand(spec)
    assert ex.h == pytest.approx(T / 200)
    assert any("版 0.1" in n and "f_sw [Hz]" in n for n in ex.notes)
    assert Spec.model_validate_json(spec.model_dump_json()) == spec      # 書き出して読み直しても同じ
    d["control"]["f_sw"] = "100k"
    with pytest.raises(ValueError, match="数値でない"):
        Spec.model_validate(d)


def test_c7_control_units_are_checked():
    with pytest.raises(UnitError, match="f_sw"):
        build(control__f_sw={"value": 100, "unit": "kV"})
    with pytest.raises(UnitError, match="Kp"):
        build(control__Kp={"value": 0.01, "unit": "1/A"})
    ex = build(control__d_max={"value": 90, "unit": "%"})
    assert ex.controllers[0].pid.limiter.upper == pytest.approx(0.9)


def test_missing_values_are_reported_together_and_not_guessed():
    with pytest.raises(MissingValueError) as info:
        build(control__Kp=None, plant__L={"value": None, "unit": "H"},
              plant__R_load_step={"value": 3, "unit": "ohm"})
    assert info.value.names == ["L", "Kp", "t_load_step"]


def test_esr_is_used_when_given():
    ex = build(plant__C_esr={"value": 20, "unit": "mohm", "source": "手入力"})
    assert ex.netlist.component("R_esr").value == pytest.approx(0.02)
    assert ex.netlist.component("C1").nodes == ("cap", "0")
    assert not any("C_esr" in n and "計算に入れていない" in n for n in ex.notes)


def test_template_value_checks():
    with pytest.raises(ValueError, match="整数倍"):
        build(control__f_ctrl={"value": 30, "unit": "kHz"})
    with pytest.raises(ValueError, match="d_min"):
        build(control__d_min={"value": 0.95, "unit": "1"})
    ex = build(control__Vref={"value": 23, "unit": "V"})
    assert any("届かない" in n for n in ex.notes)
    ex = build("average")
    assert any("平均モデル" in n for n in ex.notes)
    assert "V1" not in [c.id for c in ex.netlist.components]


def test_command_line_runs_the_example(capsys):
    assert main([str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "buck_voltage_mode" in out and "v(out)" in out


# ------------------------------------------------------------- 基準8: 再現性

@pytest.mark.parametrize("fidelity", ["switching", "average"])
def test_c8_reproducible(fidelity):
    """基準8: 同じ入力で2回実行して完全一致（同じ制御器のオブジェクトを使い回しても同じ）。"""
    changes = {**SCENARIOS["load_step"], "plant__t_load_step": {"value": 1, "unit": "ms"}}
    ex = build(fidelity, 2e-3, **changes)
    a, b = ex.run(), ex.run()
    c = build(fidelity, 2e-3, **changes).run()
    for other in (b, c):
        assert np.array_equal(a.x, other.x)
        assert a.names == other.names
        for name in a.signals:
            assert np.array_equal(a[name], other[name])


# ------------------------------------------------------------- ゲインの初期値

def discrete_margins(kp, ki, kd, tf, r_load=R, inductance=L, capacitance=C):
    """位相余裕と、その交差周波数（試験の中で別に組んだ離散の開ループ）。

    植物: scipy.signal.cont2discrete の零次ホールド（PWMの保持 0.5 サンプル分）、演算の遅れ z⁻¹、
    PID: 実装の差分方程式の z 変換  Kp + Ki·Ts/(1 − z⁻¹) + Kd·(1 − z⁻¹)/((Tf + Ts) − Tf·z⁻¹)。
    ゲインが 1 を横切る点すべての位相余裕のうち最小のもの。
    """
    a = np.array([[0.0, -1.0 / inductance], [1.0 / capacitance, -1.0 / (r_load * capacitance)]])
    b = np.array([[VIN / inductance], [0.0]])
    ad, bd, cd, _, _ = cont2discrete((a, b, np.array([[0.0, 1.0]]), np.zeros((1, 1))), T, method="zoh")
    w = np.geomspace(10.0, 0.999 * math.pi / T, 40000)
    z = np.exp(1j * w * T)
    p = np.array([(cd @ np.linalg.solve(zz * np.eye(2) - ad, bd))[0, 0] for zz in z])
    cz = kp + ki * T / (1 - 1 / z) + kd * (1 - 1 / z) / ((tf + T) - tf / z)
    loop = cz * p / z
    mag, phase = np.abs(loop), np.unwrap(np.angle(loop))
    cross = np.nonzero(np.diff(np.sign(mag - 1.0)))[0]
    margins = np.degrees(phase[cross]) + 180.0
    return margins.min(), w[cross[np.argmin(margins)]] / (2 * math.pi)


def test_example_gains_have_45_degrees_of_phase_margin():
    """例のゲイン（解析式の初期値を3桁に丸めたもの）は、遅れ1.5サンプルを含めて位相余裕 45° 以上。"""
    pm, fc = discrete_margins(0.0117, 147.0, 2.74e-6, 2.5e-6)
    assert pm >= 45.0
    assert 2e3 < fc < 6e3
    for r_load in (3.0, 60.0):                                   # 負荷の段差の先と軽負荷でも
        assert discrete_margins(0.0117, 147.0, 2.74e-6, 2.5e-6, r_load=r_load)[0] >= 45.0


def test_initial_gains_formula_is_deterministic_and_meets_target():
    g = buck_voltage_mode_pid(VIN, L, C, R, FSW)
    assert g == buck_voltage_mode_pid(VIN, L, C, R, FSW)
    assert g.phase_margin_deg == pytest.approx(55.0, abs=1e-6)
    pm, fc = discrete_margins(g.kp, g.ki, g.kd, g.t_filter)
    assert pm == pytest.approx(g.phase_margin_deg, abs=0.5)      # 別に組んだ開ループでも同じ余裕
    assert fc == pytest.approx(g.f_cross, rel=0.01)
    # 例のゲインは、この値を3桁に丸めたもの
    assert (round(g.kp, 4), round(g.ki), float(f"{g.kd:.3g}")) == (0.0117, 147, 2.74e-6)
