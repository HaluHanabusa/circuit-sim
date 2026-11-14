"""ネットリストを時間応答に変換する入口。

スイッチの無い回路は台形則だけで進める（E0 と同じ計算）。
スイッチ（S）やダイオード（D）を含む回路は、回路状態ごとの行列を使い回しながら進め、
制御周期ごとに核から戻って制御器を呼ぶ。
"""
from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import kernel
from .control import Controller, Pwm
from .ir import DC, Netlist, Sine, Step
from .mna import CircuitError, System, build_system, describe_state, state_conductance

DEFAULT_STEPS_PER_PERIOD = 200   # 搬送波1周期の分割数の既定値
DEFAULT_DAMPING_STEPS = 2        # 切り替え直後に後退オイラーで進めるステップ数の既定値
DIODE_ON_VOLTAGE = 1e-9          # オフのダイオードをオンにする順方向電圧 [V]（丸め誤差での反転を防ぐ）
SIMULTANEOUS_FLIPS = 4           # ダイオードの状態決定で、違反したものを同時に反転する回数
_GRID_TOL = 1e-6                 # 周期が時間刻みの整数倍かどうかの判定（ステップ数に対する相対値）


@dataclass
class Result:
    t: np.ndarray                 # (n_steps + 1,)
    x: np.ndarray                 # (n_steps + 1, 未知数の数)
    unknowns: list[str]
    netlist: Netlist
    h: float
    notes: list[str] = field(default_factory=list)
    # --- スイッチを含む計算で入るもの ---
    signals: dict[str, np.ndarray] = field(default_factory=dict)   # "gate(名前)" "cmd(名前)" "on(素子ID)"
    devices: list[str] = field(default_factory=list)               # S と D の素子ID（状態の符号のビット順）
    sample_state: np.ndarray | None = None    # 各時刻の値と整合する回路状態の符号（S と D の電流に使う）
    stats: dict[str, int] = field(default_factory=dict)            # 用意した回路状態の数など

    @property
    def names(self) -> list[str]:
        """[] で取り出せる波形の名前（回路の変数、ゲート信号、指令、導通状態）。"""
        return self.unknowns + list(self.signals)

    def __getitem__(self, name: str) -> np.ndarray:
        """"v(out)" や "i(L1)"、"gate(q1)"、"cmd(duty)"、"on(D1)" の形で波形を取り出す。

        回路の変数は、その時刻の値。スイッチやダイオードがその時刻で切り替わる場合は、切り替わる前の値。
        gate / cmd / on は、その時刻から次の時刻までの区間で使われた値（1 = オン）。
        最後の点は、最後の区間の値を繰り返す。
        """
        if name in self.signals:
            return self.signals[name]
        try:
            return self.x[:, self.unknowns.index(name)]
        except ValueError:
            raise KeyError(f"{name} は無い。使える名前: {self.names}") from None

    def v(self, node: str, ref: str | None = None) -> np.ndarray:
        """節点電圧。ref を指定すると2節点間の電圧。"""
        def one(n: str) -> np.ndarray:
            return np.zeros_like(self.t) if n == self.netlist.ground else self[f"v({n})"]
        return one(node) - (one(ref) if ref is not None else 0.0)

    def i(self, comp_id: str) -> np.ndarray:
        """素子を n+ から n- へ流れる電流。電圧源・インダクタ・抵抗・スイッチ・ダイオードに対応。"""
        c = self.netlist.component(comp_id)
        if c.type in ("V", "L"):
            return self[f"i({comp_id})"]
        if c.type == "R":
            return self.v(c.nodes[0], c.nodes[1]) / c.value
        if c.type in ("S", "D") and self.sample_state is not None:
            on = (self.sample_state >> self.devices.index(comp_id)) & 1
            return self.v(c.nodes[0], c.nodes[1]) * np.where(on == 1, 1.0 / c.r_on, 1.0 / c.r_off)
        raise KeyError(f"{comp_id}（{c.type}）の電流はまだ取り出せない")

    def to_csv(self, path: str | Path, names: list[str] | None = None) -> None:
        names = names or self.unknowns
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t"] + names)
            cols = [self[n] for n in names]
            for k in range(len(self.t)):
                w.writerow([repr(float(self.t[k]))] + [repr(float(c[k])) for c in cols])


