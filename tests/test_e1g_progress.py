"""E1g の範囲2（受け入れ基準3・4・5）: 計算を区切って進め、進み具合を知らせ、中断に応じる。

- 進み具合: 0 から単調に増え、最後に 1。知らせの間隔（呼び出しの前後を含む）が 0.2 秒を超えない
  （20 万ステップ以上の計算で測る）。
- 中断: 中断を求めてから 0.2 秒以内に SimulationCancelled で終わり、結果を返さない。
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
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_voltage_mode.json"
LIMIT_S = 0.2


def buck(fidelity="switching", t_end=10e-3, **plant):
    d = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    for k, v in plant.items():
        d["plant"][k] = {"value": v[0], "unit": v[1]}
    return expand(Spec.model_validate(d))


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


def check_progress(seen, span):
    values = [f for _, f in seen]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b > a for a, b in zip(values, values[1:], strict=False)), values      # 単調に増える
    times = [span[0]] + [t for t, _ in seen] + [span[1]]
    gaps = np.diff(times)
    assert gaps.max() <= LIMIT_S, gaps.max()
    return gaps.max()


@pytest.mark.parametrize("case", ["buck_switching", "rectifier", "ladder"])
def test_progress_rises_to_one_and_never_stalls_longer_than_0_2_s(case):
    """20 万ステップ以上: 降圧（制御器つき、20 万ステップ）、整流（制御器なし、40 万ステップ）、
    はしご（スイッチなし、未知数 41、20 万ステップ）。"""
    if case == "buck_switching":
        ex = buck()
        res, seen, span = watched(lambda p: ex.run(progress=p))
    elif case == "rectifier":
        gate = Pwm(name="q1", frequency=100e3, duty=0.3)
        res, seen, span = watched(lambda p: simulate(rectifier(), 20e-3, gates=[gate], progress=p))
    else:
        res, seen, span = watched(lambda p: simulate(ladder(), 0.2, 1e-6, progress=p))
    assert len(res.t) - 1 >= 200_000
    check_progress(seen, span)
    assert len(seen) >= 3


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
    took, error, result = run_and_cancel(run)
    assert isinstance(error, SimulationCancelled) and result is None
    assert took <= LIMIT_S, took
    # 準備のあいだに中断を求めることもある（k = 0）。準備（電源やゲート信号の配列）も区切って中断に応じる
    assert error.code == "RUN_CANCELLED" and 0 <= error.params["k"] < error.params["n"]
    assert not isinstance(error, ValueError)          # 入力の誤りとは分ける


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
