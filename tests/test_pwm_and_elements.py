"""E1a の部品の試験: 層Bのスイッチとダイオード、PWMのゲート信号。"""
import numpy as np
import pytest
from pydantic import ValidationError

from circuit_sim import DC, Component, Netlist, Pwm, build_system, simulate
from circuit_sim.control import Controller, gate_pattern, on_steps
from circuit_sim.mna import state_conductance

# ---------------------------------------------------------------- 層Bの素子


def test_switch_and_diode_defaults():
    s = Component(id="S1", type="S", nodes=("a", "b"), gate="q1")
    d = Component(id="D1", type="D", nodes=("a", "b"))
    assert (s.r_on, s.r_off) == (1e-3, 1e6)            # 既定は 1 mΩ と 1 MΩ
    assert (d.r_on, d.r_off) == (1e-3, 1e6)
    custom = Component(id="D2", type="D", nodes=("a", "b"), r_on=0.05, r_off=2e5)
    assert (custom.r_on, custom.r_off) == (0.05, 2e5)
    # JSON にして戻しても同じ
    assert Component.model_validate_json(s.model_dump_json()) == s


def test_switch_and_diode_validation():
    with pytest.raises(ValidationError, match="gate が必要"):
        Component(id="S1", type="S", nodes=("a", "b"))
    with pytest.raises(ValidationError, match="gate を指定できるのは S だけ"):
        Component(id="D1", type="D", nodes=("a", "b"), gate="q1")
    with pytest.raises(ValidationError, match="r_on / r_off を指定できるのは S と D だけ"):
        Component(id="R1", type="R", nodes=("a", "b"), value=1.0, r_on=1e-3)
    with pytest.raises(ValidationError, match="正の有限値"):
        Component(id="D1", type="D", nodes=("a", "b"), r_on=0.0)
    with pytest.raises(ValidationError, match="より小さくする"):
        Component(id="D1", type="D", nodes=("a", "b"), r_on=10.0, r_off=1.0)
    with pytest.raises(ValidationError, match="value / waveform は指定できない"):
        Component(id="D1", type="D", nodes=("a", "b"), value=1.0)
    with pytest.raises(ValidationError, match="両端が同じ"):
        Component(id="D1", type="D", nodes=("a", "a"))


