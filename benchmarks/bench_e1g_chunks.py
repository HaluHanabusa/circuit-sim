"""区切り（進み具合、中断、発散の確かめ）の費用を測る（E1g の受け入れ基準5・12。合否ではなく記録）。

使い方（リポジトリの直下で）:
    python benchmarks/bench_e1g_chunks.py [--repeat 5] [--baseline 前の版の circuit_sim の親フォルダ]

測るもの（どれも同じ回路を同じ刻みで、最良の値を取る）:
  区切る      : いまの既定（区切りごとに発散の確かめ。progress と cancel を渡す）
  区切らない  : 区切りの点を無くしたもの（CHUNK_STEPS_FIRST と CHUNK_STEPS_MAX を 1e12 にする。制御器のある
                回路は、制御周期ごとの呼び出しだけ。発散の確かめは計算の終わりに1回）。結果はビット単位で同じ
  前の版      : --baseline に E1g の前の circuit_sim（例: git archive 4447f5a circuit_sim | tar -x -C 場所）を
                渡したとき（区切りも発散の確かめも無い）
どの形も、別のプロセスで1回計算して CPU 時間を返す（読み込みと準備の分は捨てる）。形と回路を交互にくり返し
（--repeat 回。1つのプロセスの中では --inner 回の最良）、その中央値を比べる。機械の揺れ（この環境で ±10% 程度）が片方にだけ乗らないように。
回路: 降圧コンバータ（同梱の例、制御器つき、スイッチング 20 万ステップ / 平均 20 万ステップ）、
      降圧（ダイオード整流、制御器なし。核の1回の呼び出しが全体になる回路。40 万ステップ）、
      RC はしご 20 段（スイッチなし、未知数 41、20 万ステップ）。
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES_CODE = r'''
import json, time
from pathlib import Path
from circuit_sim import DC, Component, Netlist, Pwm, Spec, Step, expand, simulate

ROOT = Path(%(root)r)


def buck(fidelity, t_end):
    d = json.loads((ROOT / "examples" / "buck_voltage_mode.json").read_text(encoding="utf-8"))
    d["simulation"] = {"fidelity": fidelity, "t_end": t_end}
    return expand(Spec.model_validate(d))


def rectifier():
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=24.0)),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q1"),
        Component(id="D1", type="D", nodes=("0", "sw")),
        Component(id="L1", type="L", nodes=("sw", "out"), value=47e-6),
        Component(id="C1", type="C", nodes=("out", "0"), value=100e-6),
        Component(id="R1", type="R", nodes=("out", "0"), value=100.0),
    ])


def ladder(n=20):
    comps = [Component(id="V1", type="V", nodes=("n0", "0"), waveform=Step(final=1.0, t_step=1e-4))]
    for j in range(n):
        comps += [Component(id=f"R{j}", type="R", nodes=(f"n{j}", f"n{j + 1}"), value=10.0),
                  Component(id=f"C{j}", type="C", nodes=(f"n{j + 1}", "0"), value=1e-6)]
    return Netlist(components=comps)


SW, AV = buck("switching", 10e-3), buck("average", 200e-3)
CASES = {
    "buck_switching": lambda **kw: SW.run(**kw),
    "buck_average": lambda **kw: AV.run(**kw),
    "rectifier": lambda **kw: simulate(rectifier(), 4e-3, gates=[Pwm(name="q1", frequency=100e3, duty=0.3)],
                                       steps_per_period=1000, **kw),
    "ladder": lambda **kw: simulate(ladder(), 0.2, 1e-6, **kw),
}


def best(case, repeat, **kw):
    out = []
    for _ in range(repeat):
        t0 = time.process_time()          # このプロセスの CPU 時間（ほかのプロセスの揺れを受けにくい）
        res = CASES[case](**kw)
        out.append(time.process_time() - t0)
    return min(out), len(res.t) - 1
'''


WORKER = r"""
import importlib, sys, time
mode, case = sys.argv[1], sys.argv[2]
ns = {}
exec(sys.argv[3], ns)
kw = {}
if mode == "chunked":
    kw = {"progress": lambda f: None, "cancel": lambda: False}
elif mode == "unchunked":
    sim = importlib.import_module("circuit_sim.simulate")
    sim.CHUNK_STEPS_FIRST = sim.CHUNK_STEPS_MAX = 10**12
ns["CASES"][case](**kw)                       # 1回目は読み込みと準備を含むので捨てる
t, steps = ns["best"](case, int(sys.argv[4]), **kw)
print(t, steps)
"""


def measure(mode: str, case: str, code: str, path: Path, inner: int) -> tuple[float, int]:
    done = subprocess.run([sys.executable, "-c", WORKER, mode, case, code, str(inner)], cwd=str(path),
                          capture_output=True, text=True, check=True,
                          env={"PYTHONPATH": str(path), "PATH": "/usr/bin:/bin"})
    t, steps = done.stdout.split()
    return float(t), int(steps)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--inner", type=int, default=3, help="1つのプロセスの中でくり返す回数（最良を取る）")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    code = CASES_CODE % {"root": str(ROOT)}
    cases = ("buck_switching", "buck_average", "rectifier", "ladder")
    modes = [("chunked", ROOT), ("unchunked", ROOT)]
    if args.baseline:
        modes.append(("baseline", args.baseline))
    times: dict = {(c, m): [] for c in cases for m, _ in modes}
    steps: dict = {}
    # 形を交互に測り（機械の揺れが片方にだけ乗らないように）、最良を取る
    for _ in range(args.repeat):
        for case in cases:
            for mode, path in modes:
                t, steps[case] = measure(mode, case, code, path, args.inner)
                times[case, mode].append(t)
    # 値は、プロセスごとの最良の中央値（最良どうしの比は揺れに弱いので、中央値で比べる）
    print(f"{'回路':16} {'ステップ':>9} {'区切る':>9} {'区切らない':>10} {'差':>7} {'前の版':>9} {'差':>7}")
    for case in cases:
        a, b = statistics.median(times[case, "chunked"]), statistics.median(times[case, "unchunked"])
        line = f"{case:16} {steps[case]:9d} {a:9.3f} {b:10.3f} {a / b - 1:+7.1%}"
        if args.baseline:
            c = statistics.median(times[case, "baseline"])
            line += f" {c:9.3f} {a / c - 1:+7.1%}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
