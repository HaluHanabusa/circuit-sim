# W2 独立検証の記録・4回目（verifier。f757e0c）

- 対象: ブランチ `W2`、先頭 `f757e0c`（ツリー `c6f5e7e99744f69adebd65c022fc96e498705ffa`）。修正は `git diff 0b85b13 f757e0c`
- これまでの記録（`W2-verifier.md`、`-2.md`、`-3.md`）は書き換えていない。受け入れ試験への変更も無い（`git diff 6ec1b6c f757e0c -- tests/acceptance/` は空）
- 手元は Linux、Python 3.13.18、git 2.43.0。本物の `claude` は起動していない。PATH は「罠の偽 `claude`、`/usr/local/bin`、`/usr/bin`、`/bin`」だけ。
  罠はこの回のすべての実行で 0 回だった

## 判定: 基準1〜4と6は合格。基準5は未確認（POの f757e0c の結果待ち）

重大な問題は見つからなかった。ただし、オーケストレーターに伝えられた「Windows では skipped が Linux より2件少ない見込み」は誤りだった。
私の模擬では、Windows の skipped は Linux より 25 件多い（下の「基準5」）。この見込みを合否の目安に使うと、正しい結果を疑うことになる。

| # | 基準 | 実測値 | 合否 |
|---|---|---|---|
| 1〜4 | 受け入れ試験（W2 の29件）と、これまでの再現手順の試験が、全体の確認の中で通る | 合格 |
| 5 | POの結果待ち | 未確認 |
| 6 | `PATH=<罠>:/usr/local/bin:/usr/bin:/bin python scripts/check.py` が終了コード 0。1059 passed、7 skipped、9 xfailed（計 1075）、322 秒 | 合格 |

## 修正の確かめ（主張の1〜5）

1. **エラー画面（SetErrorMode）: 書き方は正しい。実際に画面が出ないことは Windows でしか確かめられない。**
   - 印の値は正しい（`SEM_FAILCRITICALERRORS` 0x0001、`SEM_NOGPFAULTERRORBOX` 0x0002、`SEM_NOOPENFILEERRORBOX` 0x8000）。もとの印に足している（`GetErrorMode() |`）。
   - 子プロセスは、`CREATE_DEFAULT_ERROR_MODE` を付けない限り、この印を引き継ぐ。scripts にその指定は無い。
   - `cycle.py`、`local_ci.py`、`doctor.py` の `main` の最初と、試験の `pytest_configure` で設定している。
   - 確かめる場所は、POの結果で `tests/test_error_dialogs.py` の Windows だけの2件が通ること、所要時間が 1216 秒のような待ちになっていないこと。
2. **違う文字コード: 正しい。** `other_encoding` は、記録された値と `codecs.lookup` の名前で比べて、別の値を選ぶ。cp932 が記録されたPCでも違いになる。
   試験 `test_other_encoding_always_differs` も通る。
3. **化けは試験の前提の誤り: 主張は正しい。**
   - 本体（`scripts/*.py`）が子プロセスの出力やファイルを読む箇所は、すべて `encoding="utf-8"` を指定していた。
     `run_captured`、`run_git`、`cycle.py` の `run_check`（子に `PYTHONIOENCODING=utf-8` を渡す）、結果と状態のファイル、仕様が該当する。
     `local_ci` の確認の出力は読まずに画面へ流し、件数と名前は JUnit の XML（文字コードは XML の宣言で決まる）から読む。
     既定の文字コードで読む箇所は残っていなかった。
   - Linux でも、同じ種類の食い違いを再現した。試験側の読み方（`text=True`、既定の文字コード）を戻した複製で、子の出力を cp932 にして
     `test_python_started_elsewhere_reads_the_checked_content_first` を流すと、試験用の確認スクリプトが読み取りで落ちて、試験が失敗した。
     修正後の試験側（ASCII の JSON）では2件とも通る。どちらでも本体は同じ。
   - POの記録の化けた場所も `…\checkout\product.py`、つまり確かめる場所の版を指していた（読み込んだ版は正しかった）。
4. **合図が2回続くと claude が止まらない件: 直っていた。** 止める処理（`kill_tree`）の始めに2回目の合図を起こす形で、2つの版を比べた（SIGHUP→SIGHUP、SIGHUP→SIGTERM）。
   - 6ec1b6c は、2回目で止める処理が切られ、子プロセスが自分で終わるまで（4.1 秒）待ってから戻った（実物なら claude の終わりまで待ち続ける）。
   - f757e0c は 0.0 秒で子を止めて戻った。合図の扱いも元に戻った。
   - 間を置かずに2回送るだけ（`kill` を2回）では、Python が1回にまとめるので、どちらの版でも止まる。この経路の再現には、上の形が要る。
5. **PATH から外した場所の表示: 試験が押さえている**（壊す確認の R12、R13、R15）。表示は画面だけで、結果のファイルには入らない。
   ホームの下は `~` から書く。

## 壊す確認（4回目の修正で入った分岐）

15 か所を壊した。内訳は `cycle.py` 6、`local_ci.py` 1、`doctor.py` 1、`tests/conftest.py` 7。**すべてで試験が落ちた。生き残りは無い。** 罠は 0 回だった。

