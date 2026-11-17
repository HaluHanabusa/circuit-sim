# A0（アプリの骨組み）の独立検証（6回目: Windows の失敗の修正の2回目。待たない読み方と、試験の後始末）

- 担当: verifier（実装の ui-engineer とは別。実装の作業は見ていない。5回目の検証役とも別の実行で、記録
  `docs/verification/A0-verifier-5.md` を読んで引き継いだ）
- 対象: ブランチ `A0`、実装者の修正の先頭 `9f594c5`（ツリー `2b2184ee469874438eb4a53086217b30d317b9a0`）。差分は
  `git diff 80da2ea 9f594c5`（`circuit_sim/app/cli.py`、`docs/design/app-design.md` 8章、`tests/app_procs.py`（新）、
  `tests/test_app_cli.py`、`test_app_browser.py`、`test_app_config.py`、`test_app_server.py`）
- 日付: 2026-11-17
- 環境: Linux、Python 3.13.18、pytest 9.1.1、uvicorn 0.54.0、playwright 1.63.0、CPU 2 個。試験と起動は
  `PATH=/usr/local/bin:/usr/bin:/bin`（本物の claude の無い PATH）。**この環境は `PYTHONUNBUFFERED=1`**（問題1 に
  関係する）。使い捨ての道具と、製品を壊す確かめは、作業ツリーの外（`/tmp/a0-verify6/` の、`git archive` で作った
  写し）で流した。作業ツリーの製品のコードと、実装者の試験には触っていない
- Windows の事実は、PO の PC（Windows 11、Python 3.13.2、cp932）で `22d2fdc` を流した結果と診断の出力
  （オーケストレーターから受け取った資料）だけ。**この環境は Linux で、Windows では1つも流していない。**
  `b48cdf9` も `9f594c5` も、5回目とこの回に足した受け入れ試験も、Windows ではまだ一度も流れていない
- 一次の資料: Microsoft の文書（PeekNamedPipe、GetFileType）、CPython 3.13.2 のソース（タグ `v3.13.2` の
  `Modules/_winapi.c`、`Modules/_io/fileio.c`、`Python/fileutils.c`、`Python/pylifecycle.c`、`Lib/webbrowser.py`、
  `Lib/ctypes/wintypes.py`、`PC/msvcrtmodule.c`、`Lib/multiprocessing/connection.py`）、入っている uvicorn のソース

## 判定

| 範囲 | 判定 | 要点 |
|---|---|---|
| Linux で確かめられる範囲 | **合格**（重大 0、中 1、軽微 3） | 5回目の中1（ブラウザのコマンドが動いている間 Enter が効かない）は直った。22d2fdc と比べた後退は見つからない。待たない読み方のコードは、Microsoft の文書・CPython のソースと合う。SIGBREAK の修正は残っている。安全の前提（D-053、D-054、D-056）に変更なし。試験が起動したプロセスは、ふつうの経路では残らない |
| Windows | **未確認**（確認待ち） | 待たない読み方が実機で効くこと、Ctrl+Break の終了コード 0、どちらも実機の結果が無い。下の「Windows で未確認の点」 |

中 1 件は、製品ではなく実装者の試験 1 件の不安定さ（問題1）。この環境では `check.py` が 8 回に 1 回ほど落ちる見込みなので、
統合の前に直すことを勧める（試験だけの直しで、製品の確かめ直しは要らない）。合格は Linux の範囲だけで、
Windows の項目を合格にしたものではない。

## 実装者の主張 1〜4 の確かめ

| 主張 | 確かめたこと | 結果 |
|---|---|---|
| 1 Enter の順序の変更をやめ、起動の直後から読む。Windows のパイプだけ待たない読み方。SIGBREAK は残した | 差分を読んだ（`after_open`、`start=False` は無い）。Enter の動きを 22d2fdc・b48cdf9・9f594c5 で比べた。待たない読み方は、読んで突き、Linux で製品の分かれ目のまま流した | 同意（Linux）。Windows は未確認 |
| 2 試験が起動したアプリ本体を止めずに終わる経路を、`tests/app_procs.py` で塞いだ。`--no-browser` の無い起動は部品が断る | わざと残す試験 3 通りと中断（Ctrl+C）で、残らない。構文木で、部品を通さない起動は無い | 同意（ふつうの経路）。穴が1つ（問題2: ハーネスが確認をまとまりごと止めたとき。POSIX） |
| 3 レビューの中2・軽微1〜6、検証の軽微（疑似端末の試験）を直した | 軽微2（HTML を書けない分岐）・3（主スレッドの外）・4（`readline` で止まる）は、製品を壊すと実装者の試験が落ちる（止まらない）。疑似端末の試験は足された | 同意。ただし、足された疑似端末の試験 1 件が不安定（問題1） |
| 4 Linux の check.py: 1982 passed、26 skipped、14 xfailed、失敗 0 | `9f594c5` で流して、同じ数になった | 同意 |

