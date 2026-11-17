"""E1g の独立検証（2回目。差し戻しの直し 48def1c の確かめ）: 進み具合の新しい作りと、警告を止める範囲。

1回目の検証（docs/verification/E1g-verifier.md）の中1・軽1・軽2 の直しを、検証役が自分の回路で確かめる。

  進み具合  制御器のある回路は、制御周期ごとに核から戻って、次の周期の配列（ゲート信号、指令）を埋める。
            そのあいだに知らせる値が、済んだ計算の値と、次に確かめる点の値のあいだに収まる（後処理の値
            0.99〜 に先回りしない）。確かめの点の値（0.02 + 0.97·k/n）が捨てられずに届く。
            済んだ計算は、核の関数（kernel.run_switched）を包んで、戻るたびに外から読む（進み具合を作る
            側の値は使わない）。
  中断      配列を埋めている最中（時間ステップの最中）にも、中断の求めを見る。途中の結果は返らず、流し直すと
            ビット単位で同じ。エラーの k は、計算の済んだ格子点（準備 0、配列を埋めている最中は制御周期の
            先頭、後処理 n）。
  結果      配列を埋める単位、区切り、progress と cancel の有無を変えても、結果がビット単位で同じ。
  警告      時間ステップのあいだ numpy のあふれと無効の演算の警告を止めても、呼び出し側の設定（np.seterr、
            np.seterrcall）が、どの終わり方でも元に戻る。0 で割る警告は止めない。警告を例外にする設定
            （np.seterr(over="raise")）でも、発散のエラーが返る。計算の値は設定によらない。

時間に寄らない形にしてある: 知らせを間引く間隔（PROGRESS_SECONDS）を 0 にして全部の知らせを受け取り、区切りの
ステップ数と配列を埋める単位を固定する（どちらも結果に効かない定数）。時間の上限を見るのは、最後の 1 件だけ。
"""
from __future__ import annotations

import importlib.util
import time
import warnings
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import Command, DivergenceError, Pwm, SimulationCancelled, expand, simulate

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

sim = hp.sim_module
kernel = sim.kernel
SETUP_SHARE, POST_SHARE = 0.02, 0.01          # 設計書（basic-design.md 6.7）の割り当て。製品の定数は読まない
PERIOD_STEPS = 20000                          # 制御周期のステップ数
N_PERIODS = 5
CHUNK = 5000                                  # 区切りのステップ数（固定する）
BLOCK = 64                                    # 配列を埋める単位（どの制御周期でも、単位を 313 回またぐ）


def run_fraction(k, n):
    """格子点 k まで計算が済んだときの進み具合（設計書の式）。"""
    return SETUP_SHARE + (1.0 - SETUP_SHARE - POST_SHARE) * k / n


def commanded_rc():
    return hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=10.0)),
                  hp.comp("R1", "R", "in", "out", 1e3), hp.comp("C1", "C", "out", "0", 1e-6))


def engine_rc():
    """指令で値が決まる電源 → R → C。制御周期 2 万ステップ × 5。"""
    h = 1e-6
    ctl = hp.Lookup([0.1, 0.5, 0.3, 0.9, 0.2], period=PERIOD_STEPS * h)
    t_end = N_PERIODS * PERIOD_STEPS * h
    return lambda **k: simulate(commanded_rc(), t_end, h, controllers=[ctl], **k)


def engine_buck():
    """スイッチと還流ダイオードの降圧。デューティを制御周期ごとに変える（ゲート信号の配列を埋め直す）。"""
    h = 1e-7
    ctl = hp.Lookup([0.3, 0.5, 0.7, 0.4, 0.6], output="d", period=PERIOD_STEPS * h)
    gates = [Pwm(name="g", frequency=1e5, duty="d", carrier="triangle")]
    t_end = N_PERIODS * PERIOD_STEPS * h
    return lambda **k: simulate(hp.buck_diode(), t_end, h, gates=gates, controllers=[ctl], **k)


