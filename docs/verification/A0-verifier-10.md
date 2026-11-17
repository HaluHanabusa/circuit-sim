# A0（アプリの骨組み）の独立検証（10回目: Windows の実機の結果の取り込みと、受け入れ試験 1 件の直し）

- 担当: verifier（実装の ui-engineer とは別。9回目の検証役とも別の実行で、記録 `docs/verification/A0-verifier-9.md`
  を読んで引き継いだ）
- 対象: ブランチ `A0`。始めの先頭 `70297bb`。この回のコミット: `8c456f1`（受け入れ試験の手当て）、この記録のコミット
- 日付: 2026-11-17
- 環境（Linux）: Python 3.13.18、pytest 9.1.1。試験と起動は `PATH=/usr/local/bin:/usr/bin:/bin`（本物の claude の
  無い PATH）。使い捨ての道具と写しは作業ツリーの外（`/tmp/a0-verify10/`）に置いた。製品のコードと実装者の試験には
  触っていない
- Windows の実機の結果: PO の PC（Windows 11、Python 3.13.2）で、`0751473` の受け入れ試験 11 ファイルと実装者の
  アプリの試験 5 ファイルを流したもの（要約 `/tmp/reports/A0-windows-run2.md`、生の出力
  `/tmp/winci/r2/a0-windows-2.txt`（cp932）、`a0-windows-2.xml`（JUnit xunit1）、`a0-windows-2-after.txt`）。
  生の出力と XML は、こちらで読み直した（XML は `python3 -I` で、作業用の道具は別の場所から読んだ）

## D-078 の守り方（この回）

- 壊した写しは 4 つ（`copy-old`、`copy-new`、`mut-M1`、`mut-M2`。どれも `git archive 70297bb` から作った作業ツリーの
  外の写し）。壊したのは、出力のパイプを閉じる所（`Launched._read` の `stdout.close()`）と、残した試験を失敗にする所
  （見張りの `pytest.fail`、`Launched.end` の `raise`）だけで、**止める道・合図を送る道（`kill_tree`、
  `kill_posix_tree`、`reap` の止める部分）には触っていない**。本物の合図は、壊していない部品が送るものだけ。
  `timeout 600` と `ulimit -v 4000000` を付けた。
- 中の pytest をこちらで流す道具（`tools/rw_inner.py`）は、作業ツリーの部品をそのまま読み込み、この実行の印（環境変数
  `A0V10_RUN=<値>`）を付けた。対照の 2 変種（`noclose-*`）だけ、中の pytest の conftest で `Launched._read` を出力の
  パイプを閉じない形に差し替えた（止める道は触らない）。流した後、印を持つプロセスが残っていないことを `/proc` で確かめた（どの実行も 0）。
- 全部の実行の後、アプリ本体（`-m circuit_sim`）、pytest、ヘッドレスの Chrome、`/tmp/a0-verify10` を引数に持つ
  プロセスが残っていないことを `/proc` で確かめた。

## 判定

| 範囲 | 判定 | 要点 |
|---|---|---|
| Windows（PO の PC、`0751473` の試験の範囲） | **製品に関わる失敗は 0** | 1 failed は受け入れ試験の作り（要約の行の探し方）。2 xpassed は Windows では落ちないと分かっていた試験（印を外した）。2 xfailed は既知の製品の軽微（strict）。37 skipped は、どれも OS の違いで決めてあるもの |
| Linux（`8c456f1`。check.py を 1 回） | **失敗 0** | `2082 passed, 29 skipped, 15 xfailed`、XPASS・FAILED なし。ruff は通る。見張りは「残っていない」 |
| 直した受け入れ試験 | **直した**（3 ファイル） | 下の「1」。確かめる中身は弱めていない |
| 2. ResourceWarning（`70297bb` の部品） | **出ない**（Linux） | 下の「2」。Windows で出た 4 つは、`0751473` の部品が出力のパイプを閉じなかったため |
| 3. 2 xpassed | `test_huge_pid_in_the_record_does_not_crash` の 2 通り。**Windows では印を外した** | 下の「3」 |
| **総合** | **合格**（Linux と Windows（`0751473` の試験の範囲）で、製品に関わる失敗は 0。重大 0、中 0） | Windows で未確認の範囲は下の「Windows で未確認の点」 |