## 確かめたことごとの結果（依頼の A の 1〜8）

時間の値は、ことわりが無ければ合否の基準ではなく待つ上限。合否は、端末の文、番号、終了コード、プロセスが動いて
いるか、読み取りの回数と大きさ。

| 確かめたこと | 実測 | 測り方 | 合否 |
|---|---|---|---|
| 1 check.py | `9f594c5`: ruff 通過、1982 passed、26 skipped、14 xfailed（600 秒）、終了コード 0。受け入れ試験を足した後は、終わりの「check.py の結果」 | `PATH=/usr/local/bin:/usr/bin:/bin CIRCUIT_SIM_NODE=/opt/node22/bin/node python scripts/check.py` | 合格 |
| 2a ブラウザがどうなっても Enter で新しい URL | 7 通り（コマンドが終わるまで戻らない、すぐ戻る `… %s &`、コマンドが失敗、開く呼び出しが「開けなかった」、例外、転送用の HTML を書けない、`--no-browser`）とも、2回押して2つ出て、番号は使える。戻らないコマンドでは、コマンドが動いている間に出る | `test_a0_fix6_enter_inputs.py`（標準入力はパイプ）。9f594c5 と 22d2fdc は全部通る。b48cdf9 は「戻らないコマンド」の 2 件が落ちる | 合格 |
| 2b 先に押した Enter | 起動の文が出る前に送った改行で、新しい URL が出る（`--no-browser`、戻らないコマンド） | 同じファイル。22d2fdc も通る | 合格 |
| 2c 続けて押した Enter | 1回につき1つ（`\n`、`\n`、`abc\n`、`\r\n`、`\r`）。余分な行は出ない | 同じファイル。22d2fdc も同じ | 合格 |
| 2d 読めない入力、入力の終わり | NUL・ファイル（改行入り）・閉じた入力（POSIX）: 起動し直す案内、Enter の案内は無い、新しい URL は出ない。すぐ閉じたパイプ: Enter の案内のまま、新しい URL は出ない。どれも動き続け、3 秒の CPU は 0.5 秒未満、SIGINT で終了コード 0 | 同じファイル。22d2fdc も同じ | 合格 |
| 3a ctypes の呼び方 | `WinDLL("kernel32", use_last_error=True)`、`argtypes`（HANDLE、LPVOID、DWORD、LPDWORD×3）、`restype` BOOL、`msvcrt.get_osfhandle` の値をそのまま、`byref(DWORD)` を 5 番目、失敗は `ctypes.get_last_error()`。Microsoft の文書の並び（hNamedPipe、lpBuffer、nBufferSize、lpBytesRead、lpTotalBytesAvail、lpBytesLeftThisMessage。バッファと量の先は NULL でよい。成功は 0 でない値）と合う。CPython の `_winapi.PeekNamedPipe` は `PeekNamedPipe(handle, NULL, 0, NULL, &navail, &nleft)`（6 番目だけ違う。NULL でよい） | 読んだ。加えて、本物の ctypes に引数を運ばせる試験（`test_a0_fix6_pipe_reader.py` の 1）: 32 ビットに収まらないハンドル `0x123456789ABF` がそのまま届く、109・233 は None、1・5・6・232 は OSError | 合格（Linux）。本物の API は未確認 |
| 3b パイプかどうかの見分け | `enter_source` は `os.fstat` の種類で見る。CPython は Windows で `GetFileType` を使い、FILE_TYPE_PIPE を FIFO、FILE_TYPE_CHAR を文字の装置にする（`_Py_fstat_noraise`）。コンソールと NUL は文字の装置 → `GetConsoleMode` で分ける（前から同じ）。ファイルはディスク → 読まない。Microsoft の文書では FILE_TYPE_PIPE はソケットも含む → "pipe" になり、`PeekNamedPipe` が失敗して、待つ読み方に戻る | 読んだ。コンソールの道は 22d2fdc から変わっていない（差分で確かめた） | 合格（読んだ範囲）。実機は未確認 |
| 3c 回り方 | 届く前は読みに行かない（0.5 秒の間、読み取り 0 回、調べるのは眠りおき）。届いた分だけ読む（頼む大きさ ≤ 届いた量、≤ 1024）。分かれて届く行は Enter 1回、CR LF は1回、CR は1回、複数の行は1回、3000 バイトは分けて全部読む。Enter への反応は、40 回測って最大 50 ms、中央 24 ms（眠りは 0.05 秒。試験の上限は 0.5 秒）。止める合図で 1 秒以内に終わる。書く側が閉じたら、届いた分を読んでから終わり、その後は調べない。調べられない入力は、1回で待つ読み方に戻り、Enter は読める | `test_a0_fix6_pipe_reader.py` の 2・3。製品の分かれ目（`cli.WINDOWS`）を真にし、`PeekNamedPipe` の所だけを POSIX の身代わり（POLLHUP と FIONREAD）にして、製品の選び方のまま流す | 合格（Linux） |
| 3d アプリ本体ごと、待っている間に読み取りの待ちが無い | Windows の分かれ目を真にした起動で、Enter を2回: 標準入力の読み取りは 2 回だけで、どちらも「届いた量 1、頼んだ量 1」。入力の終わりの後は読みに行かない。対照（調べる用意ができない）では「届いた量 0、頼んだ量 1024」の読み取り（待ち）が残る | 同じファイルの 4（POSIX だけ）。`os.read` を見張り、読みに行った時点で届いていた量を控える | 合格（Linux） |
| 3e 身代わりは Windows の約束と同じ所を見ているか | 同じ所: 読まずに・待たずに量を答える、届いた分が残る間は閉じていても量を答える、空で閉じていたら終わり。違う所（身代わりでは見られない）: 終わりを失敗の番号で知ること、ハンドルを共有する別のプロセスが読んで待っていると `PeekNamedPipe` も待つこと | 読んだ。実装者の身代わりは、量を見た後に「閉じたか」を見る順（「テストの実効性」の所見） | 妥当（範囲つき） |
| 3f Windows でだけ流れる試験の書き方 | 実装者の 8 件を読んだ（本物のパイプ、子が始まれる・対照、Ctrl+Break、結び損ねの番号、起動し直し、表示言語、予約のポート）。文法と API の使い方、スキップの条件、後始末に、誤りは見つからない | 読んだだけ | 誤りは見つからない（未確認） |
| 4 SIGBREAK | 5回目の確かめ 2a〜2e は、今も成り立つ（身代わりの合図の受け入れ試験 8 件が通る）。扱いを外すと 6 件、戻さないと 1 件、主スレッドの見分けを消すと 1 件（実装者の試験も 1 件）が落ちる | `test_a0_fix5_enter_break.py` と、写しの中での壊し方 | 合格（Linux。Windows は未確認） |
| 5a 試験の後始末（ふつうの経路） | 「起動に失敗して終わるはず」が成功: 4 秒で「終わるはずのプロセスが…終わらない（止めた）」。途中で例外、待ち時間切れ、ブラウザのコマンドが動いている間に抜けた: 見張りが止めて、その試験をエラーにする。Ctrl+C で中断: 実行の終わりに止まる。どれも、印を付けたプロセスは残らない | 写しの中に、わざと残す試験を置いて流した。残りは、環境変数の印を `/proc` で探した。受け入れ試験にした（`test_a0_fix6_test_hygiene.py` の 2） | 合格 |
| 5b ほかの人のプロセスを止めない作りか | 止めるのは、自分が起動した持ち物（Popen）だけ。子ごと止めるのは、親がまだ動いている間だけ（POSIX は受け取り前なので番号は使い回されない。Windows は持ち物が番号を押さえている）。`taskkill /T` は親の番号で子をたどるので、番号の使い回しを見分けない（`scripts/cycle.py` と同じ。観察） | 読んだ | 妥当 |
| 5c 部品を通さない起動 | `tests/test_app_*.py` に `subprocess.Popen` などの直接の起動は無い。残る `subprocess.run` は OS の道具（netsh、whoami、icacls、node、pip）で、アプリ本体ではない。部品を使う 3 ファイルに、試験ごとの見張りがある | 構文木（`test_a0_fix6_test_hygiene.py` の 1）。直接の Popen を植えると落ちる | 合格 |
| 5d 受け入れ試験の見張りとの組み合わせ | 受け入れ試験の控えは、受け入れ試験のモジュールの間だけ `subprocess.Popen` を差し替える。実装者の部品は、その外で動く。二重に止める・誤って失敗にする所は無い。全体の確認でも、どちらの見張りも失敗を出さない | 読んだ。check.py | 合格 |
| 5e ハーネスが確認を止めたとき | `cycle.kill_tree`（まとまりごと SIGKILL）の後、実装者の部品で起動したアプリ本体が残る。22d2fdc の起動では残らない | 写しの中で、`scripts/local_ci.py` と同じ起動・止め方を再現。受け入れ試験（xfail）にした | **問題2**（軽微） |
| 6 製品を壊すと、受け入れ試験が落ちるか | 待たない読み方 28 通り、Enter の読み始め 3 通り、SIGBREAK 3 通り、転送用の HTML 7 通り、起動コード 4 通り。落ちないのは、待たない読み方の中の、動きが変わらない壊し方 3 つだけ | 写しの中で、1 か所ずつ壊して受け入れ試験を流した（「テストの実効性」） | 合格 |
| 7 安全の前提と、前回までの基準 | 22d2fdc からの製品の差分は `cli.py`（Enter の読み方、SIGBREAK、説明）と `config.py` の注の文だけ。`server.py`（起動コード、合言葉）は変わっていない。製品に、試験のための環境変数や分かれ目は無い（`watch_enter` の `ready` は呼ぶ側が渡す引数で、起動の引数や環境変数からは届かない）。前回までの受け入れ試験は全部通る | 差分と `grep`。check.py | 合格 |
| 8 設計書の 8章 | 実機・資料・未確認を分けて書いている。コードと合う（眠り 0.05 秒、109・233、待つ読み方に戻す、端末と POSIX は変えていない）。5回目の問題3 の2点は直っている。引いている資料（CPython の issue 78961、Raymond Chen の記事）は実在し、題も合う。書かれていない制限がある | 読んで、コードと資料に突き合わせた | **問題3**（軽微、文書） |

