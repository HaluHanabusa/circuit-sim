"""E1g の独立検証（基準3・4・5）: 進み具合、中断、区切り。

確かめること:
  基準3  進み具合が 0 から始まり、単調に増え、最後に 1。知らせの間隔（計算の開始から最初の知らせ、最後の
         知らせから戻りまでを含む）が 0.2 秒以内（20 万ステップ以上の計算）。
  基準4  中断を求めてから 0.2 秒以内に終わる。途中の結果を返さない。中断の後に、同じ入力（同じ制御器の
         オブジェクト）で流し直すと、中断しなかった結果とビット単位で同じ。
  基準5  区切りの位置を変えても、区切らなくても、結果がビット単位で同じ（費用の実測は記録に書く）。

時間の上限（0.2 秒）を持つ確かめの扱い: 実測は、負荷のかかった機械（2 コアで負荷 2〜4.5）で、知らせの間隔が
最大 94 ms、中断の遅れが最大 39 ms（docs/verification/E1g-verifier.md）。上限に対して 2〜5 倍の余裕がある。
それでも、ほかのプロセスに時間を取られると 1 回の測定は揺れるので、時間の上限だけは 3 回まで測り直し、
1 回でも収まれば合格とする（製品が遅いなら 3 回とも超える）。値の決まり（単調、0 と 1、結果を返さない、
ビット一致）は、毎回確かめる。
"""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from circuit_sim import SimulationCancelled, Sine, Step, expand, simulate

