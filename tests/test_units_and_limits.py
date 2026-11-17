"""E1a 追補の試験: 単位（基準13）、ステップ数の上限と既定の刻み（基準14）、注意とエラーの出し方。"""
import math
import time

import numpy as np
import pytest

from circuit_sim import DC, CircuitError, Component, Netlist, Sine, Spec, Step, expand, simulate
from circuit_sim.__main__ import main as cli_main
from circuit_sim.units import UnitError, parse_unit, to_si


def spec_of(template: str, simulation: dict | None = None, **values) -> Spec:
    plant = {name: {"value": value, "unit": unit} for name, (value, unit) in values.items()}
    return Spec.model_validate({"template": template, "plant": plant, "simulation": simulation or {}})


def rc_net(wave, r=1e3, c=1e-6, ic=0.0) -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=wave),
        Component(id="R1", type="R", nodes=("in", "out"), value=r),
        Component(id="C1", type="C", nodes=("out", "0"), value=c, ic=ic),
    ])


# ------------------------------------------------------------------ 基準13: 単位

def test_prefixed_units_are_converted_to_si():
    """基準13: {"value": 1, "unit": "mH"} が 1e-3 H として計算される。換算したことは notes で分かる。"""
    ex = expand(spec_of("rlc_series", Vin=(0.01, "kV"), R=(1000, "mohm"), L=(1, "mH"), C=(10, "uF")))
    assert ex.netlist.component("L1").value == 1e-3
    assert ex.netlist.component("R1").value == 1.0
    assert ex.netlist.component("C1").value == 10e-6
    assert ex.netlist.component("V1").waveform.final == 10.0
    assert len(ex.notes) == 4
    assert any("L" in note and "mH" in note and "0.001 H" in note for note in ex.notes)
    # SI単位のままなら注意は出ない。展開した結果は同じ
    plain = expand(spec_of("rlc_series", Vin=(10, "V"), R=(1, "ohm"), L=(1e-3, "H"), C=(10e-6, "F")))
    assert plain.notes == []
    assert plain.netlist == ex.netlist


@pytest.mark.parametrize("unit", ["uF", "µF", "μF"], ids=["u", "micro-sign", "greek-mu"])
def test_three_spellings_of_micro(unit):
    """基準13: µ の3通りの表記（u、µ、μ）が通る。"""
    ex = expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(1e3, "ohm"), C=(1, unit)))
    assert ex.netlist.component("C1").value == 1e-6


@pytest.mark.parametrize("unit,expected", [
    ("ohm", 2.0), ("Ohm", 2.0), ("Ω", 2.0), ("Ω", 2.0),          # Ω は2通りの文字
    ("kohm", 2e3), ("kΩ", 2e3), ("MΩ", 2e6), ("mohm", 2e-3),      # m はミリ、M はメガ
])
def test_spellings_of_ohm(unit, expected):
    """基準13: Ω の表記（ohm、Ω）が通る。"""
    ex = expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(2, unit), C=(1e-6, "F")))
    assert ex.netlist.component("R1").value == expected


def test_wrong_dimension_and_unknown_unit_are_rejected():
    """基準13: インダクタンスに kg を渡すとエラー。どの項目の、どの単位が、何を期待していたかが分かる。"""
    with pytest.raises(UnitError) as info:
        expand(spec_of("rlc_series", Vin=(10, "V"), R=(1, "ohm"), L=(1, "kg"), C=(1e-6, "F")))
    message = str(info.value)
    assert "L:" in message and "'kg'" in message and "インダクタンス" in message
    # 読める単位でも、次元が違えばエラー。複数あれば一度に全部報告する
    with pytest.raises(ValueError) as info:
        expand(spec_of("rlc_series", Vin=(10, "ohm"), R=(1, "ohm"), L=(1, "uF"), C=(1e-6, "F")))
    message = str(info.value)
    assert "Vin:" in message and "抵抗の単位" in message and "電圧" in message
    assert "L:" in message and "キャパシタンスの単位" in message
    # 大文字と小文字を区別する（K は接頭語でない）
    with pytest.raises(UnitError):
        expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(1, "Kohm"), C=(1e-6, "F")))


