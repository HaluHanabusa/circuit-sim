"""E1b: 降圧コンバータのテンプレート buck_voltage_mode の試験（受け入れ基準3〜8と、ゲインの初期値）。

基準の回路: 同期整流、Vin 24 V、L 47 µH、C 100 µF、R_load 6 Ω、f_sw = f_ctrl = 100 kHz、Vref 12 V、
ゲインは examples/buck_voltage_mode.json（解析式の初期値を3桁に丸めたもの）。
周期平均は、搬送波の周期 T の区間 [jT, (j+1)T] を台形則で積分して T で割る（周期の整数倍の区間）。
"""
import json
import math
import time
import typing
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError
from scipy.signal import cont2discrete

import circuit_sim
from circuit_sim import Quantity, Spec, expand
from circuit_sim.__main__ import main
from circuit_sim.control import Pid
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


def ripple_offset(v_in, v_out, f_sw=FSW, inductance=L, capacitance=C):
    """三角波の測定点（周期の頭 = オフの区間の中央）のずれ: Vref − 周期平均 = ΔI·(1 + D)/(24·f·C)。

    測定点ではインダクタ電流が平均に等しく、コンデンサ電流が 0 から負に変わるので、コンデンサ電圧は山の頂点。
    頂点から周期平均までの差は、コンデンサ電流（三角波）を2回積分して求めた（D = 0.5 で ΔI/(16·f·C)）。
    """
    d = v_out / v_in
    delta_i = (v_in - v_out) * d / (inductance * f_sw)
    return delta_i * (1.0 + d) / (24.0 * f_sw * capacitance)


@pytest.mark.parametrize("vref", [12.0, 6.0])
def test_triangle_sampling_puts_switching_average_below_reference(runs, vref):
    """三角波の測定点のずれ（物理として正しい効果。legs.py の説明）: スイッチングモデルの定常の周期平均は
    Vref より ΔI·(1 + D)/(24·f·C) 低い（D = 0.5 で 7.98 mV、D = 0.25 で 4.99 mV）。平均モデルはずれない。

    区間: 最後の 2 ms（起動の過渡は 5 ms までに収まる。基準3）。許容差 0.3 mV: デューティの分解能 1/200 で
    オンの区間の位置が1刻み揺れる分（D = 0.25 で 0.1 mV。刻みを T/800 にすると 0.01 mV）の3倍。
    D = 0.5 だけで成り立つ ΔI/(16·f·C) とは D = 0.25 で 1.0 mV 違うので、式の誤りは見分けられる。
    """
    if vref == VREF:
        sw, av = runs["startup", "switching"][1], runs["startup", "average"][1]
    else:
        sw = build("switching", control__Vref={"value": vref, "unit": "V"}).run()
        av = build("average", control__Vref={"value": vref, "unit": "V"}).run()
    last = period_averages(sw)[-round(2e-3 / T):].mean()
    assert vref - last == pytest.approx(ripple_offset(VIN, vref), abs=0.3e-3)
    assert period_averages(av)[-round(2e-3 / T):].mean() == pytest.approx(vref, abs=0.05e-3)


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
        # レグの電源の電圧 v(sw_avg) が d·Vin。スイッチの節点 v(sw) は、オン抵抗の電圧降下だけ低い
        # 変わり目 6T の点は、変わる直前の値（指令で値が決まる電源の記録の決まり。D-048。E1g）
        v_src = res["v(sw_avg)"]
        assert np.allclose(v_src[: 6 * steps + 1], 0.5 * VIN, rtol=0, atol=1e-9)
        assert np.allclose(v_src[6 * steps + 1: -1], 0.3 * VIN, rtol=0, atol=1e-9)
        r_on = ex.netlist.component("Rleg").value
        assert np.allclose(res["v(sw)"], v_src - r_on * res["i(L1)"], rtol=0, atol=1e-9)
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