## 見つけた問題

### 1. 中（実装者の試験）: 疑似端末の試験 1 件が不安定で、この環境では check.py が 8 回に 1 回ほど落ちる

- 試験: `tests/test_app_cli.py::test_ctrl_c_on_a_terminal_stops_and_enter_still_works_after_a_warning`（今回足された）。
- 実測: この試験だけを 40 回流して、**5 回落ちた**（ほかに何も動かしていない機械）。`PYTHONUNBUFFERED` を外すと
  40 回で 0 回。全体の確認は、こちらで流した 4 回は通っている（「check.py の結果」）。
- 落ち方: 「'新しい URL' で始まる行が出ない」。端末の出力は
  `…10 秒以内にもう一度 Ctrl+C を押してください。新しい URL（1回だけ、5 分以内に使えます）: http://…` と1行になり、
  次の行が空。
- 原因: 試験は、止める知らせ（2 行の文）の 1 行目が出たらすぐ Enter を送り、「新しい URL」で**始まる**行を待つ。
  製品の `say`（`print(text, flush=True)`）は、文と改行を別々に書く。出力をためない設定（`PYTHONUNBUFFERED=1`、
  `-u`）では、書くたびに OS へ渡るので、主スレッドの知らせの改行より先に、Enter を読むスレッドの新しい URL が
  出ることがある。