def test_unit_parsing_table():
    assert parse_unit("mH") == ("inductance", -3)
    assert parse_unit("MHz") == ("frequency", 6)
    assert parse_unit(" nF ") == ("capacitance", -9)
    assert parse_unit("V") == ("voltage", 0)
    assert parse_unit("ms") == ("time", -3)
    for bad in ("", "m", "kg", "ohms", "mm", "HH"):
        with pytest.raises(UnitError):
            parse_unit(bad)
    # 換算した値は、同じ数を直接書いたときと最後の桁まで同じ
    assert to_si(47.0, "uH", "inductance") == 47e-6
    assert to_si(100.0, "kHz", "frequency") == 100e3
    # 次元を照合できない書き方（"SI" など）は、読めない単位として断る
    with pytest.raises(UnitError):
        to_si(3.3, "SI", "resistance")


def test_converted_values_give_the_same_waveform():
    """単位つきで書いた仕様と、SI単位で書いた仕様が、同じ結果になる。"""
    a = expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(1, "kohm"), C=(1, "uF")))
    b = expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(1e3, "ohm"), C=(1e-6, "F")))
    ra, rb = simulate(a.netlist, a.t_end, a.h), simulate(b.netlist, b.t_end, b.h)
    assert np.array_equal(ra.x, rb.x)
    tau = 1e-3
    assert np.max(np.abs(ra["v(out)"] - 5 * (1 - np.exp(-ra.t / tau)))) < 0.01 * 5


def test_non_finite_values_are_rejected_by_the_schema():
    for build in (
        lambda: DC(value=float("nan")),
        lambda: Step(final=float("inf")),
        lambda: Sine(amplitude=1.0, frequency=float("inf")),
        lambda: rc_net(DC(value=1.0), ic=float("nan")),
        lambda: spec_of("rc_lowpass", Vin=(float("inf"), "V"), R=(1e3, "ohm"), C=(1e-6, "F")),
    ):
        with pytest.raises(ValueError):
            build()
    with pytest.raises(ValueError, match="有限値"):
        simulate(rc_net(DC(value=1.0)), float("inf"), 1e-5)


# ------------------------------------------- 基準14: ステップ数の上限と既定の刻み

def test_default_timing_stays_computable_for_separated_poles():
    """基準14: R = 10 kΩ、L = 1 mH、C = 10 µF の rlc_series（計算の指定なし）が、1秒以内に結果を返す。

    速い成分（L/R = 0.1 µs）と遅い成分（RC = 0.1 s）が6桁離れている。既定の刻みは遅い側に合わせ、
    そのことを notes で知らせる。遅い側の応答（コンデンサ電圧）と、刻みより後の電流は正しい。
    """
    v_in, r, ind, cap = 10.0, 1e4, 1e-3, 10e-6
    start = time.perf_counter()
    ex = expand(spec_of("rlc_series", Vin=(v_in, "V"), R=(10, "kohm"), L=(1, "mH"), C=(10, "uF")))
    res = simulate(ex.netlist, ex.t_end, ex.h)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert len(res.t) - 1 <= 20_000
    assert any("刻み" in note and "遅い側" in note for note in ex.notes)          # 既定を変えたこと
    assert any("最も速い時定数" in note for note in res.notes)                    # その結果、何が見えないか
    # 過減衰の解析解（特性方程式の2つの実根）
    a = r / (2 * ind)
    d = math.sqrt(a * a - 1 / (ind * cap))
    s1, s2 = -a + d, -a - d
    v = v_in * (1 - (s2 * np.exp(s1 * res.t) - s1 * np.exp(s2 * res.t)) / (s2 - s1))
    i = v_in / (ind * (s1 - s2)) * (np.exp(s1 * res.t) - np.exp(s2 * res.t))
    assert np.max(np.abs(res["v(out)"] - v)) < 0.01 * v_in
    assert np.max(np.abs(res["i(L1)"][1:] - i[1:])) < 0.01 * i.max()
    assert res["i(L1)"].max() <= i.max() * 1.01                                  # 行き過ぎ（数値振動）が無い


