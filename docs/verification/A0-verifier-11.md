# A0（アプリの骨組み）の独立検証（11回目: main を取り込んだ後の確かめと、受け入れ試験の手当て）

- 担当: verifier（実装の ui-engineer とは別。10回目の検証役とも別の実行で、記録 `docs/verification/A0-verifier-10.md`
  を読んで引き継いだ）
- 対象: ブランチ `A0`。始めの先頭 `676463c`（`2a9f2e5` で main の `66c562d` を取り込み、`676463c` で実装者が
  `tests/test_app_server.py` の期待値を直したもの）。この回のコミット: `2a6bd97`（受け入れ試験の手当て）、この記録のコミット
- 日付: 2026-11-17
- 環境（Linux）: Python 3.13.18、pytest 9.1.1、pydantic 2.13.5、numpy 2.5.3、scipy 1.18.1、fastapi 0.163.1。試験と起動は
  `PATH=/usr/local/bin:/usr/bin:/bin`（本物の claude の無い PATH）。使い捨ての道具は作業ツリーの外
  （`/tmp/a0-verify11/`）に置いた。製品のコードと実装者の試験には触っていない
- D-078: この回は、合図を送る道を持つ写しを作っていない。壊して確かめたのは、pytest の差し込み（`-p`）で
  サーバーの `supported_versions` とエンジンの `TEMPLATES`・`check_spec` を、試験のプロセスの中で差し替えたもの
  だけ（止める道・合図を送る道には触らない。プロセスを起動しない）。画面の文字を取るための一時の試験ファイル
  （`tests/acceptance/test_zz_a0v11_*.py`）は、流した後すぐに消した（コミットしていない）。
  1 回目の check.py は、こちらの道具の時間の上限（10 分）で途中（81%）で止められた。止められた後、pytest・
  アプリ本体・ヘッドレスの Chrome・node のプロセスが残っていないことを `ps` で確かめてから、流し直した。
  全部の実行の後も、残っていないことを確かめた。

## 判定

| 範囲 | 判定 | 要点 |
|---|---|---|
| 1. 落ちていた受け入れ試験 2 件 | **直した**（確かめる中身は弱めていない） | 下の「1」 |
| 2. 取り込みの確かめ | **問題なし**（重大 0、中 0。軽微 3） | 製品の画面とサーバーは `50d3c1a` と同じ。エンジンの差分は main と同じで、A0 の変更と重なるファイルは無い。取り込みのコミットは機械的な 3 方向の統合と同じツリー。エンジンの口の意味の変化は、サーバーが正しく受ける（軽微 3 件は A1 へ） |
| 実装者の試験の期待値の直し（`676463c`） | **弱めていない**（前より強い） | 下の「2.4」。読める版を壊した 3 通りを、どれも見つける |
| 3. check.py（Linux、`2a6bd97`） | **失敗 0** | `3805 passed, 44 skipped, 16 xfailed in 722.21s`。ruff は通る。XPASS・FAILED なし。見張りは「残っていない」 |
| **総合（Linux の範囲）** | **合格**（重大 0、中 0） | Windows は下の「4」。`50d3c1a` の正式な確認からの差分は、main から来た変更と試験だけ |

## 1. 直した試験

**既にある受け入れ試験の書き換え**（オーケストレーターの依頼による）。ハーネスの決まりでは、既にあるファイルの
書き換えは「内容が変わった」に当たる（取り込みで、もともとツリーは変わっている）。