_spec = importlib.util.spec_from_file_location("e1g_helpers", Path(__file__).with_name("e1g_helpers.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

LIMIT_S = 0.2
ATTEMPTS = 3


def template(fidelity, t_end, **kw):
    ex = expand(hp.buck_spec(fidelity, sim={"t_end": t_end}, **kw))
    return lambda **k: ex.run(**k)            # 同じ展開（同じ制御器のオブジェクト）を使い回す


# 20 万ステップ以上の計算（回路の種類を変える: 制御器つきのスイッチング、状態平均化モデル、
# スイッチなし、ダイオード）
LONG = {
    "template_switching_200k": lambda: template("switching", 1e-2),
    "template_average_200k": lambda: template("average", 0.2),
    "linear_rc_250k": lambda: (
        lambda **k: simulate(hp.rc(Sine(amplitude=1.0, frequency=50.0)), 0.25, 1e-6, **k)),
    "rectifier_200k": lambda: (lambda **k: simulate(hp.rectifier(), 0.2, 1e-6, **k)),
}


def watch(run):
    """進み具合の値と時刻を記録しながら計算する。"""
    seen, threads = [], set()

    def progress(fraction):
        seen.append((time.perf_counter(), fraction))
        threads.add(threading.get_ident())

    start = time.perf_counter()
    res = run(progress=progress)
    end = time.perf_counter()
    gaps = np.diff([start] + [t for t, _ in seen] + [end])
    return res, [f for _, f in seen], gaps, threads


# ------------------------------------------------------------------ 基準3: 進み具合

@pytest.mark.parametrize("case", list(LONG))
def test_progress_starts_at_0_rises_monotonically_ends_at_1_and_never_stalls(case):
    run = LONG[case]()
    worst = []
    for _ in range(ATTEMPTS):
        res, values, gaps, threads = watch(run)
        assert len(res.t) - 1 >= 200_000
        assert values[0] == 0.0 and values[-1] == 1.0
        assert all(b > a for a, b in zip(values, values[1:], strict=False)), values
        assert all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in values)
        assert len(values) >= 5                                   # 途中の知らせがある（0 と 1 だけではない）
        assert threads == {threading.get_ident()}                 # 計算と同じスレッドで呼ばれる
        worst.append(float(gaps.max()))
        if worst[-1] <= LIMIT_S:
            break
    assert min(worst) <= LIMIT_S, worst


def test_progress_follows_the_work_done():
    """進み具合は、済みうる計算の割合を大きく超えない（最後の 1 を除く）。

    済みうる計算は、制御器が呼ばれた時刻で外から測る（e1g_helpers.watch_with_work）。許す先行は 0.05
    （準備と後処理の割り当ての分）。制御周期 1000 ステップ、30 万ステップ。
    """
    from circuit_sim import Command

    period, n_periods = 1e-3, 300
    ctl = hp.Lookup([0.2 + 0.001 * (j % 7) for j in range(n_periods)], period=period)
    netlist = hp.net(hp.vsrc("V1", "in", "0", Command(command="u", gain=10.0)),
                     hp.comp("R1", "R", "in", "out", 1e3), hp.comp("C1", "C", "out", "0", 1e-6))
    t_end = n_periods * period
    seen, _ = hp.watch_with_work(
        lambda p: simulate(netlist, t_end, 1e-6, controllers=[ctl], progress=p), ctl, period, t_end)
    assert len(seen) >= 5
    hp.check_progress_against_work(seen)


# ------------------------------------------------------------------ 基準4: 中断

def check_cancelled(exc, seen, n_steps):
    assert exc.code == "RUN_CANCELLED"
    assert not isinstance(exc, ValueError)                         # 入力の誤りとは分ける
    assert 0 <= exc.params["k"] <= exc.params["n"] == n_steps
    assert exc.params["t"].unit == "s" and exc.params["t"].value >= 0.0
    assert exc.text("ja") != exc.text("en") and "{" not in exc.text("en")
    assert 1.0 not in seen                                          # 中断したら、進み具合を 1 にしない
    # エラーから、途中の配列を取り出せない
    held = [v for v in list(vars(exc).values()) + list(exc.params.values()) if isinstance(v, np.ndarray)]
    assert held == []


@pytest.mark.parametrize("case", list(LONG))
def test_cancel_ends_within_0_2_s_and_returns_no_result(case):
    """中断を求めた時刻から、SimulationCancelled で戻るまでを測る。

    求め方は 2 通り。(a) 進み具合が決めた値（0.1、0.5、0.9）に届いた時に求める。区切りの確かめの直後に
    求めることになるので、次の確かめまでの 1 区切り分を丸ごと待つ、いちばん遅い当たり方になる。
    (b) 計算の途中の、進み具合と関係のない時刻（所要時間の 35% の時点）に求める。
    """
    run = LONG[case]()
    start = time.perf_counter()
    reference = run()
    total = time.perf_counter() - start
    n_steps = len(reference.t) - 1

    def by_progress(threshold):
        seen, flag = [], {}

        def progress(fraction):
            seen.append(fraction)
            if fraction >= threshold and "t" not in flag:
                flag["t"] = time.perf_counter()

        with pytest.raises(SimulationCancelled) as info:
            run(progress=progress, cancel=lambda: "t" in flag)
        delay = time.perf_counter() - flag["t"]
        check_cancelled(info.value, seen, n_steps)
        assert max(seen) < threshold + 0.2                         # 求めた後は、ほとんど進まない
        return delay

    def by_clock():
        seen = []
        t_request = time.perf_counter() + 0.35 * total
        with pytest.raises(SimulationCancelled) as info:
            run(progress=seen.append, cancel=lambda: time.perf_counter() >= t_request)
        delay = time.perf_counter() - t_request
        check_cancelled(info.value, seen, n_steps)
        return delay

    for measure in (lambda: by_progress(0.1), lambda: by_progress(0.5), lambda: by_progress(0.9), by_clock):
        delays = []
        for _ in range(ATTEMPTS):
            delays.append(measure())
            if delays[-1] <= LIMIT_S:
                break
        assert min(delays) <= LIMIT_S, delays
    # 計算の前から求めてあれば、すぐに終わる（準備も始めない）
    start = time.perf_counter()
    with pytest.raises(SimulationCancelled) as info:
        run(cancel=lambda: True)
    assert time.perf_counter() - start <= LIMIT_S and info.value.params["k"] == 0
    # 中断の後に、同じ入力で流し直すと、中断しなかった結果とビット単位で同じ
    assert hp.signature(run()) == hp.signature(reference)


SMALL = {
    "template_switching": lambda: template("switching", 1e-4),
    "template_average_load_step": lambda: template(
        "average", 3e-3, plant={"R_load_step": hp.q(3, "ohm"), "t_load_step": hp.q(1, "ms")}),
    "linear_stiff_rc": lambda: (lambda **k: simulate(hp.rc(Step(final=1.0, t_step=1.3e-2)), 5e-2, 2e-5, **k)),
    "buck_diode": lambda: (lambda **k: simulate(hp.buck_diode(), 1e-4, gates=hp.gate(), **k)),
}


@pytest.mark.parametrize("case", list(SMALL))
def test_cancel_at_every_polling_point_never_returns_a_result(case):
    """中断の求めを見る場所の全部（準備、時間ステップ、後処理、終わり）で、1 か所ずつ中断する。

    区切りを 64 ステップに固定して、求めを見る回数を決まった数にする（既定は時間で決まる）。
    """
    run = SMALL[case]()
    with hp.chunk_steps(64, setup_block=256):
        reference = run()
        polls = 0

        def count():
            nonlocal polls
            polls += 1
            return False

        assert hp.signature(run(cancel=count)) == hp.signature(reference)
        assert polls >= 10                                        # 準備・時間ステップ・後処理で見ている
        for stop_at in range(1, polls + 1):
            calls = 0

            def cancel(stop_at=stop_at):
                nonlocal calls
                calls += 1
                return calls >= stop_at

            with pytest.raises(SimulationCancelled) as info:
                run(cancel=cancel)
            assert calls == stop_at                               # 真を返したら、それ以上は進めない
            assert info.value.code == "RUN_CANCELLED"
        assert hp.signature(run()) == hp.signature(reference)     # 何度中断した後でも、同じ結果


def test_cancel_is_also_polled_while_arrays_are_prepared_and_post_processed():
    """準備（電源やゲート信号の配列）と後処理のあいだも、中断の求めを見る（1000 万ステップの計算では、
    準備と後処理だけで 0.2 秒を超えるため）。

    区切らない設定（時間ステップの間は見ない）で、配列を埋める単位を 1 行にする。求めを見る回数が、
    ステップ数より多くなる。"""
    counts = {}
    for name, run, n_steps in (
            ("linear", lambda **k: simulate(hp.rc(), 5e-3, 1e-5, **k), 500),
            ("switched", lambda **k: simulate(hp.buck_diode(), 5e-5, gates=hp.gate(), **k), 1000)):
        with hp.chunk_steps(hp.UNCHUNKED, setup_block=1):
            polls = 0

            def count():
                nonlocal polls
                polls += 1
                return False

            assert len(run(cancel=count).t) - 1 == n_steps
            counts[name] = polls
        assert polls > n_steps, counts


def test_cancel_from_another_thread_with_an_event():
    """実際の使い方: 別のスレッドが threading.Event を立てる。"""
    run = LONG["linear_rc_250k"]()
    delays = []
    for _ in range(ATTEMPTS):
        event, stamp = threading.Event(), {}

        def fire(event=event, stamp=stamp):
            time.sleep(0.15)
            stamp["t"] = time.perf_counter()
            event.set()

        thread = threading.Thread(target=fire)
        thread.start()
        try:
            with pytest.raises(SimulationCancelled):
                run(cancel=event.is_set)
            delays.append(time.perf_counter() - stamp["t"])
        finally:
            thread.join()
        if delays[-1] <= LIMIT_S:
            break
    assert min(delays) <= LIMIT_S, delays


@pytest.mark.parametrize("name,code", [("progress", "ARG_PROGRESS"), ("cancel", "ARG_CANCEL")])
def test_callbacks_that_cannot_be_called_are_refused_with_a_coded_error(name, code):
    with pytest.raises(TypeError) as info:
        simulate(hp.rc(), 1e-3, 1e-5, **{name: 0.5})
    assert info.value.code == code and info.value.text("en") != info.value.text("ja")


# ------------------------------------------------------------------ 基準5: 区切っても同じ結果

CHUNK_CASES = {
    "rc_stiff_step_midway": lambda **k: simulate(hp.rc(Step(final=1.0, t_step=1.3e-2)), 5e-2, 2e-3, **k),
    "rc_step_at_last_point": lambda **k: simulate(hp.rc(Step(final=5.0, t_step=5e-3)), 5e-3, 1e-5, **k),
    "rlc_high_q": lambda **k: simulate(hp.rlc(0.01), 5e-3, 1e-6, **k),
    "ladder_38_unknowns": lambda **k: simulate(hp.ladder(12), 2e-3, 1e-6, **k),
    "buck_diode_dcm_triangle": lambda **k: simulate(
        hp.buck_diode(r=200.0), 1e-3, gates=hp.gate(0.3, carrier="triangle"), **k),
    "buck_diode_interruption_note": lambda **k: simulate(hp.buck_diode(vc0=30.0), 3e-4, gates=hp.gate(), **k),
    "rectifier": lambda **k: simulate(hp.rectifier(), 0.02, 1e-5, **k),
    "average_model_pid": lambda **k: simulate(
        hp.avg_buck(), 2e-3, 1e-6, controllers=[hp.pid_controller(0.0117, 147.0, 2.74e-6, 0.0, 0.9)], **k),
    "template_switching_load_step": lambda **k: expand(hp.buck_spec(
        "switching", plant={"R_load_step": hp.q(3, "ohm"), "t_load_step": hp.q(0.503, "ms")},
        sim={"t_end": 1e-3})).run(**k),
    "template_average_esr_ref_step": lambda **k: expand(hp.buck_spec(
        "average", plant={"C_esr": hp.q(20, "mohm")},
        control={"Vref_step": hp.q(10, "V"), "t_ref_step": hp.q(1, "ms")}, sim={"t_end": 2e-3})).run(**k),
}
# （区切りのステップ数、準備の配列の単位）。1 ステップずつ、素数、既定より大きい、区切らない
CHUNKINGS = [(1, None), (7, 5), (997, 4096), (hp.UNCHUNKED, hp.UNCHUNKED)]


@pytest.mark.parametrize("case", list(CHUNK_CASES))
def test_results_do_not_depend_on_where_the_run_is_chunked(case):
    run = CHUNK_CASES[case]
    reference = hp.signature(run())                               # 既定（時間で決まる区切り）
    assert hp.signature(run(progress=lambda f: None, cancel=lambda: False)) == reference
    for steps, block in CHUNKINGS:
        with hp.chunk_steps(steps, setup_block=block):
            assert hp.signature(run()) == reference, (steps, block)


def test_chunk_size_setting_really_changes_how_often_the_kernel_is_called(monkeypatch):
    """上の試験の前提: 区切りの定数を変えると、核を呼ぶ回数が本当に変わる（変わらないなら、比べていない）。"""
    from circuit_sim import kernel

    calls = {"n": 0}
    original = kernel.run

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel, "run", counting)
    with hp.chunk_steps(1):
        simulate(hp.rc(), 1e-3, 1e-6)
    one_by_one = calls["n"]
    calls["n"] = 0
    with hp.chunk_steps(hp.UNCHUNKED):
        simulate(hp.rc(), 1e-3, 1e-6)
    assert one_by_one == 1000 and calls["n"] == 1