def test_default_timing_is_unchanged_when_poles_are_close():
    """極が離れていない仕様では、既定の刻みはこれまでどおり（最も速い極の周期の 1/200）。"""
    rc = expand(spec_of("rc_lowpass", Vin=(5, "V"), R=(1e3, "ohm"), C=(1e-6, "F")))
    assert round(rc.t_end / rc.h) == 159 and rc.notes == []
    rlc = expand(spec_of("rlc_series", Vin=(10, "V"), R=(1, "ohm"), L=(1e-3, "H"), C=(10e-6, "F")))
    assert round(rlc.t_end / rlc.h) == 3183 and rlc.notes == []
    # 計算時間は刻みの整数倍にそろえてある（終了時刻が丸められない）
    res = simulate(rc.netlist, rc.t_end, rc.h)
    assert res.notes == [] and res.t[-1] == pytest.approx(5e-3, rel=1e-12)


def test_default_timing_keeps_resolution_for_slowly_decaying_oscillation():
    """減衰の遅い振動では、刻みを保って計算時間を短くし、そのことを知らせる（粗くすると振動が折り返す）。"""
    ex = expand(spec_of("rlc_series", Vin=(10, "V"), R=(1e-4, "ohm"), L=(1e-3, "H"), C=(10e-6, "F")))
    period = 2 * math.pi * math.sqrt(1e-3 * 10e-6)
    assert ex.h == pytest.approx(period / 200, rel=1e-3)
    assert round(ex.t_end / ex.h) == 20_000
    assert any("終了時刻" in note and "短く" in note for note in ex.notes)


def test_too_many_steps_is_rejected_before_allocating():
    """上限（既定 2000万ステップ）を超える計算は、配列を確保する前に、理由と直し方つきで断る。"""
    net = rc_net(DC(value=1.0))
    start = time.perf_counter()
    with pytest.raises(ValueError) as info:
        simulate(net, 1.0, 1e-15)
    assert time.perf_counter() - start < 1.0
    message = str(info.value)
    for part in ("ステップ数", "上限", "h を大きく", "max_steps"):
        assert part in message
    with pytest.raises(ValueError, match="上限の 100 "):
        simulate(net, 1e-3, 1e-6, max_steps=100)
    assert len(simulate(net, 1e-3, 1e-6, max_steps=1000).t) == 1001        # 上限ちょうどは通る


def test_large_result_for_many_unknowns_is_rejected(monkeypatch):
    """未知数の多い回路では、ステップ数が上限以内でも、結果の配列の大きさで断る。"""
    import importlib

    sim = importlib.import_module("circuit_sim.simulate")
    monkeypatch.setattr(sim, "MAX_RESULT_BYTES", 10_000)
    with pytest.raises(ValueError, match="結果の配列"):
        simulate(rc_net(DC(value=1.0)), 1e-2, 1e-5)           # 1000 ステップ × 3 変数 × 8 バイト = 24 kB
    assert len(simulate(rc_net(DC(value=1.0)), 1e-2, 1e-5, max_steps=2000).t) == 1001


# ------------------------------------------------------------- 注意（notes）

def test_coarse_step_is_damped_and_reported():
    """刻みが時定数より粗いとき（h = 10 τ）: 行き過ぎず、何に対して粗いかが notes に出る。"""
    tau = 1e-3
    res = simulate(rc_net(Step(final=1.0)), 400 * 10 * tau, 10 * tau)
    # 減衰ステップ2回の後に残るのは exp(-2 h / τ) = 2e-9。その先の台形則がこれを揺らすだけなので、
    # 行き過ぎは入力の 1e-6 より十分小さい
    assert res["v(out)"].max() <= 1.0 + 1e-6
    # 1点目は厳密解に近い（1024 分割の後退オイラーの誤差は exp(-10) × 10² / 2048 = 2e-6）
    assert res["v(out)"][1] == pytest.approx(1.0 - math.exp(-10.0), abs=1e-5)
    assert len(res.notes) == 1
    assert "最も速い時定数 0.001 s" in res.notes[0] and "10 倍" in res.notes[0]
    # 減衰ステップを入れなければ、台形則の数値振動が出る（入力の 1.67 倍）
    ringing = simulate(rc_net(Step(final=1.0)), 400 * 10 * tau, 10 * tau, damping_steps=0)
    assert ringing["v(out)"].max() == pytest.approx(1 + 4 / 6, rel=1e-9)


