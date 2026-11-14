"""E1a の受け入れ試験: スイッチとダイオードを含む回路を、解析式と照合する。

基準の回路は降圧コンバータ（24 V、L = 47 µH、C = 100 µF、100 kHz、1周期200分割）。
期待値はすべてこのファイルの中の式で作る（エンジンの関数を使わない）。
平均とリプルは、過渡が収まった後の、スイッチング周期の整数倍の区間で取る。

記録の決まり（照合のときに使う）:
  res["v(...)"][k] は時刻 t[k] の値。その時刻でスイッチやダイオードが切り替わる場合は、切り替わる前の値。
  res["gate(...)"][k] と res["on(...)"][k] は、区間 [t[k], t[k+1]) での値。
"""
import math

import numpy as np
import pytest

from circuit_sim import DC, CircuitError, Component, Netlist, Pwm, Sine, Step, simulate

VIN, L, C, FSW, N = 24.0, 47e-6, 100e-6, 100e3, 200
TS = 1.0 / FSW
H = TS / N
R_ON, R_OFF = 1e-3, 1e6          # 仕様の既定値（ここでは式の中で使う）
R_CCM, R_DCM = 6.0, 100.0
DUTY = 0.5
WINDOW = 20                      # 平均を取る周期の数

# 過渡が収まるまでの時間（決め方）
#   連続導通: 出力の LC 共振の包絡は exp(-t / (2 R C))、2 R C = 1.2 ms。
#             12 ms（その10倍）で、起動時の振れ（約 12 V）は e^-10 = 5e-5 倍（約 0.5 mV、出力の 0.005%）。
#   不連続導通: 出力の時定数は R C (1 - M) / (2 - M) = 1.84 ms（M = Vout / Vin = 0.774）。
#             16 ms（その8.7倍）で、起動時の差（約 5 V）は e^-8.7 = 1.7e-4 倍（約 1 mV、出力の 0.005%）。
T_CCM = 12e-3
T_DCM = 16e-3


def buck(load: float, ic_c: float = 0.0) -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=VIN)),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q1"),
        Component(id="D1", type="D", nodes=("0", "sw")),       # アノードが基準節点側
        Component(id="L1", type="L", nodes=("sw", "out"), value=L),
        Component(id="C1", type="C", nodes=("out", "0"), value=C, ic=ic_c),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
    ])


def pwm(duty=DUTY, **kw) -> list[Pwm]:
    return [Pwm(name="q1", frequency=FSW, duty=duty, **kw)]


def rounded_duty(duty: float, divisions: int = N) -> float:
    """格子点に丸めた後のデューティ（四捨五入）。"""
    return math.floor(duty * divisions + 0.5) / divisions


def last_periods(res, name: str, periods: int = WINDOW) -> np.ndarray:
    """最後の periods 周期ぶんのサンプル（周期の整数倍。先頭の点は含めず、末尾の点を含める）。"""
    n = len(res.t) - 1
    assert n % N == 0
    return res[name][n - periods * N + 1: n + 1]


def last_intervals(res, name: str, periods: int = WINDOW) -> np.ndarray:
    """最後の periods 周期ぶんの区間の値（gate / cmd / on 用。区間 k は [t[k], t[k+1])）。"""
    n = len(res.t) - 1
    assert n % N == 0
    return res[name][n - periods * N: n]


def dcm_theory() -> tuple[float, float, float]:
    """不連続導通の降圧コンバータ: 出力電圧、ピーク電流、ダイオードの導通時間（周期に対する比）。"""
    d = rounded_duty(DUTY)
    k = 2.0 * L / (R_DCM * TS)
    v_out = VIN * 2.0 / (1.0 + math.sqrt(1.0 + 4.0 * k / d**2))
    i_peak = (VIN - v_out) * d * TS / L
    d2 = d * (VIN - v_out) / v_out
    return v_out, i_peak, d2


@pytest.fixture(scope="module")
def ccm():
    return simulate(buck(R_CCM), T_CCM, gates=pwm())


@pytest.fixture(scope="module")
def dcm():
    return simulate(buck(R_DCM), T_DCM, gates=pwm())