def template_buck(fidelity="switching"):
    """テンプレートの降圧。f_ctrl 1 kHz（スイッチングモデルで、制御周期 2 万ステップ）× 5 周期。"""
    ex = expand(hp.buck_spec(fidelity, control={"f_ctrl": hp.q(1, "kHz")}, sim={"t_end": 5e-3}))
    return lambda **k: ex.run(**k)


CASES = {"engine_rc": engine_rc, "engine_buck": engine_buck, "template_switching": template_buck}


class Trace:
    """核から戻るたびの「済んだ格子点」と、進み具合の知らせを、起きた順に記録する。"""

    def __init__(self):
        self.events = []                      # ("k", 済んだ格子点) と ("p", 進み具合)

    def __enter__(self):
        self._original = kernel.run_switched
        trace = self

        def run_switched(*args):
            status = trace._original(*args)
            ctl = args[21]
            trace.events.append(("k", int(ctl[kernel.CTL_K])))
            return status

        kernel.run_switched = run_switched
        return self

    def __exit__(self, *exc):
        kernel.run_switched = self._original

    def progress(self, fraction):
        self.events.append(("p", float(fraction)))


def traced(run, monkeypatch, chunk=CHUNK, block=BLOCK):
    """全部の知らせを受け取って（間引かない）、起きた順の記録と結果を返す。"""
    monkeypatch.setattr(sim, "PROGRESS_SECONDS", 0.0)
    with hp.chunk_steps(steps=chunk, setup_block=block), Trace() as trace:
        res = run(progress=trace.progress)
    return trace.events, res


# ------------------------------------------------------------------ 進み具合

