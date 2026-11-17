# A1 に引き継ぐもの（A1 の仕様を書くときに取り込む）

D-058 に従い、前の作業単位で出た軽微と注意を、A1（スコープと実行画面）の仕様に名前を付けて書く。A1 の仕様ができたら、このファイルの中身を仕様に移し、このファイルは消す。

## E1g から（レビュー 1〜3回目、検証 1・2回目）

1. 中断や発散の例外を持ち続けると、作業用の配列が解放されない（100 万ステップで約 90 MB、500 万ステップで約 240 MB）。サーバーは `to_dict()` を取ったら例外を捨てる。
2. 公開の属性の意味の変更: `Expanded.signals` は内部の信号も含む（表示には `internal` で絞るか `Result.display_names`）。`Expanded.run()` の結果の `notes` に展開の注意が入るので、`ex.notes + res.notes` と足すと二重になる。`Netlist.component()` の `KeyError` の `args[0]` は文面。
3. 画面仕様 8.4 の CSV の改行「LF」を CR LF に改める（D-075）。画面の CSV は `Result.to_csv(path, units=True)` と同じ書き方。
4. 古い注意のファイルを消す動きの既知の限界（D-077）: 利用者が自分で書いた同じ名前のファイルも消す。注意のある計算で同じ名前がシンボリックリンクのとき、リンクの先に書く。Windows で読み取り専用のときは消せず終了コード 2（未確認）。画面から書き出すときの扱いを決める。
5. 注意のファイルは UTF-8・BOM なし。Windows PowerShell 5.1 の `Get-Content` で文字化けするか（未確認）。BOM を付けるかを決める。
6. 未知数が数百を超える回路で、計算の準備が区切れず、その間は進み具合も中断も止まる（D-076）。v0.1 のテンプレートでは起きない。画面で「準備中」をどう見せるか。

## A0 から

7. 端末の文が、2つのスレッドから同時に出ると1行に混ざる（見た目だけ。22d2fdc からある）。
8. 止め終えた後、プロセスが終わるまでの間に2回目の合図（Ctrl+C、Ctrl+Break）が届くと、「止めました」は出るが終了コードが 0 にならない。
9. Windows で端末の窓を閉じたとき、保存していない変更の知らせを人が読めず、後始末まで進むとは限らない（未確認）。
10. A0 の UX 確認で「A1 の仕様で書く」とした項目（画面仕様第2版の未決28）。D-064 の (1): 起動コードの説明をそろえる。

## A0 の Windows の修正から（レビュー 9・10回目、検証 9回目。試験の部品と受け入れ試験の軽微）

製品の誤りではない。試験の道具の安全と、試験の名前の整理。

11. `tests/app_procs.py` 209〜212・224〜227: `except OSError` と `stopped.remove` の間に Ctrl+C が入ると、一時停止に失敗した番号へ SIGKILL を送ることがある。案: `kill_posix_tree` の間だけ SIGINT を控えて後で出す。
12. `tests/app_procs.py` 231〜237: `os.kill` の中で、SIGKILL を送る前に例外が出ると（監査フックの中の Ctrl+C）、その 1 つが一時停止のまま残る。部品の説明と食い違う。案: `except BaseException` の中で、その番号にもう一度 SIGKILL。
13. `tests/app_procs.py` 229〜239: SIGKILL の段で出た 2 つ目以降の例外は捨てられる。説明の「最初の例外」の範囲を一言。試験は最初と最後のどちらを出し直すかを見分けない。
14. 受け入れ試験 `test_a0_fix7_procs.py` 367〜373: 後始末の gate が、空いた番号も許したまま。
15. 受け入れ試験 `test_a0_fix7_procs.py` 316〜327: `start_tree` で落ちると木が最大 120 秒残る。
16. 受け入れ試験 `test_a0_fix5_enter_break.py` 181〜189: Windows で、印の確かめ（PowerShell の一覧）から送るまでの間に番号の使い回しの窓。
17. 受け入れ試験 `test_a0_fix8_procs_browser.py` 75〜85: `OPEN_FIRST` が、BROWSER が候補の先頭かを確かめる前に開く。
18. 受け入れ試験 `test_a0_fix5_watch.py` 591〜595: Linux 以外では止めない後始末（落ちたとき最大 120 秒残る）。
19. 受け入れ試験 `test_a0_fix9_kill_windows.py` 106〜108: 写しが名前空間だけを差し替えている（部品が補助関数経由で送ると、本物の os.kill に届く）。ファイル名（中身は POSIX だけ）と試験名 `..._before_the_final_loop` を中身に合わせる。`test_a0_fix8_procs_browser.py` の「b3c5b73 で直った」は 7e00e21。

## A0 の UX 確認（5回目）から