# ------------------------------------------------------------ 基準1〜2: 連続導通

def test_time_step_follows_divisions_per_period(ccm):
    """1周期の分割数の既定は200。時間刻みは 周期 / 分割数。"""
    assert ccm.h == pytest.approx(TS / 200, rel=1e-12)
    assert len(ccm.t) - 1 == round(T_CCM / H)
    res = simulate(buck(R_CCM), 2 * TS, gates=pwm(), steps_per_period=50)
    assert res.h == pytest.approx(TS / 50, rel=1e-12)


def test_ccm_output_voltage_average(ccm):
    """基準1: 連続導通の出力電圧の周期平均が D·Vin と 1% 以内。"""
    expected = rounded_duty(DUTY) * VIN
    measured = last_periods(ccm, "v(out)").mean()
    assert abs(measured - expected) < 0.01 * expected
    # 参照を1割ずらすと落ちる（この試験が誤りを見逃さないことの確認）
    assert abs(measured - 1.1 * expected) > 0.01 * expected


def test_ccm_inductor_ripple(ccm):
    """基準2: インダクタ電流のリプル（山と谷の差）が (Vin - Vout)·D / (L·fsw) と 2% 以内。"""
    d = rounded_duty(DUTY)
    v_out = d * VIN
    expected = (VIN - v_out) * d / (L * FSW)
    assert expected == pytest.approx(1.28, abs=0.005)       # 仕様に書かれた値
    i = last_periods(ccm, "i(L1)")
    measured = i.max() - i.min()
    assert abs(measured - expected) < 0.02 * expected
    assert abs(measured - 1.1 * expected) > 0.02 * expected
    # 平均電流は負荷電流（コンデンサの電荷の収支）。過渡の残りは 0.005% 程度なので 0.1% で見る
    assert i.mean() == pytest.approx(last_periods(ccm, "v(out)").mean() / R_CCM, rel=1e-3)


def test_duty_is_rounded_to_grid():
    """デューティは 1 / 分割数 の単位に丸められる。期待値は丸めた後の値で合う。"""
    duty = 0.333                                  # 200分割では 66.6 → 67 ステップ
    assert rounded_duty(duty) == pytest.approx(0.335)
    res = simulate(buck(R_CCM), T_CCM, gates=pwm(duty))
    assert last_intervals(res, "gate(q1)", 1).sum() == 67
    measured = last_periods(res, "v(out)").mean()
    # オン抵抗による降下は 0.02% 程度。丸める前（7.992 V）と後（8.04 V）の差 0.6% を
    # 見分けるために、入力電圧の 0.1% で比べる
    assert abs(measured - 0.335 * VIN) < 1e-3 * VIN
    assert abs(measured - 0.333 * VIN) > 1e-3 * VIN


# ---------------------------------------------------------- 基準3〜4: 不連続導通

def test_dcm_output_voltage_average(dcm):
    """基準3: 不連続導通の出力電圧の周期平均が理論値と 2% 以内。"""
    expected, _, _ = dcm_theory()
    assert expected == pytest.approx(18.6, abs=0.05)        # 仕様に書かれた値
    measured = last_periods(dcm, "v(out)").mean()
    assert abs(measured - expected) < 0.02 * expected
    # 連続導通の式（D·Vin = 12 V）とは合わない
    assert abs(measured - rounded_duty(DUTY) * VIN) > 0.02 * expected


def test_dcm_peak_current_and_diode_conduction_time(dcm):
    """ピーク電流とダイオードの導通時間が理論値と合う（導通時間は最大1刻みずれる）。"""
    _, i_peak, d2 = dcm_theory()
    assert last_periods(dcm, "i(L1)").max() == pytest.approx(i_peak, rel=0.02)
    on_steps = last_intervals(dcm, "on(D1)", 1).sum()
    assert abs(on_steps - d2 * N) <= 1.0


def test_dcm_current_does_not_go_negative(dcm):
    """基準4（前半）: インダクタ電流の最小値がピーク電流の -1% 以上（計算の全区間で）。"""
    _, i_peak, _ = dcm_theory()
    assert dcm["i(L1)"].min() >= -0.01 * i_peak