@pytest.mark.parametrize("case", list(CASES))
def test_progress_while_arrays_are_filled_stays_between_work_done_and_the_next_check(case, monkeypatch):
    events, res = traced(CASES[case](), monkeypatch)
    n = len(res.t) - 1
    assert n == N_PERIODS * PERIOD_STEPS
    values = [v for kind, v in events if kind == "p"]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b > a for a, b in zip(values, values[1:], strict=False))     # 厳密に増える
    assert all(0.0 <= v <= 1.0 for v in values)

    # 起きた順にたどる。done は、核が最後に戻ったときに済んでいた格子点（最初の呼び出しの前は None）
    done, during_setup, during_run, during_post, filling = None, [], {}, [], {}
    k_returns = [v for kind, v in events if kind == "k"]
    assert k_returns == sorted(k_returns) and k_returns[-1] == n
    for kind, v in events[1:-1]:              # 最初の 0 と最後の 1 を除く
        if kind == "k":
            done = v
        elif done is None:
            during_setup.append(v)
        elif done == n:
            during_post.append(v)
        else:
            during_run.setdefault(done, []).append(v)
    # 準備のあいだは準備の割り当て、後処理のあいだは後処理の割り当て
    assert during_setup and all(0.0 < v < SETUP_SHARE for v in during_setup)
    assert during_post[0] == pytest.approx(run_fraction(n, n), abs=1e-12)      # 0.99（時間ステップの終わり）
    assert all(1.0 - POST_SHARE < v < 1.0 for v in during_post[1:]) and len(during_post) > 1
    # 時間ステップのあいだ: 済んだ計算の値以上、次の確かめの点（CHUNK ステップごと）の値未満（先回りしない。
    # 0.99 にも届かない）。核は、確かめの点と制御周期の終わりのほかに、新しいスイッチング状態に出会った
    # ときにも戻る（そのときは知らせない）ので、上限は「次に核が戻る点」ではなく「次の確かめの点」で見る
    for k, seen in during_run.items():
        lower, upper = run_fraction(k, n), run_fraction((k // CHUNK + 1) * CHUNK, n)
        assert all(lower - 1e-12 <= v < upper for v in seen), (k, seen[:3], seen[-3:])
        assert all(v < 1.0 - POST_SHARE for v in seen)
        if k % PERIOD_STEPS == 0:
            filling[k] = [v for v in seen if v > lower + 1e-12]
    # 確かめの点（CHUNK ステップごと）の値が、捨てられずに届く（直す前は、後処理の値を先に知らせて
    # しまい、以後の時間ステップの値が全部捨てられた）
    for k in range(CHUNK, n + 1, CHUNK):
        assert any(v == pytest.approx(run_fraction(k, n), abs=1e-12) for v in values), k
    # 制御周期の先頭（次の周期の配列を埋める所）では、済んだ計算の値と次の確かめの点の値のあいだを、
    # 少しずつ進める（配列を埋めるのに時間がかかる回路で、知らせが止まらない）
    assert sorted(filling) == [PERIOD_STEPS * j for j in range(1, N_PERIODS)]
    for k, seen in filling.items():
        assert len(seen) >= 10, (k, len(seen))
        assert seen[-1] < run_fraction(k + CHUNK, n)              # 次の確かめの点（k + CHUNK）の手前


def test_progress_values_do_not_depend_on_the_fill_when_the_period_fits_in_one_block(monkeypatch):
    """制御周期が、配列を埋める単位より短ければ、時間ステップのあいだの知らせは確かめの点の値だけ。"""
    events, res = traced(engine_rc(), monkeypatch, block=1 << 17)
    n = len(res.t) - 1
    values = [v for kind, v in events if kind == "p"]
    in_run = [v for v in values if SETUP_SHARE - 1e-12 <= v <= 1.0 - POST_SHARE + 1e-12]
    assert in_run == pytest.approx([run_fraction(k, n) for k in range(CHUNK, n + 1, CHUNK)], abs=1e-12)


@pytest.mark.parametrize("case", list(CASES))
def test_results_do_not_depend_on_fill_blocks_chunks_or_callbacks(case):
    """配列を埋める単位と区切りを変え、progress と cancel を渡しても、結果はビット単位で同じ。"""
    run = CASES[case]()
    with hp.chunk_steps(steps=hp.UNCHUNKED):
        reference = hp.signature(run())
    for chunk, block in ((CHUNK, BLOCK), (997, 300), (64, 1000), (hp.UNCHUNKED, BLOCK), (None, None)):
        with hp.chunk_steps(steps=chunk, setup_block=block):
            got = run(progress=lambda fraction: None, cancel=lambda: False)
        assert hp.signature(got) == reference, (chunk, block)


# ------------------------------------------------------------------ 中断

SMALL_PERIOD, SMALL_PERIODS = 500, 4          # 中断の場所を全部試すための、小さな計算（2000 ステップ）


def small_rc():
    h = 1e-6
    ctl = hp.Lookup([0.1, 0.5, 0.3, 0.9], period=SMALL_PERIOD * h)
    return lambda **k: simulate(commanded_rc(), SMALL_PERIODS * SMALL_PERIOD * h, h, controllers=[ctl], **k)


def small_buck():
    h = 1e-7
    ctl = hp.Lookup([0.3, 0.5, 0.7, 0.4], output="d", period=SMALL_PERIOD * h)
    gates = [Pwm(name="g", frequency=1e5, duty="d", carrier="triangle")]
    return lambda **k: simulate(hp.buck_diode(), SMALL_PERIODS * SMALL_PERIOD * h, h, gates=gates,
                                controllers=[ctl], **k)


@pytest.mark.parametrize("make", [small_rc, small_buck])
def test_cancel_is_polled_while_the_arrays_of_the_next_control_period_are_filled(make):
    """区切らない設定にすると、時間ステップのあいだに中断の求めを見るのは、配列を埋めている所だけになる。

    求めを見る場所を、全部 1 か所ずつ中断する（配列を埋める単位は 50 行。制御周期 500 ステップ × 4）。
    どこでも結果は返らない。エラーの k は、準備のあいだ 0、配列を埋めている最中は制御周期の先頭
    （そこまで計算が済んでいる）、後処理のあいだ n。流し直すとビット一致。
    """
    run = make()
    n = SMALL_PERIODS * SMALL_PERIOD
    with hp.chunk_steps(steps=hp.UNCHUNKED, setup_block=50):
        reference = hp.signature(run())
        calls = {"n": 0}

        def never():
            calls["n"] += 1
            return False

        assert hp.signature(run(cancel=never)) == reference
        total = calls["n"]
        assert total > 100
        ks = []
        for index in range(total):
            count = {"n": 0}

            def at_index(index=index, count=count):
                count["n"] += 1
                return count["n"] == index + 1

            with pytest.raises(SimulationCancelled) as info:
                run(cancel=at_index)
            exc = info.value
            assert exc.code == "RUN_CANCELLED" and not isinstance(exc, ValueError)
            held = list(vars(exc).values()) + list(exc.params.values())
            assert not any(isinstance(v, np.ndarray) for v in held)        # エラーに配列が無い
            assert exc.params["n"] == n
            ks.append(exc.params["k"])
        assert hp.signature(run()) == reference                    # 中断の後も、同じ結果
    assert ks == sorted(ks) and ks[0] == 0 and ks[-1] == n         # 準備は 0、後処理は n
    # 時間ステップの最中の中断は、どれも制御周期の先頭（配列を埋めている最中）。2つ目からの周期の全部に
    # あり、どの周期でも、配列を埋める単位の数（500 / 50 = 10）だけ求めを見る
    middle = [k for k in ks if 0 < k < n]
    starts = [SMALL_PERIOD * j for j in range(1, SMALL_PERIODS)]
    assert sorted(set(middle)) == starts
    assert [middle.count(k) for k in starts] == [SMALL_PERIOD // 50] * len(starts)


# ------------------------------------------------------------------ あふれの警告を止める範囲

class Measured:
    """測定値から指令を作る制御器（fn(測定値) を指令にする）。step() の中の numpy の設定を控える。"""

    def __init__(self, fn, measure="v(out)", period=1e-3):
        self.fn, self.inputs, self.outputs, self.period = fn, (measure,), ("u",), period
        self.settings = []

    def reset(self):
        return [0.1]

    def step(self, t, measured):
        self.settings.append(dict(np.geterr()))
        return [self.fn(float(measured[0]))]


class ControllerFailure(Exception):
    pass


def failing(value):
    raise ControllerFailure("制御器の中の例外")


def cancel_after(n_calls):
    count = {"n": 0}

    def cancel():
        count["n"] += 1
        return count["n"] > n_calls

    return cancel


def failing_progress(fraction):
    if fraction > 0.3:
        raise ControllerFailure("progress の中の例外")


def with_chunks(steps, run):
    """区切りのステップ数を固定して計算する（中断の求めを見る場所の数を、機械の速さによらない数にする）。"""
    with hp.chunk_steps(steps=steps):
        return run()


def lc_with_a_huge_source():
    return hp.net(hp.vsrc("V1", "in", "0", hp.DC(value=1.7e308)), hp.comp("L1", "L", "in", "out", 1e-3),
                  hp.comp("C1", "C", "out", "0", 1e-5))


def run_commanded(fn, t_end=0.02, h=1e-5, **kw):
    """指令で値が決まる電源 → R → C を、fn(測定値) を指令にする制御器（制御周期 1 ms）で動かす。"""
    return simulate(commanded_rc(), t_end, h, controllers=[Measured(fn)], **kw)


def run_overflowing():
    """電源の目安が 1e300 V の正帰還（発散とみなす大きさを決められず、値があふれるまで進む）。"""
    netlist = hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=1e300)),
                     hp.comp("R1", "R", "in", "out", 1e3), hp.comp("C1", "C", "out", "0", 1e-6))
    return simulate(netlist, 0.5, 1e-5, controllers=[Measured(lambda v: 3e-300 * v)])