製品は `0751473` と `70297bb`（と `8c456f1`）で動きが同じ: `git diff --stat 0751473 70297bb -- circuit_sim` は
`circuit_sim/app/cli.py` だけで、文書文字列を除いた構文木（`ast.dump`）が同じ、コメントの並び（`tokenize`）も同じ、
製品は `__doc__` を使わない（`grep` 0 件）。こちらで確かめ直した（`tools/astcmp.py`）。

## Windows の実機の結果（要約のファイルの中身と、こちらで確かめたこと）

### 要約のファイル（`/tmp/reports/A0-windows-run2.md`）の中身

- 日時: 2026-11-17 19:10 頃（JST）。対象 `0751473`（製品は `9f594c5` 以降と同じ動き）。Windows 11、Python 3.13.2
- 流したもの: `.cycle\run-a0-windows.bat`（受け入れ試験 11 ファイルと実装者のアプリの試験 5 ファイル）
- 結果: `1 failed, 729 passed, 37 skipped, 2 xfailed, 2 xpassed`（314 秒）
- 前回（`22d2fdc`）落ちた 10 件は、すべて通った（Ctrl+Break の終了コード、ブラウザのコマンドが始まる 2 件、試験の ID の
  長さ、XDG のパス、予約のポート）
- 実機で確かめられた事実（JUnit の properties）: 本物の PeekNamedPipe の答えが `_winapi` と一致。入力の見分け（パイプは
  'pipe'、NUL とファイルは None）。対照（読んで待つ間に起動した子は始まれない）。結び損ねの番号（使用中 10048、拒まれる
  10013）。予約のポート 50000* は、この PC では結べた
- 落ちた 1 件: `test_a0_fix6_test_hygiene.py::test_leaked_processes_are_stopped_and_their_tests_fail`（最後の行を見る
  試験が、要約の後ろの ResourceWarning の行を見た）
- PO の PC に残っているもの: `%TEMP%\circuit-sim-ci-lnfn6b61`（最初の確認の作業用の場所。git の登録は無い）、
  `C:\dev\circuit-sim` の空のファイル `cd`、`powershell`、`tasklist`。python.exe の残りは無い

### こちらで確かめたこと