def test_messages_read_naturally():
    """利用者に出る文言: 無次元の量に単位の記号を付けない、制御の単位に接頭語を勧めない、
    負荷の段差の抵抗が小さすぎるときは「小さすぎる」と言う。"""
    ex = build(control__d_max={"value": 90, "unit": "%"})
    assert "d_max: 90 % を 0.9 に換算した" in ex.notes
    for unit in ("m1/V", "V"):                                   # 読めない単位、量の種類が違う単位
        with pytest.raises(UnitError) as info:
            build(control__Kp={"value": 1, "unit": unit})
        assert "1/V と書く。接頭語は付けない" in str(info.value) and "接頭語つき" not in str(info.value)
    with pytest.raises(UnitError) as info:
        build(control__d_max={"value": 1, "unit": "V"})
    assert "1 か % と書く" in str(info.value)
    with pytest.raises(UnitError) as info:                       # 回路の値は今までどおり接頭語を勧める
        build(plant__L={"value": 47, "unit": "uF"})
    assert "接頭語つきで書く" in str(info.value)
    step = {"plant__t_load_step": {"value": 5, "unit": "ms"}}
    with pytest.raises(ValueError) as info:
        build(plant__R_load_step={"value": 0.9, "unit": "mohm"}, **step)
    assert "R_load_step（0.0009 Ω）が小さすぎて" in str(info.value) and "近すぎ" not in str(info.value)
    with pytest.raises(ValueError) as info:
        build(plant__R_load={"value": 0.9, "unit": "mohm"}, plant__R_load_step={"value": 6, "unit": "ohm"},
              **step)
    assert "R_load（0.0009 Ω）が小さすぎて" in str(info.value)
    with pytest.raises(ValueError) as info:
        build(plant__L={"value": -1, "unit": "uH"}, control__Kd={"value": -1, "unit": "s/V"})
    assert "L は 0 より大きくする" in str(info.value) and "Kd は 0 以上にする" in str(info.value)


@pytest.mark.parametrize("mode", ["error", "measurement"])
def test_derivative_on_measurement_does_not_kick_at_the_reference_step(mode):
    """微分を測定値に掛けると、指令値の段差（12 → 10 V）でデューティが跳ねない。既定は偏差に掛ける。

    段差の直後の制御周期の、デューティの変化（期待値はこの試験の中の式。y は1周期ではほとんど変わらない）:
      偏差に掛ける : (Kp + Ki·Ts + Kd/(Tf + Ts))·ΔVref = 0.465（0.5 → 0.035）
      測定値に掛ける: (Kp + Ki·Ts)·ΔVref = 0.026
    """
    ex = build("average", 6e-3, control__Vref_step={"value": 10, "unit": "V"},
               control__t_ref_step={"value": 5, "unit": "ms"})
    old = ex.controllers[0].pid
    assert old.derivative_on == "error"                          # テンプレートの既定（仕様どおり）
    if mode == "measurement":
        ex.controllers[0].pid = Pid(old.kp, old.ki, old.kd, period=old.period, t_filter=old.t_filter,
                                    lower=old.limiter.lower, upper=old.limiter.upper, derivative_on=mode)
    res = ex.run()
    n = round(T / res.h)
    cmd = res["cmd(duty)"][::n]
    k = round(5e-3 / T) + 1                                      # t = 5 ms の測定の結果が使われる周期
    kp, ki, kd, tf = old.kp, old.ki, old.kd, old.t_filter
    gain = kp + ki * T + (kd / (tf + T) if mode == "error" else 0.0)
    assert cmd[k - 1] - cmd[k] == pytest.approx(gain * 2.0, abs=2e-3)
    if mode == "measurement":
        assert cmd[k:].min() > 0.4
    else:
        assert cmd[k] < 0.05


def test_file_without_version_is_read_by_its_contents():
    """版の欄が無いファイル: control の値が数値なら 0.1（注意を出す）、Quantity なら 0.2 として読む。"""
    d = spec_dict()
    d.pop("schema_version")
    spec = Spec.model_validate_json(json.dumps(d))
    assert (spec.schema_version, spec.version_inferred) == ("0.2", True)
    assert not any("版 0.1" in n for n in expand(spec).notes)
    d["control"] = {k: q["value"] for k, q in d["control"].items()}
    d["control"]["f_sw"], d["control"]["f_ctrl"], d["control"]["Tf"] = 100e3, 100e3, 2.5e-6
    spec = Spec.model_validate_json(json.dumps(d))
    assert (spec.schema_version, spec.version_inferred) == ("0.1", True)
    assert spec.control["Kp"].unit == "" and spec.control["Kp"].source == "記載なし"
    notes = expand(spec).notes
    assert any("版（schema_version）が無く" in n and "版 0.1 として読んだ" in n for n in notes)
    d["schema_version"] = "0.1"                                  # 版を書いたファイルは、そう書かない
    spec = Spec.model_validate(d)
    assert not spec.version_inferred
    assert not any("schema_version" in n for n in expand(spec).notes)
    assert any("版 0.1 の形" in n for n in expand(spec).notes)


