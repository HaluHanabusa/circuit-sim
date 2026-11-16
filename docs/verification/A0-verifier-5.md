# A0（アプリの骨組み）の独立検証（5回目: Windows の実機で出た失敗の修正）

- 担当: verifier（実装の ui-engineer とは別。実装の作業は見ていない）
- 対象: ブランチ `A0`、実装者の修正の先頭 `b48cdf9`（ツリー `3dc2704e7f7e18a32cbaf6f8584b7a6a1ad4bb7c`）。差分は
  `git diff 22d2fdc b48cdf9`（Windows の実機の確認で落ちた 10 件への手当て）
- 前回の記録: `docs/verification/A0-verifier-4.md`（合格、軽微3件）
- 日付: 2026-11-16
- 環境: Linux、Python 3.13.18、pytest 9.1.1、uvicorn 0.54.0、playwright 1.63.0。試験と起動は
  `PATH=/usr/local/bin:/usr/bin:/bin`（本物の claude の無い PATH）。使い捨ての道具は `/tmp/a0-verify5/`（作業ツリーの外）。
  製品を壊す確かめ・修正を戻す確かめは、作業ツリーの写し（`git archive` と写し）の中で流した（作業ツリーの製品の
  コードには触っていない）
- Windows の事実は、PO の PC（Windows 11 10.0.26200、Python 3.13.2、既定の文字コード cp932）での結果を資料として
  使った。**この環境は Linux で、Windows では1つも流していない**。資料は次の3つ（オーケストレーターから受け取った）
  - 修正の前（`22d2fdc`）の全体の確認の結果: 1935 件（通過 1863、失敗 6、エラー 4、スキップ 62）と、落ちた6ファイルの
    流し直しの結果（JUnit XML。失敗の中身つき）
  - 診断の出力: `signal.raise_signal(signal.SIGBREAK)` の後の終了コードは 3。標準入力がパイプで、スレッドが
    `os.read(0, 1)` で待っている間に起動した `python -c pass` は 5 秒たっても終わらない（待たない対照は終わる）。
    `netsh interface ipv4 show excludedportrange protocol=tcp` は `50000 50059 *`。`netstat` は
    `127.0.0.1:50000 LISTENING 32092`
  - PID 32092 は `python.exe -m circuit_sim.app --no-browser --port 50000`、作られた時刻は 20:06:57（最初の全体の
    確認の最中）。PO が止めた

## 判定: 不合格（差し戻し）

理由は、製品の後退が1件（中）あること。ほかに、実装者の試験が、起動したアプリ本体を残しうること（中）。

| 範囲 | 判定 | 要点 |
|---|---|---|
| Linux で確かめられる範囲 | 不合格 | Enter を読み始める順序の変更で、ブラウザのコマンドが動いている間 Enter が効かない（22d2fdc からの後退。受け入れ試験 1 件が落ちる）。SIGBREAK の修正は、身代わりの合図で確かめた範囲では正しい。安全の前提（D-053、D-056）と前回までの基準に後退は無い |
| Windows の確認待ち | 未確認 | 修正の後（b48cdf9）も、この記録で足した受け入れ試験も、Windows ではまだ一度も流れていない。下の「Windows で未確認の点」 |

オーケストレーターの方針（Enter の順序の変更をやめ、Windows のパイプは待たない読み方にする。SIGBREAK の修正は残す）で
直した後に、もう一度確かめる。確かめ直す項目は、終わりの「次の修正で確かめ直す項目」。

## Windows で落ちた 10 件の分類

資料（落ちた試験の名前と、流し直しの失敗の中身）を自分で読んで分けた。実装者の分類（製品の誤り 4 件・2 種類、
実装者の試験の誤り 5 件、受け入れ試験の作り 1 件）と同じになった。足すことが2つある（表の下）。