| ファイル | 落ちた理由 | 直し方 | 確かめ |
|---|---|---|---|
| `tests/acceptance/test_a0_browser_checks.py`（`test_japanese_screens_show_no_untranslated_english`） | 開けないテンプレートの画面（N-OPEN-TEMPLATE）の「使えるテンプレート」に、main で足された `buck_voltage_mode` が出て、許す英字の語（`LATIN_OK`）に無かった。製品は画面仕様どおり（エンジンの一覧をそのまま見せる） | `LATIN_OK` のテンプレートの名前（`rlc_series`、`rc_lowpass`）を決め打ちせず、`circuit_sim.templates.TEMPLATES` の名前から作る（`TEMPLATE_NAMES`）。ほかの語は変えない | 直す前と同じ許し（2 つの名前だけ）の写しは `{'open-error template.json': ['buck_voltage_mode']}` で落ちる（失敗の再現）。直した後は通る |
| `tests/acceptance/test_e1b_noload_and_combination_range.py`（`test_other_infinite_or_too_large_loads_are_still_refused`） | `ids=repr` で、値 `10**400` の ID が 518 文字（決まりは 200 文字まで。`test_a0_fix5_watch.py::test_acceptance_test_ids_stay_short`） | 値の並びはそのままで、`ids=["-inf", "nan", "1e31", "nextafter(1e30,inf)", "int-10**400", "int-10**31"]` にした（値の順と合う。確かめる中身は変えない） | 6 通りとも通る。ID の試験も通る |

確かめる中身を弱めていない理由（`test_a0_browser_checks.py`）:

- 語は正規表現 `[A-Za-z][A-Za-z_]{2,}` で、識別子の全体（`buck_voltage_mode`）として取り出す。許すのは全体だけなので、
  部分（`buck`、`voltage`、`mode`）が訳し忘れとして出れば、これまでどおり見つかる。
- テンプレートの名前は「データ」として、もとから許していた（10.4。`rlc_series`、`rc_lowpass`）。名前の集め方を
  決め打ちから一覧に変えただけで、許す種類は増えていない。
- 一覧に出た名前が本当にエンジンのテンプレートであり、開けることは、新しい受け入れ試験
  （`test_a0_merge_main_engine.py::test_every_template_named_in_the_notice_opens`）が確かめる。
- 実際の画面（Chromium、`2a6bd97`）の N-OPEN-TEMPLATE の文: 日本語「テンプレート「nope」は、このアプリにありません。
  template の値を確かめてください。使えるテンプレート: buck_voltage_mode, rc_lowpass, rlc_series」、英語「This app has
  no template named "nope". Check the template value. Available: buck_voltage_mode, rc_lowpass, rlc_series」。

### 200 文字を超える ID の数え直し（`pytest --collect-only -q`、直した後）

- 受け入れ試験（`tests/acceptance/`）: 2176 件、200 文字を超えるもの **0**（いちばん長いもので 200 文字）。
  直す前は 2163 件中 1 件（上の 518 文字）。
- 全体（`tests/`）: 3865 件中 14 件が 200 文字を超える。どれも受け入れ試験ではない（この決まりの範囲の外）:
  `tests/test_local_ci.py` 7、`tests/test_e1b_tuning.py` 3（最長 497）、`tests/test_harness.py` 2、
  `tests/test_e1g_messages.py` 1、`tests/test_notes_e1c.py` 1。Windows で落ちる上限（`PYTEST_CURRENT_TEST` の
  「名前=値」で 32767 文字。5回目の記録）より十分に短い。`test_local_ci.py` と `test_harness.py` の 9 件は
  `50d3c1a` にもあり、Windows の正式な確認で通っている。

## 2. 取り込みの確かめ

### 2.1 製品の差分