| 確かめたこと | 実測 | 測り方 | 合否 |
|---|---|---|---|
| 件数 | XML の試験 771 件 = 729 + 37 + 2 + 2 + 1。xunit1 では xpassed は通過に入るので、XML の通過は 731（= 729 + 2）。XML の `skipped` 39 = 37 + xfailed 2（`type="pytest.xfail"`）。失敗 1、エラー 0。出力の最後の行と一致 | `tools/xmlsum.py`（XML）、出力の最後の行 | 一致 |
| 落ちた 1 件 | XML の失敗は `test_leaked_processes_are_stopped_and_their_tests_fail` だけ。出力の中で、中の pytest の要約は `3 failed, 1 passed, 2 errors in 8.47s`（期待どおり）で、その後ろに `PytestDeprecationWarning` の説明と `<sys>:0: ResourceWarning: unclosed file <_io.TextIOWrapper name=15/14/13/12 encoding='utf-8'>` の 4 行が続いた。試験は最後の行（name=12 の行）を要約として見て落ちた | 出力（cp932 を UTF-8 にして読んだ） | 受け入れ試験の作り（製品でない） |
| 前回落ちた 10 件 | 9 件は通過: `test_launch_page_flow_leaves_no_number_behind`、`test_restart_without_port_returns_…`、`test_browser_gets_only_the_location_of_a_private_page`（受け入れ）、`test_ctrl_break_on_windows_warns_first_then_stops`、`test_config_dir_follows_os_conventions`（ID は `xdg-absolute` などに変わった）、`test_deeply_nested_json_is_reported_not_500` の 3 件（ID が短くなった）、`test_broken_settings_file_starts_with_defaults[too-large]`。**1 件（`test_windows_excluded_port_range_is_reported_as_denied`）は通過ではなくスキップ**（「この PC には、OS に拒まれる（10013）予約のポートが無い: 50000*: 結べた」。試験の作りどおり）。要約の「すべて通った」は、正しくは「9 件は通り、1 件はこの PC では流れない（スキップ）」 | XML の試験ごとの結果 | 問題なし（言い方の違いだけ） |
| JUnit の properties | `test_windows_real_pipe_answers_match_the_standard_library_and_end_with_none`: 空 0/0、3 バイト送って 3/3、2 バイト読んで 1/1、100 バイト送って 101/101、書く側を閉じた後 101、読み切って None。`test_windows_inputs_are_told_apart`: パイプ 'pipe'・答え 0、NUL とファイルは None・`OSError: [Errno 22] PeekNamedPipe failed (Windows error 1)`。`test_control_a_waiting_read_keeps_the_child_from_starting_on_windows`: `child=HANG`。`test_real_bind_errors_on_windows_have_the_numbers_of_the_stand_ins`: `{'in_use': (10048, 10048), 'denied': (13, 10013)}`。予約のポートの 2 件: `50000*: 結べた` | `tools/xmlprops.py` | 要約と一致 |
| 2 xpassed と 2 xfailed | 下の「3」 | — | — |
| スキップ 37 件 | `-rs` の 27 行の件数の和が 37。理由は、どれも OS の違いで決めてあるもの（POSIX だけの合図・権限・疑似端末・名前付きパイプ・/proc、Windows ではほかの試験が本物を流す、この PC に予約のポートが無い、シンボリックリンクを作れない） | 出力の `SKIPPED [n]` の行 | 問題なし |
| 流した後 | `a0-windows-2-after.txt`: 終了コード 1、python.exe のプロセスは無い（`INFO: No tasks are running …`）、`circuit-sim-ci-lnfn6b61` が残っている、作業ツリーは `C:/dev/circuit-sim` だけ | ファイルを読んだ | 要約と一致 |

Windows で中の pytest が ResourceWarning を既定で見せた理由（Linux では、既定の警告の扱いでは出ない。下の「2」の
`noclose-plain`）は確かめていない。`run-a0-windows.bat` には `-W` も `-X dev` も無い（読んだ）。中の pytest は外の環境
変数をそのまま受け継ぐので、PO の PC の環境変数（`PYTHONWARNINGS`、`PYTHONDEVMODE` など）か、pytest の版の違いが
考えられる（**未確認**）。

## 1. 直した試験

**既にある受け入れ試験の書き換え**（オーケストレーターの依頼の 1・3 による）。ハーネスの決まりでは、既にあるファイルの
書き換えは「内容が変わった」に当たり、`70297bb` のツリーで記録したゲートは `8c456f1` 以後のツリーでは無効になる。

| ファイル | 直し方 | Linux | Windows の見込み |
|---|---|---|---|
| `tests/acceptance/test_a0_fix6_test_hygiene.py`（依頼の 1） | 要約の行を、最後の行ではなく、要約の形の行（行の頭から `=*` `N 種類`（`, N 種類` の繰り返し）` in N.NNs`。種類は failed・passed・error(s)・skipped・xfailed・xpassed・deselected・warning(s)）のうち最後のものとして探す（`pytest_summary`）。確かめる中身（`3 failed`・`1 passed`・`2 errors` が要約にある、終了コード 1、残したプロセスが見張りの後に動いていない、3 つの番号が控えられた）は変えていない | 通る | 通る見込み（下の確かめ 1） |
| `tests/acceptance/test_a0_fix8_procs_browser.py`（同じ書き方。依頼の 1） | 子の Python の結果を `done.stdout.strip().splitlines()[-1]` で読んでいた 2 か所（`70297bb` の 107・193 行。標準出力だけを見るので、今回の警告の行には当たらなかった）を、`{` で始まる行のうち最後のもの（`child_result`）にした。JSON の行が無ければ、出力を添えて落とす | 通る（16） | 1 は Windows で流れる（まだ流していない） |
| `tests/acceptance/test_a0_fix4_restart_lastrun.py`（依頼の 3） | `test_huge_pid_in_the_record_does_not_crash` の `xfail(strict=not WINDOWS)` を `xfail(not WINDOWS, strict=True)` にした（Windows では印なし。POSIX は strict のまま）。理由の文とモジュールの説明に、実機の結果を書き足した | XFAIL 2（変わらない） | 通過 2（実機で通った形） |