| 落ちた試験 | 失敗の中身（資料） | 分類 |
|---|---|---|
| `tests/acceptance/test_a0_key_launch_settings.py::test_broken_settings_file_starts_with_defaults[too large-…]` | 準備と後始末で `ValueError: the environment variable is longer than 32767 characters`（ID が 70,130 文字） | 受け入れ試験の作り（長い ID） |
| `tests/test_app_server.py::test_deeply_nested_json_is_reported_not_500[…]` 3 件 | 同じ（ID が 200,071〜400,082 文字） | 実装者の試験（長い ID） |
| `tests/test_app_config.py::test_config_dir_follows_os_conventions[env2-…]` | `/x/conf` が絶対パスと扱われず、ホームの `.config` になった | 実装者の試験（パスの決め打ち） |
| `tests/test_app_cli.py::test_windows_excluded_port_range_is_reported_as_denied` | 流し直しでは「ポート 50000 は、ほかのプログラムが使っています」（期待は「使えません」） | 実装者の試験（前提の誤り） |
| `tests/test_app_cli.py::test_ctrl_break_on_windows_warns_first_then_stops` | 2回目の Ctrl+Break の後の終了コードが 3（期待は 0） | 製品（Ctrl+Break） |
| `tests/acceptance/test_a0_fix4_browser.py::test_restart_without_port_returns_to_the_previous_port_and_the_page_reconnects` | 止めたときの終了コードが 3（期待は 0） | 製品（Ctrl+Break） |
| `tests/acceptance/test_a0_key_launch_settings.py::test_browser_gets_only_the_location_of_a_private_page` | 30 秒たっても「ブラウザ（BROWSER）が呼ばれない」。端末には、開けなかった案内も、15 秒後の案内も出ていない（`webbrowser.open` が戻っていない） | 製品（標準入力のパイプ） |
| `tests/acceptance/test_a0_browser_recheck.py::test_launch_page_flow_leaves_no_number_behind` | 同じ | 製品（標準入力のパイプ） |

足すこと:

1. 予約のポートの試験は、前提が誤っていただけでなく、**アプリ本体を PO の PC に残した**。修正の前の試験は、netsh の
   範囲の先頭（50000）を `--port` に渡して起動し、`proc.communicate(timeout=20)` で終わるのを待つだけだった。この PC
   では 50000 を結べるので、アプリ本体は起動に成功して待ち受け続け、試験は 20 秒後に `TimeoutExpired` で抜け、
   プロセスを止めなかった。流し直しで「ほかのプログラムが使っています」と出たのは、この残りが 50000 を持っていた
   ため（製品の案内は、実態と合っていた）。下の「問題2」。
2. ブラウザの2件は、偽のブラウザが失敗したときに本物のブラウザへ進みうる作りでもあった（今回は、呼び出しが
   戻らなかったので進んでいない。PO の観察「本物のブラウザの窓は開かなかった」と合う）。受け入れ試験の側を直した
   （下の「受け入れ試験の手当て」）。

## 実装者の主張 1〜5 の確かめ

一次の資料は、CPython 3.13.2 のソース（タグ `v3.13.2` の各ファイル）、入っている uvicorn 0.54.0 と pytest 9.1.1 の
ソース、Microsoft の文書、PO の PC での結果。

| 主張 | 当たった資料と、分かったこと | 結果 |
|---|---|---|
| 1 長い ID は、Windows の環境変数の上限を超える | pytest は `_pytest/runner.py` の `_update_current_test_var` で、`PYTEST_CURRENT_TEST` に「ID (setup)」などを入れる。CPython の `Modules/posixmodule.c` の `win32_putenv` は、「名前=値」が `_MAX_ENV`（32767）文字を超えると、資料と同じ文の `ValueError` を出す。`deep` の ID は 30,102 文字で、資料でも通っている（上限のすぐ下） | 同意 |
| 2 `/x/conf` は、Windows の Python 3.13 から絶対パスではない | `Lib/ntpath.py` の `isabs`: 3.13.2 は「ドライブと区切り」か「区切り2つ」で始まるときだけ真。3.12.9 は区切り1つで始まれば真（「LEGACY BUG」の注つき）。製品の XDG の分かれ目を通るのは Windows でない OS だけ（`config_dir` は Windows なら APPDATA を使う）。製品の変更は注の文だけ | 同意（試験の誤り） |
| 3 予約のポート: `*` の範囲は結べる。WSAEACCES は errno 13・winerror 10013、WSAEADDRINUSE は errno 10048。製品の見分けは正しい | `PC/errmap.h` の `winerror_to_errno`: 10013 などは 10000 を引き、ほかの Winsock の番号はそのまま。`Modules/errnomodule.c`: Windows の `errno.EADDRINUSE` は WSA の値。Microsoft の文書（error 10013 WSAEACCES is returned…）は、netsh で除外したポートで「2回目の結び」が 10013 になる不具合を書いている（1回目は結べる前提）。PO の PC では、残ったアプリ本体が実際に 50000（`*` の範囲）で待ち受けていた | 同意。「50000 を何が使っているか」は、試験が残したアプリ本体と分かった |
| 4 Ctrl+Break の後の終了コード 3 は、uvicorn が合図を出し直し、SIGBREAK の既定の扱いで終わるため | uvicorn 0.54.0 の `server.py`: `HANDLED_SIGNALS` に、win32 でだけ SIGBREAK。`capture_signals` は、扱いを元に戻した後に `signal.raise_signal` で出し直す。CPython の `signal_raise_signal_impl` は C の `raise` を呼ぶ。PO の PC で、`raise_signal(SIGBREAK)` の後の終了コードは 3。Microsoft の文書 `signal` は、既定では合図の種類によらず終了コード 3 で終わると書いている。ただし、`signal` と `raise` の文書の表に SIGBREAK は載っていない（終了コード 3 の根拠は、実機の結果） | 同意 |
| 5 ブラウザが呼ばれないのは、標準入力のパイプを読んで待っている間に起動した子が、起動のときに止まるため | `Lib/webbrowser.py` の `GenericBrowser.open` は、Windows で `subprocess.Popen(cmdline)`（標準入力を差し替えない）の後、終わりを待つ。CPython の issue 78961（未解決）は、ほかのプロセスが読んで待っているパイプを標準入力に受け継いだ Python が、起動のときの位置の問い合わせ（`lseek`）で止まると報告している。`Python/pylifecycle.c` の `create_stdio` は、標準入力をいつもバッファつきで開く。PO の PC の診断で、同じ形が再現した（読んで待つ間に起動した子は終わらない。対照は終わる） | 同意（仕組み）。ただし、選んだ直し方に後退がある（問題1） |