def off_interval_residual(res, periods: int) -> tuple[float, float]:
    """スイッチもダイオードもオフの区間に残る振れ: |v(sw) - v(out)| と |i(L1)| の最大値。

    区間は理論から決める（エンジンの記録を使わない）: オフになるのは周期の頭から
    (D + D2) 周期が経った後。そこから2点（後退オイラーで減衰させる2ステップぶん）を除く。
    この区間ではインダクタに電圧がかからないので、v(sw) = v(out)、電流はほぼ0のはず。
    """
    _, _, d2 = dcm_theory()
    first = round(rounded_duty(DUTY) * N) + math.ceil(d2 * N) + 2
    n = len(res.t) - 1
    k = np.arange(n - periods * N + 1, n + 1)
    phase = k % N
    inside = (phase >= first) | (phase == 0)          # 周期の最後の点（次の周期の頭の直前）まで
    dv = np.abs(res["v(sw)"][k] - res["v(out)"][k])[inside]
    di = np.abs(res["i(L1)"][k])[inside]
    return float(dv.max()), float(di.max())


def test_dcm_no_ringing_while_switch_and_diode_are_off(dcm):
    """基準4（後半）: スイッチもダイオードもオフの区間で振動が残らない。

    許容差: v(sw) - v(out) は入力電圧の 1%（0.24 V）以内、電流はピークの 0.1% 以内。
    根拠: 後退オイラー1ステップで硬い成分は 1 / (1 + h·r_off / (2L)) = 1/533 になる。
    2ステップ後の残りは最大でも Vout / 533 = 35 mV で、1% は十分に緩い。
    """
    _, i_peak, _ = dcm_theory()
    dv, di = off_interval_residual(dcm, WINDOW)
    assert dv < 0.01 * VIN
    assert di < 1e-3 * i_peak


def test_ringing_check_detects_insufficient_damping():
    """後退オイラーが1ステップだけだと振動が残り、上の基準で落ちる（試験の実効性の確認）。"""
    v_out, _, _ = dcm_theory()
    res = simulate(buck(R_DCM, ic_c=v_out), 200 * TS, gates=pwm(), damping_steps=1)
    dv, _ = off_interval_residual(res, 100)
    assert dv > 0.01 * VIN


# ------------------------------------------------------------- 基準5: 半波整流

def test_half_wave_rectifier():
    """基準5: 負荷電圧が max(v, 0)·R / (R + r_on) と、振幅の 1% 以内で一致。"""
    amp, f, load = 10.0, 50.0, 100.0
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Sine(amplitude=amp, frequency=f)),
        Component(id="D1", type="D", nodes=("in", "out")),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
    ])
    res = simulate(net, 3 / f, 1 / f / 400)
    v = amp * np.sin(2 * math.pi * f * res.t)
    expected = np.maximum(v, 0.0) * load / (load + R_ON)
    assert np.max(np.abs(res["v(out)"] - expected)) < 0.01 * amp
    # ダイオードの向きを逆にした参照とは合わない
    assert np.max(np.abs(res["v(out)"] - np.minimum(v, 0.0))) > 0.5 * amp
    # 逆方向に流れるのはオフ抵抗の漏れだけ
    assert res.i("D1").min() >= -amp / R_OFF * 1.01


def test_full_bridge_rectifier():
    """ダイオード4個（状態決定で複数のダイオードが同時に反転する場合）。負荷電圧は |v|。"""
    amp, f, load = 10.0, 50.0, 100.0
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "b"), waveform=Sine(amplitude=amp, frequency=f)),
        Component(id="D1", type="D", nodes=("a", "p")),
        Component(id="D2", type="D", nodes=("b", "p")),
        Component(id="D3", type="D", nodes=("0", "a")),
        Component(id="D4", type="D", nodes=("0", "b")),
        Component(id="R1", type="R", nodes=("p", "0"), value=load),
    ])
    res = simulate(net, 3 / f, 1 / f / 400)
    v = amp * np.sin(2 * math.pi * f * res.t)
    expected = np.abs(v) * load / (load + 2 * R_ON)          # 導通するダイオードは2個直列
    assert np.max(np.abs(res["v(p)"] - expected)) < 0.01 * amp