| 確かめたこと | 結果 | 測り方 |
|---|---|---|
| 画面とサーバー | `git diff 50d3c1a 676463c -- circuit_sim/app frontend` は **空**（0 バイト）。`2a6bd97` も同じ | `git diff` |
| `circuit_sim/` のほかの差分 | 22 ファイル（`__init__.py`、`ir.py`、`simulate.py`、`messages.py`、`templates/`、`tuning/` ほか）。`git diff 66c562d 676463c -- circuit_sim ':!circuit_sim/app'` は **空**（main とまったく同じ） | `git diff` |
| A0 の変更とぶつからないか | 分かれ目（`f7b8255`）からの変更で、A0 側の `circuit_sim/` は `circuit_sim/app/` の 10 ファイルだけ、main 側は `circuit_sim/app/` の外の 22 ファイルだけ。重なるファイルは無い。両側で変わったファイルは全体で 3 つ（`docs/design/app-design.md`、`docs/design/design-system.md`、`tests/acceptance/test_e1a_units_limits.py`）で、どれも別の行（e1a は A0 側が `ids` を足し、main 側が「計算時間」→「終了時刻」） | `git diff --name-only` の突き合わせ |
| 取り込みのコミット `2a9f2e5` | ツリーが `git merge-tree --write-tree 50d3c1a 66c562d` の結果（`f48738a`）と **同じ**（手で足したもの・衝突の直しは無い）。衝突の印も無い | `git merge-tree`、`git rev-parse 2a9f2e5^{tree}` |
| `676463c` の変更 | `tests/test_app_server.py` だけ | `git diff --stat 2a9f2e5 676463c` |
| main の設定・道具 | main 側は `pyproject.toml`、`tests/conftest.py`、`scripts/`、`.github/` を変えていない。起動コマンド `circuit-sim` は `circuit_sim.app.cli:main` のままで、main が変えたエンジンの `circuit_sim/__main__.py` とは別の入口 | `git diff --stat`、`pyproject.toml` |

### 2.2 A0 のサーバーが使うエンジンの口

サーバーがエンジンから使うのは、`circuit_sim.SCHEMA_VERSION`、`circuit_sim.SUPPORTED_SCHEMA_VERSIONS`（`getattr`）、
`circuit_sim.Spec`（`check_spec` の `Spec.model_validate`）、`circuit_sim.templates.TEMPLATES` だけ
（`circuit_sim/app/*.py` を `grep`）。main での変化と、サーバーの受け方:

| 口 | main での変化 | サーバーの受け方（実測） |
|---|---|---|
| 読める版 | `SCHEMA_VERSION` が `"0.1"` → `"0.2"`。`SUPPORTED_SCHEMA_VERSIONS = ("0.1", "0.2")` を公開 | サーバーは一覧を読む（`supported_versions`）。版 0.1・0.2 は開け、0.3 は N-OPEN-VERSION（`supported: "0.1, 0.2"`）。版が数値（`0.2`）や `null` は、これまでどおり形の誤り（`schema.string`）。同梱のサンプル（版 0.1）は開ける |
| 版の欄が無いファイル | 中身から版を決める（control に数値があれば 0.1、無ければ 0.2） | どちらも開ける（サーバーは版の欄が無ければ版を確かめない。前と同じ） |
| `Spec.control` | `dict[str, Any]` → `dict[str, Quantity]`。版 0.1 の数値（と null）は読むときに組にする。数値でも null でもない値は `CodedError`（`ValueError` の子）で断る | 版 0.1 の数値・null は開ける。版 0.2 の数値だけの値は N-OPEN-SCHEMA（場所 `control.kp`、`schema.object`）。版 0.1 の文字列・真偽値・配列は N-OPEN-SCHEMA（**場所が `$`、`schema.other`**。軽微 1）。どれも 500 にならない（`CodedError` は pydantic が `ValidationError` に包む） |
| `SimulationSettings.fidelity` | 足された（`switching` / `average`） | `average` は開ける。知らない値は N-OPEN-SCHEMA（`simulation.fidelity`、`schema.other`） |
| `Quantity.source` | `manual` を `手入力` にそろえる | サーバーは仕様の文字列をそのまま持つ（`Spec` を書き戻さない）。開ける |
| `TEMPLATES` | `buck_voltage_mode` が足された | N-OPEN-TEMPLATE の一覧が 3 つになる（`buck_voltage_mode, rc_lowpass, rlc_series`）。`examples/buck_voltage_mode.json` は開ける。画面の辞書には 3 つとも両方の言語の名前がある（`template.<名前>.name`）。実際の画面（Chromium）で、ヘッダーと枠に「降圧コンバータ」／「Buck converter」が出る |
| サンプルの一覧 | （変化なし） | `SAMPLES` は `rlc_series` だけ（`s.template in TEMPLATES` で絞る。変わらない） |