## 確かめたことごとの結果

時間の値は、合否の基準ではなく待つ上限（混んだ機械で落ちないように長め）。基準にしたのは、終了コード、端末の文、
プロセスが残るかどうか。

| 確かめたこと | 実測 | 測り方 | 合否 |
|---|---|---|---|
| 1 ブラウザのコマンドが動いている間の Enter | b48cdf9: Enter の後 10 秒たっても新しい URL が出ない。22d2fdc: 出る（同じ試験が通る） | BROWSER に、止める印が置かれるまで終わらないコマンドを置き、呼ばれた後に標準入力（パイプ）へ Enter を送る。`test_a0_fix5_enter_break.py::test_enter_gives_a_new_url_while_the_browser_command_is_still_running`。22d2fdc は、`git archive` の写しに同じ試験を置いて流した | **不合格**（問題1） |
| 2a Ctrl+Break（身代わりの合図）で止める | 終了コード 0、「circuit-sim を止めました。」が1回、Traceback なし。続けて2回・3回、Ctrl+C の後、Ctrl+C の前でも同じ | Windows の経路を POSIX で流す入口（`signal.SIGBREAK` に SIGUSR1、uvicorn の受け持つ合図に足す、`cli.WINDOWS` を真）。応答が返るのを待ってから、合図を続けて送る | 合格（Linux。Windows は未確認） |
| 2b 待ち受けを始める直前の Ctrl+Break | 終了コード 0、「止めました」が1回 | 同じ入口で、`GuardedServer.run` の頭（製品の扱いの中、uvicorn が合図を受け持つ前）で合図を出す | 合格（Linux） |
| 2c 対照: 製品の扱いを外す | 終了コード −10（SIGUSR1 で終わる）、「止めました」なし | 同じ入口で `cli.break_as_interrupt` を何もしないものに差し替える | 合格（試験が、扱いの有無を見分ける） |
| 2d 扱いの出入り | 中にいる間だけ `default_int_handler`。出た後（例外で抜けても）、入る前の扱い（既定でも、自分で決めたものでも）に戻る。Windows でない OS、主スレッドの外、SIGBREAK の無い OS では変えない | `cli.break_as_interrupt` を直接呼ぶ | 合格 |
| 2e 修正を戻すと、足された試験が落ちるか | `main` の `break_as_interrupt` を外すと、実装者の `test_break_path_warns_first_then_stops_with_exit_code_0` と、こちらの 6 件が落ちる | 写しの中で1行を差し替えて流した | 合格（試験は実効的） |
| 2f 身代わりは、Windows の経路と同じ所を通るか | 通る所: 製品が決めた扱い → uvicorn が控えて戻す → `raise_signal` → Python の扱いがその場で動く → `KeyboardInterrupt` → `main` の後始末。違う所: 元の扱いが既定のときの終わり方（Windows は 3、POSIX は合図）と、OS がコンソールの合図を別のスレッドで届けること | ソース（uvicorn、`signalmodule.c`）を読んだ。違う所は、PO の PC の結果（3）と、修正の前の Windows の結果（1回目で知らせ、2回目で止まり始める所までは動いていた）で埋まる | 妥当。直した後の Windows の動きは未確認 |
| 3 安全の前提（D-053、D-056） | 起動の番号は引数に載らない、渡すのは設定の場所の中の転送用の HTML だけ、HTML は本人だけが読める（0600、場所 0700）、使われたら消える、止めたら消える、合言葉は HTML にも端末にも出ない。差分は `Launches`、`LaunchPages` に触れていない | 既存の受け入れ試験（`test_a0_key_launch_settings.py`、`test_a0_browser_recheck.py`）を、偽のブラウザの見張りつきで流した。製品を 6 通りに壊して、落ちることを確かめた（下の「テストの実効性」） | 合格 |
| 4 前回までの基準 | 受け入れ試験の全体: 821 passed、1 skipped、14 xfailed（見張りを足した後、こちらの新しい試験を足す前） | `pytest tests/acceptance` | 合格 |
| 5 修正の前の試験が、プロセスを残す仕組み | 22d2fdc の `test_windows_excluded_port_range_is_reported_as_denied` に結べるポートを渡すと、20.0 秒で `TimeoutExpired`、アプリ本体は動いたまま待ち受けを続ける | 試験の関数の本体を、netsh の代わりに空いているポートを返す形で呼んだ（`git archive 22d2fdc` の写し） | 再現した |
| 6 修正の後の同じ試験と、新しい試験 | b48cdf9 の `test_windows_excluded_port_range_is_reported_as_denied` と `test_windows_reserved_ports_are_reported_as_the_os_answers` も、起動に成功すると、同じく `TimeoutExpired` で抜け、アプリ本体が残る | 同じやり方（先に結んで確かめる部品が「拒まれた」と答え、起動は成功する形） | **穴は塞がっていない**（問題2） |
| 7 check.py | ruff は通る。pytest は 1 failed、1974 passed、23 skipped、14 xfailed（729 秒）。終了コード 1 | 下の「check.py の結果」 | 落ちるのは、問題1 の試験だけ |

