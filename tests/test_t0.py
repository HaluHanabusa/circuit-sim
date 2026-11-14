"""M0の受け入れ試験: 基礎回路を解析解と照合する（基準は誤差1%以内）。"""
import json
import math

import numpy as np
import pytest
from pydantic import ValidationError

from circuit_sim import (
    DC,
    CircuitError,
    Component,
    MissingValueError,
    Netlist,
    Sine,
    Spec,
    Step,
    expand,
    simulate,
)

TOL = 0.01  # 受け入れ基準: 解析解との差が、信号の大きさの1%以内


def spec(template: str, **values) -> Spec:
    units = {"Vin": "V", "R": "ohm", "L": "H", "C": "F"}
    return Spec(
        template=template,
        plant={k: {"value": v, "unit": units[k]} for k, v in values.items()},
    )


# ------------------------------------------------------------ 解析解との照合

def test_rc_step():
    V, R, C = 5.0, 1e3, 1e-6
    ex = expand(spec("rc_lowpass", Vin=V, R=R, C=C))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    tau = R * C
    v_exact = V * (1 - np.exp(-res.t / tau))
    i_exact = V / R * np.exp(-res.t / tau)
    assert np.max(np.abs(res["v(out)"] - v_exact)) < TOL * V
    assert np.max(np.abs(res.i("R1") - i_exact)) < TOL * V / R
    # 電圧源の枝電流は n+ から n- 向きなので、供給時は負
    assert np.allclose(res.i("V1"), -res.i("R1"), atol=1e-12)
    # t=0 の値が整合している（コンデンサ0 V、電源は既に5 V、電流は V/R）
    assert res["v(out)"][0] == pytest.approx(0.0, abs=1e-12)
    assert res["v(in)"][0] == pytest.approx(V)
    assert res.i("R1")[0] == pytest.approx(V / R)


def rlc_exact(t, V, R, L, C):
    alpha = R / (2 * L)
    wd = math.sqrt(1 / (L * C) - alpha**2)
    v = V * (1 - np.exp(-alpha * t) * (np.cos(wd * t) + alpha / wd * np.sin(wd * t)))
    i = V / (wd * L) * np.exp(-alpha * t) * np.sin(wd * t)
    return v, i


def test_rlc_step_underdamped():
    V, R, L, C = 10.0, 1.0, 1e-3, 10e-6   # 減衰係数 0.05
    ex = expand(spec("rlc_series", Vin=V, R=R, L=L, C=C))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    v_exact, i_exact = rlc_exact(res.t, V, R, L, C)
    assert np.max(np.abs(res["v(out)"] - v_exact)) < TOL * V
    assert np.max(np.abs(res["i(L1)"] - i_exact)) < TOL * np.max(np.abs(i_exact))
    # オーバーシュートが理論値どおり（exp(-pi*zeta/sqrt(1-zeta^2))）
    zeta = R / 2 * math.sqrt(C / L)
    overshoot = math.exp(-math.pi * zeta / math.sqrt(1 - zeta**2))
    assert res["v(out)"].max() / V - 1 == pytest.approx(overshoot, rel=TOL)


def test_rlc_step_overdamped():
    V, R, L, C = 10.0, 500.0, 1e-3, 10e-6
    ex = expand(spec("rlc_series", Vin=V, R=R, L=L, C=C))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    s1, s2 = np.roots([1.0, R / L, 1 / (L * C)]).real
    v_exact = V * (1 - (s2 * np.exp(s1 * res.t) - s1 * np.exp(s2 * res.t)) / (s2 - s1))
    assert np.max(np.abs(res["v(out)"] - v_exact)) < TOL * V
    assert res["v(out)"].max() <= V * (1 + 1e-9)   # 行き過ぎない


def test_second_order_convergence():
    """刻みを半分にすると誤差が約1/4になる（台形則は2次精度）。"""
    V, R, L, C = 10.0, 1.0, 1e-3, 10e-6
    net = expand(spec("rlc_series", Vin=V, R=R, L=L, C=C)).netlist
    errors = []
    for h in (8e-6, 4e-6, 2e-6):
        res = simulate(net, 5e-3, h)
        v_exact, _ = rlc_exact(res.t, V, R, L, C)
        errors.append(np.max(np.abs(res["v(out)"] - v_exact)))
    assert 3.5 < errors[0] / errors[1] < 4.5
    assert 3.5 < errors[1] / errors[2] < 4.5