# ------------------------------------------------- 基準6: 切り替え点での連続性

def test_state_continuity_ccm(ccm):
    """基準6: 切り替えの前後で、インダクタ電流とコンデンサ電圧に跳びが無い。

    測り方: 1ステップごとの変化量から、回路の式で決まる変化量（傾き × 刻み）を引いた残りを
    「跳び」とし、切り替えをまたぐステップを含む全ステップで、代表値（平均値）の 0.1% 以内。
    スイッチのオン・オフは理論の時刻（周期の頭から D 周期）で決め、エンジンの記録を使わない。
    切り替えが1刻みずれると、残りは Vin·h / L = 25.5 mA（平均電流 2 A の 1.3%）になり、落ちる。
    """
    n = len(ccm.t) - 1
    k = np.arange(n - WINDOW * N, n)                         # ステップ k は t[k] → t[k+1]
    i, v = ccm["i(L1)"], ccm["v(out)"]
    switch_on = (k % N) < round(rounded_duty(DUTY) * N)
    slope = np.where(switch_on, (VIN - v[k]) / L, -v[k] / L)   # 連続導通では、オフのあいだはダイオードが導通
    jump_i = np.abs(i[k + 1] - i[k] - slope * H)
    jump_v = np.abs(v[k + 1] - v[k] - (i[k] - v[k] / R_CCM) * H / C)
    assert jump_i.max() < 1e-3 * i[k].mean()
    assert jump_v.max() < 1e-3 * v[k].mean()
    # 切り替えを1刻み遅らせた参照とは合わない
    late = np.where(((k - 1) % N) < round(rounded_duty(DUTY) * N), (VIN - v[k]) / L, -v[k] / L)
    assert np.abs(i[k + 1] - i[k] - late * H).max() > 1e-3 * i[k].mean()


def test_state_continuity_dcm(dcm):
    """基準6（不連続導通）: ダイオードが自然に切れる点も含めて、状態量に跳びが無い。

    オフのあいだの電流は max(i - Vout·h / L, 0)（ダイオードは逆方向に流さない）。代表値はピーク電流。
    """
    _, i_peak, _ = dcm_theory()
    n = len(dcm.t) - 1
    k = np.arange(n - WINDOW * N, n)
    i, v = dcm["i(L1)"], dcm["v(out)"]
    switch_on = (k % N) < round(rounded_duty(DUTY) * N)
    expected = np.where(switch_on, i[k] + (VIN - v[k]) / L * H, np.maximum(i[k] - v[k] / L * H, 0.0))
    assert np.abs(i[k + 1] - expected).max() < 1e-3 * i_peak
    jump_v = np.abs(v[k + 1] - v[k] - (i[k] - v[k] / R_DCM) * H / C)
    assert jump_v.max() < 1e-3 * v[k].mean()


def test_recorded_samples_are_consistent(ccm, dcm):
    """整合: どの時刻でも節点の電流の和が0、ダイオードは逆方向に流さず、貫通が記録に残らない。"""
    for res in (ccm, dcm):
        i_l = res["i(L1)"]
        scale = np.abs(i_l).max()
        # 節点 sw の電流則: スイッチとダイオードから入り、インダクタへ出る
        assert np.max(np.abs(res.i("S1") + res.i("D1") - i_l)) < 1e-9 * scale
        # ダイオードの逆電流はオフ抵抗の漏れ（Vin / r_off）まで
        assert res.i("D1").min() >= -VIN / R_OFF * 1.01
        # スイッチとダイオードが同時にオンの区間は無い（貫通の瞬間はダイオードがオフに落ち着く）
        assert np.max(res["on(S1)"] + res["on(D1)"]) == 1.0
        # 電源電流にも貫通（Vin / (2 r_on) = 12 kA）の跡が無い。
        # 電源電流は、インダクタ電流にオフのダイオードの漏れ（Vin / r_off）を足したものまで
        assert np.max(np.abs(res.i("V1"))) <= scale + 1.01 * VIN / R_OFF


