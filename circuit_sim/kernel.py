"""時間ステップの核。

決まり: run() と run_switched() の中では配列と数値だけを扱う（辞書やオブジェクトを持ち込まない）。
こうしておけば、後から Numba や C / C++ / Rust に載せ替えられる。
回路の解釈（ネットリスト → 行列）は核の外（mna.py）でやる。

run()          : スイッチの無い回路。台形則だけで進める。
run_switched() : スイッチとダイオードを含む回路。回路状態ごとの行列を使い回し、
                 用意していない状態に出会ったら呼び出し側に戻る。制御器を呼ぶ区切りでも戻る。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.linalg import LinAlgWarning, lu_factor, lu_solve, null_space

from .mna import CircuitError


@dataclass
class Operators:
    """1つの回路状態（スイッチ状態）に対して一度だけ計算して使い回す行列。"""

    P: np.ndarray    # 台形則: x[k+1] = P x[k] + Q * (u[k] + u[k+1]) / 2
    Q: np.ndarray
    Rx: np.ndarray   # 不連続点での整合: x(t+) = Rx x(t-) + Ru u(t+)
    Ru: np.ndarray
    Rex: np.ndarray  # 初期化: x(0) = Rex (E x(0)) + Ru u(0)
    Pb: np.ndarray | None = None   # 後退オイラー: x[k+1] = Pb x[k] + Qb u[k+1]
    Qb: np.ndarray | None = None


def _row_scale(A: np.ndarray) -> np.ndarray:
    m = np.abs(A).max(axis=1) if A.size else np.zeros(A.shape[0])
    return np.where(m > 0, 1.0 / np.where(m > 0, m, 1.0), 1.0)


def prepare(
    E: np.ndarray, G: np.ndarray, B: np.ndarray, h: float, backward_euler: bool = False
) -> Operators:
    """1つの回路状態の行列を用意する。backward_euler=True なら後退オイラーの分も作る。"""
    n = E.shape[0]

    # --- 整合の取れた初期値・不連続点の処理 -----------------------------------
    # 状態（コンデンサ電圧・インダクタ電流）は E x で決まる。残りの変数は、
    # 方程式のうち微分を含まない部分  W^T G x = W^T B u  （W は E の左零空間）で決まる。
    scale = np.abs(E).max()
    W = np.eye(n) if scale == 0 else null_space((E / scale).T)
    A_alg = W.T @ G
    B_alg = W.T @ B
    dE, dA = _row_scale(E), _row_scale(A_alg)
    S = np.vstack([dE[:, None] * E, dA[:, None] * A_alg])
    if np.linalg.matrix_rank(S) < n:
        raise CircuitError(
            "回路が一意に解けない。基準節点から浮いた部分、電圧源だけのループ、"
            "電圧源とコンデンサの直結、電流源とインダクタの直列などが無いか確認する"
        )
    S_pinv = np.linalg.pinv(S)
    Rex = S_pinv[:, :n] * dE[None, :]
    Ru = (S_pinv[:, n:] * dA[None, :]) @ B_alg
    Rx = Rex @ E

    # --- 台形則 ---------------------------------------------------------------
    M = E / h + G / 2.0
    with warnings.catch_warnings():
        warnings.simplefilter("error", LinAlgWarning)
        try:
            lu = lu_factor(M)
            P = lu_solve(lu, E / h - G / 2.0)
            Q = lu_solve(lu, B)
        except (LinAlgWarning, np.linalg.LinAlgError, ValueError) as exc:
            raise CircuitError(f"時間刻み h={h} で行列が解けない: {exc}") from exc
    if not (np.isfinite(P).all() and np.isfinite(Q).all()):
        raise CircuitError(f"時間刻み h={h} で行列が解けない")

    ops = Operators(P=P, Q=Q, Rx=Rx, Ru=Ru, Rex=Rex)
    if not backward_euler:
        return ops

    # --- 後退オイラー（切り替え直後に硬い成分を減衰させるために使う） ---------
    with warnings.catch_warnings():
        warnings.simplefilter("error", LinAlgWarning)
        try:
            lu = lu_factor(E / h + G)
            Pb = lu_solve(lu, E / h)
            Qb = lu_solve(lu, B)
        except (LinAlgWarning, np.linalg.LinAlgError, ValueError) as exc:
            raise CircuitError(f"時間刻み h={h} で行列が解けない: {exc}") from exc
    if not (np.isfinite(Pb).all() and np.isfinite(Qb).all()):
        raise CircuitError(f"時間刻み h={h} で行列が解けない")
    ops.Pb, ops.Qb = Pb, Qb
    return ops


def run(
    P: np.ndarray,
    Q: np.ndarray,
    Rx: np.ndarray,
    Ru: np.ndarray,
    x0: np.ndarray,
    u_start: np.ndarray,
    u_end: np.ndarray,
    event: np.ndarray,
    out: np.ndarray,
) -> None:
    """固定刻みの台形則で n_steps 進め、結果を out に書く。

    u_start[k] : 時刻 t[k]   での電源値（不連続点では切り替わった後の値）
    u_end[k]   : 時刻 t[k+1] での電源値（不連続点では切り替わる前の値）
    event[k]   : t[k] で電源が不連続に変わるなら True。状態を保ったまま他の変数を合わせ直す
    out        : (n_steps + 1, n)。out[k] は t[k] の値（不連続点では切り替わった後の値）
    """
    n_steps = u_start.shape[0]
    x = x0.copy()
    out[0] = x
    for k in range(n_steps):
        if event[k]:
            x = Rx @ x + Ru @ u_start[k]
            out[k] = x
        x = P @ x + Q @ (0.5 * (u_start[k] + u_end[k]))
        out[k + 1] = x


# ---------------------------------------------------------------- スイッチング
# run_switched() の戻り値
DONE = 0           # k_stop まで進んだ
NEED_STATE = 1     # 行列を用意していない回路状態に出会った（符号は ctl[CTL_CODE]）
NOT_SETTLED = 2    # ダイオードの状態が規定回数で落ち着かない（最後に試した符号は ctl[CTL_CODE]）

# ctl（再開に必要な進行状態）の並び
CTL_K = 0          # 次に計算するステップの番号
CTL_STATE = 1      # 直前の区間の回路状態の符号（x と整合している状態）
CTL_DAMP = 2       # 後退オイラーで進める残りステップ数
CTL_CODE = 3       # NEED_STATE / NOT_SETTLED のときの符号
CTL_CHANGES = 4    # 集計: 不連続点として扱ったステップの数
CTL_RETRIES = 5    # 集計: 捨てた試行の数
CTL_SIZE = 6


def run_switched(
    P: np.ndarray,
    Q: np.ndarray,
    Pb: np.ndarray,
    Qb: np.ndarray,
    Rx: np.ndarray,
    Ru: np.ndarray,
    slot_code: np.ndarray,
    n_slots: int,
    d_pos: np.ndarray,
    d_neg: np.ndarray,
    n_sw: int,
    u_start: np.ndarray,
    u_mid: np.ndarray,
    u_end: np.ndarray,
    event: np.ndarray,
    sw_code: np.ndarray,
    x: np.ndarray,
    ctl: np.ndarray,
    k_stop: int,
    n_damp: int,
    n_simul: int,
    max_flips: int,
    v_on: float,
    out: np.ndarray,
    code_out: np.ndarray,
) -> int:
    """スイッチとダイオードを含む回路を、固定刻みで ctl[CTL_K] から k_stop まで進める。

    回路状態は整数の符号で表す（下位 n_sw ビットがスイッチ、その上がダイオード。1 = オン）。
    1ステップの手順:
      1. スイッチのビットを sw_code[k] から、ダイオードのビットを直前の区間から取り、候補にする。
      2. 候補の状態で1ステップ試す。ステップの終わりで、オンのダイオードの電圧（= 電流 × r_on）が
         負、またはオフのダイオードの電圧が v_on を超えていたら、反転してやり直す。
         最初の n_simul 回は違反したものを同時に反転し、その後は番号の小さい1つだけを反転する。
      3. 状態が直前の区間と変わったステップ（と電源の不連続点）から n_damp ステップ（1以上）は
         後退オイラー、それ以外は台形則で進める。後退オイラーは状態量（E x）だけを引き継ぐので、
         コンデンサ電圧とインダクタ電流は切り替えの前後で連続になる。台形則のままだと、
         オフ抵抗を通るインダクタ電流のような硬い成分が、符号を変えながら残る。

    行列（スロットごと）
      P, Q   : 台形則        x[k+1] = P x[k] + Q u_mid[k]
      Pb, Qb : 後退オイラー  x[k+1] = Pb x[k] + Qb u_end[k]
      Rx, Ru : 整合          x(t+) = Rx x(t-) + Ru u_start[k]
      slot_code[:n_slots] : 各スロットの回路状態の符号
    入力（時間格子上の配列）
      u_start[k], u_end[k] : 区間 [t[k], t[k+1]] の両端の電源値。u_mid[k] はその平均
      event[k]   : t[k] で電源が不連続に変わるなら True
      sw_code[k] : 区間 [t[k], t[k+1]) のスイッチのビット
      d_pos, d_neg : ダイオードのアノード・カソードの未知数の番号（基準節点は -1）
    進行状態（書き換える）
      x   : t[ctl[CTL_K]] の値
      ctl : 上の CTL_* の並び
    記録
      out[k]      : t[k] の値。スイッチやダイオードが t[k] で切り替わる場合は切り替わる前の値、
                    電源が t[k] で不連続に変わる場合は変わった後の値（回路状態は直前のまま）
      code_out[k] : 区間 [t[k], t[k+1]) の回路状態の符号

    戻り値は DONE / NEED_STATE / NOT_SETTLED。NEED_STATE のときは、呼び出し側が
    ctl[CTL_CODE] の状態の行列を足して、同じ引数で呼び直す（同じステップからやり直す）。
    """
    n_d = d_pos.shape[0]
    d_mask = ((1 << n_d) - 1) << n_sw

    k = int(ctl[CTL_K])
    code_prev = int(ctl[CTL_STATE])
    damp = int(ctl[CTL_DAMP])
    n_changes = 0
    n_retries = 0

    s_prev = -1
    for j in range(n_slots):
        if slot_code[j] == code_prev:
            s_prev = j
            break
    if s_prev < 0:
        ctl[CTL_CODE] = code_prev
        return NEED_STATE
    P_s, Q_s = P[s_prev], Q[s_prev]
    sw_prev = code_prev & ~d_mask
    xc = x.copy()    # t[k] の値

    status = DONE
    while k < k_stop:
        sw = sw_code[k]
        if damp == 0 and sw == sw_prev and not event[k]:
            # --- 通常のステップ: 状態は直前と同じ、台形則 ---
            xn = P_s @ xc + Q_s @ u_mid[k]
            ok = True
            for j in range(n_d):
                a = d_pos[j]
                c = d_neg[j]
                vd = (xn[a] if a >= 0 else 0.0) - (xn[c] if c >= 0 else 0.0)
                if (code_prev >> (n_sw + j)) & 1:
                    if vd < 0.0:
                        ok = False
                        break
                elif vd > v_on:
                    ok = False
                    break
            if ok:
                xc = xn
                code_out[k] = code_prev
                k += 1
                out[k] = xn
                continue

        # --- 状態が変わる（かもしれない）ステップ ---
        xk = xc
        disc = False
        if event[k]:
            # 電源の不連続: 回路状態は直前のまま、状態量を保って残りの変数を合わせ直す
            xk = Rx[s_prev] @ xc + Ru[s_prev] @ u_start[k]
            disc = True
        cand = int(sw) | (code_prev & d_mask)
        if cand != code_prev:
            disc = True
        be = disc or damp > 0
        flips = 0
        s = s_prev
        while True:
            if cand == code_prev:
                s = s_prev
            else:
                s = -1
                for j in range(n_slots):
                    if slot_code[j] == cand:
                        s = j
                        break
                if s < 0:
                    ctl[CTL_CODE] = cand
                    status = NEED_STATE
                    break
            if be:
                xn = Pb[s] @ xk + Qb[s] @ u_end[k]
            else:
                xn = P[s] @ xk + Q[s] @ u_mid[k]

            flip_all = 0
            flip_first = 0
            for j in range(n_d):
                a = d_pos[j]
                c = d_neg[j]
                vd = (xn[a] if a >= 0 else 0.0) - (xn[c] if c >= 0 else 0.0)
                bit = 1 << (n_sw + j)
                if cand & bit:
                    wrong = vd < 0.0
                else:
                    wrong = vd > v_on
                if wrong:
                    flip_all |= bit
                    if flip_first == 0:
                        flip_first = bit
            if flip_all == 0:
                break
            flips += 1
            if flips > max_flips:
                ctl[CTL_CODE] = cand
                status = NOT_SETTLED
                break
            cand ^= flip_all if flips <= n_simul else flip_first
            disc = True
            be = True
        if status != DONE:
            break

        # --- 採用 ---
        n_retries += flips
        if event[k]:
            out[k] = xk
        if disc:
            n_changes += 1
            damp = n_damp
        if damp > 0:
            damp -= 1
        if cand != code_prev:
            code_prev = cand
            s_prev = s
            P_s, Q_s = P[s], Q[s]
            sw_prev = cand & ~d_mask
        xc = xn
        code_out[k] = cand
        k += 1
        out[k] = xn

    x[:] = xc
    ctl[CTL_K] = k
    ctl[CTL_STATE] = code_prev
    ctl[CTL_DAMP] = damp
    ctl[CTL_CHANGES] += n_changes
    ctl[CTL_RETRIES] += n_retries
    return status