def test_spec_built_from_quantity_objects_without_version_is_version_02():
    """Python で Quantity のオブジェクトから版の欄なしで作った仕様は、版 0.2（0.1 と推定して落ちない）。"""
    d = spec_dict()
    plant = {k: Quantity(**q) for k, q in d["plant"].items()}
    control = {k: Quantity(**q) for k, q in d["control"].items()}
    spec = Spec(template="buck_voltage_mode", plant=plant, control=control, simulation=d["simulation"])
    assert (spec.schema_version, spec.version_inferred) == ("0.2", True)
    ex = expand(spec)
    assert not any("版 0.1" in n for n in ex.notes)
    assert ex.controllers[0].pid.kp == 0.0117
    legacy = Spec(schema_version="0.1", template="buck_voltage_mode", plant=plant,   # 版 0.1 に混ぜても読める
                  control={**control, "Kp": 0.0117})
    assert legacy.control["Kp"].source == "記載なし" and legacy.control["Ki"] == control["Ki"]


def test_supported_schema_versions_are_exactly_the_readable_ones():
    """公開の一覧 SUPPORTED_SCHEMA_VERSIONS が、実際に読める版と一致する。

    書き出す版 SCHEMA_VERSION はその最新。

    一覧の版はすべて読め、一覧に無い版（前後の番号、書き方の揺れ）はすべて断られる。
    """
    supported = circuit_sim.SUPPORTED_SCHEMA_VERSIONS
    assert circuit_sim.SCHEMA_VERSION == supported[-1] == "0.2"
    assert set(typing.get_args(Spec.model_fields["schema_version"].annotation)) == set(supported)
    rc = {"template": "rc_lowpass",
          "plant": {"Vin": {"value": 5, "unit": "V"}, "R": {"value": 1, "unit": "kohm"},
                    "C": {"value": 1, "unit": "uF"}}}
    candidates = [f"{major}.{minor}" for major in range(3) for minor in range(10)]
    candidates += ["0.10", "0.2.0", " 0.2", ""]
    for version in candidates:
        try:
            spec = Spec.model_validate({**rc, "schema_version": version})
        except ValidationError:
            assert version not in supported, version
        else:
            assert version in supported, version
            assert spec.schema_version == version and expand(spec).netlist.component("R1").value == 1000.0
    for version in supported:                                    # 降圧コンバータの例も、どの版でも読める
        d = spec_dict(t_end=1e-4)
        d["schema_version"] = version
        if version == "0.1":
            d["control"] = {k: q["value"] for k, q in d["control"].items()}
            d["control"].update(f_sw=100e3, f_ctrl=100e3, Tf=2.5e-6)
        assert expand(Spec.model_validate(d)).controllers[0].pid.kp == 0.0117
    assert Spec.model_validate(rc).model_dump()["schema_version"] == circuit_sim.SCHEMA_VERSION


@pytest.mark.parametrize("template,plant", [
    ("rc_lowpass", {"Vin": (5, "V"), "R": (1, "kohm"), "C": (1, "uF")}),
    ("rlc_series", {"Vin": (10, "V"), "R": (1, "ohm"), "L": (1, "mH"), "C": (10, "uF")}),
])
def test_templates_without_switches_say_they_ignored_fidelity(template, plant):
    d = {"template": template, "plant": {k: {"value": v, "unit": u} for k, (v, u) in plant.items()}}
    assert not any("fidelity" in n for n in expand(Spec.model_validate(d)).notes)
    d["simulation"] = {"fidelity": "average"}
    ex = expand(Spec.model_validate(d))
    assert any("simulation.fidelity" in n and "average" in n and "使わなかった" in n for n in ex.notes)
    assert ex.fidelity is None


def test_stats_count_command_changes_apart_from_state_changes():
    """stats: 指令で決まる電源の値の変化（command_changes）を、回路状態の変化（state_changes）と分けて数える。

    平均モデル（スイッチはレグに無く、負荷の段差のスイッチ S3 だけ）: state_changes は S3 が入る1回。
    command_changes は、上下限で止めた指令が前の制御周期と変わった回数（この試験で cmd(duty) から数える）。
    スイッチングモデル: 指令で決まる電源が無いので command_changes は 0。
    """
    changes = {"plant__R_load_step": {"value": 3, "unit": "ohm"},
               "plant__t_load_step": {"value": 1, "unit": "ms"}}
    res = build("average", 2e-3, **changes).run()
    n = round(T / res.h)
    duty = np.clip(res["cmd(duty)"][: -1: n], 0.0, 1.0)
    assert res.stats["command_changes"] == int(np.count_nonzero(np.diff(duty)))
    assert res.stats["command_changes"] > 100
    assert res.stats["state_changes"] == 1
    sw = build("switching", 0.2e-3).run()
    assert sw.stats["command_changes"] == 0
    toggles = int(np.count_nonzero(np.diff(sw["gate(q1)"][:-1])))
    assert sw.stats["state_changes"] == toggles


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

    制御対象: scipy.signal.cont2discrete の零次ホールド（PWMの保持 0.5 サンプル分）、演算の遅れ z⁻¹、
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