def test_power_balance_ccm(ccm):
    """保存則: 定常の入力電力と出力電力が合う（損失はオン抵抗の 0.02% 程度なので 1% で見る）。

    入力電流はスイッチがオンのあいだのインダクタ電流。オンの区間の両端を半分ずつ数える（台形の面積）。
    """
    n = len(ccm.t) - 1
    n_on = round(rounded_duty(DUTY) * N)
    i = ccm["i(L1)"][n - WINDOW * N: n + 1].copy()
    charge = 0.0
    for p in range(WINDOW):
        seg = i[p * N: p * N + n_on + 1]
        charge += H * (seg.sum() - 0.5 * (seg[0] + seg[-1]))
    p_in = VIN * charge / (WINDOW * TS)
    p_out = np.mean(last_periods(ccm, "v(out)") ** 2) / R_CCM
    assert p_in == pytest.approx(p_out, rel=0.01)
    assert p_out == pytest.approx((rounded_duty(DUTY) * VIN) ** 2 / R_CCM, rel=0.02)


def test_second_order_convergence_with_switching():
    """収束次数: 刻みを半分にすると誤差が約 1/4 になる（切り替えが格子点に乗る連続導通の場合）。

    切り替え直後の後退オイラーは1次精度だが、1周期に入る回数が刻みによらず一定なので、
    全体では2次のまま。起動からの40周期を、1周期1600分割の結果と、周期の頭と中央の点で比べる。
    """
    periods = 40

    def run(divisions: int) -> tuple[np.ndarray, np.ndarray]:
        res = simulate(buck(R_CCM), periods * TS, gates=pwm(), steps_per_period=divisions)
        k = np.arange(0, periods * divisions + 1, divisions // 2)
        return res["v(out)"][k], res["i(L1)"][k]

    v_ref, i_ref = run(1600)
    err_v, err_i = [], []
    for divisions in (50, 100, 200):
        v, i = run(divisions)
        err_v.append(np.max(np.abs(v - v_ref)))
        err_i.append(np.max(np.abs(i - i_ref)))
    for err in (err_v, err_i):
        assert 3.5 < err[0] / err[1] < 4.5
        assert 3.5 < err[1] / err[2] < 4.5
    assert err_v[2] < 1e-4 * VIN            # 200分割での誤差は入力電圧の 0.01% 以下


# --------------------------------------------------------- 基準7: 制御器の口

class FixedDuty:
    """固定値を返す制御器。測った値と時刻を覚えておく。"""

    def __init__(self, value: float, period: float = TS):
        self.period = period
        self.inputs = ("v(out)", "i(L1)")
        self.outputs = ("duty",)
        self.value = value
        self.seen: list[tuple[float, float, float]] = []

    def reset(self):
        self.seen = []
        return [self.value]

    def step(self, t, measured):
        self.seen.append((t, float(measured[0]), float(measured[1])))
        return [self.value]


class DutyChange(FixedDuty):
    """時刻 t_change 以降に測ったら、新しい値を返す制御器。"""

    def __init__(self, before: float, after: float, t_change: float, period: float = TS):
        super().__init__(before, period)
        self.after = after
        self.t_change = t_change

    def step(self, t, measured):
        super().step(t, measured)
        return [self.after if t >= self.t_change - 0.5 * H else self.value]


def test_constant_controller_matches_fixed_duty(ccm):
    """基準7（前半）: 固定値を返す制御器で、基準1と同じ結果になる。"""
    ctrl = FixedDuty(DUTY)
    res = simulate(buck(R_CCM), T_CCM, gates=pwm("duty"), controllers=[ctrl])
    measured = last_periods(res, "v(out)").mean()
    assert abs(measured - rounded_duty(DUTY) * VIN) < 0.01 * rounded_duty(DUTY) * VIN
    # 波形そのものも、デューティを固定値で渡したときと同じ
    assert np.array_equal(res.x, ccm.x)
    assert np.array_equal(res["gate(q1)"], ccm["gate(q1)"])
    assert np.all(res["cmd(duty)"] == DUTY)
    # 制御周期ごとに1回呼ばれ、測った値は記録されたその時刻の値と同じ
    assert len(ctrl.seen) == round(T_CCM / TS)
    for j, (t, v_out, i_l) in enumerate(ctrl.seen):
        assert t == pytest.approx(j * TS, abs=1e-3 * H)
        assert v_out == res["v(out)"][j * N]
        assert i_l == res["i(L1)"][j * N]


def test_command_takes_effect_at_next_control_period():
    """基準7（後半）: 指令を途中で変えると、次の制御周期の頭から反映される（1サンプル遅れ）。

    制御器は t = 5 Ts の測定から 0.25 を返す。指令とゲートが変わるのは t = 6 Ts から。
    """
    ctrl = DutyChange(0.5, 0.25, 5 * TS)
    res = simulate(buck(R_CCM), 10 * TS, gates=pwm("duty"), controllers=[ctrl])
    cmd, gate = res["cmd(duty)"], res["gate(q1)"]
    assert np.all(cmd[: 6 * N] == 0.5)
    assert np.all(cmd[6 * N:] == 0.25)
    on_steps = [int(gate[p * N: (p + 1) * N].sum()) for p in range(10)]
    assert on_steps == [100] * 6 + [50] * 4
    # 電流の波形にも1サンプル遅れが出る: 周期5までは100ステップ上がり続け、周期6からは50ステップで下がり始める
    i = res["i(L1)"]
    assert np.argmax(i[5 * N: 6 * N + 1]) == 100
    assert np.argmax(i[6 * N: 7 * N + 1]) == 50


def test_initial_command_and_slow_control_period():
    """最初の制御周期は reset() の指令を使う。制御周期がスイッチング周期の2倍でも同じ決まりで動く。"""
    class Ramp(FixedDuty):
        def step(self, t, measured):
            super().step(t, measured)
            return [0.1 * (len(self.seen) + 1)]       # 呼ばれるたびに 0.2, 0.3, ...

    ctrl = Ramp(0.1, period=2 * TS)
    res = simulate(buck(R_CCM), 8 * TS, gates=pwm("duty"), controllers=[ctrl])
    on_steps = [int(res["gate(q1)"][p * N: (p + 1) * N].sum()) for p in range(8)]
    # 周期0〜1: reset の 0.1、周期2〜3: t=0 の演算結果 0.2、周期4〜5: t=2Ts の 0.3、…
    assert on_steps == [20, 20, 40, 40, 60, 60, 80, 80]
    assert [t for t, _, _ in ctrl.seen] == pytest.approx([0.0, 2 * TS, 4 * TS, 6 * TS], abs=1e-3 * H)


# ------------------------------------------------------------- 基準8: 再現性

def test_reproducible():
    """基準8: 同じ入力で2回実行して、全サンプルが完全一致（制御器つき、不連続導通を含む）。"""
    def run():
        ctrl = DutyChange(0.5, 0.3, 20 * TS)
        return simulate(buck(R_DCM), 60 * TS, gates=pwm("duty"), controllers=[ctrl])

    a, b = run(), run()
    assert np.array_equal(a.t, b.t)
    assert np.array_equal(a.x, b.x)
    assert a.names == b.names
    for name in a.signals:
        assert np.array_equal(a[name], b[name])
    # 同じ制御器オブジェクトを使い回しても、reset() で内部状態が戻るので同じ結果になる
    ctrl = DutyChange(0.5, 0.3, 20 * TS)
    c = simulate(buck(R_DCM), 60 * TS, gates=pwm("duty"), controllers=[ctrl])
    d = simulate(buck(R_DCM), 60 * TS, gates=pwm("duty"), controllers=[ctrl])
    assert np.array_equal(c.x, a.x) and np.array_equal(d.x, a.x)


# ---------------------------------------------------- 状態ごとの行列の使い回し

def test_matrices_are_prepared_once_per_circuit_state(monkeypatch):
    """行列の用意は回路状態ごとに1回だけ。計算を長くしても増えない。"""
    from circuit_sim import kernel

    calls = []
    original = kernel.prepare

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel, "prepare", counting)
    short = simulate(buck(R_DCM), 20 * TS, gates=pwm())
    n_short = len(calls)
    long = simulate(buck(R_DCM), 200 * TS, gates=pwm())
    assert n_short == short.stats["states"] <= 4            # スイッチ1個とダイオード1個で最大4状態
    assert len(calls) - n_short == long.stats["states"] <= 4
    assert long.stats["state_changes"] > 10 * short.stats["states"]


