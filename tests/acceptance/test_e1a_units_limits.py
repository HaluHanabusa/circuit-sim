"""E1a 追補（基準 11〜14、17）の受け入れ試験（検証担当A が追加）。

対象: 減衰ステップを入れた後の精度（基準 12）、層Aの単位（基準 13）、ステップ数の上限と
テンプレートの既定の刻み（基準 14）、注意（notes）が出るべきときに出て、出るべきでないときに出ないこと。
実測値と測り方は `docs/verification/E1a-A.md` にある。

期待値の作り方:
- 解析解はこのファイルの中に式で書く。多状態の回路は状態方程式を手で導き、行列指数関数で解く。
- 単位の期待値は、接頭語の定義（m = 1e-3、M = 1e6 など）からこのファイルの中の表で作る。
  製品の `circuit_sim.units` の表は使わない。

許容差の根拠:
- 解析解との差 1%（E0 の基準 1 と同じ。信号の代表値に対して）。
- 「E0 の水準」は README の値（RC 0.003%、RLC 0.06%）の 2 倍までとする。丸め方の違いは許し、
  1桁悪くなる変化は落とす。
- 誤差比 3.6〜4.4（E0 と同じ）。
- 時間は基準 14 の「1秒以内」をそのまま使う（実測は約 0.07 秒）。

製品側の不具合で落ちる試験には xfail(strict=True) を付けてある。直ったら印を外すこと。
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest
from scipy.linalg import expm

from circuit_sim import DC, Component, Netlist, Sine, Spec, Step, expand, simulate
from circuit_sim.__main__ import main as cli_main

TOL = 0.01
ORDER_BAND = (3.6, 4.4)
DEFAULT_STEP_BUDGET = 20_000        # 追補: テンプレートの既定はこのステップ数以内に収める


def product_bug(severity, *lines):
    """製品側の不具合で落ちる試験に付ける印。直ったら（XPASS で失敗するので）印を外す。"""
    return pytest.mark.xfail(strict=True, reason=f"製品の不具合({severity}): " + "".join(lines))


# ---------------------------------------------------------------- 部品と参照解

def V(cid, a, b, wave):
    return Component(id=cid, type="V", nodes=(a, b), waveform=wave)


def R(cid, a, b, value):
    return Component(id=cid, type="R", nodes=(a, b), value=value)


def L(cid, a, b, value, ic=0.0):
    return Component(id=cid, type="L", nodes=(a, b), value=value, ic=ic)


def C(cid, a, b, value, ic=0.0):
    return Component(id=cid, type="C", nodes=(a, b), value=value, ic=ic)


def net(*comps):
    return Netlist(components=list(comps))


def rc_net(wave, r=1e3, c=1e-6, vc0=0.0):
    return net(V("V1", "in", "0", wave), R("R1", "in", "out", r), C("C1", "out", "0", c, ic=vc0))


def max_err(a, b, scale):
    return float(np.max(np.abs(np.asarray(a) - np.asarray(b))) / scale)


def rlc_exact(t, v_in, r, ind, cap):
    """直列RLCのステップ応答（初期値ゼロ）。戻り値: コンデンサ電圧、インダクタ電流。

    過減衰では遅い根を s1 = w0^2 / s2 で求める（-a + sqrt(...) は桁落ちする）。
    """
    a = r / (2 * ind)
    w0 = 1 / math.sqrt(ind * cap)
    disc = a * a - w0 * w0
    if abs(disc) < 1e-12 * w0 * w0:                                    # 臨界減衰（重根）
        return v_in * (1 - np.exp(-a * t) * (1 + a * t)), v_in / ind * t * np.exp(-a * t)
    if disc < 0:
        wd = math.sqrt(-disc)
        v = v_in * (1 - np.exp(-a * t) * (np.cos(wd * t) + a / wd * np.sin(wd * t)))
        i = v_in / (wd * ind) * np.exp(-a * t) * np.sin(wd * t)
        return v, i
    s2 = -a - math.sqrt(disc)
    s1 = w0 * w0 / s2
    with np.errstate(under="ignore"):
        e1, e2 = np.exp(s1 * t), np.exp(s2 * t)
    return v_in * (1 - (s2 * e1 - s1 * e2) / (s2 - s1)), v_in / (ind * (s1 - s2)) * (e1 - e2)


def spec_of(template, simulation=None, **values):
    plant = {k: {"value": v, "unit": u} for k, (v, u) in values.items()}
    return Spec.model_validate({"template": template, "plant": plant, "simulation": simulation or {}})


def rlc_spec(r, simulation=None, v_in=10.0, ind=1e-3, cap=10e-6):
    return spec_of("rlc_series", simulation, Vin=(v_in, "V"), R=(r, "ohm"), L=(ind, "H"), C=(cap, "F"))


def run_spec(spec):
    ex = expand(spec)
    res = simulate(ex.netlist, ex.t_end, ex.h)
    return ex, res, list(ex.notes) + list(res.notes)


def stiff_two_branch(h, wave=None, t_end=3e-3):
    """遅い枝 R1-C1（1 ms）と速い枝 R2-C2（1 µs）を電源に並列につなぐ。既定は t = 1 ms で 0 → 5 V。"""
    wave = wave or Step(final=5.0, t_step=1e-3)
    return simulate(net(
        V("V1", "in", "0", wave),
        R("R1", "in", "out", 1e3), C("C1", "out", "0", 1e-6),
        R("R2", "in", "x", 1e2), C("C2", "x", "0", 1e-8),
    ), t_end, h)


# ============================================== 基準 12: 既存の精度と、減衰の境目

def test_c12_template_defaults_keep_the_e0_precision_level():
    """README の水準: RC 0.003%、RLC（ζ=0.05）0.06%。その 2 倍以内で、注意は出ない。"""
    ex, res, notes = run_spec(spec_of("rc_lowpass", Vin=(5.0, "V"), R=(1e3, "ohm"), C=(1e-6, "F")))
    assert max_err(res["v(out)"], 5.0 * (1 - np.exp(-res.t / 1e-3)), 5.0) < 2 * 0.003e-2
    assert notes == [] and len(res.t) - 1 == 159
    assert res.t[-1] == pytest.approx(5e-3, rel=1e-9)            # 5 時定数ちょうどで終わる

    ex, res, notes = run_spec(rlc_spec(1.0))
    v, i = rlc_exact(res.t, 10.0, 1.0, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < 2 * 0.06e-2
    assert max_err(res["i(L1)"], i, float(np.abs(i).max())) < 2 * 0.07e-2
    assert notes == []


def test_c12_second_order_is_kept_while_damping_is_active():
    """速い枝を解像しない刻み（減衰ステップが入る領域）でも、遅い枝の誤差は刻み半分で約 1/4。"""
    errors = []
    for h in (2.5e-5, 1.25e-5, 6.25e-6):                         # 速い時定数の 25 / 12.5 / 6.25 倍
        res = stiff_two_branch(h, t_end=4e-3)
        after = res.t >= 1e-3 - 1e-12
        ref = np.where(after, 5.0 * (1 - np.exp(-(res.t - 1e-3) / 1e-3)), 0.0)
        errors.append(max_err(res["v(out)"], ref, 5.0))
        assert res.notes, "速い枝を解像していないのに注意が無い"
    for ratio in (errors[0] / errors[1], errors[1] / errors[2]):
        assert ORDER_BAND[0] < ratio < ORDER_BAND[1], f"誤差 {errors}"


@pytest.mark.parametrize("ratio", [0.5, 1.0, 1.5, 1.9, 1.99, 2.0, 2.01, 2.1, 2.5, 3.0, 5.0, 10.0, 100.0])
def test_c12_no_numerical_oscillation_on_either_side_of_the_damping_threshold(ratio):
    """RC のステップ応答で h/τ を境目（2）の前後に振る。どちら側でも入力を 1% 以上超えない。"""
    tau = 1e-3
    res = simulate(rc_net(Step(final=1.0)), 400 * ratio * tau, ratio * tau)
    v = res["v(out)"]
    assert v.max() <= 1.0 + TOL and v.min() >= -TOL, f"最大 {v.max()}、最小 {v.min()}"
    assert v[-1] == pytest.approx(1.0, rel=1e-6)


@pytest.mark.parametrize("ratio", [2.01, 2.1, 2.5, 3.0, 5.0, 10.0, 100.0])
def test_c12_above_the_threshold_the_result_is_accurate_and_noted(ratio):
    """境目より粗い側: 格子点の値は真値と 1% 以内で、何に対して粗いかと直し方が注意に出る。"""
    tau = 1e-3
    res = simulate(rc_net(Step(final=1.0)), 400 * ratio * tau, ratio * tau)
    assert max_err(res["v(out)"], 1 - np.exp(-res.t / tau), 1.0) < TOL
    assert len(res.notes) == 1
    assert "時定数" in res.notes[0] and "0.001" in res.notes[0]        # 何に対して粗いか（1 ms の時定数）
    assert "以下にする" in res.notes[0]                                # 直し方


@pytest.mark.parametrize("ratio", [1.0, 1.5, 1.9, 1.99])
def test_c12_just_below_the_threshold_is_accurate_or_noted(ratio):
    """境目のすぐ下: 真値と 1% 以内か、刻みが粗いことが注意で分かること。"""
    tau = 1e-3
    res = simulate(rc_net(Step(final=1.0)), 400 * ratio * tau, ratio * tau)
    error = max_err(res["v(out)"], 1 - np.exp(-res.t / tau), 1.0)
    assert error < TOL or res.notes, f"h = {ratio}τ: 誤差 {error:.1%}、注意なし"


@pytest.mark.parametrize("h", [2.1e-6, 3.125e-6, 1e-5, 1e-4])
def test_c12_stiff_circuit_mid_run_step_is_clean_above_the_threshold(h):
    """途中のステップ（t = 1 ms）。速い枝の時定数（1 µs）の 2.1〜100 倍の刻み。"""
    res = stiff_two_branch(h)
    ks = int(round(1e-3 / h))
    after = np.arange(len(res.t)) >= ks
    td = np.where(after, res.t - ks * h, 0.0)
    with np.errstate(under="ignore"):
        fast = np.where(after, 5.0 * (1 - np.exp(-td / 1e-6)), 0.0)
        slow = np.where(after, 5.0 * (1 - np.exp(-td / 1e-3)), 0.0)
    assert res["v(x)"].max() <= 5.0 * (1 + TOL)
    assert max_err(res["v(x)"], fast, 5.0) < TOL
    assert max_err(res["v(out)"], slow, 5.0) < TOL
    assert np.all(res["i(V1)"][ks:] <= TOL * 0.055)                    # 電源は供給だけ（電流は負）
    assert np.all(np.abs(res.x[:ks]) < 1e-12)                          # ステップの前は動かない


def test_c12_sine_driven_stiff_branch_shows_no_alternating_error():
    """正弦波（1 kHz）で駆動した硬い枝の電流。真の振幅は 5·ω·C2 = 0.314 mA と小さく、交互の誤差が目立つ量。"""
    w = 2 * math.pi * 1e3
    res = stiff_two_branch(1e-5, Sine(amplitude=5.0, frequency=1e3, phase_deg=90.0), t_end=5e-3)
    t = res.t
    a = w * 1e-6
    gain, lag = 1 / math.sqrt(1 + a * a), math.atan(a)
    with np.errstate(under="ignore"):
        transient = math.sin(math.pi / 2 - lag) * np.exp(-t / 1e-6)
    vx = 5.0 * gain * (np.sin(w * t + math.pi / 2 - lag) - transient)
    i_true = (5.0 * np.sin(w * t + math.pi / 2) - vx) / 1e2
    settled = slice(100, None)                             # 先頭の充電パルスの後（100 ステップ = 1 ms）
    assert max_err(res.i("R2")[settled], i_true[settled], 5.0 * w * 1e-8) < TOL


@pytest.mark.parametrize("multiple", [10.0, 100.0])
def test_c12_ladder_with_step_size_inside_its_spectrum(multiple):
    """RCはしご20段（時定数 25 µs〜17 ms）で、刻みを最速の時定数の 10 倍・100 倍に置く。"""
    n_stage, r, c, v_in = 20, 1e3, 1e-7, 3.0
    comps = [V("V1", "n0", "0", Step(final=v_in))]
    for k in range(1, n_stage + 1):
        comps += [R(f"R{k}", f"n{k - 1}", f"n{k}", r), C(f"C{k}", f"n{k}", "0", c)]
    a_mat = np.zeros((n_stage, n_stage))
    for k in range(n_stage):
        a_mat[k, k] = (-2.0 if k < n_stage - 1 else -1.0) / (r * c)
        if k > 0:
            a_mat[k, k - 1] = 1 / (r * c)
        if k < n_stage - 1:
            a_mat[k, k + 1] = 1 / (r * c)
    h = multiple / np.abs(np.linalg.eigvals(a_mat).real).max()
    n = int(round(0.05 / h))
    res = simulate(Netlist(components=comps), n * h, h)
    m = np.zeros((n_stage + 1, n_stage + 1))
    m[:n_stage, :n_stage] = a_mat
    m[0, n_stage] = v_in / (r * c)
    phi = expm(m * h)
    z = np.zeros(n_stage + 1)
    z[n_stage] = 1.0
    ref = np.empty((n + 1, n_stage))
    for k in range(n + 1):
        ref[k] = z[:n_stage]
        z = phi @ z
    sim = np.column_stack([res[f"v(n{k})"] for k in range(1, n_stage + 1)])
    assert max_err(sim, ref, v_in) < TOL
    assert sim.max() <= v_in * (1 + TOL) and sim.min() >= -TOL * v_in


# ============================================================== 基準 13: 単位

PREFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
          "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}
SYMBOLS = {"Vin": ["V"], "R": ["ohm", "Ohm", "Ω", "Ω"], "L": ["H"], "C": ["F"]}
COMPONENT_OF = {"R": "R1", "L": "L1", "C": "C1"}


def _value_in_netlist(ex, name):
    if name == "Vin":
        return ex.netlist.component("V1").waveform.final
    return ex.netlist.component(COMPONENT_OF[name]).value


def _expand_with(name, value, unit, simulation=None):
    values = {"Vin": (10.0, "V"), "R": (1.0, "ohm"), "L": (1e-3, "H"), "C": (10e-6, "F")}
    values[name] = (value, unit)
    return expand(spec_of("rlc_series", simulation or {"h": 1e-6, "t_end": 1e-3}, **values))


@pytest.mark.parametrize("name", ["Vin", "R", "L", "C"])
@pytest.mark.parametrize("prefix", list(PREFIX), ids=lambda p: p or "接頭語なし")
def test_c13_every_prefix_and_symbol_converts_to_si(name, prefix):
    """接頭語 11 通り × 項目 4 つ × 記号の書き方（Ω は 4 通り）。m はミリ、M はメガ。"""
    for symbol in SYMBOLS[name]:
        ex = _expand_with(name, 4.7, prefix + symbol)
        assert _value_in_netlist(ex, name) == pytest.approx(4.7 * PREFIX[prefix], rel=1e-12)
        converted = [n for n in ex.notes if n.startswith(f"{name}:")]
        if prefix:
            assert len(converted) == 1, "換算したことが返っていない"
            assert "4.7" in converted[0] and prefix + symbol in converted[0]
        else:
            assert converted == [], "換算していないのに注意が出た"


def test_c13_criterion_examples():
    """基準 13 の例: 1 mH は 1e-3 H、µ の3通り、Ω の2通り、kg はエラー。"""
    assert _value_in_netlist(_expand_with("L", 1, "mH"), "L") == 1e-3
    for micro in ("u", "µ", "μ"):
        assert _value_in_netlist(_expand_with("L", 47, micro + "H"), "L") == pytest.approx(47e-6, rel=1e-12)
    for ohm in ("ohm", "Ω"):
        assert _value_in_netlist(_expand_with("R", 3.3, "k" + ohm), "R") == pytest.approx(3300.0, rel=1e-12)
    with pytest.raises(ValueError, match="kg"):
        _expand_with("L", 1, "kg")


def test_c13_milli_and_mega_are_distinguished():
    assert _value_in_netlist(_expand_with("R", 5, "mohm"), "R") == pytest.approx(5e-3, rel=1e-12)
    assert _value_in_netlist(_expand_with("R", 5, "Mohm"), "R") == pytest.approx(5e6, rel=1e-12)
    assert _value_in_netlist(_expand_with("R", 5, "mΩ"), "R") == pytest.approx(5e-3, rel=1e-12)
    assert _value_in_netlist(_expand_with("R", 5, "MΩ"), "R") == pytest.approx(5e6, rel=1e-12)


def test_c13_units_give_the_same_waveform_as_si_values():
    """接頭語つきの仕様と、SI の値で書いた仕様が、同じ波形になる（解析解とも 1% 以内）。"""
    prefixed = spec_of("rlc_series", Vin=(10000.0, "mV"), R=(0.06, "kohm"), L=(1.0, "mH"), C=(10.0, "µF"))
    plain = rlc_spec(60.0)
    ex_a, res_a, notes_a = run_spec(prefixed)
    ex_b, res_b, notes_b = run_spec(plain)
    assert res_a.t.shape == res_b.t.shape
    assert np.allclose(res_a.x, res_b.x, rtol=1e-9, atol=1e-12)
    v, i = rlc_exact(res_a.t, 10.0, 60.0, 1e-3, 10e-6)
    assert max_err(res_a["v(out)"], v, 10.0) < TOL
    assert max_err(res_a["i(L1)"], i, float(i.max())) < TOL
    assert len(notes_a) == 4 and notes_b == []                   # 4 項目とも換算したことが返る


@pytest.mark.parametrize("name, unit, found, expected", [
    ("R", "V", "電圧", "抵抗"), ("L", "F", "キャパシタンス", "インダクタンス"),
    ("C", "uH", "インダクタンス", "キャパシタンス"), ("Vin", "A", "電流", "電圧"),
    ("L", "kHz", "周波数", "インダクタンス"), ("R", "ms", "時間", "抵抗"),
    ("C", "W", "電力", "キャパシタンス"),
], ids=["R-V", "L-F", "C-uH", "Vin-A", "L-kHz", "R-ms", "C-W"])  # 項目と単位だけ（量の名前は ID に入れない）
def test_c13_wrong_dimension_is_an_error_naming_item_unit_and_expectation(name, unit, found, expected):
    with pytest.raises(ValueError) as exc:
        _expand_with(name, 1.0, unit)
    text = str(exc.value)
    assert f"{name}:" in text and f"'{unit}'" in text            # どの項目の、どの単位が
    assert found in text and expected in text                    # 何の単位で、何を期待していたか


@pytest.mark.parametrize("unit", [
    "kg", "SI", "", "   ", "x", "henry", "ohms", "OHM", "KΩ", "Kohm", "kOHM", "mh", "uf", "UF",
    "R", "Vdc", "volt", "m H", "k ohm", "ｍＨ", "mmH", "1e-3H", "H/m", "daH", "TΩ", "㏀",
], ids=lambda u: repr(u))
def test_c13_unreadable_unit_is_an_error_not_a_guess(unit):
    """読めない単位（"SI"、空文字、大文字小文字の揺れ、間の空白、全角など）は、推測せずに断る。"""
    name = "R" if ("ohm" in unit.lower() or "Ω" in unit or unit in ("R", "㏀")) else "L"
    with pytest.raises(ValueError) as exc:
        _expand_with(name, 1.0, unit)
    text = str(exc.value)
    assert f"{name}:" in text and f"'{unit}'" in text
    assert ("抵抗" if name == "R" else "インダクタンス") in text    # 期待していたもの


@pytest.mark.parametrize("unit", [" mH", "mH ", "\tmH\n", "　mH", " mH"], ids=lambda u: repr(u))
def test_c13_surrounding_whitespace_is_ignored(unit):
    assert _value_in_netlist(_expand_with("L", 2.0, unit), "L") == pytest.approx(2e-3, rel=1e-12)


def test_c13_all_unit_errors_are_reported_together():
    spec = spec_of("rlc_series", Vin=(10.0, "V"), R=(1.0, "V"), L=(1.0, "kg"), C=(1.0, "H"))
    with pytest.raises(ValueError) as exc:
        expand(spec)
    text = str(exc.value)
    assert all(f"{name}:" in text for name in ("R", "L", "C")) and "Vin:" not in text


def test_c13_cli_shows_conversions_and_unit_errors(tmp_path, capsys):
    path = tmp_path / "spec.json"
    plant = {"Vin": {"value": 10, "unit": "V"}, "R": {"value": 1, "unit": "kohm"},
             "L": {"value": 1, "unit": "mH"}, "C": {"value": 10, "unit": "µF"}}
    text = json.dumps({"template": "rlc_series", "plant": plant}, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    assert cli_main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "R: 1 kohm" in out and "1000 ohm" in out and "L: 1 mH" in out and "C: 10 µF" in out

    plant["L"]["unit"] = "kg"
    text = json.dumps({"template": "rlc_series", "plant": plant}, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    assert cli_main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "L:" in captured.err and "'kg'" in captured.err
    assert "Traceback" not in captured.err and len(captured.err.strip().splitlines()) == 1


def test_c13_unused_items_are_reported_and_plain_si_is_silent():
    values = {"Vin": (10.0, "V"), "R": (1.0, "ohm"), "L": (1e-3, "H"), "C": (10e-6, "F")}
    assert expand(spec_of("rlc_series", **values)).notes == []
    ex = expand(spec_of("rlc_series", Rload=(5.0, "ohm"), **values))
    assert len(ex.notes) == 1 and "Rload" in ex.notes[0]


# ================================================== 基準 14: ステップ数の上限と既定

def test_c14_criterion_circuit_returns_within_a_second_and_is_correct():
    """R = 10 kΩ、L = 1 mH、C = 10 µF（ζ = 500）、計算の指定なし。E0 では 1.6 億ステップで MemoryError。"""
    start = time.perf_counter()
    ex, res, notes = run_spec(rlc_spec(1e4))
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"{elapsed:.2f} 秒"
    assert len(res.t) - 1 <= DEFAULT_STEP_BUDGET
    assert res.t[-1] == pytest.approx(5 * 1e4 * 10e-6, rel=1e-3)        # 遅い時定数 RC の 5 倍まで計算する
    v, i = rlc_exact(res.t, 10.0, 1e4, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL
    # t=0 からの立ち上がり（0.1 µs）は格子に無いので、最初の点は比べない
    assert max_err(res["i(L1)"][1:], i[1:], float(i.max())) < TOL
    coarse = [n for n in ex.notes if "刻み" in n]
    assert len(coarse) == 1, f"既定の刻みを粗くしたことが返っていない: {notes}"
    assert "速い成分" in coarse[0] and "遅い成分" in coarse[0]           # 原因
    assert "simulation.h" in coarse[0]                                  # 直し方


@pytest.mark.parametrize("r", [113.0, 200.0, 400.0, 500.0, 600.0, 800.0, 1e3, 2e3, 1e5, 1e6])
def test_c14_coarsened_default_step_gives_the_right_waveform_or_says_so(r):
    """過減衰（ζ = 5.65〜50000）の既定。20,000 ステップ以内で、v(out) は常に 1% 以内。

    i(L1) は、速い極を解像しない刻みになるので、先頭の数点が真値とずれうる。そのときは注意が出ていること。
    """
    ex, res, notes = run_spec(rlc_spec(r))
    assert len(res.t) - 1 <= DEFAULT_STEP_BUDGET
    v, i = rlc_exact(res.t, 10.0, r, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL
    assert res["i(L1)"].max() == pytest.approx(float(i.max()), rel=TOL)   # ピーク値は合う
    assert res["i(L1)"].min() >= -TOL * float(i.max())                    # 符号を変えて振れない
    assert max_err(res["i(L1)"], i, float(i.max())) < TOL or any("刻み" in n for n in notes)
    assert max_err(res["i(L1)"][3:], i[3:], float(i.max())) < 3 * TOL     # 先頭 3 点を除けば 3% 以内


@pytest.mark.parametrize("r", [1.0, 20.0, 60.0, 112.0])
def test_c14_default_is_untouched_when_it_fits(r):
    """20,000 ステップに収まる回路（ζ ≤ 5.6）は、最速の極の周期を 200 分割する既定のまま。注意も出ない。"""
    ex, res, notes = run_spec(rlc_spec(r))
    poles = np.roots([1.0, r / 1e-3, 1.0 / (1e-3 * 10e-6)])
    assert ex.h == pytest.approx(2 * math.pi / np.abs(poles).max() / 200, rel=0.01)
    assert notes == []
    v, i = rlc_exact(res.t, 10.0, r, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL


def test_c14_slowly_decaying_oscillation_shortens_the_default_time_and_says_so():
    """不足減衰で減衰が遅い（ζ = 0.0005）: 刻みは保ち、計算時間を 20,000 ステップに短くして知らせる。"""
    ex, res, notes = run_spec(rlc_spec(0.01))
    period = 2 * math.pi * math.sqrt(1e-3 * 10e-6)
    assert len(res.t) - 1 == DEFAULT_STEP_BUDGET
    assert period / ex.h == pytest.approx(200, rel=0.01)                  # 振動は 1 周期 200 点のまま
    short = [n for n in ex.notes if "終了時刻" in n]   # E1g（D-063）: 「計算時間」→「終了時刻」
    assert len(short) == 1 and "simulation.t_end" in short[0] and "減衰が遅い" in short[0]
    v, _ = rlc_exact(res.t, 10.0, 0.01, 1e-3, 10e-6)
    assert max_err(res["v(out)"][:2001], v[:2001], 10.0) < TOL             # 最初の 10 周期は 1% 以内


def test_c14_slowly_decaying_oscillation_default_is_within_one_percent():
    ex, res, notes = run_spec(rlc_spec(0.01))
    v, _ = rlc_exact(res.t, 10.0, 0.01, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL or any("位相" in n or "精度" in n for n in notes)


def test_c14_long_user_time_on_oscillating_circuit_is_accurate_or_says_oscillation_is_unresolved():
    ex, res, notes = run_spec(rlc_spec(1.0, {"t_end": 1.0}))
    assert len(res.t) - 1 <= DEFAULT_STEP_BUDGET
    v, _ = rlc_exact(res.t, 10.0, 1.0, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL or any("振動" in n for n in notes)


def test_c14_user_time_on_oscillating_circuit_is_fine_while_resolution_holds():
    """同じ回路で t_end = 0.1 s なら 1 周期 126 点。1% 以内で、刻みを変えたことは注意に出る。"""
    ex, res, notes = run_spec(rlc_spec(1.0, {"t_end": 0.1}))
    v, _ = rlc_exact(res.t, 10.0, 1.0, 1e-3, 10e-6)
    assert max_err(res["v(out)"], v, 10.0) < TOL
    assert len(res.t) - 1 == DEFAULT_STEP_BUDGET and any("刻み" in n for n in ex.notes)


def test_c14_user_step_is_respected_and_too_many_steps_is_an_error():
    """simulation.h を指定したら、そのまま使う。上限（2000万）を超えるなら、計算の前に断る。

    断る文面には、理由（ステップ数と上限）と直し方（h を大きく、t_end を短く）が入っている。
    """
    ex = expand(rlc_spec(1e4, {"h": 1e-6}))
    assert ex.h == 1e-6
    ex = expand(rlc_spec(1e4, {"h": 1e-9}))                                # 5 億ステップ
    start = time.perf_counter()
    with pytest.raises(ValueError) as exc:
        simulate(ex.netlist, ex.t_end, ex.h)
    assert time.perf_counter() - start < 0.5
    text = str(exc.value)
    assert "上限" in text and "h を大きく" in text and "t_end を短く" in text


@pytest.mark.parametrize("steps, ok", [
    (100, True), (100.4, True), (100.6, False), (101, False), (1e12, False),
])
def test_c14_max_steps_boundary(steps, ok):
    h = 1e-5
    if ok:
        res = simulate(rc_net(DC(value=1.0)), steps * h, h, max_steps=100)
        assert len(res.t) - 1 == 100
    else:
        with pytest.raises(ValueError, match="上限の 100"):
            simulate(rc_net(DC(value=1.0)), steps * h, h, max_steps=100)


def test_c14_default_limit_rejects_before_allocating_and_can_be_raised():
    h = 1e-5
    start = time.perf_counter()
    with pytest.raises(ValueError, match="上限") as exc:
        simulate(rc_net(DC(value=1.0)), 20_000_001 * h, h)                 # 既定の上限 2000万 + 1
    with pytest.raises(ValueError, match="上限"):
        simulate(rc_net(DC(value=1.0)), 1e12 * h, h)
    assert time.perf_counter() - start < 0.5, "配列を確保してから断っている"
    assert "max_steps" in str(exc.value)
    res = simulate(rc_net(DC(value=1.0)), 300 * h, h, max_steps=300)
    assert len(res.t) - 1 == 300


def test_c14_result_size_limit_for_many_unknowns():
    """未知数の多い回路（はしご 300 段 = 302 変数）× 100 万ステップ = 2.3 GB は、確保する前に断る。"""
    comps = [V("V1", "n0", "0", Step(final=1.0))]
    for k in range(1, 301):
        comps += [R(f"R{k}", f"n{k - 1}", f"n{k}", 1e3), C(f"C{k}", f"n{k}", "0", 1e-7)]
    start = time.perf_counter()
    with pytest.raises(ValueError) as exc:
        simulate(Netlist(components=comps), 1.0, 1e-6)
    assert time.perf_counter() - start < 1.0
    assert "GB" in str(exc.value) and "302" in str(exc.value)


def test_c14_cli_reports_the_limit_in_one_line(tmp_path, capsys):
    path = tmp_path / "spec.json"
    spec = {"template": "rlc_series", "simulation": {"h": 1e-9}, "plant": {
        "Vin": {"value": 10, "unit": "V"}, "R": {"value": 10, "unit": "kohm"},
        "L": {"value": 1, "unit": "mH"}, "C": {"value": 10, "unit": "uF"}}}
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert cli_main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "上限" in captured.err and len(captured.err.strip().splitlines()) == 1

    del spec["simulation"]                                                 # 基準 14 の回路そのもの
    path.write_text(json.dumps(spec), encoding="utf-8")
    start = time.perf_counter()
    assert cli_main([str(path), "--csv", str(tmp_path / "out.csv")]) == 0
    assert time.perf_counter() - start < 1.0
    out = capsys.readouterr().out
    assert "ステップ数: 20000" in out and out.count("注意:") >= 4          # 換算 3 件 ＋ 刻みを粗くした旨
    assert len((tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()) == 20002


# ====================================== 注意（notes）: 出るべきときに出て、出ないべきときに出ない

QUIET = {
    "解像したRC": lambda: simulate(rc_net(Step(final=1.0)), 5e-3, 1e-5),
    "刻みが時定数の半分": lambda: simulate(rc_net(Step(final=1.0)), 5e-3, 5e-4),
    "格子点に乗るステップ": lambda: simulate(rc_net(Step(final=1.0, t_step=1e-3)), 5e-3, 1e-5),
    "丸め誤差のある整数倍 0.3 / 0.1": lambda: simulate(rc_net(DC(value=1.0), r=1e6), 0.3, 0.1),
    "1周期 20 点の正弦波": lambda: simulate(rc_net(Sine(amplitude=1.0, frequency=1e3), c=1e-7), 5e-3, 5e-5),
    "純抵抗": lambda: simulate(net(V("V1", "a", "0", Step(final=1.0, t_step=5e-4)), R("R1", "a", "b", 1e3),
                                R("R2", "b", "0", 1e3)), 1e-3, 1e-5),
    "矛盾しない初期値（直列C・浮いたC）": lambda: simulate(net(
        V("V1", "in", "0", DC(value=10.0)), R("R1", "in", "a", 1e3), C("C1", "a", "m", 1e-6, ic=1.0),
        C("C2", "m", "0", 2e-6, ic=2.0)), 5e-3, 1e-5),
    "電流のランプ（V‖L、時定数なし）": lambda: simulate(
        net(V("V1", "a", "0", DC(value=1.0)), L("L1", "a", "0", 1e-3)), 1e-3, 1e-5),
    "初期値つき直列RLC": lambda: simulate(net(
        V("V1", "in", "0", DC(value=10.0)), R("R1", "in", "a", 5.0), L("L1", "a", "out", 1e-3, ic=0.2),
        C("C1", "out", "0", 10e-6, ic=3.0)), 5e-3, 2.5e-6),
}


@pytest.mark.parametrize("name", list(QUIET))
def test_notes_stay_empty_when_nothing_is_wrong(name):
    assert QUIET[name]().notes == []


NOISY = {
    "刻みが時定数の 10 倍": (lambda: simulate(rc_net(Step(final=1.0)), 1.0, 1e-2), ["時定数", "以下にする"]),
    "正弦波が 1 周期 5 点": (lambda: simulate(rc_net(Sine(amplitude=1.0, frequency=2e4), c=1e-9), 5e-3, 1e-5),
                      ["V1", "正弦波", "以下"]),
    "格子点に乗らないステップ": (lambda: simulate(rc_net(Step(final=1.0, t_step=1.234e-4)), 5e-3, 1e-5),
                      ["V1", "丸めた"]),
    "t_end が刻みの整数倍でない": (lambda: simulate(rc_net(DC(value=1.0)), 1e-3, 6.7e-5),
                         ["整数倍", "で終える"]),
    "矛盾する初期値": (lambda: simulate(net(
        V("V1", "in", "0", DC(value=0.0)), R("R1", "in", "a", 1e3), C("C1", "a", "0", 1e-6, ic=2.0),
        C("C2", "a", "0", 1e-6, ic=5.0)), 5e-3, 1e-5), ["C1", "ic=2", "3.5", "矛盾"]),
    "振動が 1 周期 6 点": (lambda: simulate(net(
        V("V1", "in", "0", Step(final=10.0)), R("R1", "in", "a", 1.0), L("L1", "a", "out", 1e-3),
        C("C1", "out", "0", 10e-6)), 1e-2, 1e-4), ["振動", "周期", "以下"]),
}


@pytest.mark.parametrize("name", list(NOISY))
def test_notes_name_the_cause_and_the_fix(name):
    run, words = NOISY[name]
    notes = run().notes
    assert notes, "注意が出ていない"
    assert any(all(word in note for word in words) for note in notes), f"{words} を含む注意が無い: {notes}"


def test_notes_do_not_claim_damping_when_it_is_switched_off():
    res = simulate(rc_net(Step(final=1.0)), 4.0, 1e-2, damping_steps=0)
    assert res["v(out)"].max() > 1.5                                       # 台形則だけなので振れている
    assert res.notes and not any("減衰させている" in note for note in res.notes)