def test_state_conductance_uses_on_and_off_resistance():
    """状態の符号のビットは S が先、D が後。1 のビットの素子はオン抵抗、0 はオフ抵抗になる。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="D1", type="D", nodes=("0", "sw"), r_on=0.5, r_off=200.0),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q1", r_on=0.25, r_off=100.0),
        Component(id="R1", type="R", nodes=("sw", "0"), value=4.0),
    ])
    system = build_system(net)
    assert system.devices == ["S1", "D1"] and system.n_switches == 1
    sw = system.unknowns.index("v(sw)")
    base = 1 / 4.0
    expected = {
        0b00: 1 / 100 + 1 / 200,      # S オフ、D オフ
        0b01: 1 / 0.25 + 1 / 200,     # S オン、D オフ
        0b10: 1 / 100 + 1 / 0.5,      # S オフ、D オン
        0b11: 1 / 0.25 + 1 / 0.5,     # S オン、D オン
    }
    for code, g in expected.items():
        assert state_conductance(system, code)[sw, sw] == pytest.approx(base + g)
    assert system.G[sw, sw] == pytest.approx(base)       # 元の G には S と D が入らない


# -------------------------------------------------------------------- PWM


def test_on_steps_rounds_to_nearest_grid_point():
    assert on_steps(0.5, 200) == 100
    assert on_steps(0.333, 200) == 67          # 66.6 → 67
    assert on_steps(0.332, 200) == 66          # 66.4 → 66
    assert on_steps(0.0, 200) == 0
    assert on_steps(1.0, 200) == 200
    assert on_steps(1.3, 200) == 200           # 1 で止める
    assert on_steps(-0.2, 200) == 0            # 0 で止める


def test_sawtooth_pattern_turns_on_at_period_start():
    k = np.arange(20)
    pattern = gate_pattern("sawtooth", k, 10, 3)
    assert pattern.astype(int).tolist() == [1, 1, 1, 0, 0, 0, 0, 0, 0, 0] * 2


def test_triangle_pattern_is_centered():
    k = np.arange(10)
    assert gate_pattern("triangle", k, 10, 4).astype(int).tolist() == [0, 0, 0, 1, 1, 1, 1, 0, 0, 0]
    assert gate_pattern("triangle", k, 10, 0).sum() == 0
    assert gate_pattern("triangle", k, 10, 10).sum() == 10
    # オンの数が奇数で残りも奇数のときは、半ステップ前に寄る
    assert gate_pattern("triangle", k, 10, 3).astype(int).tolist() == [0, 0, 0, 1, 1, 1, 0, 0, 0, 0]


def test_pwm_validation():
    with pytest.raises(ValidationError, match="0〜1"):
        Pwm(name="q1", frequency=1e5, duty=1.5)
    with pytest.raises(ValidationError):
        Pwm(name="q1", frequency=0.0, duty=0.5)
    with pytest.raises(ValidationError):
        Pwm(name="q1", frequency=1e5, duty=0.5, carrier="sine")
    assert Pwm(name="q1", frequency=1e5, duty="duty").duty == "duty"


@pytest.mark.parametrize("carrier", ["sawtooth", "triangle"])
def test_pwm_switch_into_resistor(carrier):
    """動きの無い回路（電源、スイッチ、抵抗）: 負荷電圧はゲートどおりの矩形波で、平均は D·Vin。

    記録の決まりにより、区間 [t[k], t[k+1]) のゲートの結果は、次の点 k+1 の値に現れる。
    """
    v_in, load, f, n, duty = 10.0, 50.0, 1e3, 40, 0.3
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=v_in)),
        Component(id="S1", type="S", nodes=("in", "out"), gate="q1"),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
    ])
    gates = [Pwm(name="q1", frequency=f, duty=duty, carrier=carrier)]
    res = simulate(net, 3 / f, gates=gates, steps_per_period=n)
    gate = res["gate(q1)"]
    n_on = 12                                                   # 0.3 × 40
    first = 0 if carrier == "sawtooth" else (n - n_on) // 2     # 三角波はオンの区間が周期の中央
    expected_gate = [(first <= k % n < first + n_on) for k in range(3 * n)]
    assert gate[:-1].astype(bool).tolist() == expected_gate
    assert gate[-1] == gate[-2]                                 # 最後の点は、最後の区間の値を繰り返す
    v_on = v_in * load / (load + 1e-3)
    v_off = v_in * load / (load + 1e6)
    expected_v = np.where(gate[:-1] == 1.0, v_on, v_off)
    assert np.allclose(res["v(out)"][1:], expected_v, rtol=1e-9, atol=1e-12)
    assert res["v(out)"][1:].mean() == pytest.approx(duty * v_on + (1 - duty) * v_off, rel=1e-9)
    assert np.array_equal(res["on(S1)"], gate)


def test_gate_and_state_signals_are_exported(tmp_path):
    """ゲート信号と導通状態も、回路の変数と同じ形で取り出せて、CSVにも書ける。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=10.0)),
        Component(id="S1", type="S", nodes=("in", "out"), gate="q1"),
        Component(id="R1", type="R", nodes=("out", "0"), value=50.0),
    ])
    res = simulate(net, 2e-3, gates=[Pwm(name="q1", frequency=1e3, duty=0.5)], steps_per_period=10)
    assert res.names == ["v(in)", "v(out)", "i(V1)", "gate(q1)", "on(S1)"]
    with pytest.raises(KeyError, match="使える名前"):
        res["gate(nothing)"]
    path = tmp_path / "out.csv"
    res.to_csv(path, ["v(out)", "gate(q1)"])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t,v(out),gate(q1)"
    assert len(lines) == len(res.t) + 1
    # スイッチの電流は負荷の電流と同じ（直列）
    assert np.allclose(res.i("S1"), res.i("R1"), rtol=1e-9, atol=1e-12)


def test_controller_protocol_is_structural():
    """制御器は、決まった属性とメソッドを持っていれば何でもよい（継承は要らない）。"""
    class Minimal:
        period = 1e-5
        inputs = ("v(out)",)
        outputs = ("duty",)

        def reset(self):
            return [0.0]

        def step(self, t, measured):
            return [0.0]

    assert isinstance(Minimal(), Controller)
    assert not isinstance(object(), Controller)