# 終わり方ごとの計算: （計算、期待する終わり方）。None は、結果が返る。
# 中断の2つは、区切りを 500 ステップに固定する。準備のあいだに求めを見るのは数回なので、13 回目は
# 時間ステップの最中になる
OUTCOMES = {
    "finishes_switched": (lambda: run_commanded(lambda v: 0.5), None),
    "finishes_linear": (lambda: simulate(hp.rc(), 5e-3, 1e-5), None),
    "diverges_past_the_limit": (lambda: run_commanded(lambda v: 0.3 * v, t_end=0.2), "RUN_DIVERGED"),
    "overflows_switched": (run_overflowing, "RUN_DIVERGED_NOT_FINITE"),
    "overflows_linear": (lambda: simulate(lc_with_a_huge_source(), 5e-3, 1e-5), "RUN_DIVERGED_NOT_FINITE"),
    "command_is_nan": (lambda: run_commanded(lambda v: float("nan")), "CONTROLLER_COMMAND_NOT_FINITE"),
    "controller_raises": (lambda: run_commanded(failing), ControllerFailure),
    "cancelled_switched": (
        lambda: with_chunks(500, lambda: run_commanded(lambda v: 0.5, t_end=0.2, cancel=cancel_after(12))),
        "RUN_CANCELLED"),
    "cancelled_linear": (
        lambda: with_chunks(500, lambda: simulate(hp.rc(), 0.05, 1e-6, cancel=cancel_after(12))),
        "RUN_CANCELLED"),
    "progress_raises": (
        lambda: run_commanded(lambda v: 0.5, t_end=0.2, h=1e-6, progress=failing_progress),
        ControllerFailure),
}
SETTINGS = {
    "numpy_default": dict(divide="warn", over="warn", under="ignore", invalid="warn"),
    "all_raise": dict(divide="raise", over="raise", under="raise", invalid="raise"),
    "all_ignore": dict(divide="ignore", over="ignore", under="ignore", invalid="ignore"),
    "mixed_with_call": dict(divide="call", over="print", under="warn", invalid="call"),
}