測り方: `/tmp/a0-verify11/tools/probe_open.py`（作業ツリーの部品をそのまま読み込み、starlette の TestClient で
`/api/open` に 21 通りの仕様を送った。全部 200 で、例外は 0）。画面の文字は Chromium（playwright）で取った。

### 2.3 意味の変わった所（どれも軽微。A1 へ）

| # | 内容 | 再現 | 想定される原因 |
|---|---|---|---|
| 軽1 | 版 0.1 の control に数値でない値（`"kp": "abc"`、`true`、`[1, 2]`）があると、エンジンが断り、画面は「legacy.json が仕様JSONの形に合いません（1 か所）。… `$: 形式が違います`」と出す。場所が全体（`$`）で、`control.kp` を示さない（版 0.2 の同じ誤りは `control.kp` を示す）。取り込む前の A0 では、この形は開けていた（`control` が何でも入る形だった） | `{"schema_version":"0.1","template":"rlc_series","plant":{…},"control":{"kp":"abc"}}` を開く | エンジンは版の読み替えを `Spec` の全体の検証（`model_validator(mode="wrap")`）で行うので、pydantic の誤りの場所が `()` になり、種類が `value_error`（サーバーの対応表 `SCHEMA_PROBLEMS` に無い → `schema.other`）になる。エンジンの誤りは番号 `SPEC_LEGACY_CONTROL_NOT_NUMBER` と項目の名前（`name`）を持つので、サーバーはそれで場所を作れる。**`test_a0_merge_main_engine.py::test_legacy_control_notice_names_the_item` を `xfail(strict=True)` にした** |
| 軽2 | 読めない版の案内で、読める版が 2 つになったので文が不自然になる。日本語「このアプリが読めるのは版 0.1, 0.2 です。 アプリを新しい版にするか、仕様を版 0.1, 0.2 の形に直してください。」、英語「This app reads version 0.1, 0.2. Update the app, or rewrite the spec in version 0.1, 0.2.」。日本語の区切りが「, 」、英語の version が単数、「版 0.1, 0.2 の形に直す」がどちらの形か分からない | 版 `9` の仕様を開く（受け入れ試験の `version.json`） | サーバーが言語によらず `", ".join(版)` で `{supported}` を作る。辞書の文は版が 1 つの前提で書かれている（`50d3c1a` から同じ文）。文の決め方は ux-designer の範囲 |
| 軽3 | サーバーの `supported_versions()` の控え（`SUPPORTED_SCHEMA_VERSIONS` が無いときは `(SCHEMA_VERSION,)`）は、main で `SCHEMA_VERSION` が `"0.2"` になったので、使われると同梱のサンプル（版 0.1）を断る。いまは main が一覧を公開しているので使われない（使われない道） | pytest の差し込みで `supported_versions` を控えの形にすると、サンプルが開けなくなる（下の 2.4 の `fallback`） | E1b の統合の前に書いた控えが残っている。A1 で一覧だけを読む形にしてよい |

観察（問題にしない）: 版 0.1 の control の値が大きい配列だと、エンジンの誤りの文が値の `repr` をそのまま含み、
案内の「詳しく」（`details`）が入力と同じくらいの大きさになる（20 万個の整数で 1.49 MB）。入力（10 MB まで）より
大きくはならない。また「詳しく」は、英語の画面でもエンジンの日本語の文になる（pydantic の誤りの文が日本語の画面でも
英語なのと同じく、生の文として見せる作り。エンジンは番号と両方の言語の文を持つので、A1 で使える）。

### 2.4 実装者の試験の期待値の直し（`676463c`）は確かめを弱めていないか