- 再現: `for i in $(seq 40); do PYTHONUNBUFFERED=1 python -m pytest "tests/test_app_cli.py::test_ctrl_c_on_a_terminal_stops_and_enter_still_works_after_a_warning" -q -p no:cacheprovider | tail -1; done`
- 影響: 製品の動きは正しい（Enter は効いている）。試験が、まれな出方で落ちる。`main` は常に check.py が通る、という
  決まりが、この環境では確率で崩れる。Windows では、この試験はスキップ（疑似端末は POSIX だけ）。
- 直し方の案（直すのは実装者）: 知らせの 2 行目（「もう一度 Ctrl+C を押してください」）が出てから Enter を送る。
  または、行の始まりではなく、含むかどうかで待つ（`wait_line(contains=…)`）。もとの製品の弱さ（問題4）を直せば、
  試験はそのままでも落ちなくなる。
- 受け入れ試験は、行の始まりに頼らない書き方（含むかどうか）なので、同じ出方でも落ちない。

### 2. 軽微（実装者の試験の部品）: ハーネスが確認をまとまりごと止めると、試験が起動したアプリ本体が残る（POSIX）

- 内容: `tests/app_procs.py` の `spawn` は、POSIX で、起動するものを新しいまとまり（`start_new_session=True`）に入れる
  （子ごと止めるため）。`scripts/local_ci.py` は、確認を新しいまとまりで起動し、時間切れ（既定 3600 秒）と中断
  （Ctrl-C、終了の合図）のときに `cycle.kill_tree`（まとまりごと SIGKILL）で止める。pytest は後始末をする間もなく
  終わる。アプリ本体は別のまとまりにいるので、待ち受けたまま残る。