# ----------------------------------------------------------- ほかのトポロジー

def test_boost_converter_ccm():
    """昇圧コンバータ（インダクタ電流がスイッチからダイオードへ移る別の形）: Vout = Vin / (1 - D)。

    過渡: 包絡は exp(-t / (2 R C)) で 2 R C = 0.4 ms。6 ms（その15倍）後の20周期で平均する。
    許容差は基準1と同じ 1%（オン抵抗の降下は 0.03% 程度）。
    """
    v_in, ind, cap, load, duty = 12.0, 100e-6, 10e-6, 20.0, 0.4
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=v_in)),
        Component(id="L1", type="L", nodes=("in", "x"), value=ind),
        Component(id="S1", type="S", nodes=("x", "0"), gate="q1"),
        Component(id="D1", type="D", nodes=("x", "out")),
        Component(id="C1", type="C", nodes=("out", "0"), value=cap),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
    ])
    res = simulate(net, 6e-3, gates=pwm(duty))
    expected = v_in / (1.0 - rounded_duty(duty))
    assert last_periods(res, "v(out)").mean() == pytest.approx(expected, rel=0.01)
    assert last_periods(res, "i(L1)").mean() == pytest.approx(expected**2 / load / v_in, rel=0.01)
    # ダイオードの逆電流は、オフ抵抗の漏れ（かかる逆電圧は出力電圧。起動時の行き過ぎを含む）まで
    assert res.i("D1").min() >= -res["v(out)"].max() / R_OFF * 1.01