- 直した所: 「読める版」の期待値を決め打ちの `"0.1"` から `circuit_sim.SUPPORTED_SCHEMA_VERSIONS` に、「0.2 は断る」を
  「読める版の次の、まだ無い版（いまは 0.3）は断る」に。`test_every_readable_version_opens` を足し、
  `test_version_is_checked_in_one_place` の `supported_versions() == ("0.1",) or hasattr(circuit_sim, "SUPPORTED_SCHEMA_VERSIONS")`
  を `supported_versions() == READABLE` にした。
- 判断: **弱めていない。前より強い**。前の `or hasattr(...)` は、一覧が公開された後は何も確かめていなかった。
  期待値を一覧から作るのは「サーバーはエンジンの一覧を見せる」という画面仕様に合い、一覧に出た版が本当に開けることは
  足された試験が確かめる（一覧の作り方によらない確かめ）。
- 落ちるべきときに落ちるか（pytest の差し込みでサーバーの `supported_versions` を壊した。`tests/test_app_server.py -k "version or open_errors or sample"`、19 件）:

| 壊し方 | 結果 |
|---|---|
| なし | 19 passed |
| `("0.1",)`（main の前の一覧のまま） | 5 failed（9.9 と 0.3 の案内、`test_every_readable_version_opens[0.2]`、`test_version_is_checked_in_one_place`、`test_list_of_supported_versions_is_used_when_published`） |
| `("0.1", "0.2", "0.3")`（読めない版まで見せる） | 4 failed |
| `(SCHEMA_VERSION,)`（控えの形。軽3） | 6 failed（サンプルが開けない、を含む） |

## 3. 新しい受け入れ試験（`tests/acceptance/test_a0_merge_main_engine.py`、13 件）

取り込んだエンジンの口を A0 のサーバーがどう受けるか（`check_spec` をそのまま呼ぶ。ブラウザもプロセスも使わない
ので、Windows でも同じに流れる）。

| 試験 | 確かめること |
|---|---|
| `test_every_template_named_in_the_notice_opens` | N-OPEN-TEMPLATE の一覧は並んでいて重ならず、エンジンの一覧と同じで、同梱のサンプルのテンプレートを含み、どの名前も版 0.1・0.2 で開ける |
| `test_every_engine_template_has_a_name_in_both_languages` | エンジンのどのテンプレートにも、画面の辞書に日本語（仮名・漢字を含む）と英語（含まない）の名前がある |
| `test_buck_example_from_main_opens` | `examples/buck_voltage_mode.json` を開ける |
| `test_bundled_sample_version_is_readable_and_the_sample_opens` | 同梱のサンプルの版が読める版に入っていて、開ける |
| `test_files_the_engine_reads_also_open_in_the_app`（4） | 版の欄なし（0.2 になる形、0.1 になる形）、版 0.1 の数値の control、版 0.2 の組の control は、エンジンが読み、アプリでも開ける |
| `test_legacy_control_that_is_not_a_number_is_a_schema_notice`（3） | 版 0.1 の control の文字列・真偽値・配列は、落ちずに N-OPEN-SCHEMA |
| `test_bare_number_control_in_version_02_names_the_item` | 版 0.2 の数値だけの control は、場所 `control.kp` を示して断る |
| `test_legacy_control_notice_names_the_item` | **xfail(strict=True)**（軽1）。版 0.1 の同じ誤りも `control.kp` を示す |

期待値の作り方: テンプレートと版の一覧は読むが（画面仕様が「エンジンの一覧を見せる」と決めている）、確かめるのは
一覧の作り方によらない性質（出たものが開ける、名前が両方の言語にある）。

落ちるべきときに落ちるか（pytest の差し込み。`/tmp/a0-verify11/tools/mut_merge.py`）:

| 壊し方 | 結果 |
|---|---|
| なし | 12 passed, 1 xfailed |
| エンジンに名前の無いテンプレート `foo_bar` を足す | `test_every_engine_template_has_a_name_in_both_languages` が落ちる |
| サーバーが読める版の一覧を読まず、現在の版だけを読む（軽3 の形） | 6 failed（サンプル、版 0.1 の 3 通り、版 0.1 の数値の control、テンプレートの一覧が開けない） |
| 案内の一覧を main の前の決め打ち（`rc_lowpass, rlc_series`）にする | `test_every_template_named_in_the_notice_opens` が落ちる |

## 4. Windows について

### `50d3c1a` の正式な確認からの差分

`50d3c1a` の正式な確認（PO の PC、Windows 11、Python 3.13.2、`local_ci`、記録 `.cycle/runs/A0-windows-20261117T115006627817Z.json`）:
試験 2125 件、通過 1990、失敗 0、エラー 0、スキップ 135、`verdict: pass`、check.py は 1084.5 秒。

`50d3c1a` → `2a6bd97` の差分は、次だけ（製品の画面とサーバー `circuit_sim/app`、`frontend` は同じ）:

1. main から来たもの（`2a9f2e5` のツリーは機械的な統合と同じ）: エンジン（`circuit_sim/` の `app` の外の 22 ファイル）、
   E1b・E1g（と E1a・E1c の手直し）の試験（実装者の試験 11 ファイル、受け入れ試験 18 ファイルと `e1g_helpers.py`）、
   `examples/buck_voltage_mode.json`、`benchmarks/bench_e1g_chunks.py`、`CLAUDE.md`、文書。
2. 試験の期待値: `tests/test_app_server.py`（`676463c`、実装者）、`tests/acceptance/test_a0_browser_checks.py` と
   `tests/acceptance/test_e1b_noload_and_combination_range.py`（`2a6bd97`、この回）。
3. 新しい受け入れ試験: `tests/acceptance/test_a0_merge_main_engine.py`（`2a6bd97`、この回）。

### PO にもう一度流してもらう `local_ci` の見込み（`python scripts\local_ci.py --ref origin/A0`）

- **A0 の試験**（製品は `50d3c1a` と同じ）: 前回と同じ結果の見込み。直した 3 ファイルと新しいファイルは、OS に
  よらない（`check_spec` を呼ぶだけ、版の一覧、ID の文字列）。`test_a0_browser_checks.py` は PO の PC では
  playwright が無いのでスキップのまま（モジュールの読み込みで `circuit_sim.templates` を読むようになったが、
  エンジンの読み込みは Windows で前回も通っている）。`test_acceptance_test_ids_stay_short` は、Windows でも
  受け入れ試験を全部集めるので、E1b・E1g の受け入れ試験が Windows で集められること（読み込みで落ちないこと）にも
  頼る。
- **main から来た試験は、記録の上では Windows で一度も流れていない**（`E1g-verifier.md` と `E1g-verifier-2.md` は
  「受け入れ試験は Linux でだけ流した」と書く。`.cycle/runs/` の Windows の記録は A0 の 1 件だけ）。このため、
  落ちうる所はここに集まる。落ちても A0 の製品の失敗ではないが、main（統合の後の本体）も同じく落ちるので、
  その場合はエンジンの作業単位の不具合として扱う:
  1. 時間の上限の試験: `tests/acceptance/test_e1g_progress_cancel_chunks.py` と `tests/test_e1g_progress.py`
     （中断が 0.2 秒以内に終わる、進み具合が 0.2 秒より長く止まらない。3 回まで測り直す）。20 万ステップ以上の
     計算を何度も流すので、遅い PC やウイルス対策の走査で揺れうる（`E1g-verifier.md` も未確認と書く）。
  2. ファイルの扱い: `tests/acceptance/test_e1g_recheck_csv_notes.py`（読み取り専用のファイルの試験は Windows では
     飛ばす。シンボリックリンクの試験は作れなければ飛ばす。注意のファイルの名前 6 通りと似た名前 8 つは Windows でも
     使える名前で、大文字と小文字だけが違う組は無い）。
  3. 子のプロセス: `tests/acceptance/test_e1b_independent.py::test_c8_same_result_in_other_processes_with_other_hash_seeds`
     （`CIRCUIT_SIM_SAFE_PATH` で PATH を組み、ハッシュの 16 進を読むだけ。文字コードの心配は小さい）。
  4. 端末の文字コード（cp932）: エンジンの CLI（`circuit_sim/__main__.py`）は出力に置き換えの誤り処理を付ける
     （`reconfigure(errors=…)`）。試験は capsys で受けるので、端末の文字コードには頼らない。
  5. 数値: 150 桁の参照との一致などの許容差（余裕 1e-6°、半径 1e-12 など）は、Linux の numpy・scipy で測ったもの。
     Windows の numpy・scipy（別の BLAS・CPU の命令）で最後の桁が違っても収まる大きさだが、実機では未確認。
     同じ PC の中で 2 回流して同じかを見る試験（再現性）は、OS によらない。