- 実測: 9f594c5 の部品で起動したアプリ本体は残った（こちらで番号を確かめて止めた）。22d2fdc の起動（そのままの
  Popen）では残らない。
- 再現: `pytest tests/acceptance/test_a0_fix6_test_hygiene.py -k harness`（xfail(strict)。直ると XPASS で落ちるので、
  そのとき印を外す）。
- 影響: Linux・macOS だけ。Windows は `taskkill /T` が親子をたどるので、まとまりによらない（読んだだけ）。
  5回目の問題2（試験が PO の PC にアプリ本体を残した）と同じ種類の残り方が、開発の機械で、確認を打ち切ったときに
  起こる。
- 直し方の案（直すのは実装者。ハーネスの側で受けるなら tooling-engineer）: 新しいまとまりに入れずに起動し、
  子は別のやり方で止める。または、ハーネスの側で、実行の印（環境変数）を持つプロセスを止める。

### 3. 軽微（文書・説明）: 待たない読み方の、残る制限が書かれていない

レビュー（6回目）の中1 と同じ所。こちらでも、資料との突き合わせで同じ点に当たった（重さは、製品の動きを
変えない文書の直しなので、ここでは軽微に数えた。指摘の中身は同じ）。

- `PeekNamedPipe` は、いつも待たないわけではない。Microsoft の文書の注: 同期のハンドルでは、スレッドが複数ある
  ときに、ほかの入出力と同じように待つことがある。起動された側（標準入力を受け継いだプログラム）が標準入力を
  読んで待つと、アプリ本体の調べも、その読み取りが終わるまで待つ（Enter は、起動された側が取る）。
  `cli.py` の「調べるだけなら、待ちは残らない」と 8.3 の「読まずに・待たずに調べる」は、アプリ本体が待ちを
  残さない、という意味では正しいが、言い切りになっている。
- 調べてから読むまでの間に、同じパイプの別の読み手が先に取ると、`os.read` が待つ。
- コンソール（端末）は、待つ読み方のまま。コンソールで起動して BROWSER にコマンドを指定したとき、そのコマンドが
  始まれるかは、実機の診断に無い（パイプだけを確かめている）。未確認に足す。手で確かめるなら: コンソールから、
  BROWSER に「渡された場所をファイルに書いて終わるスクリプト」を指定して起動し、ファイルができるかを見る。
  BROWSER を指定しない起動（ふつうの使い方。`os.startfile`）は、この止まり方に関係しない。
- `cli.py` の「CPython の `_winapi.PeekNamedPipe` も、同じ形で呼ぶ」: CPython は 6 番目に受け取る先を渡す
  （製品は NULL。どちらも文書どおり）。
- 8.4 の「起動したプロセスは、必ず止める」「（子も）」: 親が先に終わった子と、問題2 の場合は止まらない。

### 4. 軽微（製品。22d2fdc からある）: 端末の文が、2つのスレッドから同時に出ると1行に混ざる

- 内容: 問題1 の原因。`say` が文と改行を別々に書くので、止める知らせ・Enter の新しい URL・使い終わった URL の
  案内・開けなかった案内が重なると、1行に続いて出て、次の行が空になる。端末（1行ごとに OS へ渡す）でも、2 行の
  文（止める知らせ）の途中で起こりうる。人の操作では、重なることはまれ。見た目だけで、番号や動きは変わらない。
- 再現: `pytest tests/acceptance/test_a0_fix6_enter_inputs.py -k run_into`（xfail(strict)）。
- A1 へ持ち越す（5回目までの軽微と同じ扱い）。

### 観察（判定に使わない）

- pytest そのものが強制終了されると（メモリ不足など）、どの見張りも働かず、起動していたものが残る。この回の
  確かめの途中で、こちらの壊し方（読むスレッドが回り続ける）が試験のプロセスのメモリを使い切り、受け入れ試験が
  起動したアプリ本体が 1 個残った。番号・作業フォルダ・実行の印でこちらのものと確かめて、止めた。受け入れ試験の
  見張りに、回り続けるスレッドの歯止め（調べた回数の上限）を入れた。