def test_resolved_circuit_gets_no_note_and_plain_trapezoid():
    """刻みが回路を解像していれば、注意は出ず、減衰ステップも入らない（台形則だけの結果と全点一致）。"""
    net = rc_net(Step(final=5.0, t_step=1e-3))
    res = simulate(net, 6e-3, 1e-5)
    assert res.notes == []
    assert np.array_equal(res.x, simulate(net, 6e-3, 1e-5, damping_steps=0).x)


def test_step_after_coarse_grid_does_not_ring():
    """途中のステップの後も、粗い刻みで行き過ぎない（速い枝 1 µs に対して h = 10 µs）。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=5.0, t_step=1e-3)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
        Component(id="R2", type="R", nodes=("in", "x"), value=1e2),
        Component(id="C2", type="C", nodes=("x", "0"), value=1e-8),
    ])
    res = simulate(net, 3e-3, 1e-5)
    after = res.t >= 1e-3 - 1e-12
    slow = np.where(after, 5.0 * (1 - np.exp(-(res.t - 1e-3) / 1e-3)), 0.0)
    assert np.max(np.abs(res["v(out)"] - slow)) < 0.01 * 5.0
    assert res["v(x)"].max() <= 5.0 * (1 + 1e-6)            # 上と同じ理由で 1e-6
    assert res["i(V1)"][100:].max() <= 1e-6 * 0.055         # 電源は供給だけ（尺度は 5 V / 91 Ω）
    assert any("1e-06 s" in note for note in res.notes)


def test_unresolved_sine_source_is_reported():
    res = simulate(rc_net(Sine(amplitude=1.0, frequency=1.001e6), c=1e-9), 5e-3, 1e-5)
    assert any("V1" in note and "正弦波" in note and "折り返" in note for note in res.notes)
    fine = simulate(rc_net(Sine(amplitude=1.0, frequency=1e3)), 5e-3, 1e-5)     # 1周期 100 点
    assert fine.notes == []


def test_unresolved_oscillation_of_the_circuit_is_reported():
    ind, cap = 1e-3, 1e-6                               # 共振の周期 0.199 ms
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(final=1.0)),
        Component(id="R1", type="R", nodes=("in", "a"), value=0.1),
        Component(id="L1", type="L", nodes=("a", "out"), value=ind),
        Component(id="C1", type="C", nodes=("out", "0"), value=cap),
    ])
    res = simulate(net, 1e-2, 5e-5)                      # 1周期に約4点
    assert any("振動" in note and "周期 0.000199 s" in note for note in res.notes)
    assert simulate(net, 1e-2, 1e-6).notes == []


def test_end_time_rounding_is_reported():
    res = simulate(rc_net(DC(value=1.0)), 1e-3, 6.7e-4)
    assert res.t[-1] == pytest.approx(6.7e-4)
    assert any("整数倍でない" in note and "0.00067 s" in note for note in res.notes)


def test_contradictory_initial_values_are_reported():
    """並列のコンデンサに違う初期値: 電荷を保存した値で始め、そのことを知らせる。"""
    net = Netlist(components=[
        Component(id="I1", type="I", nodes=("0", "a"), waveform=DC(value=0.0)),
        Component(id="R1", type="R", nodes=("a", "0"), value=1e3),
        Component(id="C1", type="C", nodes=("a", "0"), value=1e-6, ic=2.0),
        Component(id="C2", type="C", nodes=("a", "0"), value=3e-6, ic=6.0),
    ])
    res = simulate(net, 1e-3, 1e-5)
    assert res["v(a)"][0] == pytest.approx((1e-6 * 2.0 + 3e-6 * 6.0) / 4e-6)      # 電荷の合計を保存: 5 V
    assert len(res.notes) == 2
    assert "C1" in res.notes[0] and "ic=2 V" in res.notes[0] and "5 V から始めた" in res.notes[0]
    assert "C2" in res.notes[1]
    # 矛盾していなければ出ない
    assert simulate(rc_net(DC(value=1.0), ic=0.3), 1e-3, 1e-5).notes == []


def test_step_at_the_last_grid_point_is_recorded():
    """電源が最後の格子点でステップする場合も、その点にはステップした後の値が入る。"""
    res = simulate(rc_net(Step(final=4.0, initial=1.0, t_step=1e-3), ic=1.0), 1e-3, 1e-5)
    assert res["v(in)"][-2] == pytest.approx(1.0)
    assert res["v(in)"][-1] == pytest.approx(4.0)
    assert res["v(out)"][-1] == pytest.approx(1.0)              # 状態量は連続
    assert res.i("R1")[-1] == pytest.approx(3.0 / 1e3)


# ------------------------------------------------------------ 桁の違う定数

def test_tiny_capacitor_and_huge_inductor_keep_their_initial_values():
    """C = 1 pF と L = 1 kH（値の比 1e-15）でも、小さい方の初期値が無視されない。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e6),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-12, ic=0.7),
        Component(id="R2", type="R", nodes=("in", "b"), value=1.0),
        Component(id="L1", type="L", nodes=("b", "0"), value=1e3, ic=0.2),
    ])
    res = simulate(net, 5e-6, 1e-8)
    assert res["v(out)"][0] == pytest.approx(0.7, rel=1e-9)
    assert res["i(L1)"][0] == pytest.approx(0.2, rel=1e-9)
    assert res["v(b)"][0] == pytest.approx(1.0 - 0.2 * 1.0, rel=1e-9)
    tau = 1e6 * 1e-12
    assert np.max(np.abs(res["v(out)"] - (1.0 - 0.3 * np.exp(-res.t / tau)))) < 0.01