- **所要時間**: Linux で `8c456f1`（2126 件）618 秒 → `2a6bd97`（3865 件）722 秒。前回の Windows の比（1084.5 / 618 ≈ 1.75）
  なら約 1270 秒。`local_ci` の打ち切り（既定 3600 秒）の内に収まる見込み。
- main の先頭は、取り込んだ `66c562d` の後に 2 コミット進んでいる（`ba8ca5e`、`390967f`。`docs/decisions.md` と
  `docs/research/` だけ）。`git merge-tree --write-tree 2a6bd97 main` は衝突なし。試験の結果には関わらない。

## 5. check.py の結果

- コマンド: `PATH=/usr/local/bin:/usr/bin:/bin CIRCUIT_SIM_NODE=/opt/node22/bin/node python scripts/check.py`（外側に
  `timeout 3000` だけを付けた）。対象は `2a6bd97`（作業ツリーはきれい）
- ruff は `All checks passed!`。pytest は `3805 passed, 44 skipped, 16 xfailed in 722.21s (0:12:02)`。終了コード 0。
  XPASS も FAILED も無い。見張りの表示は「受け入れ試験が起動したプロセスは 572 個。実行の終わりに、この実行から
  生まれたアプリ本体は残っていない。」
- 数の突き合わせ: 実装者の `2 failed, 3791 passed, 44 skipped, 15 xfailed` に対し、落ちていた 2 件が通り（+2）、
  新しい受け入れ試験が 12 通過・1 xfail（+12、+1）。3791 + 2 + 12 = 3805、15 + 1 = 16。スキップは同じ 44。
- この記録のコミットと `2a6bd97` の違いは、この記録のファイルだけ。

## A1 へ引き継ぐもの

この回に新しく見つけた軽微（どれも取り込みで見えるようになったもの。統合は止めない）:

1. 軽1: 版 0.1 の control の数値でない値の案内の場所が `$`（`control.kp` を示さない）。受け入れ試験に strict の xfail
   （直ったら XPASS で落ちるので、印を外す）。
2. 軽2: 読める版が 2 つになった後の N-OPEN-VERSION の文（日本語の区切り「, 」、英語の単数、「版 0.1, 0.2 の形」）。
   ux-designer の判断（例: 「版 0.1 か 0.2」、直す先は現在の版 0.2 を示す）。
3. 軽3: サーバーの `supported_versions()` の控え `(SCHEMA_VERSION,)` は、いまの `SCHEMA_VERSION`（0.2）では同梱の
   サンプルを断る形で、使われない道として残っている。

観察: 受け入れ試験の外に、200 文字を超える ID が 14 件ある（上の 1）。Windows の上限には遠い。決まりを広げるかは
道具の担当の判断。10回目までの軽微（9回目の 8 件と、10回目の観察）はそのまま。