- `taskkill /T` は、親の番号だけで子をたどる。番号が使い回されていると、無関係のプロセスを止めうる（可能性は
  低い。`scripts/cycle.py` も同じ。レビューの軽微5）。
- 実装者の控え（`app_procs.STARTED`）は、終わったプロセスの持ち物を持ち続ける（レビューの軽微7）。実測:
  `tests/test_app_cli.py` と `test_app_config.py` を流した終わりで、控えたプロセス 67 個、開いている fd 54 個。
  この数なら、fd の上限には届かない。
- Enter の数え方（1回の読み取りに改行があれば1回）は 22d2fdc と同じ。待たない読み方では、眠り（0.05 秒）の間に
  続けて届いた Enter は1回になる。CR と LF が別々の読み取りに分かれて届くと2回になる（どちらの読み方でも）。

## テストの実効性

製品を、写しの中で 1 か所ずつ壊して流した（作業ツリーは変えていない）。

### 実装者の試験（Linux）で通ってしまう壊し方

| 壊し方 | 実装者の試験 | 受け入れ試験（この回に足したもの） |
|---|---|---|
| Windows のパイプでも、待つ読み方にする（待たない読み方を使わない） | 通ってしまう | 落ちる（5 件） |
| `WinDLL` に `use_last_error=True` を付けない | 通ってしまう | 落ちる |
| `kernel32` でない DLL から取る | 通ってしまう | 落ちる |
| CR を Enter と見ない | 通ってしまう | 落ちる |
| Windows の端末にも、待たない読み方を用意する | 通ってしまう | 落ちる |

1 つ目は、今回の修正の中心の1行（`if ready is None and WINDOWS and source == "pipe"`）。Linux の確認だけでは、
この行が消えても気づけなかった。Windows でだけ流れる実装者の試験（本物のパイプ、子が始まれる）は、1〜3 つ目で
落ちる見込み（読んだだけ）。

### 受け入れ試験で確かめた壊し方

| 所 | 壊し方（数） | 落ちる | 落ちないもの |
|---|---|---|---|
| `PeekNamedPipe` の呼び方 | 14（引数の入れ替え 2、ハンドルの型、`argtypes` 無し、`use_last_error` 無し、別の DLL、233 を外す、閉じても 0、いつも 1、fd をそのまま、`restype` 無し、`GetLastError` を直接、大きさに 1024、失敗をすべて終わりに） | 13 | `restype` を決めない（既定の int と BOOL は同じ大きさで、動きは変わらない） |
| 読むスレッドの回り方 | 12（上の表の 2 つ、届いた量より多く読む、眠らない、閉じても調べ続ける、調べられない入力でやめる・回り続ける、止める合図を見ない、眠りを 3 秒に、届く前に読みに行く、CR、スレッドを始めない） | 12 | — |
| 止める合図 | 2（眠りが合図で抜けない、`main` が合図を出さない） | 0 | どちらも、動きが変わらない（止まるまでの遅れは眠りの 0.05 秒まで。スレッドは、プロセスと一緒に終わる） |
| Enter の読み始め | 3（`--no-browser` のときだけ読む、読み始めない、b48cdf9 の順序） | 3 | — |
| SIGBREAK | 3（扱いを外す、戻さない、主スレッドの見分けを消す） | 3 | — |
| 転送用の HTML | 7（番号つきの URL を渡す、場所に番号を足す、使われても消さない、止めても消さない、合言葉を書く、止めている最中に開く、止めた後に案内を出す） | 7 | 「止めても消さない」は、消す所が2つあり、両方を外すと落ちる（片方だけでは落ちない） |
| 起動コード | 4（何回でも使える、端末の URL に合言葉、Enter の URL が前と同じ番号、無くても通す） | 4 | — |

所見:

- 実装者の `PeekNamedPipe` の試験は、身代わりを Python の関数にしているので、引数の型の指定が実際に効くかどうかは
  見ていない（型の一覧を比べるだけ）。受け入れ試験は、本物の ctypes に引数を運ばせて、届いた値を見る。
- 実装者の身代わり `posix_pipe_ready` は、量を見た後に「閉じたか」を見る。その間に「書いて、閉じる」が挟まると、
  届いた分を残したまま終わりと答える（レビューの軽微1 と同じ指摘）。受け入れ試験の身代わりは、先に「閉じたか」を
  見る。
