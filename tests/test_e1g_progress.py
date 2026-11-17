"""E1g の範囲2（受け入れ基準3・4・5）: 計算を区切って進め、進み具合を知らせ、中断に応じる。

- 進み具合: 0 から単調に増え、最後に 1。知らせの間隔（呼び出しの前後を含む）が 0.2 秒を超えない
  （20 万ステップ以上の計算で測る）。時間ステップが済む前に、後処理の値（0.99〜）に先回りしない
  （制御周期が、準備の配列を埋める単位より長い回路でも）。
- 中断: 中断を求めてから 0.2 秒以内に SimulationCancelled で終わり、結果を返さない。準備、時間ステップの
  最中に次の制御周期の配列を埋めるあいだ、後処理でも、中断の求めを見る。
- 時間の上限（0.2 秒）を持つ試験は、上限を超えたときだけ 1 回測り直す（ほかの仕事で機械が止まった分を、
  製品の遅れと取り違えないため。上限そのものは基準のまま。値の決まりは毎回確かめる）。
- 区切りの費用: 区切った計算と区切らない計算（1回の核の呼び出し）の結果がビット単位で同じ。
  所要時間の差は benchmarks/bench_e1g_chunks.py で測る（合否の試験にすると機械で揺れるので、
  ここでは測らない）。
"""
from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import (
    DC,
    Command,
    Component,
    Netlist,
    Pwm,
    SimulationCancelled,
    Sine,
    Spec,
    Step,
    expand,
    simulate,
)

simulate_module = importlib.import_module("circuit_sim.simulate")   # 名前 simulate は関数を指すので
kernel = simulate_module.kernel
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"
LIMIT_S = 0.2
ATTEMPTS = 2          # 時間の上限を測る回数（上限を超えたときだけ、もう1回）


def buck(fidelity="switching", t_end=10e-3, control=None, **plant):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    for k, v in plant.items():
        d["plant"][k] = {"value": v[0], "unit": v[1]}
    for k, v in (control or {}).items():
        d["control"][k] = {"value": v[0], "unit": v[1]}
    return expand(Spec.model_validate(d))


def within_limit(measure_once) -> None:
    """時間の上限（LIMIT_S）を持つ測定。上限を超えたときだけ、1 回測り直す。

    measure_once() は、値の決まり（単調、0 と 1、結果を返さない、など）をその場で確かめてから、測った時間
    [s] を返す（値の決まりは、測り直しでも毎回確かめる）。"""
    times = []
    for _ in range(ATTEMPTS):
        times.append(measure_once())
        if times[-1] <= LIMIT_S:
            break
    assert min(times) <= LIMIT_S, times


def ladder(n_sections=20) -> Netlist:
    comps = [Component(id="V1", type="V", nodes=("n0", "0"), waveform=Step(final=1.0, t_step=1e-4))]
    for j in range(n_sections):
        comps += [Component(id=f"R{j}", type="R", nodes=(f"n{j}", f"n{j + 1}"), value=10.0),
                  Component(id=f"C{j}", type="C", nodes=(f"n{j + 1}", "0"), value=1e-6)]
    return Netlist(components=comps)


def rectifier() -> Netlist:
    """スイッチとダイオード（制御器なし）。1つの核の呼び出しが計算の全体になる回路。"""
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=24.0)),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q1"),
        Component(id="D1", type="D", nodes=("0", "sw")),
        Component(id="L1", type="L", nodes=("sw", "out"), value=47e-6),
        Component(id="C1", type="C", nodes=("out", "0"), value=100e-6),
        Component(id="R1", type="R", nodes=("out", "0"), value=100.0),
    ])


def watched(run):
    """進み具合の値と時刻を記録しながら計算する。戻り値: （結果、[(時刻, 値)]、呼び出しの前後の時刻）。"""
    seen = []
    start = time.perf_counter()
    res = run(lambda f: seen.append((time.perf_counter(), f)))
    return res, seen, (start, time.perf_counter())