観察（判定に使わない。どちらも Ctrl+C と同じ形で、今回の差分より前からある）:

- 止め終えた後、プロセスが終わるまでの間に2回目の合図が届くと、「止めました」は出るが、終了コードは 0 にならない。
  身代わりの合図での実測（1回目から 0〜0.3 秒後に2回目、各 3 回）: 0.15 秒までは終了コード 0、0.2 秒と 0.3 秒は
  −10（Windows の Ctrl+Break なら 3 に当たる）。2回目を Ctrl+C にしても同じ形（−2）。
- 起動の文を出してから、待ち受けを始めるまでの間に届いた合図は、製品の扱いの外（既定の動き）。受け入れ試験は、
  応答が返るのを待ってから合図を送る（つながるだけでは足りない。待ち受けのソケットは、その前からつながる）。

## 見つけた問題

### 1. 中（製品の後退）: ブラウザのコマンドが動いている間、Enter が効かない

- 内容: b48cdf9 は、端末の Enter を読み始めるのを、`webbrowser.open` が戻った後にした。Python の `webbrowser` は、
  名前を知らないコマンド（BROWSER の指定）の終わりを待つ（`GenericBrowser.open` の `p.wait()`）。ブラウザを閉じる
  まで戻らないコマンドのとき、その間 Enter を読まない。起動の文は「ここで Enter を押すと新しい URL を出します」と
  案内している。
- 再現: `pytest tests/acceptance/test_a0_fix5_enter_break.py -k enter_gives_a_new_url`。b48cdf9 は落ちる（Enter の後
  10 秒、新しい URL が出ない）。22d2fdc は通る。
- 影響: BROWSER に、Python の知らない名前のブラウザ（終わるまで戻らないもの）を置いている利用者。22d2fdc では
  効いていた。
- 受け入れ試験は、印（xfail）を付けずに、落ちるままにしてある（オーケストレーターの判断）。読み方が「待たない
  読み方」に変わっても通る書き方（順序に頼らない）。

### 2. 中（実装者の試験）: 起動したアプリ本体を、止めずに終わる経路がある

「起動に失敗して終わるはず」の起動を、終わるのを待つだけで、止める手当てが無い。期待と違って起動に成功すると、
`TimeoutExpired`（`communicate(timeout=…)`、`wait(timeout=…)`）で抜け、アプリ本体が待ち受けたまま残る。PO の PC に
残った PID 32092 は、この形（確かめたこと 5）。b48cdf9 の `tests/test_app_cli.py` で、この形のもの:

