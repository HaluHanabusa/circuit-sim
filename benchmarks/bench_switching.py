"""スイッチング回路の計算速度を測る（合否ではなく記録）。

使い方（リポジトリの直下で）: python benchmarks/bench_switching.py [ステップ数]

基準の回路: 降圧コンバータ（24 V、L = 47 µH、C = 100 µF、100 kHz、1周期200分割）。
  - 連続導通（負荷 6 Ω）   : 1周期に状態の切り替えが2回
  - 不連続導通（負荷 100 Ω）: 1周期に状態の切り替えが3回
  - 制御器つき（連続導通）  : 制御周期（= スイッチング周期）ごとに核から戻って制御器を呼ぶ
比較のために、同じ大きさの線形回路（スイッチを抵抗に置き換えたもの。E0 の経路）も測る。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from circuit_sim import DC, Component, Netlist, Pwm, simulate  # noqa: E402

VIN, L, C, FSW, DIVISIONS = 24.0, 47e-6, 100e-6, 100e3, 200


def buck(load: float) -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=VIN)),
        Component(id="S1", type="S", nodes=("in", "sw"), gate="q1"),
        Component(id="D1", type="D", nodes=("0", "sw")),
        Component(id="L1", type="L", nodes=("sw", "out"), value=L),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
        Component(id="R1", type="R", nodes=("out", "0"), value=load),
    ])


def linear() -> Netlist:
    return Netlist(components=[
        Component(id="V1", type="V", nodes=("in", "0"), waveform=DC(value=VIN)),
        Component(id="Rs", type="R", nodes=("in", "sw"), value=1e-3),
        Component(id="Rd", type="R", nodes=("0", "sw"), value=1e6),
        Component(id="L1", type="L", nodes=("sw", "out"), value=L),
        Component(id="C1", type="C", nodes=("out", "0"), value=C),
        Component(id="R1", type="R", nodes=("out", "0"), value=6.0),
    ])


class FixedDuty:
    """固定値を返すだけの制御器（呼び出しの手間を測るため）。"""

    period = 1.0 / FSW
    inputs = ("v(out)",)
    outputs = ("duty",)

    def reset(self):
        return [0.5]

    def step(self, t, measured):
        return [0.5]


def best_of(fn, repeat: int = 3) -> float:
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def main() -> None:
    n_steps = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    h = 1.0 / (FSW * DIVISIONS)
    t_end = n_steps * h
    fixed = [Pwm(name="q1", frequency=FSW, duty=0.5)]
    by_command = [Pwm(name="q1", frequency=FSW, duty="duty")]
    cases = [
        ("降圧・連続導通（基準1の回路）", lambda: simulate(buck(6.0), t_end, gates=fixed)),
        ("降圧・不連続導通", lambda: simulate(buck(100.0), t_end, gates=fixed)),
        ("降圧・連続導通・制御器つき",
         lambda: simulate(buck(6.0), t_end, gates=by_command, controllers=[FixedDuty()])),
        ("線形回路（E0 の経路）", lambda: simulate(linear(), t_end, h)),
    ]
    print(f"ステップ数 {n_steps}（3回のうち最も速い値）")
    for name, fn in cases:
        seconds = best_of(fn)
        print(f"  {name}: {seconds:.3f} s（1ステップあたり {seconds / n_steps * 1e6:.2f} µs）")


if __name__ == "__main__":
    main()
