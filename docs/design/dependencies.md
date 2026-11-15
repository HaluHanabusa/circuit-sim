# アプリの依存とライセンス

アプリ（`circuit-sim` コマンドと画面）を動かすのに入る依存と、そのライセンス。推移的な依存（依存の依存）も含む。
決まり7（商用利用できるライセンスだけ。GPL / AGPL は使わない）を確かめた一覧。
`tests/test_app_licenses.py` が、入っている依存の全部（`pyproject.toml` から辿る）と画面の同梱物
（`frontend/package-lock.json` から辿る）がこの表にあり、表のライセンスが認めたものだけであることを確かめる。
配布するときの全文の告知（`THIRD_PARTY_NOTICES.md`）は R1（release-manager）で作る。

## Python（`pip install` で入る）

版は 2026-11-15 に確かめた時点のもの。下限は `pyproject.toml`。

| 名前 | 確かめた版 | ライセンス | 入る理由 |
|---|---|---|---|
| numpy | 2.5.3 | BSD-3-Clause（同梱の部品に 0BSD、MIT、Zlib、CC0-1.0） | 直接 |
| scipy | 1.18.1 | BSD-3-Clause | 直接 |
| pydantic | 2.13.5 | MIT | 直接 |
| pydantic-core | 2.46.5 | MIT | pydantic |
| annotated-types | 0.8.0 | MIT | pydantic |
| typing-inspection | 0.4.4 | MIT | pydantic、fastapi |
| typing-extensions | 4.16.0 | PSF-2.0 | pydantic、fastapi、starlette、uvicorn、opentelemetry-api |
| fastapi | 0.163.1 | MIT | 直接 |
| annotated-doc | 0.0.6 | MIT | fastapi |
| opentelemetry-api | 1.45.0 | Apache-2.0 | fastapi（0.150 前後から） |
| starlette | 1.7.0 | BSD-3-Clause | 直接（安全の前提の下限。D-051）、fastapi |
| anyio | 4.15.1 | MIT | starlette、fastapi |
| idna | 3.20 | BSD-3-Clause | anyio |
| exceptiongroup | — | MIT | anyio（Python 3.11 より前だけ。このアプリは 3.11 以上なので入らない） |
| uvicorn | 0.54.0 | BSD-3-Clause | 直接 |
| click | 8.5.0 | BSD-3-Clause | uvicorn |
| colorama | 0.4.6 | BSD-3-Clause | click（Windows だけ） |
| h11 | 0.16.0 | MIT | uvicorn |

## 画面（ビルド結果に同梱。`circuit_sim/app/static/`）

| 名前 | 確かめた版 | ライセンス | 入る理由 |
|---|---|---|---|
| react | 19.3.1 | MIT | 直接 |
| react-dom | 19.3.1 | MIT | 直接 |
| scheduler | 0.28.0 | MIT | react-dom |

画面のビルドと試験だけに使う道具（vite、typescript、vitest、jsdom、@testing-library など）は配る物に入らない。