def _sample_sources(system: System, n_steps: int, h: float, notes: list[str]):
    """電源を時間格子上の配列にする。戻り値: u_start, u_end, event"""
    m = len(system.sources)
    t = h * np.arange(n_steps + 1)
    u_start = np.zeros((n_steps, m))
    u_end = np.zeros((n_steps, m))
    event = np.zeros(n_steps, dtype=bool)

    for j, cid in enumerate(system.sources):
        w = system.netlist.component(cid).waveform
        if isinstance(w, DC):
            u_start[:, j] = w.value
            u_end[:, j] = w.value
        elif isinstance(w, Sine):
            y = w.offset + w.amplitude * np.sin(
                2.0 * math.pi * w.frequency * t + math.radians(w.phase_deg)
            )
            u_start[:, j] = y[:-1]
            u_end[:, j] = y[1:]
        elif isinstance(w, Step):
            ks = int(round(w.t_step / h))
            if abs(ks * h - w.t_step) > 1e-9 * h:
                notes.append(f"{cid}: ステップ時刻 {w.t_step} を格子点 {ks * h} に丸めた")
            k = np.arange(n_steps)
            u_start[:, j] = np.where(k >= ks, w.final, w.initial)       # t[k] の右側の値
            u_end[:, j] = np.where(k + 1 > ks, w.final, w.initial)      # t[k+1] の左側の値
            if 0 < ks < n_steps and w.final != w.initial:
                event[ks] = True
        else:  # pragma: no cover
            raise CircuitError(f"{cid}: 未対応の波形 {type(w).__name__}")
    return u_start, u_end, event


def simulate(
    netlist: Netlist,
    t_end: float,
    h: float | None = None,
    *,
    gates: Sequence[Pwm] = (),
    controllers: Sequence[Controller] = (),
    steps_per_period: int | None = None,
    damping_steps: int = DEFAULT_DAMPING_STEPS,
) -> Result:
    """ネットリストを固定刻みで計算する。

    スイッチ（S）もダイオード（D）も無く、gates と controllers も渡さない場合は、
    台形則だけで進める（E0 と同じ計算）。h は必須。

    スイッチやダイオードを含む場合:
      gates            : ゲート信号（PWM）。S の gate から名前で参照する。
      controllers      : 制御器。制御周期ごとに測定して演算し、指令は次の制御周期の頭から使う。
      h                : 時間刻み。省略すると、最も速い搬送波の周期を steps_per_period で割った値。
      steps_per_period : 搬送波1周期の分割数（既定 200）。h と同時には指定しない。
      damping_steps    : 切り替え直後に後退オイラーで進めるステップ数（1以上。既定 2）。
    """
    if not (t_end > 0) or (h is not None and not (h > 0)):
        raise ValueError("t_end と h は正の値が必要")

    switching = any(c.type in ("S", "D") for c in netlist.components)
    if switching or gates or controllers:
        return _simulate_switched(netlist, t_end, h, gates, controllers, steps_per_period, damping_steps)

    if h is None:
        raise ValueError("スイッチの無い回路では時間刻み h が必要")
    if steps_per_period is not None:
        raise ValueError("steps_per_period を使えるのは、ゲート信号（gates）を渡すときだけ")
    return _simulate_linear(netlist, t_end, h)


def _simulate_linear(netlist: Netlist, t_end: float, h: float) -> Result:
    """スイッチの無い回路（E0 と同じ計算）。"""
    n_steps = int(round(t_end / h))
    if n_steps < 1:
        raise ValueError("t_end が h より短い")

    system = build_system(netlist)
    notes: list[str] = []
    u_start, u_end, event = _sample_sources(system, n_steps, h, notes)

    ops = kernel.prepare(system.E, system.G, system.B, h)
    x0 = ops.Rex @ system.ex0 + ops.Ru @ u_start[0]

    out = np.empty((n_steps + 1, len(system.unknowns)))
    kernel.run(ops.P, ops.Q, ops.Rx, ops.Ru, x0, u_start, u_end, event, out)

    return Result(
        t=h * np.arange(n_steps + 1),
        x=out,
        unknowns=system.unknowns,
        netlist=netlist,
        h=h,
        notes=notes,
    )


# ---------------------------------------------------------------- スイッチング

def _whole_steps(period: float, h: float, what: str) -> int:
    """周期が時間刻みの何ステップ分か。整数倍でなければエラー。"""
    ratio = period / h
    n = int(round(ratio))
    if n < 1 or abs(ratio - n) > _GRID_TOL * max(n, 1):
        raise ValueError(
            f"{what}（{period:.6g} s）が時間刻み h（{h:.6g} s）の整数倍になっていない。"
            "h を周期の整数分の1にする"
        )
    return n