def test_initial_condition_discharge():
    R, C, V0 = 2e3, 1e-6, 5.0
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=0.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C, ic=V0),
    ])
    res = simulate(net, 5 * R * C, R * C / 100)
    assert res["v(out)"][0] == pytest.approx(V0)
    assert res.i("V1")[0] == pytest.approx(V0 / R)
    assert np.max(np.abs(res["v(out)"] - V0 * np.exp(-res.t / (R * C)))) < TOL * V0


def test_inductor_initial_current():
    R, L, I0 = 10.0, 1e-3, 2.0
    net = Netlist(components=[
        Component(id="L1", type="L", nodes=("a", "0"), value=L, ic=I0),
        Component(id="R1", type="R", nodes=("a", "0"), value=R),
    ])
    res = simulate(net, 5 * L / R, L / R / 100)
    # L の電流は a → 0。抵抗には 0 → a に流れるので v(a) = -R * i
    assert res["i(L1)"][0] == pytest.approx(I0)
    assert res["v(a)"][0] == pytest.approx(-R * I0)
    assert np.max(np.abs(res["i(L1)"] - I0 * np.exp(-res.t * R / L))) < TOL * I0


def test_delayed_step_has_no_ringing():
    """途中で電源が切り替わっても、状態以外の変数に数値振動が出ない。"""
    V, R, C, td = 5.0, 1e3, 1e-6, 1e-3
    h = 1e-5
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=V, t_step=td)),
        Component(id="R1", type="R", nodes=("in", "out"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
    ])
    res = simulate(net, 6e-3, h)
    ks = round(td / h)
    assert res["v(in)"][ks - 1] == pytest.approx(0.0, abs=1e-12)
    assert res["v(in)"][ks] == pytest.approx(V)
    assert np.allclose(res["v(in)"][ks:], V, atol=1e-9)          # 振動なし
    v_exact = np.where(res.t < td, 0.0, V * (1 - np.exp(-(res.t - td) / (R * C))))
    assert np.max(np.abs(res["v(out)"] - v_exact)) < 1e-4 * V     # 丸めずに厳密な時刻で切り替わる
    i_exact = np.where(res.t < td, 0.0, V / R * np.exp(-(res.t - td) / (R * C)))
    assert np.max(np.abs(res.i("R1") - i_exact)) < 1e-4 * V / R


def test_floating_capacitor_between_nodes():
    """両端とも基準節点でないコンデンサ（方程式の組み合わせで代数式が出る場合）。"""
    V, R1, R2, C = 6.0, 1e3, 2e3, 1e-6
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=V)),
        Component(id="R1", type="R", nodes=("in", "a"), value=R1),
        Component(id="C1", type="C", nodes=("a", "b"), value=C),
        Component(id="R2", type="R", nodes=("b", "0"), value=R2),
    ])
    tau = (R1 + R2) * C
    res = simulate(net, 5 * tau, tau / 200)
    i_exact = V / (R1 + R2) * np.exp(-res.t / tau)
    assert np.max(np.abs(res.i("R1") - i_exact)) < TOL * V / (R1 + R2)
    assert np.max(np.abs(res["v(b)"] - R2 * i_exact)) < TOL * V
    assert np.max(np.abs(res.v("a", "b") - V * (1 - np.exp(-res.t / tau)))) < TOL * V


def test_rl_sine_steady_state():
    V, R, L, f = 10.0, 5.0, 1e-3, 1e3
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Sine(amplitude=V, frequency=f)),
        Component(id="R1", type="R", nodes=("in", "a"), value=R),
        Component(id="L1", type="L", nodes=("a", "0"), value=L),
    ])
    res = simulate(net, 10e-3, 1 / f / 400)
    w = 2 * math.pi * f
    amp = V / math.hypot(R, w * L)
    phi = math.atan2(w * L, R)
    steady = res.t > 20 * L / R
    i_exact = amp * np.sin(w * res.t - phi)
    assert np.max(np.abs(res["i(L1)"][steady] - i_exact[steady])) < TOL * amp