ほかの受け入れ試験に、同じ「最後の行を見る」書き方は無い（`tests/acceptance/` を `splitlines()[-`、`rsplit`、
`[-1]`、`endswith`、`passed`・`failed` の検索で洗った）:

- `test_a0_fix5_watch.py` の `counts`（559 行）は、もとから `in N.Ns` を含む行のうち最後のものを見る（直す要なし）。
- `test_w1_recheck.py`（227・234 行）は、出力の全体に `N passed` / `1 failed` が含まれるかを見る（最後の行に頼らない）。
- 実装者の試験（`tests/*.py`）には無い。`scripts/cycle.py` 1045 行（`cmd_check`）が出力の最後の行を見せるが、表示だけで、
  合否は終了コードで決める（観察。道具の担当の範囲）。

### 確かめ（依頼の 1）

1. **Windows の実物の出力で**: 外の試験の説明に入っていた、中の pytest の出力の終わり（3000 文字）を取り出して、前と後の
   探し方に通した（`tools/check_summary.py`）。前の探し方は `<sys>:0: ResourceWarning: … name=12 …` を返し（Windows の
   失敗の再現）、新しい探し方は `3 failed, 1 passed, 2 errors in 8.47s` を返し、3 つとも見つかる。
2. **Linux で Windows と同じ形を作って**: 出力のパイプを閉じない部品（`0751473` の `Launched._read` の形）の写しで、
   `PYTHONWARNINGS=always::ResourceWarning` を付けて流すと、前の試験（`copy-old`）は Windows と同じ形で落ち
   （終わりの行: `ResourceWarning: Enable tracemalloc to get the object allocation traceback`。中の要約は
   `3 failed, 1 passed, 1 warning, 2 errors`）、新しい試験（`copy-new`）は通る。
3. **落ちるべきときに落ちる**（壊した写し。止める道は触らない）:
   - M1 見張りが、止めた試験をエラーにしない → 新しい試験は落ちる（要約の行 `3 failed, 1 passed in 6.72s`、`2 errors` が無い）
   - M2 `run_to_end` が、止めた後に落とさない → 新しい試験は落ちる（要約の行 `2 failed, 2 passed, 2 errors in 6.89s`）
   - 作った出力: 数が違う（`2 failed, 2 passed, 2 errors`）、要約が無い（落ちた試験の説明の中の `E   3 failed, …` だけ）、
     途中で終わった、正しい要約の後ろに別の要約 → どれも「失敗になっていない」で落ちる。`=` の囲みと `(0:00:08)` 付き、
     `1 warning` 入りの正しい要約は通る。
4. 中の pytest をこちらで 6 通りに流した出力（下の「2」）でも、新しい探し方は、どれも `3 failed, 1 passed, 2 errors`
   を含む要約を返す（前の探し方は、閉じない部品＋警告を見せる形だけで外れる）。

## 2. ResourceWarning（unclosed file）は `70297bb` の部品でも出るか

中の pytest（受け入れ試験の `LEAKING_TESTS`。起動 4 回: 起動に成功してしまう、途中の例外、行を待つ時間切れ、`--help`）
を、受け入れ試験と同じ形（同じコマンドと環境変数。`PATH` は claude の無いもの）で、警告の扱いを変えて流した
（`tools/rw_inner.py`。結果は `/tmp/a0-verify10/rw/<変種>/output.txt`）。