class _StateTable:
    """回路状態ごとの行列。出会った状態だけ一度計算して、核に渡せる配列に並べて持つ。"""

    _ARRAYS = ("code", "P", "Q", "Pb", "Qb", "Rx", "Ru")

    def __init__(self, system: System, h: float):
        self.system = system
        self.h = h
        self.count = 0
        self.index: dict[int, int] = {}      # 状態の符号 → 配列の中の位置
        self.rex: list[np.ndarray] = []      # 初期値の計算にだけ使う（核には渡さない）
        n, m = system.B.shape
        capacity = 4
        self.code = np.full(capacity, -1, dtype=np.int64)
        self.P = np.zeros((capacity, n, n))
        self.Q = np.zeros((capacity, n, m))
        self.Pb = np.zeros((capacity, n, n))
        self.Qb = np.zeros((capacity, n, m))
        self.Rx = np.zeros((capacity, n, n))
        self.Ru = np.zeros((capacity, n, m))

    def _grow(self) -> None:
        for name in self._ARRAYS:
            old = getattr(self, name)
            new = np.full((2 * old.shape[0],) + old.shape[1:], -1 if name == "code" else 0, dtype=old.dtype)
            new[: old.shape[0]] = old
            setattr(self, name, new)

    def slot(self, code: int) -> int:
        """状態 code の行列の位置。まだ無ければ計算して足す。"""
        if code in self.index:
            return self.index[code]
        try:
            ops = kernel.prepare(
                self.system.E, state_conductance(self.system, code), self.system.B, self.h,
                backward_euler=True,
            )
        except CircuitError as exc:
            raise CircuitError(
                f"回路状態（{describe_state(self.system, code)}）で: {exc}。"
                "オフ抵抗 r_off が極端に大きいと、オフのときに節点が浮いたのと同じになって解けない"
            ) from exc
        if self.count == self.code.shape[0]:
            self._grow()
        s = self.count
        self.code[s] = code
        self.P[s], self.Q[s] = ops.P, ops.Q
        self.Pb[s], self.Qb[s] = ops.Pb, ops.Qb
        self.Rx[s], self.Ru[s] = ops.Rx, ops.Ru
        self.rex.append(ops.Rex)
        self.index[code] = s
        self.count += 1
        return s


def _flip_limit(n_diodes: int) -> int:
    """1ステップの中で、ダイオードの状態の反転を何回まで試すか。"""
    return max(20, 4 * n_diodes)


def _diode_voltage(system: System, x: np.ndarray, j: int) -> float:
    a, c = int(system.dev_pos[j]), int(system.dev_neg[j])
    return (x[a] if a >= 0 else 0.0) - (x[c] if c >= 0 else 0.0)


def _initial_state(
    system: System, table: _StateTable, sw_code: int, u0: np.ndarray, max_flips: int
) -> tuple[int, np.ndarray]:
    """t = 0 の回路状態と、整合した初期値 x(0)。

    初期条件（コンデンサ電圧、インダクタ電流）を保ったまま、ダイオードの状態を
    「オンなら順方向電流、オフなら逆電圧」になるまで反転して決める。
    """
    n_sw, n_dev = system.n_switches, len(system.devices)
    code = sw_code
    for flips in range(max_flips + 1):
        s = table.slot(code)
        x0 = table.rex[s] @ system.ex0 + table.Ru[s] @ u0
        wrong = []
        for j in range(n_sw, n_dev):
            vd = _diode_voltage(system, x0, j)
            if (vd < 0.0) if (code >> j) & 1 else (vd > DIODE_ON_VOLTAGE):
                wrong.append(j)
        if not wrong:
            return code, x0
        for j in wrong if flips < SIMULTANEOUS_FLIPS else wrong[:1]:
            code ^= 1 << j
    raise CircuitError(
        f"時刻 t=0 でダイオードの状態が {max_flips} 回の反転で落ち着かない。"
        f"最後に試した状態: {describe_state(system, code)}。"
        "初期値（ic）と、ダイオードだけで閉じたループが無いかを確認する"
    )