def check_progress(seen, span) -> float:
    """値の決まり（0 から単調に増えて、最後に 1）を確かめ、知らせの間隔の最大 [s] を返す。"""
    values = [f for _, f in seen]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b > a for a, b in zip(values, values[1:], strict=False)), values      # 単調に増える
    times = [span[0]] + [t for t, _ in seen] + [span[1]]
    return float(np.diff(times).max())


@pytest.mark.parametrize("case", ["buck_switching", "buck_slow_control", "rectifier", "ladder"])
def test_progress_rises_to_one_and_never_stalls_longer_than_0_2_s(case):
    """20 万ステップ以上: 降圧（制御器つき、20 万ステップ）、制御周期の長い降圧（f_ctrl 100 Hz。制御周期が
    20 万ステップで、準備の配列を埋める単位 131072 行より長い。60 万ステップ）、整流（制御器なし、
    40 万ステップ）、はしご（スイッチなし、未知数 41、20 万ステップ）。"""
    if case == "buck_switching":
        ex = buck()
        run = lambda p: ex.run(progress=p)                                         # noqa: E731
    elif case == "buck_slow_control":
        ex = buck(t_end=30e-3, control={"f_ctrl": (100, "Hz")})
        run = lambda p: ex.run(progress=p)                                         # noqa: E731
    elif case == "rectifier":
        gate = Pwm(name="q1", frequency=100e3, duty=0.3)
        run = lambda p: simulate(rectifier(), 20e-3, gates=[gate], progress=p)     # noqa: E731
    else:
        run = lambda p: simulate(ladder(), 0.2, 1e-6, progress=p)                  # noqa: E731

    def measure_once():
        res, seen, span = watched(run)
        assert len(res.t) - 1 >= 200_000 and len(seen) >= 3
        return check_progress(seen, span)

    within_limit(measure_once)


def run_and_cancel(start_run, after_s=0.3):
    """計算を別のスレッドで始め、after_s 後に中断を求める。

    戻り値: （中断を求めてから終わるまで、例外、結果）。"""
    stop = threading.Event()
    box = {}

    def work():
        try:
            box["result"] = start_run(stop.is_set)
        except BaseException as exc:      # noqa: BLE001
            box["error"] = exc
        box["end"] = time.perf_counter()

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(after_s)
    asked = time.perf_counter()
    stop.set()
    thread.join(10.0)
    assert not thread.is_alive()
    return box["end"] - asked, box.get("error"), box.get("result")


@pytest.mark.parametrize("case", ["buck_switching", "buck_average", "rectifier", "ladder"])
def test_cancel_ends_within_0_2_s_without_a_result(case):
    """降圧（100 万ステップ、制御器つき）、状態平均化モデル（200 万）、整流（200 万。制御器が無いので、
    ゲート信号の配列を計算の全体でまとめて作る準備がある）、はしご（200 万）。"""
    if case == "buck_switching":
        ex = buck(t_end=50e-3)
        run = lambda c: ex.run(cancel=c)                                  # noqa: E731
    elif case == "buck_average":
        ex = buck("average", t_end=2.0)
        run = lambda c: ex.run(cancel=c)                                  # noqa: E731
    elif case == "rectifier":
        gate = Pwm(name="q1", frequency=100e3, duty=0.3)
        run = lambda c: simulate(rectifier(), 0.1, gates=[gate], cancel=c)  # noqa: E731
    else:
        run = lambda c: simulate(ladder(), 2.0, 1e-6, cancel=c)           # noqa: E731
    def measure_once():
        took, error, result = run_and_cancel(run)
        assert isinstance(error, SimulationCancelled) and result is None
        # 準備のあいだに中断を求めることもあり（k = 0）、時間ステップが済んだ後の後処理のあいだのこともある
        # （k = n）。どちらも区切って中断に応じる
        assert error.code == "RUN_CANCELLED" and 0 <= error.params["k"] <= error.params["n"]
        assert not isinstance(error, ValueError)          # 入力の誤りとは分ける
        return took

    within_limit(measure_once)