| 変種 | 部品 | 警告の扱い | ResourceWarning の行 | 要約が最後の行か | 残り（`left.txt`、印つきのプロセス） |
|---|---|---|---|---|---|
| plain | `70297bb` | 既定 | 0 | はい | 0、0 |
| always | `70297bb` | `-W always::ResourceWarning -X tracemalloc=25` | 0 | はい | 0、0 |
| dev | `70297bb` | `-X dev -X tracemalloc=25` | 0 | はい | 0、0 |
| error | `70297bb` | 解釈系に `-W error::ResourceWarning -X tracemalloc=25`、pytest にも `-W error::ResourceWarning` | 0 | はい | 0、0 |
| noclose-plain（対照） | 出力のパイプを閉じない形（`0751473` の `_read`） | 既定 | 0 | はい | 0、0 |
| noclose-always（対照） | 同じ | always と同じ | **3**（試験の中で 1、終わりに 2） | **いいえ**（後ろに割り当ての場所の行が続く） | 0、0 |

- **`70297bb` の部品では出ない**（警告をすべて見せる・誤りにする、どの形でも 0）。`Launched._read`（346〜359 行）が、
  出力を終わりまで読んだら `finally` で `stdout.close()` を呼ぶ。
- 対照（閉じない形、同じ警告の扱い）では出て、割り当ての場所は `tests/app_procs.py` 128 行 `subprocess.Popen(cmd, **kwargs)`
  → `subprocess.py` の `io.TextIOWrapper(self.stdout, …)`（`Launched` が `text=True, encoding="utf-8"` で開く出力の
  パイプ）。だから、この測り方は、閉じ忘れを見つけられる。
- Windows で出た 4 つ（`TextIOWrapper name=12〜15 encoding='utf-8'`）は、起動 4 回の出力のパイプの数と合い、
  `0751473` の `Launched._read`（`for line in self.proc.stdout:` だけで閉じない）と合う。閉じるようになったのは
  `74ff54b`（`91213f3` の 1 つ前。`git log -S`）。
- **どこが閉じていないか**: `70297bb` では、この流れで閉じていないものは見つからなかった（Linux）。Windows で消えた
  ことは**未確認**（`70297bb` の試験は Windows で流していない）。

## 3. 2 xpassed はどれか、印を外すべきか

- **特定**: 出力の進み具合の 2 行目 `...................XXs....` の 20・21 文字目（1 行は 72 文字なので、0 から数えて通しの
  91・92）。XML の並びの 91・92（0 から数えて）は
  `test_a0_fix4_restart_lastrun.py::test_huge_pid_in_the_record_does_not_crash[2147483648]` と
  `[1000000000000000000000000000000]`（XML では通過）。並びが合っていることは、ほかの印（0 から数えて、1 行目の `sss` が
  10〜12、`x` が 57、3 行目の `F` が 180、続く `s` が 181。XML の同じ位置は、スキップ・xfailed・失敗・スキップ）で確かめた。
- **印**: `xfail(strict=not WINDOWS)`（4回目の検証で付けた。Windows では strict でない）。理由の文は「Windows は ctypes が
  切り詰めるので落ちない」。4回目の記録は、Windows で XPASS か XFAIL かを見てほしいと書いていた。
- **判断: 実機では通ってしまうだけのものではなく、Windows で正しく通るもの。Windows では印を外した**。製品の
  `pid_alive` の Windows の道は `OpenProcess` の引数を `wintypes.DWORD` にしていて、ctypes は 32 ビットに切り詰める
  （2^31 → 0x80000000、10^30 → 0x40000000）。そのような番号のプロセスは無いので `OpenProcess` は失敗し、「動いて
  いない」と見る。前回のポートはほかのプログラムが使っているので待たずに次へ進み、例外は出ない。実機の 2 通りの通過は
  この見込みと合う。印を残すと、Windows で落ちるようになっても XFAIL に隠れるので、`xfail(not WINDOWS, strict=True)`
  にした（POSIX は前と同じく strict の xfail。製品の軽微の不具合はそのまま）。