def test_current_source():
    I0, R, C = 1e-3, 1e3, 1e-6
    net = Netlist(components=[
        # 電流は n+ から素子を通って n- へ流れる。0 → out 向きに注入する
        Component(id="I1", type="I", nodes=("0", "out"), waveform=DC(value=I0)),
        Component(id="R1", type="R", nodes=("out", "0"), value=R),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
    ])
    res = simulate(net, 5 * R * C, R * C / 100)
    assert np.max(np.abs(res["v(out)"] - I0 * R * (1 - np.exp(-res.t / (R * C))))) < TOL * I0 * R


# ------------------------------------------------------------------ 検証と入出力

def test_missing_values_are_reported_together():
    s = Spec(template="rlc_series", plant={
        "Vin": {"value": 10, "unit": "V", "source": "p.2 表1"},
        "R": {"value": None, "unit": "ohm"},
        "L": {"value": 1e-3, "unit": "H"},
    })
    assert s.null_fields() == ["R"]
    with pytest.raises(MissingValueError) as exc:
        expand(s)
    assert exc.value.names == ["R", "C"]


def test_unknown_template():
    with pytest.raises(ValueError, match="未対応のテンプレート"):
        expand(spec("no_such_template", Vin=1.0))


def test_netlist_validation():
    with pytest.raises(ValidationError, match="重複"):
        Netlist(components=[
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0),
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0),
        ])
    with pytest.raises(ValidationError, match="1つしかつながっていない"):
        Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1.0)),
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0),
            Component(id="R2", type="R", nodes=("a", "b"), value=1.0),
        ])
    with pytest.raises(ValidationError, match="正の有限値"):
        Component(id="R1", type="R", nodes=("a", "0"), value=-1.0)
    with pytest.raises(ValidationError, match="両端が同じ"):
        Component(id="R1", type="R", nodes=("a", "a"), value=1.0)


def test_unsolvable_circuits_are_rejected():
    # 電圧源とコンデンサの直結
    with pytest.raises(CircuitError):
        simulate(Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1.0)),
            Component(id="C1", type="C", nodes=("a", "0"), value=1e-6),
        ]), 1e-3, 1e-6)
    # 電圧源どうしの並列
    with pytest.raises(CircuitError):
        simulate(Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1.0)),
            Component(id="V2", type="V", nodes=("a", "0"), waveform=DC(value=2.0)),
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0),
        ]), 1e-3, 1e-6)
    # 基準節点から浮いた部分
    with pytest.raises(CircuitError):
        simulate(Netlist(components=[
            Component(id="V1", type="V", nodes=("a", "0"), waveform=DC(value=1.0)),
            Component(id="R1", type="R", nodes=("a", "0"), value=1.0),
            Component(id="R2", type="R", nodes=("x", "y"), value=1.0),
            Component(id="R3", type="R", nodes=("x", "y"), value=1.0),
        ]), 1e-3, 1e-6)


def test_spec_json_roundtrip_and_schema():
    text = json.dumps({
        "schema_version": "0.1",
        "template": "rc_lowpass",
        "plant": {
            "Vin": {"value": 5, "unit": "V", "source": "p.2 表1"},
            "R": {"value": 1000, "unit": "ohm", "source": "仕様!B4"},
            "C": {"value": 1e-6, "unit": "F", "source": None},
        },
        "targets": {"settling_time_s_max": 0.005},
        "missing": [],
    })
    s = Spec.model_validate_json(text)
    assert Spec.model_validate_json(s.model_dump_json()) == s
    schema = Spec.model_json_schema()          # M3でAIに渡すJSON Schema
    assert schema["properties"]["plant"]["additionalProperties"]["$ref"].endswith("Quantity")
    with pytest.raises(ValidationError):
        Spec.model_validate({"template": "rc_lowpass", "plant": {}, "unknown_key": 1})


def test_csv_export(tmp_path):
    ex = expand(spec("rc_lowpass", Vin=5.0, R=1e3, C=1e-6))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    path = tmp_path / "out.csv"
    res.to_csv(path, ex.probes)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t,v(out),i(V1)"
    assert len(lines) == len(res.t) + 1