- 「止めている最中はブラウザを開かない・案内を出さない」の確かめが、実装者の試験に無い（レビューの軽微2。
  22d2fdc にも無かった）。受け入れ試験に足した。
- 端末（コンソール、tty）から Enter を読む道: Linux は、実装者の疑似端末の試験 2 件が流れる（1 件は問題1）。
  Windows のコンソールは、自動の試験が無い（手で確かめる）。

## 受け入れ試験の手当て

**既にある受け入れ試験のファイルと記録は、書き換えていない。** 足したのは新しいファイル 3 つ（と、この記録）。
5回目に足した試験（`test_a0_fix5_*.py`、`conftest.py`、`acceptance_watch.py`、`a0_fake_browser.py`）は、
9f594c5 で意図どおり動く（落ちるままにしてあった Enter の試験は通り、ほかも通る。見張りは残りを報告しない）。

| ファイル | 中身 | Linux | Windows の見込み |
|---|---|---|---|
| `tests/acceptance/test_a0_fix6_enter_inputs.py` | アプリ本体ごとの Enter（確かめたこと 2a〜2d）、端末の文が混ざらない（xfail。問題4）、止めている最中はブラウザを開かない | 15 passed、1 xfailed | 14 passed、1 xfailed |
| `tests/acceptance/test_a0_fix6_pipe_reader.py` | 待たない読み方（確かめたこと 3a、3c、3d）と、Windows でだけ流れる試験 3 件（本物のパイプの答えが `_winapi` と合う、入力の見分け、読んで待つと子が始まれないことの対照）。子が始まれる試験は、どの OS でも流れる | 12 passed、3 skipped | 11 passed、4 skipped（対照が成り立たない PC では 10 passed、5 skipped） |
| `tests/acceptance/test_a0_fix6_test_hygiene.py` | アプリの試験の後始末（確かめたこと 5a、5c、5e） | 2 passed、1 xfailed | 2 passed、1 skipped |

Windows でだけ流れる試験は、落ちたときに原因が分かるよう、調べた値を添える（Python の版、ハンドル、`GetFileType`、
`_winapi.PeekNamedPipe` の答えか失敗の番号、製品の答えの並び）。本物のパイプに送る量は、入れ物（4096 バイト）に
必ず収まる量だけ（読む側のいない試験が、書く所で待ち続けない）。わざと残す試験は、別の pytest の中で、起動した
ときの持ち物で「まだ動いているか」を見て、その場で止める（プロセスの一覧に頼らない。PC に残さない）。

## Windows で未確認の点

1. **待たない読み方が、実機で効くこと。** 標準入力がパイプの起動で、BROWSER のコマンドが始まる（前回落ちた
   `test_browser_gets_only_the_location_of_a_private_page`、`test_launch_page_flow_leaves_no_number_behind`、
   5回目の Enter の試験、この回の `command-waits`・`command-returns-at-once`、子が始まれる試験と対照）。
2. 本物の `PeekNamedPipe` の答え（0、量、書く側が閉じたら失敗の番号が 109 か 233）。違う番号でも、待つ読み方に
   戻って入力の終わりで終わる作りだが、そのときは Windows の試験が番号を添えて落ちる。
3. 標準入力が、改行を変換しない読み方であること（CR だけの Enter の試験。CPython のソースでは、そうなる）。
4. 眠り 0.05 秒での反応と CPU（本物の `PeekNamedPipe`）。
5. Ctrl+Break の終了コード 0 と「止めました」（5回目から未確認のまま）。
6. コンソール（端末）から起動したときの Enter と Ctrl+C（読み方は 22d2fdc から変えていない。手で確かめる）。
   コンソールで BROWSER にコマンドを指定したとき、そのコマンドが始まるか。Git Bash（mintty）のように、端末が
   パイプに見える環境での Enter（待たない読み方を通る）。
7. BROWSER のコマンドが標準入力を読むとき（アプリ本体の調べも待つ。問題3）。試験は無い。
8. 見張りの Windows の部分: 受け入れ試験の側（PowerShell の一覧、`taskkill /T`、`os.startfile` を止める監査）と、
   実装者の部品の `taskkill /T`（`test_a0_fix6_test_hygiene.py` のわざと残す試験が、初めて流す）。