def _error_handler(kind, flag):
    pass


@pytest.fixture
def numpy_settings():
    """試験の後に、numpy の設定を必ず元に戻す。"""
    saved, saved_call = np.geterr(), np.geterrcall()
    yield
    np.seterr(**saved)
    np.seterrcall(saved_call)


@pytest.mark.parametrize("setting", list(SETTINGS))
@pytest.mark.parametrize("outcome", list(OUTCOMES))
def test_the_callers_numpy_error_settings_come_back_however_the_run_ends(outcome, setting, numpy_settings):
    run, expected = OUTCOMES[outcome]
    np.seterrcall(_error_handler)
    np.seterr(**SETTINGS[setting])
    before, before_call = dict(np.geterr()), np.geterrcall()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            run()
            ended = None
        except ControllerFailure:
            ended = ControllerFailure
        except (DivergenceError, SimulationCancelled) as exc:
            ended = exc.code
    assert ended == expected                                       # 設定によらず、同じ終わり方（番号つき）
    assert dict(np.geterr()) == before and np.geterrcall() is before_call
    assert [str(w.message) for w in caught] == []                  # numpy の警告が出ない


def test_divide_by_zero_inside_a_controller_still_warns(numpy_settings):
    """止めるのは、あふれ（over）と無効の演算（invalid）だけ。0 で割る警告は、制御器の中でも出る。"""
    np.seterr(divide="warn", over="warn", under="ignore", invalid="warn")
    one, zero = np.float64(1.0), np.float64(0.0)
    ctl = Measured(lambda v: (one / zero, 0.5)[1])                 # 1/0 を計算して、指令には使わない
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        simulate(commanded_rc(), 0.01, 1e-5, controllers=[ctl])
    assert len(caught) == len(ctl.settings) == 10
    assert all("divide by zero" in str(w.message) for w in caught)
    assert all(s["divide"] == "warn" and s["under"] == "ignore" for s in ctl.settings)
    # 0 で割るのを例外にする設定なら、制御器の中で例外になる（設定が step() の中まで届いている）
    np.seterr(divide="raise")
    with pytest.raises(FloatingPointError):
        simulate(commanded_rc(), 0.01, 1e-5, controllers=[Measured(lambda v: (one / zero, 0.5)[1])])
    assert np.geterr()["divide"] == "raise"