# --------------------------------------------------------------- エラーの出し方

def test_series_inductors_error_names_the_place():
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="L1", type="L", nodes=("in", "a"), value=1e-3),
        Component(id="L2", type="L", nodes=("a", "b"), value=1e-3),
        Component(id="R1", type="R", nodes=("b", "0"), value=10.0),
    ])
    with pytest.raises(CircuitError) as info:
        simulate(net, 1e-3, 1e-6)
    message = str(info.value)
    assert "節点 'a' には L1, L2 しかつながっていない" in message and "インダクタどうしの直列" in message
    assert "決まらない変数: v(a)" in message


def test_floating_part_error_names_the_undetermined_variables():
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="R1", type="R", nodes=("in", "0"), value=1.0),
        Component(id="R2", type="R", nodes=("x", "y"), value=1.0),
        Component(id="R3", type="R", nodes=("x", "y"), value=1.0),
    ])
    with pytest.raises(CircuitError) as info:
        simulate(net, 1e-3, 1e-5)
    assert "決まらない変数: v(x), v(y)" in str(info.value)


@pytest.mark.parametrize("text,expected", [
    ('{"template": "no_such", "plant": {}}', "未対応のテンプレート"),
    ('{"template": "rc_lowpass", "plant": {"Vin": {"value": 5, "unit": "V"},'
     ' "R": {"value": -1, "unit": "ohm"}, "C": {"value": 1e-6, "unit": "F"}}}', "正の値が必要"),
    ('{"template": "rc_lowpass", "plant": {"Vin": {"value": 5, "unit": "V"},'
     ' "R": {"value": 1, "unit": "kg"}, "C": {"value": 1e-6, "unit": "F"}}}', "'kg'"),
    ('{"template": "rc_lowpass", "plant": {"Vin": {"value": 5}}}', "plant.Vin.unit"),
    ("{ これはJSONではない", "仕様JSONの形が正しくありません"),
], ids=["template", "non-positive", "unit", "schema", "broken"])
def test_cli_explains_errors_in_one_line(tmp_path, capsys, text, expected):
    """コマンドラインは、計算できない理由を1行で出して終了コード 2 を返す（トレースバックにしない）。"""
    path = tmp_path / "spec.json"
    path.write_text(text, encoding="utf-8")
    assert cli_main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err and len(captured.err.strip().splitlines()) == 1
    assert cli_main([str(tmp_path / "missing.json")]) == 2
    assert "読めません" in capsys.readouterr().err


def test_cli_prints_unit_conversion_notes(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text('{"template": "rc_lowpass", "plant": {"Vin": {"value": 5, "unit": "V"},'
                    ' "R": {"value": 1, "unit": "kohm"}, "C": {"value": 1, "unit": "uF"},'
                    ' "Rload": {"value": 3, "unit": "ohm"}}}', encoding="utf-8")
    assert cli_main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "R: 1 kohm を 1000 ohm に換算した" in out
    assert "C: 1 uF を 1e-06 F に換算した" in out
    assert "使わない項目を無視した: Rload" in out