| 行 | 試験 | 残る条件 |
|---|---|---|
| 342 | `test_busy_port_is_reported_and_exits_2` | 使用中のポートで、起動してしまったとき |
| 563 | `test_privileged_port_is_reported_as_denied` | 一般の利用者がポート 1 を結べる環境（1024 未満を許す設定のコンテナなど） |
| 624 | `test_windows_excluded_port_range_is_reported_as_denied` | 先の確かめと起動のあいだに、ポートが空いたとき（確かめたこと 6 で再現） |
| 645 | `test_windows_reserved_ports_are_reported_as_the_os_answers` | 同じ（再現した） |
| 675 | `test_help_is_in_one_language` | 製品が `--help` で終わらなくなったとき |
| 713 | `test_bad_arguments_are_explained_in_one_language` | 製品が、誤った引数で起動してしまったとき |
| 1217 | `test_unwritable_config_folder_is_reported` | 設定の場所に書けてしまったとき |
| 1336 | `test_break_path_warns_first_then_stops_with_exit_code_0` | `break_warns_first_then_stops`（1227 行）の `try` より前（起動の URL を待つ、受け付けを待つ、状態を書く）で落ちたとき |

ほかに、`tests/test_app_browser.py` の `app`（121 行。モジュールの間だけ動かすフィクスチャ）は、`running.start()` が
起動の後で落ちると、後始末（`running.stop()`）が呼ばれない。

675 行と 713 行は、`--no-browser` を付けず、偽のブラウザも見張りも無い。製品が起動してしまうと、**本物のブラウザが、
生きている起動の番号を持つ HTML を開く**（実装者の決まり「ブラウザを開く起動は、必ず `launch_with_fake_browser`」の外）。

直し方の案（直すのは実装者）: 終わるのを待つ起動は、`subprocess.run(..., timeout=…)`（時間切れで止める）にするか、
`try/finally` で `stop(proc)` を呼ぶ。引数の誤りと `--help` の起動は、`--no-browser` を足すか見張りつきで起動する。
フィクスチャは、`start()` を `try` で包んで、落ちたら `stop()` する。実装者の試験の全体に効く見張り（下の受け入れ
試験の見張りと同じもの）を `tests/conftest.py` に置くかどうかは、tooling-engineer と決める。

### 3. 軽微（文書）: 設計書の 8章

- 8.2 の「ポート 50000 を何が使っているかは、まだ確かめていない」は、試験が残したアプリ本体と分かった。
- 8.1 が引いている Microsoft の文書 `signal` の表に、SIGBREAK は載っていない（終了コード 3 の根拠は、実機の結果）。

### 4. 道具の課題（直すのは tooling-engineer。判定には使わない）

- **`scripts/local_ci.py` は、確認が残したプロセスに気づかない。** 確認（`check.py`）がふつうに終わったときは、
  その先のプロセスを調べも止めもしない（`run_streaming` が止めるのは、時間切れと中断のときだけ）。
- **残ったプロセスが、取り出した場所を作業フォルダにしていると、Windows では片付けられない。** 試験はアプリ本体を
  `cwd=ROOT`（取り出した場所）で起動する。`clean_up` は、`git worktree remove --force` → `shutil.rmtree`（5 秒まで
  やり直す）→ もう一度 `git worktree remove` の順で、どれも失敗すると `False` を返す。知らせは、標準エラーの1行
  （「注意: 作業用の場所を片付けられなかった。消してよい: <場所>」）だけで、結果のファイルには残らない。理由（動いて
  いるプロセスがある）も言わない。PO の PC には、`%TEMP%\circuit-sim-ci-…` と、リポジトリの作業ツリーの登録
  （`git worktree prune` で外れる）が残っている見込み（コードからの見立て。実機では未確認）。
- **`import conftest` で `tests/conftest.py` を引く試験がある**（`tests/test_claude_guard.py`、
  `tests/test_dev_tools.py`）。pytest は、パッケージでない場所の conftest.py をどれも `conftest` という名前で読むので、
  下の場所に conftest.py を置くだけで、そちらを引いてしまう（集める段階でエラーになった）。受け入れ試験の
  conftest.py が、名前を `tests/conftest.py` に戻している。

## 受け入れ試験の手当て

**既にある受け入れ試験のファイルを 5 つ書き換えた**（オーケストレーターの指示。期待している中身は変えていない）。
ハーネスの決まり（既にあるファイルの書き換えは「変わった」と見なす）により、レビューと検証のゲートは、次の修正の
後の内容でやり直しになる。