| ID | 壊し方 | 落とした試験 |
|---|---|---|
| R01 | 合図を受けた後、続く合図を無視しない | `test_stop_posix.py::test_second_stop_signal_while_stopping_the_cli_does_not_leave_it_running` |
| R02、R03 | 入れ子の内側を出るとき外側の扱いをそのまま戻す、外側の枠の印を付けない | `test_stop_posix.py::test_stop_signal_stays_ignored_until_the_outermost_clean_up_is_done` |
| R04 | `cycle.py` がエラー画面を止めない | `test_error_dialogs.py::test_harness_turns_the_error_dialogs_off_first` |
| R05、R06 | 起動できない実行ファイルの印を外す、もとの印を残さない | `test_error_dialogs.py::test_error_dialogs_are_turned_off_on_windows_keeping_the_existing_settings` |
| R07 | `local_ci.py` がエラー画面を止めない | `test_local_ci.py::test_local_ci_turns_the_error_dialogs_off_first` |
| R08 | `doctor.py` がエラー画面を止めない | `test_dev_tools.py::test_doctor_turns_the_error_dialogs_off_first` |
| R09 | 試験のプロセスがエラー画面を止めない | `test_claude_guard.py::test_every_test_process_turns_the_os_error_dialogs_off` |
| R10、R11 | 試験の側の印から起動できない実行ファイルの印を外す、もとの印を残さない | `test_claude_guard.py::test_tests_turn_the_os_error_dialogs_off_on_windows[Windows]` |
| R12、R15 | ホームの下を `~` で書かない、リンクで残した道具を表示しない | `test_claude_guard.py::test_session_start_report_names_the_places_taken_off_and_the_tools_kept` |
| R13 | 外した場所を表示しない | `test_claude_guard.py::test_session_fails_when_the_sentinel_is_started_even_if_every_test_passes` |
| R14 | 中を確かめられない場所を残す | `test_claude_guard.py::test_places_that_hold_anything_launched_as_claude_are_taken_off` |

R05、R06、R10、R11 は、Linux では `ctypes` を差し替えた試験が落とした。実際の Windows の画面は、`test_error_dialogs.py` の Windows だけの2件でしか確かめられない。

## 基準5: Windows で何件が飛ばされるか（模擬）

- 測り方: 集めるだけの実行（`--collect-only`）で、試験のモジュールに Windows だと思わせた（`os.name` を `"nt"` にし、`pathlib.Path` は Linux の形のままにする。
  `msvcrt` と `ctypes.WinDLL` は形だけの偽物）。そのうえで、試験ごとの skip、skipif、xfail の印を数えた。
  数値計算の9ファイルは、この模擬では読み込めなかった。そこには OS で分かれる箇所が無いことを確かめ、Linux の数を使った。
  実行時に飛ばす `pytest.skip` は1件（W2 の受け入れ試験の、別のOSの結果の試験）を足した。
- 模擬の確かめ: 同じ模擬を 6ec1b6c に当てると、件数 1054、skipped 35、passed+failed 1019 となり、POの実際の結果（1054、35、1017+2）と一致した。
- f757e0c の見込み:

| | 件数 | passed | skipped（JUnit。xfail を含む） | 内訳 |
|---|---|---|---|---|
| Linux（実測） | 1075 | 1059 | 16 | skip 7、xfail 9 |
| Windows（模擬） | 1075 | 1034 | 41 | skip 32、xfail 9 |

  - Windows の skip 32 の内訳は次のとおり。`test_stop_posix.py` 15、`test_claude_guard.py` 7、`test_harness.py` 3、W2 の受け入れ試験 3（ほかに実行時の1）、
    `test_local_ci.py` 2、`test_w1_recheck.py` 1。
  - Linux だけで飛ばす7件（Windows でだけ走る試験）は、Windows では走る。

## 基準5: f757e0c の結果で確かめること

- `commit` が `f757e0c7508e688c92d8036deb4abb3b5cca5907`、`tree` が `c6f5e7e99744f69adebd65c022fc96e498705ffa`。
  `mode` が `checkout`、`ref` が `origin/W2`、`dirty` が false、`changed` が空。
- `os.system` が `Windows`、`encoding.locale` が `cp932`、`encoding.utf8_mode` が false。
- `check.returncode` が 0、`timed_out` が false。`check.seconds` は数分。fee52d2 は 476 秒で、6ec1b6c の 1216 秒のような待ちが無いこと。
- `tests.total` が 1075、`failed` と `errors` が 0、`failed` の一覧が空、`verdict` が `pass`、`reason` が `ok`。
  見込みは passed 1034、skipped 41。**skipped は Linux（16）より多くて正しい。** 41 から数件ずれても、OS で分かれる試験の扱いの差なので、
  件数の合計が 1075 であることを優先して見る。
- 前回落ちた2件が `failed` に無い。1件目の `test_python_started_elsewhere_reads_the_checked_content_first` は、今回 `[utf-8]` と `[cp932]` の2つに分かれた。
  2件目は `test_result_that_is_not_for_the_same_check_is_not_reused[既定の文字コードが違う結果]`。
- 試験の間に Windows のエラー画面が出なかった（POに確かめる）。画面に「見張り役の claude が … 回起動された」が無い。
  `%TEMP%\circuit-sim-ci-*` と `%TEMP%\circuit-sim-guard-*` が残っておらず、`git worktree list` が1行。
- 結果に、PCの名前、利用者名、`C:\Users\…` のような場所が入っていない。
- ハーネスの側で `python scripts/cycle.py record-ci W2 --result <ファイル>` が終了コード 0。

## 追加した試験と記録

- 試験は足していない。この記録 `docs/verification/W2-verifier-4.md`。
- 試し書きは `/tmp/w2-verify4/` に置いた。
  - 2回続く合図: `double.py`、`hup2.py`
  - 壊す確認: `mutate4.py`
  - Windows の模擬で数える差し込み: `winsim/winsim_plugin.py`、`winsim/linux_plugin.py`
  - 試験側の読み方を戻した複製: `rev3/`