# ------------------------------------------------------------------ 中断の求めと進み具合を、起きた順に見る
# 時間によらない確かめ: 区切りのステップ数と、配列を埋める単位を固定し、進み具合は間引かずに知らせる。
# 核の呼び出し、中断の求め（cancel）、進み具合（progress）を、起きた順に1つの記録に並べる。


class Sequence:
    """測定値によらず、決めた値を制御周期ごとに1つずつ出す制御器。"""

    inputs = ()
    outputs = ("u",)

    def __init__(self, values, period):
        self.values, self.period = list(values), float(period)

    def reset(self):
        self.j = 0
        return [self.values[0]]

    def step(self, t, measured):
        self.j += 1
        return [self.values[min(self.j, len(self.values) - 1)]]


def commanded_rc() -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=Command(command="u", gain=10.0)),
        Component(id="R1", type="R", nodes=("in", "out"), value=1e3),
        Component(id="C1", type="C", nodes=("out", "0"), value=1e-6),
    ])


CHUNK, BLOCK = 300, 64                      # 固定する区切りのステップ数と、配列を埋める単位の行数
PERIOD_STEPS, N_PERIODS = 1000, 12          # 制御周期（BLOCK より長い: 配列を 16 回に分けて埋める）と、その数
TRACED = {
    # 名前: （計算する関数、ステップ数）
    "linear": (lambda **kw: simulate(ladder(3), 2e-3, 1e-6, **kw), 2000),
    "switched_no_controller": (
        lambda **kw: simulate(rectifier(), 2e-4, gates=[Pwm(name="q1", frequency=100e3, duty=0.3)],
                              steps_per_period=100, **kw), 2000),
    "long_control_period": (
        lambda **kw: simulate(commanded_rc(), N_PERIODS * PERIOD_STEPS * 1e-6, 1e-6,
                              controllers=[Sequence([0.1 * (j % 5) for j in range(N_PERIODS)],
                                                    PERIOD_STEPS * 1e-6)], **kw),
        N_PERIODS * PERIOD_STEPS),
}


@pytest.fixture
def traced(monkeypatch):
    """区切りを固定して計算し、起きたことを順に記録する。

    trace(name, stop_at=None) → （記録、例外か None）。記録の要素は
      ("kernel", 計算の済んだ格子点)   核を1回呼んで戻った（スイッチの無い回路は、区切りの終わりの点）
      ("poll", 何回目か)               中断の求めを見た
      ("progress", 値)                 進み具合を知らせた
    stop_at を渡すと、その回の求めで中断する（真を返す）。"""
    monkeypatch.setattr(simulate_module, "CHUNK_STEPS_FIRST", CHUNK)
    monkeypatch.setattr(simulate_module, "CHUNK_STEPS_MIN", CHUNK)
    monkeypatch.setattr(simulate_module, "CHUNK_STEPS_MAX", CHUNK)
    monkeypatch.setattr(simulate_module, "SETUP_BLOCK", BLOCK)
    monkeypatch.setattr(simulate_module, "PROGRESS_SECONDS", 0.0)
    log = []
    done = {"linear": 0}
    run_linear, run_switched = kernel.run, kernel.run_switched

    def linear(*args, **kwargs):
        out = run_linear(*args, **kwargs)
        done["linear"] += args[5].shape[0]                    # u_start の行数 = この区切りのステップ数
        log.append(("kernel", done["linear"]))
        return out

    def switched(*args, **kwargs):
        status = run_switched(*args, **kwargs)
        log.append(("kernel", int(args[21][kernel.CTL_K])))   # ctl[CTL_K]: 次に計算するステップ
        return status

    monkeypatch.setattr(kernel, "run", linear)
    monkeypatch.setattr(kernel, "run_switched", switched)

    def trace(name, stop_at=None):
        log.clear()
        done["linear"] = 0
        polls = 0

        def cancel():
            nonlocal polls
            polls += 1
            log.append(("poll", polls))
            return stop_at is not None and polls >= stop_at

        try:
            TRACED[name][0](progress=lambda f: log.append(("progress", f)), cancel=cancel)
        except SimulationCancelled as exc:
            return list(log), exc
        return list(log), None

    return trace