| ファイル | 変えたこと |
|---|---|
| `test_a0_key_launch_settings.py` | 壊れた設定ファイルの試験に短い ID（`garbage`、`array`、`bad-utf-8`、`too-large`、`deep`）。ブラウザを開く起動は `a0_fake_browser` を通す。`App` の準備の途中で失敗したら、起動したアプリ本体を止める。`test_unused_launch_page_is_removed_when_stopped` を `try/finally` で包む |
| `test_a0_browser_recheck.py` | ブラウザを開く起動は `a0_fake_browser` を通す。`App` の準備の途中で失敗したら止める |
| `test_a0_fix3_stop_stale_sddl.py` | `test_sddl_with_parenthesis_inside_a_condition_string` に短い ID（日本語の名前は、試験に渡る値に残る） |
| `test_a0_fix4_restart_lastrun.py` | `test_file_argument_is_told_as_a_file_not_an_option` に短い ID（`ja`、`en`） |
| `test_e1a_units_limits.py` | `test_c13_wrong_dimension_is_an_error_naming_item_unit_and_expectation` に短い ID（項目と単位） |

200 文字を超える ID は、受け入れ試験に 8 件あった（70,130、30,102、294、254、221、206、204、203 文字）。直した後の
最長は 200 文字。

足したファイル:

| ファイル | 中身 |
|---|---|
| `conftest.py` | 受け入れ試験の全体に掛かる見張りの配線 |
| `acceptance_watch.py` | 起動したプロセスの控えと、残りの確かめ |
| `a0_fake_browser.py` | アプリ本体を「偽のブラウザだけ」で起動する部品 |
| `test_a0_fix5_watch.py` | 見張りそのものの試験（ID の長さ、偽のブラウザ、プロセス） |
| `test_a0_fix5_enter_break.py` | 問題1 の試験、偽のブラウザが失敗したときのアプリ本体ごとの確かめ、Ctrl+Break |

### 偽のブラウザが失敗しても、本物のブラウザへ進まない

`webbrowser` は、BROWSER の候補を OS の既定の候補の前に足すだけで、失敗した候補の次へ進む（`Lib/webbrowser.py` の
`register_standard_browsers` と `open`）。守りは二重で、どちらもアプリ本体を始める前に、同じプロセスの中で仕掛ける
（製品のコードは変えない）。

1. 候補の先頭に、「BROWSER の偽だけを試し、結果がどうでも『開いた』と答えて終わる」候補を登録する。
2. Python の監査の仕組み（`sys.addaudithook`）で、`webbrowser` が偽のほかのものを起動しようとしたら（`os.startfile`、
   `webbrowser` の中からの `subprocess.Popen`）、起動させずに例外で止め、記録に残す。

そのうえで、受け入れ試験の間、BROWSER に見張り役を置く。この部品を通さずにブラウザを開く起動をした試験があると、
本物の代わりに見張り役が呼ばれ、その試験は失敗になる（「起動に失敗して終わるはず」の起動が成功した場合も、
ここに掛かる）。

確かめ（`test_a0_fix5_watch.py`、`test_a0_fix5_enter_break.py`）:

- 偽が終了コード 1・3・7 で終わっても、起動できなくても、`webbrowser.open` はそこで終わり、本物の経路は呼ばれない。
- 対照（Linux）: 守りの 1 を外すと、偽の次に、本物の代わりに置いた `xdg-open` が呼ばれかける。守りの 2 が止めて
  記録し、`xdg-open` は実行されない。「偽が失敗すると本物へ進む」という前提も、ここで確かめた。
- Windows の経路（`os.startfile`）を守りの 2 が止めることは、Windows でだけ流れる試験にした（未確認）。

### 起動したプロセスを、必ず止める

- 試験ごと: その試験が起動したプロセスが、試験の終わり（ほかの後始末の後）に動いていたら、止めて、その試験を
  失敗にする。
- モジュールごと: モジュールの間だけ動かすフィクスチャが起動したものが、モジュールの終わりに動いていたら、止めて
  失敗にする。準備の途中で失敗したフィクスチャが起動したものは、その場で止める。
- 実行の全体: 始める前から動いているアプリ本体らしいプロセスを知らせる（止めない）。実行の終わりに、この実行から
  生まれたアプリ本体らしいプロセスが残っていたら、実行の全体を失敗にする（終了コード 72）。同じ実行の、受け入れ
  試験のほかの試験が残したものも、ここで見つかる（受け入れ試験が1つでも選ばれている実行のとき）。