- **2 xfailed**（どちらも strict。製品の既知の軽微）: `test_a0_fix4_browser.py::test_auto_cleared_save_notice_on_the_start_screen_keeps_focus_visible`、
  `test_a0_fix6_enter_inputs.py::test_messages_from_two_threads_do_not_run_into_each_other`。Linux でも xfail。

## Windows で未確認の点（この回に分かった範囲）

1. `0751473` の後で足された・変わった試験は、Windows で流していない: 受け入れ試験 `test_a0_fix7_procs.py`・
   `test_a0_fix8_procs_browser.py`・`test_a0_fix9_kill_windows.py`、`a0_marked.py`、実装者の `tests/test_app_procs.py`、
   `0751473..70297bb` で変わった `tests/app_procs.py`（約 300 行）、`tests/test_app_cli.py`（約 340 行）、
   `tests/test_app_browser.py`、受け入れ試験 3 ファイル（`fix5_enter_break`、`fix5_watch`、`fix6_enter_inputs` の小さな
   変更）。この回に直した 3 ファイルも同じ。
2. `70297bb` の部品で、Windows の中の pytest の ResourceWarning が消えること（Linux では出ないことを確かめた）。
3. Windows で中の pytest が ResourceWarning を既定で見せた理由（上）。
4. 9回目の「Windows で未確認の点」の 3（cmd.exe での `set BROWSER=…`）と、手で確かめる項目（窓を閉じたとき、コンソール
   の Enter と Ctrl+C、Git Bash など）。
5. `PeekNamedPipe`、入力の見分け、子が始まれない対照、結び損ねの番号、Ctrl+Break の終了コード、ブラウザのコマンドが
   始まる 2 件、XDG のパス、長い ID は、この回の実機の結果で**確かめられた**（9回目の 4 の多くが閉じた）。

### local_ci の見込み（PO が `python scripts\local_ci.py --ref origin/A0` を流すとき）

- 製品は `0751473` と同じ動きなので、製品の試験の結果は今回と同じ見込み。
- `test_leaked_processes_are_stopped_and_their_tests_fail` は通る見込み（`70297bb` の部品は出力のパイプを閉じる。
  閉じなくても、新しい探し方は要約の後ろの行に惑わされない）。
- `test_huge_pid_in_the_record_does_not_crash` の 2 通りは、ふつうの通過になる（XPASS は出なくなる）。
- 落ちうる所は、9回目の見込みと同じ（`test_a0_fix8_procs_browser.py` の 1 と 4）。
- PO の PC に残っている `%TEMP%\circuit-sim-ci-lnfn6b61` と、`C:\dev\circuit-sim` の空のファイル `cd`・`powershell`・
  `tasklist` は、消してよい（どれも、この確認の道具が作ったもので、製品も試験も使わない）。

## check.py の結果

- コマンド: `PATH=/usr/local/bin:/usr/bin:/bin CIRCUIT_SIM_NODE=/opt/node22/bin/node python scripts/check.py`（外側に
  `timeout 1500` だけを付けた）。対象は `8c456f1`（受け入れ試験の手当てのコミット）
- ruff は `All checks passed!`。pytest は `2082 passed, 29 skipped, 15 xfailed in 617.79s`。終了コード 0。XPASS も
  FAILED も無い。見張りの表示は「受け入れ試験が起動したプロセスは 568 個。実行の終わりに、この実行から生まれた
  アプリ本体は残っていない。」
- 数は 9回目と同じ（この回は試験を足していない。`test_huge_pid…` は Linux では前と同じく xfail）。
- この記録のコミットと `8c456f1` の違いは、この記録のファイルだけ。
- 流した後、アプリ本体、pytest、ヘッドレスの Chrome、この検証の写しを使うプロセスが残っていないことを `/proc` で確かめた。

## A1 へ引き継ぐもの

この回に新しく見つけた軽微は無い（9回目の 8 件はそのまま）。観察として、`scripts/cycle.py` 1045 行の表示（出力の最後の
行）は、要約の後ろに警告の行が続くと、要約でない行を見せる（合否には関わらない）。