def test_source_step_with_diode():
    """電源のステップとダイオードの組み合わせ: ステップの後、ダイオードを通して RC を充電する。"""
    v, load, cap, t_step, h = 5.0, 1e3, 1e-6, 1e-3, 1e-5
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Step(initial=-v, final=v, t_step=t_step)),
        Component(id="D1", type="D", nodes=("in", "a")),
        Component(id="R1", type="R", nodes=("a", "out"), value=load),
        Component(id="C1", type="C", nodes=("out", "0"), value=cap),
    ])
    res = simulate(net, 6e-3, h)
    ks = round(t_step / h)
    # 電源の値は、ステップの時刻の点に切り替わった後の値が入る（E0 と同じ決まり）
    assert res["v(in)"][ks - 1] == pytest.approx(-v)
    assert res["v(in)"][ks] == pytest.approx(v)
    tau = (load + R_ON) * cap
    expected = np.where(res.t < t_step, 0.0, v * (1 - np.exp(-(res.t - t_step) / tau)))
    # ステップの前は、オフ抵抗の漏れで -v·t / (r_off·C) だけ下がる（1 ms で 5 mV = 0.1%）
    assert np.max(np.abs(res["v(out)"] - expected)) < 0.01 * v


# ------------------------------------------------------------ 入力の誤りと極限

def test_duty_limits():
    """デューティ 0 と 1（境界）でも壊れない。指令が 0〜1 の外に出たら 0 と 1 で止める。"""
    off = simulate(buck(R_CCM), 20 * TS, gates=pwm(0.0))
    assert off["gate(q1)"].max() == 0.0
    assert np.abs(off["v(out)"]).max() < 1e-3 * VIN           # 漏れ電流だけ
    on = simulate(buck(R_CCM), 20 * TS, gates=pwm(1.0))
    assert on["gate(q1)"].min() == 1.0

    class OutOfRange(FixedDuty):
        def step(self, t, measured):
            return [1.7 if t < 2.5 * TS else -0.4]      # t = 0, Ts, 2Ts の演算では 1.7、その後は -0.4

    res = simulate(buck(R_CCM), 8 * TS, gates=pwm("duty"), controllers=[OutOfRange(0.5)])
    on_steps = [int(res["gate(q1)"][p * N: (p + 1) * N].sum()) for p in range(8)]
    # 周期0: reset の 0.5、周期1〜3: 1.7 → 1 で止める、周期4〜: -0.4 → 0 で止める
    assert on_steps == [100, 200, 200, 200, 0, 0, 0, 0]
    assert res["cmd(duty)"][N] == 1.7                         # 記録には止める前の指令が残る


