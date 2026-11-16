"""区切り（進み具合、中断、発散の確かめ）の費用と、知らせの間隔を測る（E1g の受け入れ基準3・4・5・12）。

合否ではなく記録（速さは機械で変わる）。使い方（リポジトリの直下で）:
    python benchmarks/bench_e1g_chunks.py [--repeat 15] [--baseline 前の版の circuit_sim の親フォルダ]
                                          [--sections cost,direct,intervals,instructions]

測るもの
  cost      : 同じ回路を同じ刻みで計算する所要時間を、次の形で比べる
                区切る      いまの既定（区切りごとに発散の確かめ。progress と cancel を渡す）
                区切らない  区切りの点を無くしたもの（CHUNK_STEPS_FIRST と CHUNK_STEPS_MAX を 1e12 にする。
                            制御器のある回路は、制御周期ごとの呼び出しだけ。発散の確かめは計算の終わりに1回）。
                            結果はビット単位で同じ（tests/test_e1g_progress.py）
                前の版      --baseline に E1g の前の circuit_sim を渡したとき（区切りも発散の確かめも無い）。
                            例: git archive 168c764 circuit_sim | tar -x -C 場所
              回路ごとに別のプロセスを立て、その中で形を交互にくり返す（--repeat 回。順番も回す）。
              機械の揺れが片方にだけ乗らないように。比は、最良どうしの比と、同じ回の対の比の中央値の
              両方を出す（機械によっては、同じ形のくり返しでも最良と中央値が 10〜40% 違う。差が数 % より
              小さいときは、どちらの比も揺れの中）
  direct    : 発散の確かめと区切りの手間（simulate._Monitor の中で使った時間）を、計算の時間に対する
              割合で測る。形どうしの揺れを受けないので、「区切りと確かめによる所要時間の増加」の値に使える
  intervals : 大きな計算（100 万〜200 万ステップ）で、中断の求めを見る間隔と、進み具合を知らせる間隔の最大
              （呼び出しの初めから最初の1回までと、最後の1回から戻るまでを含む。準備と後処理も入る）
  instructions : 1ステップあたりの命令数を valgrind（cachegrind）で数えて、cost と同じ3つの形を比べる。
              時間と違って機械の揺れを受けない（同じ条件なら 1e-5 以内で同じ値）ので、数 % より小さい差を
              見分けられる。valgrind が要る（無ければ飛ばす）。既定では測らない（数分かかる）。
              2つの長さの計算の命令数の差から、読み込みと準備の分を除く。valgrind の下では時間が遅く進むので、
              区切る形は、区切りのステップ数を IR_CHUNK（ふつうの速さでの 20 ms に当たる数）に固定する

回路: 降圧コンバータ（同梱の例、制御器つき、スイッチング / 状態平均化モデル）、
      降圧（ダイオード整流、制御器なし。核の1回の呼び出しが計算の全体になる回路）、
      RC はしご 20 段（スイッチなし、未知数 41）。cost と direct は 20 万〜40 万ステップ。
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ("buck_switching", "buck_average", "rectifier", "ladder")
# 回路 → （cost と direct の計算の長さ、intervals の計算の長さ）[s]
T_END = {
    "buck_switching": (10e-3, 50e-3),     # 20 万ステップ、100 万ステップ
    "buck_average": (200e-3, 2.0),        # 20 万、200 万
    "rectifier": (4e-3, 0.1),             # 40 万（1周期 1000 分割）、200 万（1周期 200 分割）
    "ladder": (0.2, 2.0),                 # 20 万、200 万
}
# 回路 → 1 秒あたりのステップ数（cost の刻み。instructions で、ステップ数から計算の長さを決める）
STEPS_PER_SECOND = {"buck_switching": 2e7, "buck_average": 1e6, "rectifier": 1e8, "ladder": 1e6}
IR_STEPS = (2000, 42000)                  # instructions で比べる2つの長さ [ステップ]
IR_CHUNK = 5000                           # instructions の「区切る」形の、区切りのステップ数


def load_package(name: str, parent: Path):
    """parent/circuit_sim を、別の名前のパッケージとして読み込む（前の版を同じプロセスに並べるため）。"""
    spec = importlib.util.spec_from_file_location(
        name, parent / "circuit_sim" / "__init__.py",
        submodule_search_locations=[str(parent / "circuit_sim")])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_case(cs, case: str, long: bool, steps: int | None = None):
    """回路 case を計算する関数（cs は circuit_sim のパッケージ。版ごとに作る）。

    steps を渡すと、cost の刻みで、そのステップ数の長さにする。"""
    t_end = T_END[case][1 if long else 0] if steps is None else steps / STEPS_PER_SECOND[case]
    if case.startswith("buck_"):
        d = json.loads((ROOT / "examples" / "buck_voltage_mode.json").read_text(encoding="utf-8"))
        d["simulation"] = {"fidelity": case.removeprefix("buck_"), "t_end": t_end}
        ex = cs.expand(cs.Spec.model_validate(d))
        return lambda **kw: ex.run(**kw)
    comp = cs.Component
    if case == "rectifier":
        net = cs.Netlist(components=[
            comp(id="V1", type="V", nodes=("in", "0"), waveform=cs.DC(value=24.0)),
            comp(id="S1", type="S", nodes=("in", "sw"), gate="q1"),
            comp(id="D1", type="D", nodes=("0", "sw")),
            comp(id="L1", type="L", nodes=("sw", "out"), value=47e-6),
            comp(id="C1", type="C", nodes=("out", "0"), value=100e-6),
            comp(id="R1", type="R", nodes=("out", "0"), value=100.0),
        ])
        gate = cs.Pwm(name="q1", frequency=100e3, duty=0.3)
        divisions = 200 if long else 1000
        return lambda **kw: cs.simulate(net, t_end, gates=[gate], steps_per_period=divisions, **kw)
    comps = [comp(id="V1", type="V", nodes=("n0", "0"), waveform=cs.Step(final=1.0, t_step=1e-4))]
    for j in range(20):
        comps += [comp(id=f"R{j}", type="R", nodes=(f"n{j}", f"n{j + 1}"), value=10.0),
                  comp(id=f"C{j}", type="C", nodes=(f"n{j + 1}", "0"), value=1e-6)]
    net = cs.Netlist(components=comps)
    return lambda **kw: cs.simulate(net, t_end, 1e-6, **kw)


def current_package():
    sys.path.insert(0, str(ROOT))
    cs = importlib.import_module("circuit_sim")
    return cs, importlib.import_module("circuit_sim.simulate")      # 名前 simulate は関数を指すので


# ------------------------------------------------------------------ 別のプロセスで動かす中身


def worker_cost(case: str, repeat: int, baseline: Path | None) -> dict:
    cs, sim = current_package()
    first, largest = sim.CHUNK_STEPS_FIRST, sim.CHUNK_STEPS_MAX
    run_new = make_case(cs, case, long=False)
    run_old = make_case(load_package("circuit_sim_before", baseline), case, long=False) if baseline else None
    quiet = {"progress": lambda f: None, "cancel": lambda: False}

    def one(mode: str) -> tuple[float, int]:
        if mode == "baseline":
            fn, kw = run_old, {}
        else:
            fn, kw = run_new, (quiet if mode == "chunked" else {})
            sim.CHUNK_STEPS_FIRST, sim.CHUNK_STEPS_MAX = (first, largest) if mode == "chunked" else (
                10**12, 10**12)
        t0 = time.perf_counter()
        res = fn(**kw)
        return time.perf_counter() - t0, len(res.t) - 1

    modes = ["chunked", "unchunked"] + (["baseline"] if baseline else [])
    steps = 0
    for mode in modes:                      # 1回目は読み込みと準備を含むので捨てる
        _, steps = one(mode)
    times: dict[str, list[float]] = {m: [] for m in modes}
    for r in range(repeat):
        shift = r % len(modes)
        for mode in modes[shift:] + modes[:shift]:
            times[mode].append(one(mode)[0])
    return {"steps": steps, "times": times}


def worker_direct(case: str, repeat: int) -> dict:
    cs, sim = current_package()
    spent = {"check": 0.0, "monitor": 0.0}

    def timed(fn, key):
        def wrapper(*args):
            t0 = time.perf_counter()
            try:
                return fn(*args)
            finally:
                spent[key] += time.perf_counter() - t0
        return wrapper

    monitor = sim._Monitor
    monitor.check_values = timed(monitor.check_values, "check")       # after() の中から呼ばれる分も入る
    monitor.after, monitor.tick = timed(monitor.after, "monitor"), timed(monitor.tick, "monitor")
    run = make_case(cs, case, long=False)
    run()
    rows = []
    for _ in range(repeat):
        spent["check"] = spent["monitor"] = 0.0
        t0 = time.perf_counter()
        run()
        rows.append((time.perf_counter() - t0, spent["check"], spent["monitor"]))
    total, check, monitor_time = min(rows)
    return {"total": total, "check": check, "monitor": monitor_time}


def worker_intervals(case: str) -> dict:
    cs, _ = current_package()
    run = make_case(cs, case, long=True)
    polls: list[float] = []
    reports: list[float] = []

    def cancel() -> bool:
        polls.append(time.perf_counter())
        return False

    t0 = time.perf_counter()
    res = run(progress=lambda f: reports.append(time.perf_counter()), cancel=cancel)
    t1 = time.perf_counter()

    def largest_gap(times: list[float]) -> float:
        points = [t0] + times + [t1]
        return max(b - a for a, b in zip(points, points[1:], strict=False))

    return {"steps": len(res.t) - 1, "total": t1 - t0, "polls": len(polls), "cancel_gap": largest_gap(polls),
            "reports": len(reports), "progress_gap": largest_gap(reports)}


def worker_count(case: str, mode: str, steps: int, baseline: Path | None) -> dict:
    """決まったステップ数の計算を1回行う（valgrind の下で動かして、命令数を数える）。"""
    if mode == "baseline":
        run = make_case(load_package("circuit_sim_before", baseline), case, long=False, steps=steps)
        return {"steps": len(run().t) - 1}
    cs, sim = current_package()
    run = make_case(cs, case, long=False, steps=steps)
    if mode == "unchunked":
        sim.CHUNK_STEPS_FIRST = sim.CHUNK_STEPS_MAX = 10**12
        return {"steps": len(run().t) - 1}
    sim.CHUNK_STEPS_FIRST = sim.CHUNK_STEPS_MIN = sim.CHUNK_STEPS_MAX = IR_CHUNK
    sim.PROGRESS_SECONDS = 0.0             # 区切りごとに知らせる（時間によらず、同じ回数になる）
    return {"steps": len(run(progress=lambda f: None, cancel=lambda: False).t) - 1}


# ------------------------------------------------------------------ 表にする


def in_new_process(*args: str) -> dict:
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", *args],
                          cwd=str(ROOT), capture_output=True, text=True, check=True)
    return json.loads(done.stdout.splitlines()[-1])


def compare(times: dict[str, list[float]], a: str, b: str) -> str:
    """a / b − 1 を、最良どうしの比と、同じ回の対の比の中央値で。"""
    best = min(times[a]) / min(times[b]) - 1
    paired = statistics.median(x / y for x, y in zip(times[a], times[b], strict=True)) - 1
    return f"{best:+6.1%} / {paired:+6.1%}"


def show_cost(repeat: int, baseline: Path | None) -> None:
    print("所要時間 [s]（最良）と差（最良どうしの比 / 同じ回の対の比の中央値）")
    head = f"{'回路':16} {'ステップ':>8} {'区切る':>8} {'区切らない':>8} {'区切る ÷ 区切らない':>18}"
    if baseline:
        head += f" {'前の版':>8} {'区切る ÷ 前の版':>18} {'区切らない ÷ 前の版':>18}"
    print(head)
    for case in CASES:
        got = in_new_process("cost", case, str(repeat), str(baseline or ""))
        t = got["times"]
        line = (f"{case:16} {got['steps']:8d} {min(t['chunked']):8.3f} {min(t['unchunked']):8.3f} "
                f"{compare(t, 'chunked', 'unchunked'):>18}")
        if baseline:
            line += (f" {min(t['baseline']):8.3f} {compare(t, 'chunked', 'baseline'):>18} "
                     f"{compare(t, 'unchunked', 'baseline'):>18}")
        print(line, flush=True)


def show_direct(repeat: int) -> None:
    print("発散の確かめと区切りに使った時間（計算の時間に対する割合。区切り全体は、発散の確かめを含む）")
    print(f"{'回路':16} {'所要時間':>8} {'発散の確かめ':>10} {'区切り全体':>10}")
    for case in CASES:
        got = in_new_process("direct", case, str(repeat))
        print(f"{case:16} {got['total']:8.3f} {got['check'] / got['total']:10.2%} "
              f"{got['monitor'] / got['total']:10.2%}", flush=True)


def count_instructions(case: str, mode: str, steps: int, baseline: Path | None) -> int:
    done = subprocess.run(
        ["valgrind", "--tool=cachegrind", "--cache-sim=no", f"--cachegrind-out-file={os.devnull}",
         sys.executable, str(Path(__file__).resolve()), "--worker", "count", case, mode, str(steps),
         str(baseline or "")],
        cwd=str(ROOT), capture_output=True, text=True, check=True, env={**os.environ, "PYTHONHASHSEED": "0"})
    return int(re.search(r"I\s+refs:\s+([\d,]+)", done.stderr).group(1).replace(",", ""))


def show_instructions(baseline: Path | None) -> None:
    if shutil.which("valgrind") is None:
        print("valgrind が無いので、命令数は数えない")
        return
    modes = ["chunked", "unchunked"] + (["baseline"] if baseline else [])
    jobs = [(case, mode, steps) for case in CASES for mode in modes for steps in IR_STEPS]
    # 命令数は、並べて数えても同じ値になる
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        counts = dict(zip(jobs, pool.map(lambda job: count_instructions(*job, baseline), jobs), strict=True))
    span = IR_STEPS[1] - IR_STEPS[0]
    print(f"1ステップあたりの命令数（valgrind。{IR_STEPS[1]} ステップと {IR_STEPS[0]} ステップの"
          f"計算の差から。区切る形は {IR_CHUNK} ステップごと）")
    head = f"{'回路':16} {'区切る':>10} {'区切らない':>10} {'区切る ÷ 区切らない':>18}"
    if baseline:
        head += f" {'前の版':>10} {'区切る ÷ 前の版':>16} {'区切らない ÷ 前の版':>18}"
    print(head)
    for case in CASES:
        per = {m: (counts[case, m, IR_STEPS[1]] - counts[case, m, IR_STEPS[0]]) / span for m in modes}
        line = (f"{case:16} {per['chunked']:10.0f} {per['unchunked']:10.0f} "
                f"{per['chunked'] / per['unchunked'] - 1:18.2%}")
        if baseline:
            line += (f" {per['baseline']:10.0f} {per['chunked'] / per['baseline'] - 1:16.2%} "
                     f"{per['unchunked'] / per['baseline'] - 1:18.2%}")
        print(line, flush=True)


def show_intervals() -> None:
    print("中断の求めを見る間隔と、進み具合を知らせる間隔の最大 [ms]（基準はどちらも 200 ms 以内）")
    print(f"{'回路':16} {'ステップ':>8} {'所要時間':>8} {'中断を見た回数':>10} {'間隔の最大':>10} "
          f"{'知らせた回数':>10} {'間隔の最大':>10}")
    for case in CASES:
        got = in_new_process("intervals", case)
        print(f"{case:16} {got['steps']:8d} {got['total']:8.2f} {got['polls']:10d} "
              f"{got['cancel_gap'] * 1e3:10.1f} {got['reports']:10d} {got['progress_gap'] * 1e3:10.1f}",
              flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=15, help="形を交互にくり返す回数")
    parser.add_argument("--baseline", type=Path, help="E1g の前の circuit_sim の親フォルダ")
    parser.add_argument("--sections", default="cost,direct,intervals")
    parser.add_argument("--worker", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        kind, case, *rest = args.worker
        if kind == "cost":
            got = worker_cost(case, int(rest[0]), Path(rest[1]).resolve() if rest[1] else None)
        elif kind == "direct":
            got = worker_direct(case, int(rest[0]))
        elif kind == "count":
            got = worker_count(case, rest[0], int(rest[1]), Path(rest[2]).resolve() if rest[2] else None)
        else:
            got = worker_intervals(case)
        print(json.dumps(got))
        return 0
    sections = args.sections.split(",")
    baseline = args.baseline.resolve() if args.baseline else None
    if "cost" in sections:
        show_cost(args.repeat, baseline)
        print()
    if "direct" in sections:
        show_direct(min(args.repeat, 5))
        print()
    if "intervals" in sections:
        show_intervals()
        print()
    if "instructions" in sections:
        show_instructions(baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
