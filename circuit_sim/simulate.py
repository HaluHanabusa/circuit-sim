"""ネットリストを時間応答に変換する入口。

基本は台形則。計算の先頭と不連続点の直後だけ、刻みより速い成分を消す減衰ステップで進める。
スイッチ（S）やダイオード（D）を含む回路は、回路状態ごとの行列を使い回しながら進め、
制御周期ごとに核から戻って制御器を呼ぶ。
大きすぎる計算は配列を確保する前に断り、刻みが粗いときなどは結果の notes で知らせる。
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
DEFAULT_DAMPING_STEPS = 2        # 計算の先頭と不連続点の直後に入れる減衰ステップの数の既定値
MAX_STEPS = 20_000_000           # ステップ数の上限の既定値（スコープが扱う点数の目安と同じ 2000万）
MAX_RESULT_BYTES = 2 * 2**30     # 計算に使う配列（結果と作業用）の大きさの上限 [バイト]。8 GB の PC で、
#                                  画面とブラウザの分を残して1回の計算に使ってよい目安。
#                                  見積もりは _memory_bytes
MIN_POINTS_PER_PERIOD = 10       # 正弦波や回路の振動の1周期に、これより点が少ないと注意を出す
MAX_STEP_PER_TIME_CONSTANT = 2.0  # 刻みが回路の時定数のこの倍数を超えると注意を出す（台形則が振動し始める境）
DAMP_STEP_PER_TIME_CONSTANT = 0.5  # スイッチの無い回路で、刻みが時定数のこの倍数を超えると
#                                    減衰ステップを入れる。
#                                    台形則だけの誤差は RC のステップ応答で h = τ/2 のとき 0.79%、τ で 3.5%、
#                                    1.99τ で 13%（E1a-A.md）。減衰ステップを入れると τ/2〜2τ で 0.5% 以下
DIODE_ON_VOLTAGE = 1e-9          # オフのダイオードをオンにする順方向電圧 [V]（丸め誤差での反転を防ぐ）
DIODE_BAND_REL = 1e-8            # t = 0 の状態が落ち着かないとき、丸め誤差とみなす電圧の幅
#                                  （節点電圧の最大値に対する比。_diode_band）。三相整流（r_on 1 mΩ、
#                                  r_off 1 MΩ）の t=0 で測った丸め誤差は最大 6.5e-10 倍で、その約15倍。
#                                  通常の判定には使わない
SIMULTANEOUS_FLIPS = 4           # ダイオードの状態決定で、違反したものを同時に反転する回数
_GRID_TOL = 1e-6                 # 周期が時間刻みの整数倍かどうかの判定（ステップ数に対する相対値）


class StepLimitError(ValueError):
    """計算が大きすぎて、計算の前に断ったときのエラー。

    文面は「理由と直し方（h と t_end）」＋「simulate() の max_steps で上限を上げられること」。
    without_api_hint は前半だけ（max_steps を渡せない仕様JSONやコマンドから使うとき）。
    """

    def __init__(self, reason: str, api_hint: str):
        super().__init__(reason + api_hint)
        self.without_api_hint = reason


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
    # stats の欄: states（用意した回路状態の数）、state_changes（不連続点として扱ったステップ）、
    #   retries（捨てた試行）、rescues_at_start（t = 0 でダイオードの状態が落ち着かず、違反が丸め誤差の
    #   幅の中の状態を採った（救済した）なら 1。_initial_state を参照）

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
    """電源を時間格子上の配列にする。戻り値: u_start, u_end, event, u_last

    u_last は、最後の格子点でちょうど電源がステップするときの、ステップした後の値（無ければ None）。
    """
    m = len(system.sources)
    t = h * np.arange(n_steps + 1)
    u_start = np.zeros((n_steps, m))
    u_end = np.zeros((n_steps, m))
    event = np.zeros(n_steps, dtype=bool)
    last_step: dict[int, float] = {}       # 最後の格子点でステップする電源の、ステップした後の値

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
            points = 1.0 / (w.frequency * h)
            if points < MIN_POINTS_PER_PERIOD:
                notes.append(
                    f"{cid}: 正弦波の1周期（{1.0 / w.frequency:.6g} s）に時間刻みが {points:.3g} 点しか無い。"
                    "波形が折り返して、別の周波数や振幅に見える。"
                    f"h を {1.0 / (20 * w.frequency):.3g} s 以下（1周期 20 点以上）にする"
                )
        elif isinstance(w, Step):
            ks = int(round(w.t_step / h))
            if abs(ks * h - w.t_step) > 1e-9 * h:
                notes.append(f"{cid}: ステップ時刻 {w.t_step} を格子点 {ks * h} に丸めた")
            k = np.arange(n_steps)
            u_start[:, j] = np.where(k >= ks, w.final, w.initial)       # t[k] の右側の値
            u_end[:, j] = np.where(k + 1 > ks, w.final, w.initial)      # t[k+1] の左側の値
            if 0 < ks < n_steps and w.final != w.initial:
                event[ks] = True
            if ks == n_steps and w.final != w.initial:
                last_step[j] = w.final
        else:  # pragma: no cover
            raise CircuitError(f"{cid}: 未対応の波形 {type(w).__name__}")
    u_last = None
    if last_step:
        u_last = u_end[-1].copy()          # ほかの電源は、最後の区間の終わりの値のまま
        for j, value in last_step.items():
            u_last[j] = value
    return u_start, u_end, event, u_last


def _count_steps(t_end: float, h: float, max_steps: int | None, notes: list[str]) -> int:
    """ステップ数。配列を確保する前に、大きすぎる計算を理由つきで断る。"""
    limit = MAX_STEPS if max_steps is None else max_steps
    ratio = t_end / h
    if not math.isfinite(ratio) or ratio > limit + 0.5:
        raise StepLimitError(
            f"ステップ数が約 {ratio:.3g}（t_end / h = {t_end:.6g} s / {h:.6g} s）になり、"
            f"上限の {limit} を超える。"
            "時間刻み h を大きくするか、計算時間 t_end を短くする",
            "。この大きさが本当に必要なら simulate(..., max_steps=...) で上限を上げる"
            "（記憶量は、1ステップあたり 8 バイト × 未知数の数の数倍）",
        )
    n_steps = int(round(ratio))
    if n_steps < 1:
        raise ValueError("t_end が h より短い")
    if abs(ratio - n_steps) > 1e-6:
        given, used = _distinct(t_end, n_steps * h)
        notes.append(
            f"計算時間 t_end={given} s は時間刻み h={h:.6g} s の整数倍でないので、"
            f"{used} s（{n_steps} ステップ）で終える"
        )
    return n_steps


def _distinct(a: float, b: float) -> tuple[str, str]:
    """2つの値を、違いが見える桁数で書く（6桁で同じに見えるときは桁を増やす）。

    a（利用者が指定した値）は、10桁までで書けるならその桁数まで書く（0.4999995 を 0.499999 と書かない）。
    """
    for digits in range(6, 18):
        x, y = f"{a:.{digits}g}", f"{b:.{digits}g}"
        if x != y and (float(x) == a or digits >= 10):
            return x, y
    return repr(a), repr(b)


FIXED_BYTES = 2**20      # ステップ数によらない分（回路状態ごとの行列など。小さな回路の実測は 0.1 MB 未満）


def _memory_bytes(
    n_steps: int, n_unknowns: int, n_sources: int, *,
    switched: bool = False, n_gates: int = 0, n_commands: int = 0, n_devices: int = 0,
) -> tuple[int, int]:
    """計算に使う配列の大きさの見積もり [バイト]。戻り値: （全体、そのうち結果の回路の変数 x）。

    1ステップあたり（8 バイト = float64 / int64、1 バイト = bool）:
      共通       : x 8·未知数、電源の値 8·電源数 × 2（区間の両端）、不連続の印 1、時刻 8、
                   電源の波形や時刻を作る一時配列 8。ほかに、ステップ数によらない分 FIXED_BYTES
      スイッチあり: 電源の値の平均 8·電源数、回路状態の記録 8 × 3（区間ごと、スイッチのビット、各時刻）、
                   ゲート 1 + 8（bool と、結果に返す値）× ゲート数、指令 8 × 2 × 指令数、
                   導通の信号 8 × 素子数、その計算の一時配列 8 × 2
    実測（tracemalloc の最大値、40万ステップ）: 基準の降圧回路で 138 バイト（見積もり 146）、
    直列RLC（スイッチなし）で 73 バイト（見積もり 73）。結果の x に対して 3.5 倍と 1.8 倍。
    """
    per_step = 8 * n_unknowns + 16 * n_sources + 1 + 8 + 8
    if switched:
        per_step += 8 * n_sources + 24 + 9 * n_gates + 16 * n_commands + 8 * n_devices + 16
    return (n_steps + 1) * per_step + FIXED_BYTES, (n_steps + 1) * 8 * n_unknowns


def _check_result_size(
    n_steps: int, n_unknowns: int, max_steps: int | None, n_sources: int = 0, **switching
) -> None:
    """結果と作業用の配列が大きくなりすぎないか（ステップ数の上限を自分で指定したときは見ない）。"""
    total, result = _memory_bytes(n_steps, n_unknowns, n_sources, **switching)
    if max_steps is None and total > MAX_RESULT_BYTES:
        raise StepLimitError(
            f"結果の配列と計算の作業用の配列が約 {total / 2**30:.1f} GB"
            f"（{n_steps} ステップ × 未知数 {n_unknowns} 個。"
            f"結果の配列はそのうち {result / 2**30:.1f} GB）になり、"
            f"上限の {MAX_RESULT_BYTES / 2**30:.0f} GB を超える。"
            "h を大きくするか t_end を短くする",
            "。この大きさが本当に必要なら simulate(..., max_steps=...) でステップ数の上限を指定する",
        )


def _has_unresolved_rate(rates: np.ndarray, h: float) -> bool:
    """台形則だけでは誤差が大きくなる速い成分があるか（|λ|·h > DAMP_STEP_PER_TIME_CONSTANT）。

    |λ|·h > 2 では、その成分が符号を変えながら残る。0.5〜2 では振動はしないが、計算の先頭や
    電源のステップの直後の誤差が 1% を超える。
    """
    rates = rates[np.isfinite(rates)]
    return bool(rates.size) and float(np.abs(rates).max()) * h > DAMP_STEP_PER_TIME_CONSTANT


def _resolution_notes(
    rates: np.ndarray, h: float, notes: list[str], damped: bool = True, *,
    n_steps: int | None = None, limit: int = MAX_STEPS, who=None,
) -> None:
    """時間刻みが回路の速い成分を解像していなければ、何に対して粗いかを注意に書く。

    damped : 計算の先頭と不連続点の直後に減衰ステップを入れているか（damping_steps > 0）。
    n_steps, limit : 計算のステップ数と上限。勧める刻みでのステップ数が上限を超えるなら、刻みを
             細かくする代わりに、その成分を作る素子を挙げて、値を見直すよう書く。
    who    : 固有値 λ → その成分を作る素子の ID の一覧（_mode_elements）。
    """
    rates = rates[np.isfinite(rates)]
    decaying = rates[np.abs(rates.imag) <= np.abs(rates.real)]
    ringing = rates[np.abs(rates.imag) > np.abs(rates.real)]
    if decaying.size:
        k = int(np.argmax(np.abs(decaying)))
        fastest = float(np.abs(decaying[k]))
        if fastest * h > MAX_STEP_PER_TIME_CONSTANT:
            tau = 1.0 / fastest
            head = f"時間刻み h={h:.6g} s は、回路の最も速い時定数 {tau:.3g} s の {h / tau:.3g} 倍で、"
            needed = None if n_steps is None else n_steps * h / (tau / 10)
            if needed is not None and needed > limit:
                ids = who(decaying[k]) if who is not None else []
                parts = f"（{', '.join(ids)} が作る成分）" if ids else ""
                fix = (
                    f"この成分{parts}を見るには h を {tau / 10:.3g} s 以下にする必要があるが、"
                    f"この計算時間では約 {needed:.2g} ステップになり、上限の {limit} を超える。"
                    "この成分を見る必要が無ければ、対処は要らない。見るには"
                    + (f" {', '.join(ids)} の値を見直して" if ids else "")
                    + "時定数を大きくする（インダクタと抵抗なら L / R、コンデンサと抵抗なら R·C）"
                )
            else:
                fix = f"この成分を見るには h を {tau / 10:.3g} s 以下にする"
            if damped:
                notes.append(
                    head + "この成分の変化は波形に現れない（計算の先頭と、電源のステップや"
                    "スイッチの切り替えの直後は、数値振動が出ないように減衰させている）。" + fix
                )
            else:
                notes.append(
                    head + "この成分の変化は波形に現れない。"
                    "減衰ステップを切っている（damping_steps=0）ので、"
                    "計算の先頭と電源のステップの直後に、この成分が符号を変えながら残り、波形が振れる。"
                    f"h を {tau / 10:.3g} s 以下にするか、damping_steps を 1 以上にする"
                )
    if ringing.size:
        omega = float(np.abs(ringing.imag).max())
        points = 2.0 * math.pi / (omega * h)
        if points < MIN_POINTS_PER_PERIOD:
            period = 2.0 * math.pi / omega
            notes.append(
                f"時間刻み h={h:.6g} s では、回路の振動（周期 {period:.3g} s）の"
                f"1周期が {points:.3g} 点しか無い。"
                "振動の周波数と減衰が正しく出ない。"
                f"h を {period / 20:.3g} s 以下（1周期 20 点以上）にする"
            )


MODE_SHARE = 0.1          # 成分のエネルギー（L、C）や損失（抵抗）の最大に対して、
#                           この比以上の素子を「作る素子」とする


def _mode_elements(system: System, G: np.ndarray, Rex: np.ndarray, lam: complex, code: int = 0) -> list[str]:
    """固有値 lam の成分を作る素子の ID（蓄える素子 L・C と、損失の大きい抵抗・スイッチ・ダイオード）。

    状態量 y = E x について y' = −G Rex y なので、−G Rex の固有ベクトル w から、その成分の
    回路の変数 x = Rex w が分かる。インダクタは L·|i|²、コンデンサは C·|v|²、抵抗は |v|² / R
    （スイッチとダイオードは回路状態 code のオン・オフの抵抗）で比べ、それぞれ最大の MODE_SHARE 倍
    以上のものを挙げる。
    """
    values, vectors = np.linalg.eig(-G @ Rex)
    x = Rex @ vectors[:, int(np.argmin(np.abs(values - lam)))]
    netlist = system.netlist
    idx = {name: i for i, name in enumerate(system.unknowns)}

    def across(c) -> float:
        a = 0.0 if c.nodes[0] == netlist.ground else x[idx[f"v({c.nodes[0]})"]]
        b = 0.0 if c.nodes[1] == netlist.ground else x[idx[f"v({c.nodes[1]})"]]
        return float(abs(a - b))

    stored, lost = {}, {}
    for c in netlist.components:
        if c.type == "L":
            stored[c.id] = c.value * abs(x[idx[f"i({c.id})"]]) ** 2
        elif c.type == "C":
            stored[c.id] = c.value * across(c) ** 2
        elif c.type == "R":
            lost[c.id] = across(c) ** 2 / c.value
        elif c.type in ("S", "D"):
            on = (code >> system.devices.index(c.id)) & 1
            lost[c.id] = across(c) ** 2 / (c.r_on if on else c.r_off)
    out = []
    for group in (stored, lost):
        top = max(group.values(), default=0.0)
        out += [cid for cid, v in group.items() if top > 0 and v >= MODE_SHARE * top]
    return out


OFF_PROBE_SCALE = 0.1     # オフの素子のコンダクタンスをこの倍（オフ抵抗を10倍）にして、動く成分を見分ける
SAME_RATE_TOL = 0.01      # オフ抵抗を変えても、この相対差より動かない成分を回路そのものの成分とみなす


def _switched_rates(system: System, table: _StateTable, codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """計算で使った回路状態ごとの固有値のうち、回路そのものの成分（オフ抵抗が作る見かけの成分を除く）。

    オフの素子はオフ抵抗 r_off（既定 1 MΩ）の抵抗なので、インダクタの電流がオフの素子しか通れない状態では、
    時定数 L / r_off（数 ps）の成分ができる。これは理想スイッチのモデルの都合で、
    切り替えの直後の後退オイラーと
    減衰ステップが消す。この成分の大きさは r_off に比例するので、r_off を10倍にして同じ大きさ |λ| の
    固有値が（相対 SAME_RATE_TOL 以内で）残るものだけを返す。スイッチを抵抗に置き換えた回路と同じ注意になる。
    戻り値: （固有値、それぞれの回路状態の符号）。
    """
    found, where = [], []
    for code in codes:
        code = int(code)
        G = state_conductance(system, code)
        rates = kernel.natural_rates(G, table.rex[table.index[code]])
        rates = rates[np.isfinite(rates) & (rates != 0)]
        if not rates.size:
            continue
        G_probe = state_conductance(system, code, off_scale=OFF_PROBE_SCALE)
        try:
            Rex_probe, _ = kernel.consistent_maps(system.E, G_probe, system.B)
        except CircuitError:
            found.append(rates)
            where.append(np.full(rates.size, code))
            continue
        # 大きさ |λ| で比べる。オフ抵抗だけで減衰する振動（オフ・オフの区間の L とスイッチ節点の容量など）は、
        # 減衰（実部）は r_off で動くが |λ| = 1/√(LC) は動かない。
        # 見かけの成分 L / r_off は実数で、|λ| ごと動く
        moved = np.abs(kernel.natural_rates(G_probe, Rex_probe))
        same = np.array([np.min(np.abs(moved - abs(lam))) <= SAME_RATE_TOL * abs(lam) for lam in rates])
        found.append(rates[same])
        where.append(np.full(int(same.sum()), code))
    if not found:
        return np.zeros(0, dtype=complex), np.zeros(0, dtype=np.int64)
    return np.concatenate(found), np.concatenate(where)


INTERRUPT_FACTOR = 2.0    # インダクタ電流の遮断とみなす、L·|i| / h と回路の電圧の目安（電源とコンデンサ）の比


def _forced_through_off(system: System, code: int) -> list[int]:
    """回路状態 code で、電流の行き場がオフのスイッチ・ダイオードしか無いインダクタ（素子の並びの番号）。

    インダクタの両端が、そのインダクタ自身とオフの素子と電流源を除いた素子でつながっていなければ、
    電流はオフの素子（r_off）を通るしかない。
    """
    netlist = system.netlist
    on = {cid for j, cid in enumerate(system.devices) if (code >> j) & 1}
    forced = []
    for k, ind in enumerate(netlist.components):
        if ind.type != "L":
            continue
        paths = [
            c.nodes for c in netlist.components
            if c is not ind and c.type != "I" and not (c.type in ("S", "D") and c.id not in on)
        ]
        root = _node_groups(paths)
        if root(ind.nodes[0]) != root(ind.nodes[1]):
            forced.append(k)
    return forced


def _node_groups(edges: list[tuple[str, str]]):
    """節点の組（枝）から、つながった節点を同じ代表に写す関数を作る。"""
    parent: dict[str, str] = {}

    def root(n: str) -> str:
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in edges:
        parent[root(a)] = root(b)
    return root


def _interruption_notes(
    system: System, out: np.ndarray, before: np.ndarray, after: np.ndarray, u: np.ndarray, h: float,
    notes: list[str],
) -> None:
    """インダクタ電流が、行き場の無いまま切り替えで遮断された点があれば注意に書く。

    before[k], after[k]: ステップ k の直前と、ステップ k の回路状態。out[k] は切り替わる前の値。
    遮断されると、電流はオフ抵抗を通って1刻みで消え、オフの素子の両端に L·|Δi| / h の大きさの
    （刻みで決まる、意味の無い）電圧が1点記録される。ダイオードが電流 0 付近で切れる通常の切り替えでは、
    L·|i| / h は回路の電圧の程度に収まる。そこで L·|i| / h が電源とコンデンサの電圧の最大の
    INTERRUPT_FACTOR 倍を超え、かつ行き場がオフの素子しか無いときだけ知らせる。
    """
    netlist = system.netlist
    steps = np.nonzero(before != after)[0]
    if not steps.size:
        return
    idx = {cid: i for i, cid in enumerate(system.unknowns)}

    def volt(node: str) -> np.ndarray | float:
        return 0.0 if node == netlist.ground else out[:, idx[f"v({node})"]]

    v_ref = 0.0
    for j, cid in enumerate(system.sources):
        if netlist.component(cid).type == "V" and u.size:
            v_ref = max(v_ref, float(np.abs(u[:, j]).max()))
    for c in netlist.components:
        if c.type == "C":
            v_ref = max(v_ref, float(np.abs(volt(c.nodes[0]) - volt(c.nodes[1])).max()))
    events = []
    for code in np.unique(after[steps]):
        forced = _forced_through_off(system, int(code))
        if not forced:
            continue
        at = steps[after[steps] == code]
        for k in forced:
            ind = netlist.components[k]
            current = out[at, idx[f"i({ind.id})"]]
            hit = ind.value * np.abs(current) / h > INTERRUPT_FACTOR * v_ref
            events += [(int(step), ind, float(i)) for step, i in zip(at[hit], current[hit], strict=True)]
    if not events:
        return
    events.sort(key=lambda e: e[0])
    step, ind, current = events[0]
    off = [cid for j, cid in enumerate(system.devices) if not (int(after[step]) >> j) & 1]
    k_next = min(step + 1, out.shape[0] - 1)
    peak = max(
        abs(float(np.asarray(volt(c.nodes[0]) - volt(c.nodes[1]))[k_next]))
        for c in netlist.components if c.id in off
    )
    others = f"（ほかに {len(events) - 1} 回）" if len(events) > 1 else ""
    notes.append(
        f"時刻 t={step * h:.6g} s で、インダクタ {ind.id} の電流 {current:.3g} A の行き場が、"
        f"オフのスイッチやダイオード（{', '.join(off)}）しか無くなり、電流が1刻みで消えた{others}。"
        f"その直後の点のオフの素子の電圧（最大 {peak:.3g} V）は刻みの大きさで決まる見かけの値で、"
        "実際の回路ではサージ電圧になる。還流ダイオードやスナバを入れる"
        "（還流ダイオードがあっても、逆向きに流れていた電流は遮断される。降圧回路で出力の初期値が入力より高いときなど）"
    )


def _initial_value_notes(system: System, x0: np.ndarray, notes: list[str]) -> None:
    """与えた初期値（ic）と、実際に始めた値が食い違っていれば注意に書く。"""
    netlist = system.netlist
    floor = 1e-9 * max(float(np.abs(x0).max(initial=0.0)), max(abs(c.ic) for c in netlist.components))

    def at(node: str) -> float:
        return 0.0 if node == netlist.ground else float(x0[system.unknowns.index(f"v({node})")])

    for c in netlist.components:
        if c.type == "C":
            actual, unit = at(c.nodes[0]) - at(c.nodes[1]), "V"
        elif c.type == "L":
            actual, unit = float(x0[system.unknowns.index(f"i({c.id})")]), "A"
        else:
            continue
        if abs(actual - c.ic) > 1e-6 * max(abs(actual), abs(c.ic)) + floor:
            notes.append(
                f"{c.id}: 初期値 ic={c.ic:.6g} {unit} は回路のつなぎ方と矛盾するので、"
                f"{actual:.6g} {unit} から始めた"
                "（コンデンサの並列や電圧源との直結、インダクタの直列などで、初期値どうしが食い違っている）"
            )


def _inductor_cutset_hints(netlist: Netlist) -> list[str]:
    """インダクタ（と電流源）だけで回路の残りとつながる部分（2節点以上）を探して、直し方を添える。

    抵抗・コンデンサ・電圧源・スイッチ・ダイオードでつながった節点を1つの部分とみなす。
    基準節点を含まない部分が、インダクタと電流源だけで外とつながっていると、そこへ流れ込む
    インダクタ電流の和が 0 に縛られ、部分全体の電位が式から決まらない（三相の整流器や負荷の
    中性点・直流側を接地しない場合など）。1節点だけの場合は、呼び出し側の文面が扱う。
    """
    nodes = [netlist.ground] + netlist.nodes()
    parent = {n: n for n in nodes}

    def root(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for c in netlist.components:
        if c.type not in ("L", "I"):
            parent[root(c.nodes[0])] = root(c.nodes[1])
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(root(n), []).append(n)
    hints = []
    for members in groups.values():
        if netlist.ground in members or len(members) < 2:
            continue
        links = [c for c in netlist.components if (c.nodes[0] in members) != (c.nodes[1] in members)]
        inductors = [c.id for c in links if c.type == "L"]
        if not inductors:
            continue
        hints.append(
            f"節点 {', '.join(members)} の部分は、{', '.join(c.id for c in links)} だけで"
            "回路の残りとつながっていて、"
            "流れ込む電流の和が 0 に縛られるので、この部分の電位が決まらない。"
            "この部分のどこか1点と基準節点の間に、回路のほかの抵抗より十分に大きな抵抗（例 1 MΩ）を入れる"
            "（小さいと、この抵抗に流れる電流で回路の動きが変わる）。"
            f"この抵抗と {', '.join(inductors)} で、"
            "時定数（インダクタンス / 抵抗）がとても短い成分ができるが、"
            "計算では減衰させるので波形には現れない（そのことは注意に出る）"
        )
    return hints


def _explain_unsolvable(system: System, exc: CircuitError, state: str | None = None) -> CircuitError:
    """解けない回路のエラーに、手がかり（決まらない変数、インダクタだけの節点）を添える。"""
    netlist = system.netlist
    parts = [str(exc)]
    names = [system.unknowns[i] for i in getattr(exc, "undetermined", [])]
    if names:
        parts.append(f"決まらない変数: {', '.join(names)}")
    for node in netlist.nodes():
        attached = [c for c in netlist.components if node in c.nodes]
        if all(c.type in ("L", "I") for c in attached) and any(c.type == "L" for c in attached):
            ids = ", ".join(c.id for c in attached)
            with_source = any(c.type == "I" for c in attached)
            kind = "電流源とインダクタの直列" if with_source else "インダクタどうしの直列"
            parts.append(
                f"節点 '{node}' には {ids} しかつながっていない（{kind}）。"
                "直列のインダクタは1つにまとめる（値は和）"
            )
    cutsets = _inductor_cutset_hints(netlist)
    text = "。".join(parts + cutsets)
    if state is not None:
        text = f"回路状態（{state}）で: {text}"
        if not cutsets:
            text += "。オフ抵抗 r_off が極端に大きいと、オフのときに節点が浮いたのと同じになって解けない"
    return CircuitError(text)


def simulate(
    netlist: Netlist,
    t_end: float,
    h: float | None = None,
    *,
    gates: Sequence[Pwm] = (),
    controllers: Sequence[Controller] = (),
    steps_per_period: int | None = None,
    damping_steps: int = DEFAULT_DAMPING_STEPS,
    max_steps: int | None = None,
) -> Result:
    """ネットリストを固定刻みで計算する。

    基本は台形則。計算の先頭と不連続点（電源のステップ、スイッチやダイオードの切り替え）の
    直後だけ、刻みより速い成分を消す減衰ステップで進める。
    刻みが回路の速い成分や電源の周期を解像していないときは、結果の notes に注意が入る。

      h             : 時間刻み。スイッチの無い回路では必須。
      damping_steps : 計算の先頭と不連続点の直後に入れる減衰ステップの数（既定 2。0 なら入れない）。
                      スイッチの無い回路では、刻みより速い成分があるときだけ入れる。
                      スイッチを含む回路では、状態が変わったステップを後退オイラーで進め、その後に入れる。
      max_steps     : ステップ数の上限（既定 2000万）。超える計算は、配列を確保する前に断る。

    スイッチ（S）やダイオード（D）を含む場合:
      gates            : ゲート信号（PWM）。S の gate から名前で参照する。
      controllers      : 制御器。制御周期ごとに測定して演算し、指令は次の制御周期の頭から使う。
      h                : 省略すると、最も速い搬送波の周期を steps_per_period で割った値。
      steps_per_period : 搬送波1周期の分割数（既定 200）。h と同時には指定しない。
    """
    if not (t_end > 0 and math.isfinite(t_end)) or (h is not None and not (h > 0 and math.isfinite(h))):
        raise ValueError("t_end と h は正の有限値が必要")
    if damping_steps < 0:
        raise ValueError("damping_steps は 0 以上にする")

    switching = any(c.type in ("S", "D") for c in netlist.components)
    if switching or gates or controllers:
        return _simulate_switched(
            netlist, t_end, h, gates, controllers, steps_per_period, damping_steps, max_steps
        )

    if h is None:
        raise ValueError("スイッチの無い回路では時間刻み h が必要")
    if steps_per_period is not None:
        raise ValueError("steps_per_period を使えるのは、ゲート信号（gates）を渡すときだけ")
    return _simulate_linear(netlist, t_end, h, damping_steps, max_steps)


def _simulate_linear(
    netlist: Netlist, t_end: float, h: float, damping_steps: int, max_steps: int | None
) -> Result:
    """スイッチの無い回路。"""
    notes: list[str] = []
    n_steps = _count_steps(t_end, h, max_steps, notes)

    system = build_system(netlist)
    _check_result_size(n_steps, len(system.unknowns), max_steps, len(system.sources))
    u_start, u_end, event, u_last = _sample_sources(system, n_steps, h, notes)

    try:
        ops = kernel.prepare(system.E, system.G, system.B, h)
    except CircuitError as exc:
        raise _explain_unsolvable(system, exc) from exc
    x0 = ops.Rex @ system.ex0 + ops.Ru @ u_start[0]
    _initial_value_notes(system, x0, notes)
    rates = kernel.natural_rates(system.G, ops.Rex)
    _resolution_notes(
        rates, h, notes, damped=damping_steps > 0, n_steps=n_steps,
        limit=MAX_STEPS if max_steps is None else max_steps,
        who=lambda lam: _mode_elements(system, system.G, ops.Rex, lam),
    )
    # 減衰ステップは、刻みが最も速い時定数の半分を超えるときだけ入れる。刻みが回路を解像していれば
    # 台形則だけで進め、2次精度をそのまま保つ。
    n_damp = damping_steps if _has_unresolved_rate(rates, h) else 0

    out = np.empty((n_steps + 1, len(system.unknowns)))
    kernel.run(
        ops.P, ops.Q, ops.Rx, ops.Ru, x0, u_start, u_end, event, out,
        ops.Pd, ops.Qd_start, ops.Qd_end, n_damp,
    )
    if u_last is not None:
        # 最後の格子点でちょうど電源がステップする場合も、その点にはステップした後の値を入れる
        out[n_steps] = ops.Rx @ out[n_steps] + ops.Ru @ u_last

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

    _ARRAYS = ("code", "P", "Q", "Pb", "Qb", "Pd", "Qd_start", "Qd_end", "Rx", "Ru")

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
        self.Pd = np.zeros((capacity, n, n))
        self.Qd_start = np.zeros((capacity, n, m))
        self.Qd_end = np.zeros((capacity, n, m))
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
            raise _explain_unsolvable(self.system, exc, describe_state(self.system, code)) from exc
        if self.count == self.code.shape[0]:
            self._grow()
        s = self.count
        self.code[s] = code
        self.P[s], self.Q[s] = ops.P, ops.Q
        self.Pb[s], self.Qb[s] = ops.Pb, ops.Qb
        self.Pd[s], self.Qd_start[s], self.Qd_end[s] = ops.Pd, ops.Qd_start, ops.Qd_end
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
) -> tuple[int, np.ndarray, bool]:
    """t = 0 の回路状態と、整合した初期値 x(0)、救済したか（下の説明）。

    初期条件（コンデンサ電圧、インダクタ電流）を保ったまま、ダイオードの状態を
    「オンなら順方向電流、オフなら逆電圧」になるまで反転して決める（判定は核と同じ）。
    落ち着かないときは、試した状態のうち違反が最も小さいものを、違反が丸め誤差の幅
    （_diode_band）以内なら採る（救済。stats の rescues_at_start に 1 が入る）。インダクタ電流がすべて 0 の
    整流器のように、電流がちょうど 0 のダイオードは、オンでもオフでも電圧が丸め誤差だけになるため。
    状態は最初のステップで核が決め直す。
    """
    n_sw, n_dev = system.n_switches, len(system.devices)
    n_nodes = len(system.netlist.nodes())
    code = sw_code
    flipped = [0] * n_dev
    last: dict[int, float] = {}
    best: tuple[float, int, np.ndarray] | None = None
    for flips in range(max_flips + 1):
        s = table.slot(code)
        x0 = table.rex[s] @ system.ex0 + table.Ru[s] @ u0
        wrong = []
        for j in range(n_sw, n_dev):
            vd = _diode_voltage(system, x0, j)
            if (vd < 0.0) if (code >> j) & 1 else (vd > DIODE_ON_VOLTAGE):
                wrong.append(j)
                last[j] = vd
        if not wrong:
            return code, x0, False
        violation = max(abs(last[j]) for j in wrong)
        if best is None or violation < best[0]:
            best = (violation, code, x0)
        for j in wrong if flips < SIMULTANEOUS_FLIPS else wrong[:1]:
            code ^= 1 << j
            flipped[j] += 1
    violation, best_code, best_x0 = best
    band = _diode_band(best_x0, n_nodes)
    if violation <= band:
        return best_code, best_x0, True
    raise CircuitError(
        f"時刻 t=0 でダイオードの状態が {max_flips} 回の反転で落ち着かない。"
        f"最後に試した状態: {describe_state(system, code)}。"
        + _unsettled_hint(system, flipped, last, band)
    )


def _diode_band(x: np.ndarray, n_nodes: int) -> float:
    """t = 0 のダイオードの状態が落ち着かないときに、丸め誤差とみなす電圧の幅 [V]。

    幅は max(DIODE_ON_VOLTAGE, DIODE_BAND_REL × 節点電圧の最大の絶対値)。ダイオードの電圧は、
    大きさが節点電圧と同程度の2つの値の差なので、丸め誤差も節点電圧に比例する。インダクタ電流が
    すべて 0 の点では、オンでもオフでも電圧が丸め誤差（初期値の整合の計算で 1e-9 倍程度）だけになり、
    その符号で2つの状態を往復する。
    """
    scale = float(np.abs(x[:n_nodes]).max(initial=0.0))
    return max(DIODE_ON_VOLTAGE, DIODE_BAND_REL * scale)


def _unsettled_hint(
    system: System, flipped: list[int], last: dict[int, float], band: float | None = None
) -> str:
    """ダイオードの状態が決まらないときの、手がかりと直し方。"""
    parts = []
    most = max(flipped, default=0)
    if most > 0:
        detail = ", ".join(
            f"{system.devices[j]}（最後の電圧 {last[j]:.3g} V）"
            for j, n in enumerate(flipped) if n == most and j in last
        )
        parts.append(f"オンとオフを繰り返したダイオード: {detail}")
    ratio = max(
        (float(system.dev_g_on[j] / system.dev_g_off[j])
         for j in range(system.n_switches, len(system.devices))),
        default=0.0,
    )
    band_text = f"（いまは ±{band:.3g} V）" if band is not None else ""
    parts.append(
        "オンにしてもオフにしても、ダイオードの電圧が判定と食い違い、その食い違いが丸め誤差とみなせる幅"
        f"{band_text}より大きい。"
        f"r_off / r_on の比（いまは最大 {ratio:.3g}）が大きいほど丸め誤差が大きくなる。"
        "r_off を小さく（例 1e5 Ω）、または r_on を大きくして試す。"
        "ダイオードどうしが向きをそろえずに並列・直列につながった部分が無いかも確認する"
    )
    return "。".join(parts)


def _simulate_switched(
    netlist: Netlist,
    t_end: float,
    h: float | None,
    gates: Sequence[Pwm],
    controllers: Sequence[Controller],
    steps_per_period: int | None,
    damping_steps: int,
    max_steps: int | None,
) -> Result:
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
    n_steps = _count_steps(t_end, h, max_steps, notes)
    gate_steps = [_whole_steps(1.0 / g.frequency, h, f"ゲート信号 '{g.name}' の周期") for g in gates]
    for g, n_div in zip(gates, gate_steps, strict=True):
        if n_div < 2:
            raise ValueError(f"ゲート信号 '{g.name}': 1周期が1ステップしか無い。h を小さくする")

    # --- 回路 -----------------------------------------------------------------
    system = build_system(netlist)
    n = len(system.unknowns)
    n_sw, n_dev = system.n_switches, len(system.devices)
    switch_gate = [gate_names.index(netlist.component(cid).gate) for cid in system.devices[:n_sw]]
    _check_result_size(
        n_steps, n, max_steps, len(system.sources), switched=True, n_gates=len(gates),
        n_commands=sum(len(c.outputs) for c in controllers if isinstance(c, Controller)), n_devices=n_dev,
    )
    u_start, u_end, event, u_last = _sample_sources(system, n_steps, h, notes)
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

    latched = np.zeros(len(gates), dtype=np.int64)     # いまの搬送波の周期のオンのステップ数
    carried = np.zeros(len(gates))                      # 次の周期に持ち越す丸めの余り

    def fill(k0: int, k1: int) -> None:
        """区間 [k0, k1) のゲート信号・指令・スイッチのビットを、いまの指令で埋める。

        デューティは搬送波の周期の頭で取り込む。区間の途中から始まる周期は、取り込み済みの値を使う。
        """
        k = np.arange(k0, k1)
        cmd_out[k0:k1] = active
        for g, (pwm, n_div) in enumerate(zip(gates, gate_steps, strict=True)):
            duty = pwm.duty if gate_cmd[g] < 0 else float(active[gate_cmd[g]])
            first = -(-k0 // n_div)                     # この区間の中で始まる最初の周期の番号
            n_periods = max(0, -(-k1 // n_div) - first)
            counts, carried[g] = pwm.on_counts(duty, n_div, n_periods, float(carried[g]))
            index = k // n_div - first                  # 負なら、区間の前に始まった周期
            n_on = np.where(index >= 0, counts[np.maximum(index, 0)] if n_periods else 0, latched[g])
            if n_periods:
                latched[g] = counts[-1]
            gate_out[k0:k1, g] = pwm.pattern(k, n_div, n_on)
        code = np.zeros(k1 - k0, dtype=np.int64)
        for j, g in enumerate(switch_gate):
            code |= gate_out[k0:k1, g].astype(np.int64) << j
        sw_code[k0:k1] = code

    # --- 初期値 ---------------------------------------------------------------
    def next_stop(k: int) -> int:
        """次に核から戻るステップ番号（制御器を呼ぶ点、または計算の終わり）。"""
        return min([n_steps] + [(k // m + 1) * m for m in ctrl_steps])

    table = _StateTable(system, h)
    max_flips = _flip_limit(n_dev - n_sw)
    k0, k1 = 0, next_stop(0)
    fill(k0, k1)
    code0, x, rescued_at_start = _initial_state(system, table, int(sw_code[0]), u_start[0], max_flips)
    out[0] = x
    _initial_value_notes(system, x, notes)
    ctl = np.zeros(kernel.CTL_SIZE, dtype=np.int64)
    ctl[kernel.CTL_STATE] = code0
    ctl[kernel.CTL_DAMP] = damping_steps      # 計算の先頭も、減衰ステップから始める
    d_pos = np.ascontiguousarray(system.dev_pos[n_sw:])
    d_neg = np.ascontiguousarray(system.dev_neg[n_sw:])

    # --- 制御周期ごとに区切って核を呼ぶ ---------------------------------------
    while True:
        while True:
            status = kernel.run_switched(
                table.P, table.Q, table.Pb, table.Qb, table.Pd, table.Qd_start, table.Qd_end,
                table.Rx, table.Ru, table.code, table.count,
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
                + _unsettled_hint(system, [], {})
            )
        # k0 の測定値で演算する。結果は次の制御周期の頭から使う
        for c, m, ins, outs in zip(controllers, ctrl_steps, ctrl_inputs, ctrl_outputs, strict=True):
            if k0 % m == 0:
                pending[outs] = commands(c, c.step(k0 * h, out[k0, ins].copy()), k0 * h, len(outs))
        for m, outs in zip(ctrl_steps, ctrl_outputs, strict=True):
            if k1 % m == 0:
                active[outs] = pending[outs]
        if k1 >= n_steps:
            break
        k0, k1 = k1, next_stop(k1)
        fill(k0, k1)
    if u_last is not None:
        # 最後の格子点でちょうど電源がステップする場合も、その点にはステップした後の値を入れる
        s_last = table.slot(int(ctl[kernel.CTL_STATE]))
        out[n_steps] = table.Rx[s_last] @ out[n_steps] + table.Ru[s_last] @ u_last
    # 刻みが回路の速い成分を解像しているか（計算で使った回路状態ごと。スイッチの無い回路と同じ注意）
    used = np.unique(np.concatenate([[code0], code_out[:n_steps]]))
    rates, rate_state = _switched_rates(system, table, used)

    def who(lam: complex) -> list[str]:
        code = int(rate_state[int(np.argmin(np.abs(rates - lam)))])
        G_code, Rex_code = state_conductance(system, code), table.rex[table.index[code]]
        return _mode_elements(system, G_code, Rex_code, lam, code)

    _resolution_notes(
        rates, h, notes, damped=damping_steps > 0, n_steps=n_steps,
        limit=MAX_STEPS if max_steps is None else max_steps, who=who,
    )
    before = np.concatenate([[code0], code_out[: n_steps - 1]])
    _interruption_notes(system, out, before, code_out[:n_steps], u_start, h, notes)
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
            "rescues_at_start": int(rescued_at_start),
        },
    )
