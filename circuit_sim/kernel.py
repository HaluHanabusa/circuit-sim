"""時間ステップの核。

決まり: run() と run_switched() の中では配列と数値だけを扱う（辞書やオブジェクトを持ち込まない）。
こうしておけば、後から Numba や C / C++ / Rust に載せ替えられる。
回路の解釈（ネットリスト → 行列）は核の外（mna.py）でやる。

run()          : スイッチの無い回路。台形則で進め、計算の先頭と電源の不連続点の直後だけ減衰ステップにする。
run_switched() : スイッチとダイオードを含む回路。回路状態ごとの行列を使い回し、
                 用意していない状態に出会ったら呼び出し側に戻る。制御器を呼ぶ区切りでも戻る。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.linalg import LinAlgWarning, lu_factor, lu_solve, null_space

from .messages import Message, Value
from .mna import CircuitError

DAMPING_SUBSTEPS = 1024   # 減衰ステップ1つを、この数の後退オイラーに刻む（2 のべき）


@dataclass
class Operators:
    """1つの回路状態（スイッチ状態）に対して一度だけ計算して使い回す行列。"""

    P: np.ndarray    # 台形則: x[k+1] = P x[k] + Q * (u[k] + u[k+1]) / 2
    Q: np.ndarray
    Rx: np.ndarray   # 不連続点での整合: x(t+) = Rx x(t-) + Ru u(t+)
    Ru: np.ndarray
    Rex: np.ndarray  # 初期化: x(0) = Rex (E x(0)) + Ru u(0)
    Pd: np.ndarray        # 減衰ステップ: x[k+1] = Pd x[k] + Qd_start u_start[k] + Qd_end u_end[k]
    Qd_start: np.ndarray
    Qd_end: np.ndarray
    Pb: np.ndarray | None = None   # 後退オイラー: x[k+1] = Pb x[k] + Qb u[k+1]
    Qb: np.ndarray | None = None


def _row_scale(A: np.ndarray) -> np.ndarray:
    m = np.abs(A).max(axis=1) if A.size else np.zeros(A.shape[0])
    return np.where(m > 0, 1.0 / np.where(m > 0, m, 1.0), 1.0)


def _left_null_space(E: np.ndarray) -> np.ndarray:
    """E の左零空間（微分を含まない式を作る、式の組み合わせ）。

    E は、コンデンサでつながった節点の組と、インダクタ1個ずつのブロックに分かれる。
    零空間はブロックごとに、そのブロックの大きさで正規化して求める。全体を1つの大きさで
    正規化すると、F と H のように桁の違う量が混ざり、小さい方の素子が 0 とみなされる。
    """
    n = E.shape[0]
    linked = (E != 0) | (E.T != 0)
    block_of = np.full(n, -1)
    columns = []
    for first in range(n):
        if block_of[first] >= 0:
            continue
        block_of[first] = first
        members, todo = [first], [first]
        while todo:
            i = todo.pop()
            for j in np.nonzero(linked[i])[0]:
                if block_of[j] < 0:
                    block_of[j] = first
                    members.append(int(j))
                    todo.append(int(j))
        members.sort()
        block = E[np.ix_(members, members)]
        scale = np.abs(block).max()
        basis = np.eye(len(members)) if scale == 0 else null_space((block / scale).T)
        for j in range(basis.shape[1]):
            w = np.zeros(n)
            w[members] = basis[:, j]
            columns.append(w)
    return np.array(columns).T if columns else np.zeros((n, 0))


def _solve_or_raise(M: np.ndarray, rhs: list[np.ndarray], h: float) -> list[np.ndarray]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", LinAlgWarning)
        try:
            lu = lu_factor(M)
            out = [lu_solve(lu, r) for r in rhs]
        except (LinAlgWarning, np.linalg.LinAlgError, ValueError) as exc:
            raise CircuitError(Message("MATRIX_SINGULAR_DETAIL", h=Value(h, "s"), detail=str(exc))) from exc
    if not all(np.isfinite(r).all() for r in out):
        raise CircuitError(Message("MATRIX_SINGULAR", h=Value(h, "s")))
    return out


def consistent_maps(E: np.ndarray, G: np.ndarray, B: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """整合の取れた値を作る行列 Rex, Ru:  x = Rex (E x) + Ru u。

    解けない回路では CircuitError。undetermined 属性に、決まらない未知数の番号が入る。
    """
    n = B.shape[0]
    # 状態（コンデンサ電圧・インダクタ電流）は E x で決まる。残りの変数は、
    # 方程式のうち微分を含まない部分  W^T G x = W^T B u  （W は E の左零空間）で決まる。
    W = _left_null_space(E)
    A_alg = W.T @ G
    B_alg = W.T @ B
    dE, dA = _row_scale(E), _row_scale(A_alg)
    S = np.vstack([dE[:, None] * E, dA[:, None] * A_alg])
    if np.linalg.matrix_rank(S) < n:
        error = CircuitError(Message("CIRCUIT_UNSOLVABLE"))
        # 決まらない方向（S の零空間）に成分を持つ未知数を、原因の手がかりとして添える
        _, sv, vt = np.linalg.svd(S)
        free = vt[np.sum(sv > sv.max() * max(S.shape) * np.finfo(float).eps):]
        weight = np.abs(free).max(axis=0) if free.size else np.zeros(n)
        error.undetermined = [int(i) for i in np.nonzero(weight > 0.1 * weight.max())[0]] if free.size else []
        raise error
    S_pinv = np.linalg.pinv(S)
    Rex = S_pinv[:, :n] * dE[None, :]
    Ru = (S_pinv[:, n:] * dA[None, :]) @ B_alg
    return Rex, Ru


def prepare(
    E: np.ndarray, G: np.ndarray, B: np.ndarray, h: float, backward_euler: bool = False
) -> Operators:
    """1つの回路状態の行列を用意する。backward_euler=True なら後退オイラーの分も作る。

    解けない回路では CircuitError。undetermined 属性に、決まらない未知数の番号が入る。
    """
    n, n_u = B.shape

    # --- 整合の取れた初期値・不連続点の処理 -----------------------------------
    Rex, Ru = consistent_maps(E, G, B)
    Rx = Rex @ E

    # --- 台形則 ---------------------------------------------------------------
    P, Q = _solve_or_raise(E / h + G / 2.0, [E / h - G / 2.0, B], h)

    # --- 減衰ステップ ---------------------------------------------------------
    # 計算の先頭と不連続点の直後に使う。1ステップを DAMPING_SUBSTEPS 個の後退オイラーに刻んだもの。
    # 刻みより速い成分は消え、遅い成分はほぼ厳密に進む（誤差は後退オイラー1ステップの 1 / 刻む数）。
    # 途中の電源値は両端の直線補間。行列のべき乗（2乗の繰り返し）で1つの行列にまとめる。
    hs = h / DAMPING_SUBSTEPS
    Ps, Qs = _solve_or_raise(E / hs + G, [E / hs, B], h)
    M = np.zeros((n + 2 * n_u, n + 2 * n_u))      # [x; u; 1刻みごとの u の増分] を1刻み進める行列
    M[:n, :n] = Ps
    M[:n, n:n + n_u] = Qs
    M[:n, n + n_u:] = Qs
    M[n:, n:] = np.kron(np.array([[1.0, 1.0], [0.0, 1.0]]), np.eye(n_u))
    for _ in range(DAMPING_SUBSTEPS.bit_length() - 1):
        M = M @ M
    Pd = np.ascontiguousarray(M[:n, :n])
    Qd_end = M[:n, n + n_u:] / DAMPING_SUBSTEPS
    Qd_start = M[:n, n:n + n_u] - Qd_end
    if not (np.isfinite(Pd).all() and np.isfinite(Qd_start).all() and np.isfinite(Qd_end).all()):
        raise CircuitError(Message("MATRIX_SINGULAR", h=Value(h, "s")))

    ops = Operators(P=P, Q=Q, Rx=Rx, Ru=Ru, Rex=Rex, Pd=Pd, Qd_start=Qd_start, Qd_end=Qd_end)
    if backward_euler:
        # 状態が変わったステップの判定に使う（run_switched の説明を参照）
        ops.Pb, ops.Qb = _solve_or_raise(E / h + G, [E / h, B], h)
    return ops


def natural_rates(G: np.ndarray, Rex: np.ndarray) -> np.ndarray:
    """回路の固有値 λ（電源を止めたときの成分 exp(λ t)）。時定数は 1 / |λ|。

    状態量 y = E x について y' = -G x、x = Rex y なので、-G Rex の固有値を取る。
    状態でない方向の分は 0 として混ざるが、速い成分を探す用途では害が無い。
    """
    if G.shape[0] == 0:
        return np.zeros(0, dtype=complex)
    return np.linalg.eigvals(-G @ Rex)


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
    Pd: np.ndarray | None = None,
    Qd_start: np.ndarray | None = None,
    Qd_end: np.ndarray | None = None,
    n_damp: int = 0,
    damp: int = -1,
) -> int:
    """固定刻みで n_steps 進め、結果を out に書く。戻り値は、続きの区間に持ち越す減衰ステップの残り。

    計算の先頭と電源の不連続点から n_damp ステップは減衰ステップ（Pd, Qd_start, Qd_end）、
    それ以外は台形則。台形則だけだと、刻みより速い成分が符号を変えながら残る。
    damp   : この区間の初めに残っている減衰ステップの数（-1 なら n_damp。計算の先頭）。
             計算を区間に区切って呼ぶとき（進み具合と中断のため）は、前の区間の戻り値を渡す。
             区切っても、区切らないときと同じ演算を同じ順に行うので、結果はビット単位で同じ。

    u_start[k] : 時刻 t[k]   での電源値（不連続点では切り替わった後の値）
    u_end[k]   : 時刻 t[k+1] での電源値（不連続点では切り替わる前の値）
    event[k]   : t[k] で電源が不連続に変わるなら True。状態を保ったまま他の変数を合わせ直す
    out        : (n_steps + 1, n)。out[k] は t[k] の値（不連続点では切り替わった後の値）
    """
    n_steps = u_start.shape[0]
    x = x0.copy()
    out[0] = x
    if damp < 0:
        damp = n_damp
    for k in range(n_steps):
        if event[k]:
            x = Rx @ x + Ru @ u_start[k]
            out[k] = x
            damp = n_damp
        if damp > 0:
            x = Pd @ x + Qd_start @ u_start[k] + Qd_end @ u_end[k]
            damp -= 1
        else:
            x = P @ x + Q @ (0.5 * (u_start[k] + u_end[k]))
        out[k + 1] = x
    return damp


# ---------------------------------------------------------------- スイッチング

# run_switched() の戻り値
DONE = 0           # k_stop まで進んだ
NEED_STATE = 1     # 行列を用意していない回路状態に出会った（符号は ctl[CTL_CODE]）
NOT_SETTLED = 2    # ダイオードの状態が規定回数で落ち着かない（最後に試した符号は ctl[CTL_CODE]）

# ctl（再開に必要な進行状態）の並び
CTL_K = 0          # 次に計算するステップの番号
CTL_STATE = 1      # 直前の区間の回路状態の符号（x と整合している状態）
CTL_DAMP = 2       # 減衰ステップで進める残りステップ数
CTL_CODE = 3       # NEED_STATE / NOT_SETTLED のときの符号
CTL_CHANGES = 4    # 集計: 不連続点として扱ったステップの数
CTL_RETRIES = 5    # 集計: 捨てた試行の数
CTL_SIZE = 6


def run_switched(
    P: np.ndarray,
    Q: np.ndarray,
    Pb: np.ndarray,
    Qb: np.ndarray,
    Pd: np.ndarray,
    Qd_start: np.ndarray,
    Qd_end: np.ndarray,
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
    record_after: np.ndarray,
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
         max_flips 回で落ち着かないときは NOT_SETTLED を返す。
         （計算の先頭の状態決定には丸め誤差の幅での救済があるが、ここには置かない。判定に使う値は
         後退オイラーのステップの終わりの値で、電流がちょうど 0 の点でも、試した回路（1e10 V まで、
         r_off / r_on = 1e18 まで）で往復が落ち着かなくなることが無かったため。E1c の報告を参照）
      3. 状態が直前の区間と変わったステップ（やり直したステップを含む）は、後退オイラーで進める。
         後退オイラーでの判定は「このステップの終わりまでダイオードが導通し続けるか」と同じになり、
         状態が1つに決まる。続く n_damp ステップは減衰ステップ（刻みより速い成分を消す。
         遅い成分はほぼ厳密）、それ以外は台形則で進める。計算の先頭と電源の不連続点も、
         そこから n_damp ステップを減衰ステップにする。
         後退オイラーも減衰ステップも状態量（E x）だけを引き継ぐので、コンデンサ電圧と
         インダクタ電流は切り替えの前後で連続になる。台形則のままだと、オフ抵抗を通る
         インダクタ電流のような硬い成分が、符号を変えながら残る。

    行列（スロットごと）
      P, Q   : 台形則        x[k+1] = P x[k] + Q u_mid[k]
      Pb, Qb : 後退オイラー  x[k+1] = Pb x[k] + Qb u_end[k]
      Pd, Qd_start, Qd_end : 減衰ステップ  x[k+1] = Pd x[k] + Qd_start u_start[k] + Qd_end u_end[k]
      Rx, Ru : 整合          x(t+) = Rx x(t-) + Ru u_start[k]
      slot_code[:n_slots] : 各スロットの回路状態の符号
    入力（時間格子上の配列）
      u_start[k], u_end[k] : 区間 [t[k], t[k+1]] の両端の電源値。u_mid[k] はその平均
      event[k]   : t[k] で電源が不連続に変わるなら True（時刻で決まる電源のステップと、指令で値が決まる
                   電源の変わり目）。その直後を減衰ステップにする
      record_after[k] : t[k] の記録を、電源が変わった後の値にするなら True（時刻で決まる電源のステップ。
                   D-019）。False なら変わる前の値のまま（指令で値が決まる電源だけが変わる点。スイッチの
                   切り替え点と同じ。D-048）。event[k] が True の点でだけ見る
      sw_code[k] : 区間 [t[k], t[k+1]) のスイッチのビット
      d_pos, d_neg : ダイオードのアノード・カソードの未知数の番号（基準節点は -1）
    進行状態（書き換える）
      x   : t[ctl[CTL_K]] の値
      ctl : 上の CTL_* の並び
    記録
      out[k]      : t[k] の値。スイッチやダイオードが t[k] で切り替わる場合は切り替わる前の値、
                    時刻で決まる電源が t[k] でステップする場合は変わった後の値（record_after[k]）、
                    指令で値が決まる電源だけが変わる場合は変わる前の値
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
        be = cand != code_prev                  # 状態が変わるステップは後退オイラー
        left = n_damp if disc else damp         # このステップを含めた、減衰ステップの残り
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
            elif left > 0:
                xn = Pd[s] @ xk + Qd_start[s] @ u_start[k] + Qd_end[s] @ u_end[k]
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
            be = True
        if status != DONE:
            break

        # --- 採用 ---
        n_retries += flips
        if disc and record_after[k]:
            out[k] = xk
        if be or disc:
            n_changes += 1
        if be:
            damp = n_damp                       # 後退オイラーの後に、減衰ステップを n_damp 回
        elif left > 0:
            damp = left - 1                     # このステップが減衰ステップだった
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