def test_input_errors_are_explained():
    net = buck(R_CCM)
    with pytest.raises(ValueError, match="ゲート信号 'q1' が渡されていない"):
        simulate(net, 1e-4, 1e-7)
    with pytest.raises(ValueError, match="整数倍"):
        simulate(net, 1e-4, 3e-8, gates=pwm())                 # 10 µs / 30 ns は整数でない
    with pytest.raises(ValueError, match="どちらか一方"):
        simulate(net, 1e-4, 5e-8, gates=pwm(), steps_per_period=200)
    with pytest.raises(ValueError, match="1 以上"):
        simulate(net, 1e-4, gates=pwm(), damping_steps=0)
    with pytest.raises(ValueError, match="デューティ指令 'duty' を出す制御器が無い"):
        simulate(net, 1e-4, gates=pwm("duty"))
    with pytest.raises(ValueError, match="制御周期"):
        simulate(net, 1e-4, gates=pwm("duty"), controllers=[FixedDuty(0.5, period=TS + 0.3 * H)])

    bad_input = FixedDuty(0.5)
    bad_input.inputs = ("v(nowhere)",)
    with pytest.raises(ValueError, match="回路に無い"):
        simulate(net, 1e-4, gates=pwm("duty"), controllers=[bad_input])

    class NoStep:
        period = TS
        inputs = ("v(out)",)
        outputs = ("duty",)

        def reset(self):
            return [0.5]

    with pytest.raises(TypeError, match="step"):
        simulate(net, 1e-4, gates=pwm("duty"), controllers=[NoStep()])

    class NotFinite(FixedDuty):
        def step(self, t, measured):
            return [float("nan")]

    with pytest.raises(ValueError, match="有限値でない"):
        simulate(net, 1e-4, gates=pwm("duty"), controllers=[NotFinite(0.5)])

    # スイッチの無い回路では h が必要（E0 と同じ使い方）
    rc = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="R1", type="R", nodes=("in", "0"), value=1.0),
    ])
    with pytest.raises(ValueError, match="h が必要"):
        simulate(rc, 1e-3)


def test_unsolvable_switched_circuit_is_rejected_before_stepping():
    """解けない回路（基準節点から浮いた部分）は、スイッチを含んでいても計算の前にエラーになる。"""
    net = Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=1.0)),
        Component(id="D1", type="D", nodes=("in", "out")),
        Component(id="R1", type="R", nodes=("out", "0"), value=1.0),
        Component(id="R2", type="R", nodes=("x", "y"), value=1.0),
        Component(id="R3", type="R", nodes=("x", "y"), value=1.0),
    ])
    with pytest.raises(CircuitError, match="回路状態"):
        simulate(net, 1e-3, 1e-5)


def test_unsettled_diode_state_reports_time_and_state(monkeypatch):
    """ダイオードの状態が規定回数で落ち着かないときは、時刻と状態が分かるエラーになる。

    反転の上限を0回にして、必ず反転が要る場面（スイッチがオフになる瞬間）で止める。
    """
    import importlib

    sim = importlib.import_module("circuit_sim.simulate")     # パッケージの simulate は関数なので、名前で引く
    monkeypatch.setattr(sim, "_flip_limit", lambda n_diodes: 0)
    with pytest.raises(CircuitError) as exc:
        simulate(buck(R_CCM), 2 * TS, gates=pwm())
    message = str(exc.value)
    assert "t=5e-06 s" in message                              # スイッチがオフになる時刻（0.5 周期）
    assert "S1=オフ" in message and "D1=オフ" in message