def split_phases(log):
    """記録を、最初の核の呼び出しの前（準備）、最後の核の呼び出しまで（時間ステップ）、その後（後処理）に分ける。"""
    kernels = [j for j, (kind, _) in enumerate(log) if kind == "kernel"]
    return log[: kernels[0]], log[kernels[0]: kernels[-1] + 1], log[kernels[-1] + 1:]


@pytest.mark.parametrize("name", list(TRACED))
def test_cancel_is_polled_during_setup_and_post_processing_and_stops_there(name, traced):
    """準備（電源やゲート信号の配列を埋める）と後処理（注意の判定、信号の配列）のあいだも、中断の求めを見る。
    そこで求めると、準備なら時間ステップを始めずに、後処理なら結果を返さずに終わる。

    見ていなければ、準備で見るのは計算の初めの 1 回、後処理で見るのは終わりの 1〜2 回だけになる。"""
    n_steps = TRACED[name][1]
    log, error = traced(name)
    assert error is None and log[-1] == ("progress", 1.0)
    setup, run, post = split_phases(log)
    assert run[-1] == ("kernel", n_steps)
    setup_polls = [n for kind, n in setup if kind == "poll"]
    post_polls = [n for kind, n in post if kind == "poll"]
    blocks = n_steps // BLOCK                   # 1つの配列を埋めるあいだに見る回数
    assert len(setup_polls) >= blocks and len(post_polls) >= blocks, (len(setup_polls), len(post_polls))
    total = max(n for kind, n in log if kind == "poll")
    # 準備の初め・中ほど・終わりで求める: 核を1回も呼ばずに終わる。知らせる格子点は 0
    for stop_at in (setup_polls[0], setup_polls[len(setup_polls) // 2], setup_polls[-1]):
        cut, error = traced(name, stop_at)
        assert isinstance(error, SimulationCancelled) and error.code == "RUN_CANCELLED"
        assert error.params["k"] == 0 and error.params["n"] == n_steps
        assert not any(kind == "kernel" for kind, _ in cut) and cut[-1] == ("poll", stop_at)
    # 後処理の初め・中ほど・終わり（計算の最後の確かめ）で求める: 時間ステップは済んでいるが、結果は返さない
    for stop_at in (post_polls[0], post_polls[len(post_polls) // 2], post_polls[-1]):
        cut, error = traced(name, stop_at)
        assert isinstance(error, SimulationCancelled) and error.params["k"] == error.params["n"] == n_steps
        assert cut[-1] == ("poll", stop_at) and ("progress", 1.0) not in cut
    assert post_polls[-1] == total
    # 求めなかったときの進み具合: 準備は 0〜0.02 の中、後処理は 0.99 より上で、どちらも少しずつ進む
    setup_values = [v for kind, v in setup if kind == "progress"]
    post_values = [v for kind, v in post if kind == "progress"]
    assert setup_values[0] == 0.0 and len(setup_values) >= blocks and max(setup_values) < 0.02
    assert len(post_values) >= blocks and min(post_values) >= 0.99 and post_values[-1] == 1.0


def run_fraction(k, n_steps):
    """格子点 k まで計算が済んだときの進み具合（準備 0.02、後処理 0.01 を除いた割り当ての中）。"""
    return 0.02 + 0.97 * k / n_steps


def test_progress_stays_with_the_time_steps_while_the_next_control_period_is_filled(traced):
    """制御周期が、配列を埋める単位より長い回路: 時間ステップの最中に、次の制御周期のゲート信号と指令の
    配列を何回かに分けて埋める。そのあいだの進み具合は、計算の済んだステップ数の値から先へ、次の区切りの
    値の手前までしか進まない（後処理の値 0.99〜 に跳ばない）。止まりもしない（埋めるあいだも知らせる）。

    直す前は、埋めるあいだの最初の知らせが 0.99 以上になり、以後の時間ステップの知らせが捨てられた。"""
    n_steps = TRACED["long_control_period"][1]
    log, error = traced("long_control_period")
    assert error is None
    values = [v for kind, v in log if kind == "progress"]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b > a for a, b in zip(values, values[1:], strict=False))
    _, run, post = split_phases(log)
    k_done, while_filling = 0, {}
    for kind, value in run:
        if kind == "kernel":
            k_done = value
        elif kind == "progress":
            # 計算の済んだ格子点の値以上で、次の区切り（CHUNK ステップ先）の値より小さい
            here, ahead = run_fraction(k_done, n_steps), run_fraction(k_done + CHUNK, n_steps)
            assert here <= value < ahead and value < 0.99, (k_done, value)
            if value > here:
                while_filling.setdefault(k_done, []).append(value)
    # 配列を埋めるのは、制御周期の先頭（最初の周期の分は準備のうち）。そのたびに、何回か知らせている
    assert sorted(while_filling) == [j * PERIOD_STEPS for j in range(1, N_PERIODS)]
    assert all(len(v) >= PERIOD_STEPS // BLOCK - 1 for v in while_filling.values())
    # 時間ステップの側の知らせ（区切りの点の値そのもの）も、捨てられずに全部出る（最後の区切りの点の知らせは、
    # 最後の核の呼び出しの後）。後処理の知らせは、その値より上
    at_checks = {run_fraction(k, n_steps) for k in range(CHUNK, n_steps + 1, CHUNK)}
    assert len([v for kind, v in run + post if kind == "progress" and v in at_checks]) == n_steps // CHUNK
    post_values = [v for kind, v in post if kind == "progress"]
    assert post_values[0] == run_fraction(n_steps, n_steps) and len(post_values) > 3


def test_cancel_works_while_the_next_control_period_is_filled(traced):
    """時間ステップの最中に配列を埋めるあいだも、中断の求めを見る。そこで求めると、次の時間ステップに
    進まずに終わり、知らせる格子点は、計算の済んだ点（その制御周期の先頭）。"""
    n_steps = TRACED["long_control_period"][1]
    log, _ = traced("long_control_period")
    setup, run, _ = split_phases(log)
    # 核から戻った後に、2 回以上続けて求めを見ている所（1 回目は区切りの確かめのこともある）の、3 回目
    targets, k_done, since = [], 0, 0
    for kind, value in run:
        if kind == "kernel":
            k_done, since = value, 0
        elif kind == "poll":
            since += 1
            if since == 3:
                targets.append((value, k_done))
    assert [k for _, k in targets] == [j * PERIOD_STEPS for j in range(1, N_PERIODS)]
    for stop_at, k_done in (targets[0], targets[len(targets) // 2], targets[-1]):
        cut, error = traced("long_control_period", stop_at)
        assert isinstance(error, SimulationCancelled)
        assert error.params["k"] == k_done and error.params["n"] == n_steps
        assert error.params["t"].value == pytest.approx(k_done * 1e-6)
        assert cut[-1] == ("poll", stop_at)
        assert max(v for kind, v in cut if kind == "kernel") == k_done      # その先は計算していない


def test_cancel_before_start_returns_nothing():
    with pytest.raises(SimulationCancelled) as info:
        simulate(ladder(2), 1e-3, 1e-6, cancel=lambda: True)
    assert info.value.params["k"] == 0


def test_bad_callbacks_are_rejected():
    with pytest.raises(TypeError):
        simulate(ladder(2), 1e-3, 1e-6, progress=0.5)
    with pytest.raises(TypeError):
        simulate(ladder(2), 1e-3, 1e-6, cancel=True)


# ------------------------------------------------------------------ 区切ってもビット単位で同じ


@pytest.fixture
def unchunked(monkeypatch):
    """区切らない（核を1回で呼ぶ）。制御器のある回路は、制御周期ごとの呼び出しだけ（E1g の前と同じ）。"""
    def apply():
        monkeypatch.setattr(simulate_module, "CHUNK_STEPS_FIRST", 10**12)
        monkeypatch.setattr(simulate_module, "CHUNK_STEPS_MAX", 10**12)
    return apply


def same(a, b):
    assert np.array_equal(a.x, b.x) and np.array_equal(a.t, b.t)
    assert a.signals.keys() == b.signals.keys()
    assert all(np.array_equal(a.signals[k], b.signals[k]) for k in a.signals)
    assert a.stats == b.stats and [str(n) for n in a.notes] == [str(n) for n in b.notes]


CASES = {
    "ladder_step": lambda **kw: simulate(ladder(), 0.05, 1e-6, **kw),
    "ladder_coarse_damped": lambda **kw: simulate(ladder(), 0.5, 5e-5, **kw),      # 減衰ステップが入る刻み
    "rectifier": lambda **kw: simulate(rectifier(), 5e-3, gates=[Pwm(name="q1", frequency=100e3, duty=0.3)],
                                       **kw),
    "buck_switching_load_step": lambda **kw: buck(t_end=3e-3, R_load_step=(3, "ohm"),
                                                  t_load_step=(1, "ms")).run(**kw),
    "buck_average": lambda **kw: buck("average", t_end=20e-3).run(**kw),
}


@pytest.mark.parametrize("case", list(CASES))
def test_chunked_and_unchunked_results_are_bit_identical(case, unchunked, monkeypatch):
    chunks = []
    original = simulate_module._Monitor.after

    def count(self, k):
        if k >= self.next_check:
            chunks.append(k)
        return original(self, k)

    monkeypatch.setattr(simulate_module._Monitor, "after", count)
    monkeypatch.setattr(simulate_module, "CHUNK_STEPS_FIRST", 97)         # 小さく、半端な区切りで割る
    monkeypatch.setattr(simulate_module, "CHUNK_STEPS_MAX", 997)
    chunked = CASES[case](progress=lambda f: None, cancel=lambda: False)
    assert len(chunks) > 10
    unchunked()
    chunks.clear()
    whole = CASES[case]()
    same(chunked, whole)


def sine_rc() -> Netlist:
    """正弦波の電源（区切って作る配列に、値が格子点ごとに違う波形が入る）と、途中でステップする電源。"""
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("a", "0"),
                  waveform=Sine(amplitude=1.7, frequency=1e3, offset=0.3, phase_deg=17.0)),
        Component(id="R1", type="R", nodes=("a", "b"), value=1e3),
        Component(id="C1", type="C", nodes=("b", "0"), value=1e-7),
        Component(id="V2", type="V", nodes=("c", "0"), waveform=Step(final=2.0, t_step=3.3e-3)),
        Component(id="R2", type="R", nodes=("c", "b"), value=2e3),
    ])


BLOCK_CASES = {
    **CASES,
    "sine_and_step": lambda **kw: simulate(sine_rc(), 2e-2, 1e-6, **kw),
}


@pytest.mark.parametrize("case", list(BLOCK_CASES))
def test_setup_and_post_processing_in_blocks_give_the_same_result(case, monkeypatch):
    """準備（電源・ゲート信号の配列）と後処理（注意、信号の配列）を区切って行っても、まとめて行ったときと
    結果（波形、信号、集計、注意）がビット単位で同じ。区切りの行数を小さく半端にして、何度も区切らせる。"""
    ticks = []
    original = simulate_module._Monitor.tick

    def count(self):
        ticks.append(self.phase)
        return original(self)

    monkeypatch.setattr(simulate_module._Monitor, "tick", count)
    monkeypatch.setattr(simulate_module, "SETUP_BLOCK", 509)
    blocked = BLOCK_CASES[case]()
    assert ticks.count("setup") > 10 and ticks.count("post") > 10
    monkeypatch.setattr(simulate_module, "SETUP_BLOCK", 10**12)
    whole = BLOCK_CASES[case]()
    same(blocked, whole)