9. 窓を閉じたとき、Ctrl+C の2回押し（手で確かめる。前回までと同じ）。
10. PO の PC に、最初の確認の作業用の場所と作業ツリーの登録が残っているか（5回目の 6）。

PO に流してもらうコマンド（コマンド プロンプト。オーケストレーターの並びに、この回のファイル 3 つを足した）:

```
python -m pytest tests/acceptance/test_a0_browser_recheck.py tests/acceptance/test_a0_fix3_stop_stale_sddl.py tests/acceptance/test_a0_fix4_browser.py tests/acceptance/test_a0_fix4_restart_lastrun.py tests/acceptance/test_a0_fix5_enter_break.py tests/acceptance/test_a0_fix5_watch.py tests/acceptance/test_a0_fix6_enter_inputs.py tests/acceptance/test_a0_fix6_pipe_reader.py tests/acceptance/test_a0_fix6_test_hygiene.py tests/acceptance/test_a0_key_launch_settings.py tests/acceptance/test_e1a_units_limits.py tests/test_app_cli.py tests/test_app_config.py tests/test_app_server.py tests/test_app_browser.py tests/test_app_design_tokens.py -q -rs -p no:cacheprovider -o junit_family=xunit1 --junitxml=a0-windows-2.xml > a0-windows-2.txt 2>&1
```

合格の見分け方: 失敗 0、エラー 0、終了コード 0。`a0-windows-2.txt` の終わりの近くに「受け入れ試験の見張り: …この
実行から生まれたアプリ本体は残っていない。」が出ている。本物のブラウザの窓が開かない。流した後、`tasklist` に、
流す前から動いていたもののほかに python.exe が残っていない。スキップの理由（`-rs`）に「読んで待つ読み方でも、
子が始まれた」が出たら、この PC では、子が始まれる試験は読み方の違いを見分けていない（そのときは 1 の根拠は、
ブラウザのコマンドを使う試験だけになる）。

## 次に確かめ直す項目

1. Windows の結果（上のコマンド）が届いたら、未確認の 1〜5 と 8 を、結果のファイルで確かめる。
2. 問題1 を直した後、その試験を `PYTHONUNBUFFERED=1` で 40 回流して、落ちないこと。
3. 問題2・4 を直したら、xfail(strict) の印を外す（直ると、印のある試験が XPASS で落ちて知らせる）。
4. 問題3 の文書の直し。

## check.py の結果

- コマンド: `PATH=/usr/local/bin:/usr/bin:/bin CIRCUIT_SIM_NODE=/opt/node22/bin/node python scripts/check.py`
- 実装者の修正の先頭 `9f594c5`: ruff は `All checks passed!`。pytest は `1982 passed, 26 skipped, 14 xfailed in 600.70s`。
  終了コード 0。見張りの表示は「受け入れ試験が起動したプロセスは 500 個。…残っていない。」
- 受け入れ試験を足した途中の `09c24c4`: `2010 passed, 29 skipped, 16 xfailed in 687.35s`。終了コード 0。
- 受け入れ試験を足した `f114fc2`: `2010 passed, 29 skipped, 16 xfailed in 625.38s`。終了コード 0。見張りの表示は
  「受け入れ試験が起動したプロセスは 519 個。…残っていない。」
- 最後に流した内容: コミット `a12ea20`（受け入れ試験の最後のコミット `b35add8` に、この記録の最初の版を足したもの。
  その後の違いは、この記録のファイルだけ）。ruff は `All checks passed!`。pytest は
  `2011 passed, 29 skipped, 16 xfailed in 713.84s`。終了コード 0。見張りの表示は「受け入れ試験が起動したプロセスは
  519 個。…残っていない。」実装者の報告（1982 passed、26 skipped、14 xfailed）との差は、こちらが足した 3 ファイルの分
  （通過 29、Windows でだけ流れるスキップ 3、xfail 2）。
- `b35add8` での1回目は、87% まで進んだ所（そこまで失敗なし）で、環境の再起動で止まった。上の結果は、流し直したもの
  （同じ時間に、別の担当の確認が別の作業ツリーで流れていた）。
- 問題1 の試験は、こちらが流した全体の確認 4 回（`9f594c5`、`09c24c4`、`f114fc2`、`a12ea20`）では、どれも通った
  （単独では 40 回に 5 回落ちる）。
- 流した後、作業ツリーと `/tmp/a0-verify6/` を作業フォルダにするアプリ本体が残っていないことを、`/proc` で確かめた。