20. **A0d で行う**: 設計書 `app-design.md` 8.5（PO が手で確かめる項目）の書き直し。保存していない変更を作る手段（別の窓から `/api/session` に `dirty: true` を送る1行）、準備の手順（版の確かめ、`pip install -e .`）、Ctrl+Break の押し方（Fn の組み合わせ、届かなかったときの扱い）、起動し直しで最大 5 秒黙ること、BROWSER の行の後始末（`del opened.txt`、`set BROWSER=`、15 秒後の案内は正しい動き）、Git Bash の見分け方（`stat.S_ISFIFO`、`python.exe`、文字化けの見込み、見る物）、窓を閉じたときの見方（次の起動のポート）、番号つきの順序と記録の渡し方。PO の手の確認は、A0d の Windows の確認のときに行う。
21. 起動し直すと、画面が送り直す作業の状態がいつも「保存済み」になる（`App.tsx` の `putSession(docRef.current, false)`）。値を直せるようになると、起動し直した後の Ctrl+C が知らせずに止まる。画面が保存していない印も送り直す。
22. Git Bash（パイプ）での日本語の文字化け（見込み）。8.5 の結果で確かめ、化けるならパイプへ出す文字コードの扱いを決める。
23. 画面仕様に端末の節を作る: 止め方は「Ctrl+C（Windows の Ctrl+Break も同じ）」、Enter への反応、続けて届いた Enter を1回と数える。`screens-v0.1.md` 14章 #11（窓を閉じたときは知らせない）を Windows の実態に合わせて書き直す。
24. `app-design.md` の冒頭の「最終更新」と責任者の記載を、8章の追記に合わせる。4回目の UX 確認の改善1〜4（起動し直しの黙った待ちなど）は未対応のまま。
25. 受け入れ試験 `test_a0_fix6_test_hygiene.py` の `pytest_summary`: docstring は「行の頭から」だが実際は `line.strip()` の後で照合する（字下げされた行も拾う。実害なし）。検証の記録 A0-verifier-10 の「3」の理由づけ（OpenProcess が失敗して動いていないと見る）は強すぎる（合否は pid_alive の値に左右されない）。

## A0 に main を取り込んだ後の確かめから（検証 11回目、レビュー 12回目、UX 7回目）

画面とサーバーのファイルは変わっていない。エンジン側の変更（読める版が 0.1・0.2 の2つ、`buck_voltage_mode` の追加）で、画面に出る案内が変わった所。記録: `docs/verification/A0-verifier-11.md`。

26. **A1 で最初に直すものの1つ**: 版 0.1 の control に数値でない値があると、案内の場所が `$`（全体）・「形式が違います」になり、`control.kp` を示さない。エンジン（`circuit_sim/ir.py` 101〜124 の `_read_versions`、122 行）が `handler` より前の最初の1つで止めるので、同じファイルの `plant.R`・`plant.L`・`extra` の誤りも隠れる（取り込み前は 3 か所、後は 1 か所）。英語の画面でも「詳細」は日本語の文だけ。直し方: 1 件ごとに場所を `control.<名前>`、中身は既存の `schema.number`、数値でない値を全部並べ「{n} か所」をその数にする。エンジンが全部を集めるか、サーバー（`server.py` 355〜360・395〜402）が先に調べるかは A1 の仕様で決める（エンジンは番号 `SPEC_LEGACY_CONTROL_NOT_NUMBER` と項目の名前を持つ）。受け入れ試験 `test_a0_merge_main_engine.py` の strict の xfail は直ったら外す。
27. 読めない版の案内（N-OPEN-VERSION）の文。UX が決めた文に差し替える。サーバーは `supported` を文字列にせず並びのまま送り、`latest`（いちばん新しい読める版）も送る。N-OPEN-TEMPLATE の一覧も同じ区切り方（日本語「、」、英語「, 」）にそろえる。
    - `what`: 「{file} は仕様の版 {found} で書かれていて、このアプリでは読めません。」／ "{file} uses spec version {found}, which this app can't read."
    - `fix`: 「アプリを新しい版にするか、仕様を版 {latest} の形で書き直してください。読める版: {supported}」／ "Update the app, or rewrite the spec in version {latest}. Supported versions: {supported}"
28. `server.py` 363〜368 の `supported_versions()` の控え `(SCHEMA_VERSION,)` と説明文が古い（いまの版 0.2 では同梱のサンプル（版 0.1）を断る形。使われない道）。一覧を直接 import して控えを消す。
29. 画面の辞書の差し込む値（`frontend/src/i18n`）を、エンジンが実際に出す params と直接照らす試験が無い（main の試験は付録B を読むだけで、N-RUN-STEPS の `{t_max}`・`{h_min}` を確かめていない。いまは食い違いなし）。
30. `simulation.fidelity` の誤った値（例 `"ideal"`）の案内が「形式が違います」だけで、使える値（switching、average）は「詳細」の英語の文の中にしかない。新しいキー `schema.choice`（「{choices} のどれかが必要です」／ "must be one of {choices}"）を足す。
31. A1 の仕様で決める: 「使えるテンプレート」の一覧をエンジンの一覧のままにするか、画面が表示定義を持つものだけにするか（D-073 の 2 では v0.1 の画面は降圧コンバータだけだが、いまは RC ローパスも開けて一覧に出る）。同梱のサンプル（直列RLC）には control が無いので、A1 の受け入れ基準に「降圧コンバータのサンプルで版 0.2 の control の書き方が見える」を入れる。
32. Windows でまだ一度も流れていない main 由来の試験（E1g の中断・止まりの時間の上限 0.2 秒、`test_e1g_recheck_csv_notes.py`、`test_e1b_independent.py::test_c8_…`、Linux で測った数値の許容差）。A0 の Windows の確認で落ちたら、エンジンの作業単位の不具合として扱う。