def _simulate_switched(
    netlist: Netlist,
    t_end: float,
    h: float | None,
    gates: Sequence[Pwm],
    controllers: Sequence[Controller],
    steps_per_period: int | None,
    damping_steps: int,
) -> Result:
    if damping_steps < 1:
        raise ValueError(
            "damping_steps は 1 以上にする（台形則だけでは、オフ抵抗を通るインダクタ電流が減衰しない）"
        )
    notes: list[str] = []

    # --- ゲート信号と時間刻み -------------------------------------------------
    gate_names = [g.name for g in gates]
    dup = sorted({n for n in gate_names if gate_names.count(n) > 1})
    if dup:
        raise ValueError(f"ゲート信号の名前が重複している: {dup}")
    for c in netlist.components:
        if c.type == "S" and c.gate not in gate_names:
            raise ValueError(
                f"{c.id}: ゲート信号 '{c.gate}' が渡されていない。渡された名前: {gate_names}"
            )
    if h is None:
        if not gates:
            raise ValueError("ゲート信号（gates）が無いときは、時間刻み h を指定する")
        n_div = DEFAULT_STEPS_PER_PERIOD if steps_per_period is None else steps_per_period
        if n_div < 2:
            raise ValueError("steps_per_period は 2 以上にする")
        h = 1.0 / (max(g.frequency for g in gates) * n_div)
    elif steps_per_period is not None:
        raise ValueError("h と steps_per_period は、どちらか一方だけを指定する")
    n_steps = int(round(t_end / h))
    if n_steps < 1:
        raise ValueError("t_end が h より短い")
    gate_steps = [_whole_steps(1.0 / g.frequency, h, f"ゲート信号 '{g.name}' の周期") for g in gates]
    for g, n_div in zip(gates, gate_steps, strict=True):
        if n_div < 2:
            raise ValueError(f"ゲート信号 '{g.name}': 1周期が1ステップしか無い。h を小さくする")

    # --- 回路 -----------------------------------------------------------------
    system = build_system(netlist)
    n = len(system.unknowns)
    n_sw, n_dev = system.n_switches, len(system.devices)
    switch_gate = [gate_names.index(netlist.component(cid).gate) for cid in system.devices[:n_sw]]
    u_start, u_end, event = _sample_sources(system, n_steps, h, notes)
    u_mid = 0.5 * (u_start + u_end)

    # --- 制御器と指令 ---------------------------------------------------------
    cmd_names: list[str] = []
    ctrl_steps: list[int] = []
    ctrl_inputs: list[list[int]] = []
    ctrl_outputs: list[list[int]] = []
    for c in controllers:
        if not isinstance(c, Controller):
            raise TypeError(
                f"制御器 {type(c).__name__} に period / inputs / outputs / reset() / step() のどれかが無い"
            )
        ctrl_steps.append(_whole_steps(float(c.period), h, "制御周期"))
        missing = [name for name in c.inputs if name not in system.unknowns]
        if missing:
            raise ValueError(f"制御器が測る信号 {missing} が回路に無い。使える名前: {system.unknowns}")
        ctrl_inputs.append([system.unknowns.index(name) for name in c.inputs])
        outs = []
        for name in c.outputs:
            if name in cmd_names:
                raise ValueError(f"指令の名前 '{name}' が重複している")
            outs.append(len(cmd_names))
            cmd_names.append(name)
        ctrl_outputs.append(outs)
    gate_cmd = []     # 各ゲート信号のデューティに使う指令の番号（固定値なら -1）
    for g in gates:
        if isinstance(g.duty, str):
            if g.duty not in cmd_names:
                raise ValueError(
                    f"ゲート信号 '{g.name}': デューティ指令 '{g.duty}' を出す制御器が無い。"
                    f"制御器が出す指令: {cmd_names}"
                )
            gate_cmd.append(cmd_names.index(g.duty))
        else:
            gate_cmd.append(-1)

    def commands(c: Controller, values, t: float, n_out: int) -> np.ndarray:
        """制御器が返した指令を配列にして確かめる。"""
        y = np.atleast_1d(np.asarray(values, dtype=float))
        if y.shape != (n_out,):
            raise ValueError(f"制御器が返した指令の数が outputs（{list(c.outputs)}）と合わない: {values!r}")
        if not np.isfinite(y).all():
            raise ValueError(f"時刻 t={t:.6g} s で制御器の指令 {list(c.outputs)} が有限値でない: {y}")
        return y

    active = np.zeros(len(cmd_names))      # いま使っている指令
    pending = np.zeros(len(cmd_names))     # 次の制御周期の頭から使う指令
    for c, outs in zip(controllers, ctrl_outputs, strict=True):
        active[outs] = commands(c, c.reset(), 0.0, len(outs))
    pending[:] = active

    # --- 記録 -----------------------------------------------------------------
    out = np.empty((n_steps + 1, n))
    code_out = np.zeros(n_steps + 1, dtype=np.int64)
    sw_code = np.zeros(n_steps + 1, dtype=np.int64)
    gate_out = np.zeros((n_steps + 1, len(gates)), dtype=bool)
    cmd_out = np.zeros((n_steps + 1, len(cmd_names)))

    def fill(k0: int, k1: int) -> None:
        """区間 [k0, k1) のゲート信号・指令・スイッチのビットを、いまの指令で埋める。"""
        k = np.arange(k0, k1)
        cmd_out[k0:k1] = active
        for g, (pwm, n_div) in enumerate(zip(gates, gate_steps, strict=True)):
            duty = pwm.duty if gate_cmd[g] < 0 else float(active[gate_cmd[g]])
            gate_out[k0:k1, g] = pwm.pattern(k, n_div, duty)
        code = np.zeros(k1 - k0, dtype=np.int64)
        for j, g in enumerate(switch_gate):
            code |= gate_out[k0:k1, g].astype(np.int64) << j
        sw_code[k0:k1] = code

    # --- 初期値 ---------------------------------------------------------------
    table = _StateTable(system, h)
    max_flips = _flip_limit(n_dev - n_sw)
    fill(0, 1)
    code0, x = _initial_state(system, table, int(sw_code[0]), u_start[0], max_flips)
    out[0] = x
    ctl = np.zeros(kernel.CTL_SIZE, dtype=np.int64)
    ctl[kernel.CTL_STATE] = code0
    ctl[kernel.CTL_DAMP] = damping_steps      # 計算の頭も切り替え直後として扱う
    d_pos = np.ascontiguousarray(system.dev_pos[n_sw:])
    d_neg = np.ascontiguousarray(system.dev_neg[n_sw:])

    # --- 制御周期ごとに区切って核を呼ぶ ---------------------------------------
    k0 = 0
    while k0 < n_steps:
        k1 = min([n_steps] + [(k0 // m + 1) * m for m in ctrl_steps])
        fill(k0, k1)
        while True:
            status = kernel.run_switched(
                table.P, table.Q, table.Pb, table.Qb, table.Rx, table.Ru, table.code, table.count,
                d_pos, d_neg, n_sw,
                u_start, u_mid, u_end, event, sw_code,
                x, ctl, k1,
                damping_steps, SIMULTANEOUS_FLIPS, max_flips, DIODE_ON_VOLTAGE,
                out, code_out,
            )
            if status == kernel.DONE:
                break
            code = int(ctl[kernel.CTL_CODE])
            if status == kernel.NEED_STATE:
                table.slot(code)       # 行列を用意して、同じステップからやり直す
                continue
            k = int(ctl[kernel.CTL_K])
            raise CircuitError(
                f"時刻 t={k * h:.6g} s（ステップ {k}）でダイオードの状態が {max_flips} 回の反転で"
                f"落ち着かない。最後に試した状態: {describe_state(system, code)}。"
                "ダイオードだけで閉じたループが無いか、r_on と r_off が極端でないかを確認する"
            )
        # k0 の測定値で演算する。結果は次の制御周期の頭から使う
        for c, m, ins, outs in zip(controllers, ctrl_steps, ctrl_inputs, ctrl_outputs, strict=True):
            if k0 % m == 0:
                pending[outs] = commands(c, c.step(k0 * h, out[k0, ins].copy()), k0 * h, len(outs))
        for m, outs in zip(ctrl_steps, ctrl_outputs, strict=True):
            if k1 % m == 0:
                active[outs] = pending[outs]
        k0 = k1
    # 最後の点から始まる区間は無いので、区間ごとの記録は最後の区間の値を繰り返す
    gate_out[n_steps] = gate_out[n_steps - 1]
    cmd_out[n_steps] = cmd_out[n_steps - 1]
    code_out[n_steps] = code_out[n_steps - 1]

    # --- 結果 -----------------------------------------------------------------
    signals: dict[str, np.ndarray] = {}
    for g, name in enumerate(gate_names):
        signals[f"gate({name})"] = gate_out[:, g].astype(float)
    for j, name in enumerate(cmd_names):
        signals[f"cmd({name})"] = cmd_out[:, j].copy()
    for j, cid in enumerate(system.devices):
        signals[f"on({cid})"] = ((code_out >> j) & 1).astype(float)
    sample_state = np.empty(n_steps + 1, dtype=np.int64)
    sample_state[0] = code0
    sample_state[1:] = code_out[:n_steps]

    return Result(
        t=h * np.arange(n_steps + 1),
        x=out,
        unknowns=system.unknowns,
        netlist=netlist,
        h=h,
        notes=notes,
        signals=signals,
        devices=list(system.devices),
        sample_state=sample_state,
        stats={
            "states": table.count,
            "state_changes": int(ctl[kernel.CTL_CHANGES]),
            "retries": int(ctl[kernel.CTL_RETRIES]),
        },
    )