def test_a_nan_made_inside_a_controller_is_reported_with_a_code(numpy_settings):
    """制御器の中の 0/0（無効の演算。警告は止まる）が指令に出たら、番号つきのエラーで知らせる。"""
    np.seterr(divide="warn", over="warn", under="ignore", invalid="warn")
    zero = np.float64(0.0)
    with pytest.raises(DivergenceError) as info:
        simulate(commanded_rc(), 0.01, 1e-5, controllers=[Measured(lambda v: zero / zero)])
    assert info.value.code == "CONTROLLER_COMMAND_NOT_FINITE"
    assert info.value.params["signal"] == "cmd(u)" and info.value.params["t"].value == pytest.approx(0.0)


@pytest.mark.parametrize("case", ["engine_rc", "engine_buck"])
def test_values_do_not_depend_on_the_callers_numpy_error_settings(case, numpy_settings):
    run = CASES[case]()
    np.seterr(divide="warn", over="warn", under="ignore", invalid="warn")
    reference = hp.signature(run())
    np.seterr(divide="raise", over="raise", under="ignore", invalid="raise")
    assert hp.signature(run()) == reference
    np.seterr(divide="ignore", over="ignore", under="ignore", invalid="ignore")
    assert hp.signature(run()) == reference


def test_overflow_in_a_circuit_without_switches_is_reported_without_warnings():
    """スイッチも制御器も無い回路（核の別の関数）でも、あふれたら警告なしで発散のエラー。

    電源 1.7e308 V（倍精度の上限の近く）の、損失の無い LC。コンデンサ電圧は電源の 2 倍まで振れるので、
    最初の半周期（π√(LC) = 0.31 ms）のうちにあふれる。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(DivergenceError) as info:
            simulate(lc_with_a_huge_source(), 5e-3, 1e-5)
    assert info.value.code == "RUN_DIVERGED_NOT_FINITE"
    assert 0.0 < info.value.params["t"].value < 3.2e-4


# ------------------------------------------------------------------ 実際の条件（時間の上限つき）

def test_long_control_period_run_reports_only_work_that_is_done_and_never_stalls():
    """同梱の例と同じ値で f_ctrl を 100 Hz に（制御周期 20 万ステップ）。スイッチングモデル、30 ms。

    区切りと配列を埋める単位は既定のまま（時間で決まる）。済んだ計算は核から読む。知らせの間隔の上限
    （0.2 秒）だけは、3 回まで測り直す（ほかのプロセスに時間を取られたときのため）。値の決まりは毎回見る。
    """
    ex = expand(hp.buck_spec("switching", control={"f_ctrl": hp.q(100, "Hz")}, sim={"t_end": 30e-3}))
    worst = []
    for _ in range(3):
        stamps = []
        with Trace() as trace:
            start = time.perf_counter()

            def progress(fraction, trace=trace, stamps=stamps):
                stamps.append(time.perf_counter())
                trace.progress(fraction)

            res = ex.run(progress=progress)
            end = time.perf_counter()
        n = len(res.t) - 1
        assert n == 600000
        done, values = 0, []
        for kind, v in trace.events:
            if kind == "k":
                done = v
                continue
            values.append(v)
            if v < 1.0:
                # 時間ステップが終わる前に、後処理の値を出さない
                assert v < 1.0 - POST_SHARE or done == n, (v, done)
                # 済んだ計算を、制御周期の半分より先回りしない（配列を埋めるあいだに進めるのは、次の確かめの
                # 点と制御周期の終わりの早いほうまでの半分。区切りは時間で決まるので、機械によらない上限は
                # 制御周期の半分 = 10 万ステップ）。直す前は、ここで 0.998 に跳んだ
                assert v <= run_fraction(done, n) + 0.97 * 0.5 * 200000 / n + 1e-9, (v, done)
        assert values[0] == 0.0 and values[-1] == 1.0
        assert all(b > a for a, b in zip(values, values[1:], strict=False))
        assert len(values) >= 10
        worst.append(float(np.diff([start] + stamps + [end]).max()))
        if worst[-1] <= 0.2:
            break
    assert min(worst) <= 0.2, worst