- 「この実行から生まれた」の見分け方: Linux は、実行ごとの印を環境変数に置く（子も孫も引き継ぐ。ほかの担当の
  プロセスには触らない）。Windows は、プロセスの一覧（PowerShell）の親子をたどる。動いている親をたどって届くものは
  止める。親がもう無いものは、番号が使い回されるので、止めずに知らせる。

受け入れ試験の中にあった穴（直す前）: 6 つの `App` の準備（起動の後、URL と受け付けを待つ間に落ちると、呼んだ側の
`finally` に届かない）、4 つのフィクスチャ（同じ）、`test_unused_launch_page_is_removed_when_stopped`（`try/finally`
が無い）。書き換えるファイルを増やさないよう、見張りで受ける形にした（上の2ファイルは、直接も直した）。

確かめ: 見張りの3つのファイルと、わざと残す試験を、一時フォルダに写して別の pytest で流す。残したプロセスは
すべて止まり、残した試験はエラーになる。親が先に終わったアプリ本体らしいプロセスを残すと、終了コード 72 になる。
始める前から動いているものは、知らせるだけで止めない。

## テストの実効性

製品をわざと壊して、受け入れ試験が落ちることを確かめた（作業ツリーの写しの中で）。

| 壊し方 | 落ちた試験 |
|---|---|
| ブラウザに起動の URL（番号つき）を渡す | `test_browser_gets_only_the_location_of_a_private_page`、`test_launch_page_flow_leaves_no_number_behind` |
| 転送用の HTML の場所に、番号を足して渡す | 同じ 2 件 |
| 転送用の HTML を 0644 で作る | `test_browser_gets_only_the_location_of_a_private_page` |
| 番号が使われても HTML を消さない | 同じ 2 件 |
| 止めたときに HTML を消さない | `test_unused_launch_page_is_removed_when_stopped[SIGINT]`、`[SIGTERM]` |
| HTML に合言葉を書く | `test_browser_gets_only_the_location_of_a_private_page` |
| `main` の SIGBREAK の扱いを外す | 実装者の 1 件と、こちらの 6 件 |

所見:

- 実装者の `test_break_path_…` の2件と、こちらの身代わりの試験は、同じ考え方（SIGUSR1 を当てる）。Linux で
  見分けられるのは「`main` が扱いを使っているか」まで。
- Enter の順序の修正を見分ける実装者の試験（`test_enter_is_read_only_after_the_browser_call_returns`）は、問題1 の
  後退を「正しい動き」として固定している。次の修正で、書き直しが要る。
- `tests/test_app_cli.py::test_browser_gets_only_the_location_of_a_private_page[stdin-pipe]` は、Linux では、修正の
  有無にかかわらず通る（止まるのは Windows だけ）。
- 端末（Linux の tty、Windows のコンソール）から Enter を読む道には、自動の試験が無い（実装者の試験も受け入れ試験も、
  標準入力はパイプ。`enter_source` の試験は、パイプ・NUL・ファイルだけ）。Linux は疑似端末で流せる。今回は足して
  いない（次の修正で読み方が変わるので、そのときに足す）。

## Windows で未確認の点

1. 直した後の Ctrl+Break の終了コード 0 と「止めました」（実装者の Windows の試験 2 件、
   `test_a0_fix4_browser.py` の起動し直し、こちらの `test_ctrl_break_on_windows_stops_with_exit_code_0`）。
2. 標準入力がパイプの起動で、偽のブラウザが呼ばれること（落ちていた受け入れ試験 2 件）。
3. 受け入れ試験の ID を短くした後、準備の段階のエラーが出ないこと。
4. こちらの見張りの、Windows だけの部分: PowerShell でのプロセスの一覧、`taskkill /T`、`os.startfile` を止める監査、
   親がもう無いプロセスの見分け。一覧を作れない PC では、関係する試験はスキップになり、実行の終わりに
   「調べられなかった」と出る。
5. 窓を閉じたとき、Ctrl+C の2回押し（別のプロセスから送れないので、手で確かめる。前回までと同じ）。
6. PO の PC に、最初の確認の作業用の場所（`%TEMP%\circuit-sim-ci-…`）と作業ツリーの登録が残っているか。

PO に流してもらうコマンド（コマンド プロンプト）:

```
cd /d C:\dev\circuit-sim
git fetch origin
git checkout --detach origin/A0
python -m pytest tests/acceptance/test_a0_browser_recheck.py tests/acceptance/test_a0_fix3_stop_stale_sddl.py tests/acceptance/test_a0_fix4_browser.py tests/acceptance/test_a0_fix4_restart_lastrun.py tests/acceptance/test_a0_fix5_enter_break.py tests/acceptance/test_a0_fix5_watch.py tests/acceptance/test_a0_key_launch_settings.py tests/acceptance/test_e1a_units_limits.py tests/test_app_cli.py tests/test_app_config.py tests/test_app_server.py -q -rs -p no:cacheprovider -o junit_family=xunit1 --junitxml=a0-windows-2.xml > a0-windows-2.txt 2>&1
echo exit code %ERRORLEVEL%
tasklist /FI "IMAGENAME eq python.exe"
dir /b /ad "%TEMP%\circuit-sim-ci-*"
git worktree list
```

合格の見分け方:

- いまの内容（製品は b48cdf9）: 落ちるのは
  `test_a0_fix5_enter_break.py::test_enter_gives_a_new_url_while_the_browser_command_is_still_running` の 1 件だけで、
  終了コードは 1。エラーは 0。次の修正の後は、失敗 0、終了コード 0。
- `a0-windows-2.txt` の終わりの近くに「受け入れ試験の見張り: …この実行から生まれたアプリ本体は残っていない。」が
  出ている。「失敗: 試験が起動したプロセスが、実行の終わりにも動いていた」が出たら、終了コードは 72 で、残した
  プロセスが並ぶ（実装者の試験が残したものも、ここに出る）。
- 本物のブラウザの窓が開かない。
- `tasklist` に、流す前から動いていたもののほかに python.exe が残っていない。
- 終わりの2つは、最初の確認の残りの確かめ（問題4）。`circuit-sim-ci-` で始まるフォルダが出たら消してよい。
  `git worktree list` に、もう無い場所が出たら `git worktree prune` で外れる。

## 次の修正で確かめ直す項目

1. `test_enter_gives_a_new_url_while_the_browser_command_is_still_running` が、Linux と Windows で通る。
2. 標準入力がパイプの起動で、BROWSER のコマンドが始まる（Windows。読み取りの待ちが無くなったこと）。先に押した
   Enter を捨てない。`--no-browser`、端末でない入力、開けなかったときも、Enter が効く。
3. Windows のパイプの「待たない読み方」の角: パイプが閉じたとき（入力の終わり）に読むのをやめる、何も届かない間に
   CPU を使い続けない、端末（コンソール）のときの読み方は変わらない。端末から Enter を読む道の試験（Linux は
   疑似端末）を足す。
4. SIGBREAK の修正が残っている（確かめたこと 2a〜2e と、Windows の実機）。
5. 実装者の試験の後始末（問題2 の表）と、見張りを通さないブラウザの起動（675 行、713 行）。
6. 受け入れ試験の全体と check.py。Windows での流し直し（上のコマンド）。
7. 設計書の 8章（問題3 と、Enter の読み方の説明）。

## check.py の結果

- 流した内容: 受け入れ試験を足したコミット `f8af97a`（ツリー `0a4729d46ceb4ca9b609b835e0f0035f635c4125`）。その後の違いは、
  この記録のファイルだけ（流している間に、下書きを作業ツリーに置いた。試験は読まない）。
- コマンド: `PATH=/usr/local/bin:/usr/bin:/bin CIRCUIT_SIM_NODE=/opt/node22/bin/node python scripts/check.py`
- 結果: ruff は `All checks passed!`。pytest は `1 failed, 1974 passed, 23 skipped, 14 xfailed in 729.48s`。終了コード 1。
- 落ちたのは `tests/acceptance/test_a0_fix5_enter_break.py::test_enter_gives_a_new_url_while_the_browser_command_is_still_running`
  の 1 件だけ（問題1。落ちるのが正しい）。
- 実装者の報告（b48cdf9 で 1925 passed、20 skipped、14 xfailed）との差は、こちらが足した試験の分（通過 49、失敗 1、
  Windows でだけ流れるスキップ 3）。
- 終わりの見張りの表示: 「受け入れ試験が起動したプロセスは 500 個。実行の終わりに、この実行から生まれたアプリ本体は
  残っていない。」流した後、この作業ツリーと `/tmp/a0-verify5/` を作業フォルダにするプロセスが残っていないことを、
  `/proc` で確かめた。
- 途中の手順の誤り（結果には関係しない）: 見張りの部品を直した後、流している途中の check.py を自分で止めた。
  pytest は SIGINT で終わらず（playwright の呼び出しの中だった）、起動していたアプリ本体とブラウザごと、自分の
  プロセスだけを番号で選んで止めた。上の結果は、その後に始めから流し直したもの。
