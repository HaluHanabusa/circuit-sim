"""アプリ本体のサーバー（FastAPI）。画面（static/）と、画面が使う API を出す。

API（画面仕様 12章の番号）:
  GET  /api/health          D8 つながっているかの確かめ。起動ごとに変わる instance を返す（起動し直しを知る）
  GET  /api/samples         D1 サンプルの一覧（名前と説明は辞書のキー）
  POST /api/open            仕様JSONを開く（4.2 の 5〜8 を確かめる。1〜4 は画面）。開けたら作業の状態に入れる
  GET  /api/session         D9 作業の状態（ブラウザを再読み込みしても、開いている仕様が戻る）
  PUT  /api/session         作業の状態を送り直す（起動し直した後。保存していない変更があるかも受け取る）
  GET  /api/settings        画面の好み（言語、テーマ、仕切りの位置、お知らせの帯の開閉。D-050）
  PUT  /api/settings        画面の好みを書く（端末の中の設定ファイル。番号が変わっても次の起動で戻る）

エラーとお知らせは、文面ではなく番号と差し込む値で返す（文面は画面の辞書が持つ。12章）。
外部への通信はしない。

安全（D-051。依存の版の既定の動きに頼らず、ここで確かめる）:
  1. Host がこのPCの名前（127.0.0.1、localhost）でなければ 400（DNS の付け替えで外のページから届く要求）
  2. 書き込みの要求（GET・HEAD・OPTIONS 以外）は、Origin がこのページと同じでなければ 403。Origin が無ければ
     Sec-Fetch-Site を見て、same-origin 以外は 403（別のサイト、同じ PC の別の番号のページから届く要求。
     クッキーの SameSite は番号を区別しないので、これで防ぐ）
  3. /api/ は合言葉のクッキーが無ければ 401（同じPCの別の利用者）。クッキーは、起動したときにブラウザへ渡す
     URL（/?k=起動の番号）で付け、番号の無い URL（/）へ移す。URL に載せるのは合言葉ではなく、
     一度だけ・数分だけ使える起動の番号（Launches）。ブラウザの起動の引数（/proc/<pid>/cmdline）や履歴から読まれても、使い終わって
     いるか期限が切れている
  4. 書き込みの要求の Content-Type が application/json でなければ 415
  5. 画面のファイルは、決まった形の名前（英数字と . _ -）だけを、画面のフォルダの中からだけ出す。
     絶対パス、UNC（\\\\ や //）、バックスラッシュ、ドライブ名（C:）は、パスを組み立てる前に拒む
     （Windows では、組み立てたパスを realpath に渡すだけで、外の機械へ SMB の接続が起きうる）
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

import circuit_sim

from .. import SCHEMA_VERSION, Spec
from ..templates import TEMPLATES
from .config import PREFS, Settings, valid_pref

VERSION = "0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
COOKIE = "circuit_sim_key"
TOKEN_PARAM = "k"
COOKIE_MAX_AGE = 400 * 24 * 3600  # ブラウザが受け付ける最長（再起動の後も、開いている画面が再接続できる）
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LAUNCH_SECONDS = 300  # 起動の番号を使える時間（5 分）
LAUNCH_KEPT = 8  # まだ使われていない起動の番号を、いくつまで持つか（古いものから捨てる）
STALE_REPORT_SECONDS = 5.0  # 使えない番号で開かれたことを端末に知らせる間隔の下限

# 画面の部品が使うもの以外を読み込ませない（外部への通信を、ブラウザの側でも止める）
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

# 画面のファイルの種類（OS の登録に頼らない。Windows では .js が text/plain に登録されていることがある）
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
SAFE_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}
)


@dataclass(frozen=True)
class Sample:
    """同梱のサンプル（画面仕様 付録A.5）。name と desc は辞書のキー。"""

    id: str
    file: str
    template: str
    name: str
    desc: str


# テンプレートが無いサンプルは一覧に出さない（使えないものを置かない。A.5）。RC と降圧は A1 で足す
SAMPLES = (
    Sample("rlc_series", "rlc_series.json", "rlc_series", "sample.rlc.name", "sample.rlc.desc"),
)

# 仕様の形の誤り（pydantic の種類）→ 画面の辞書のキー（付録B の schema.*）
SCHEMA_PROBLEMS = {
    "missing": "schema.required",
    "extra_forbidden": "schema.extra",
    "float_type": "schema.number",
    "float_parsing": "schema.number",
    "finite_number": "schema.number",
    "string_type": "schema.string",
    "dict_type": "schema.object",
    "model_type": "schema.object",
    "model_attributes_type": "schema.object",
    "greater_than": "schema.positive",
}
SCHEMA_PLACES_SHOWN = 5


# ------------------------------------------------------------------ パスの確かめ


def safe_parts(route_path: str) -> list[str] | None:
    """URL の道（復号した後）を、画面のフォルダの中の名前の並びにする。認めない形なら None。

    パスを組み立てる前（realpath の前）に、絶対パス、UNC、バックスラッシュ、ドライブ名、.. を拒む。
    名前は、ビルドが書き出す形（英数字と . _ -）だけを認める。
    """
    if not route_path.startswith("/"):
        return None
    rel = route_path[1:]
    if not rel or "\\" in rel or "\x00" in rel or ":" in rel:
        return None  # バックスラッシュ（\\host\share）、ドライブ名（C:）、NTFS の別のストリーム（a:b）
    if rel.startswith("/") or "//" in rel:
        return None  # 絶対パス、UNC（//host/share）
    parts = rel.split("/")
    for part in parts:
        if part in (".", "..") or not SAFE_PART.fullmatch(part) or part.endswith("."):
            return None
        if part.split(".")[0].lower() in WINDOWS_DEVICES:
            return None
    return parts


def static_file(route_path: str, root: str, pathmod: Any = os.path) -> str | None:
    """画面のフォルダ root の中のファイルの場所。外を指す・認めない形なら None。

    pathmod は os.path（試験では ntpath を渡し、Windows での組み立てを再現する）。
    """
    parts = safe_parts(route_path)
    if parts is None:
        return None
    real_root = pathmod.realpath(root)
    full = pathmod.realpath(pathmod.join(real_root, *parts))
    try:
        if pathmod.commonpath([full, real_root]) != real_root:
            return None
    except ValueError:  # Windows でドライブが違う
        return None
    return full


def sample_text(sample: Sample) -> str:
    # サンプルの名前は決まった一覧から選ぶだけだが、念のため1つの名前であることを確かめてから読む
    if len(safe_parts("/" + sample.file) or []) != 1:
        raise ValueError(f"サンプルの名前の形が違う: {sample.file!r}")
    return resources.files(__package__).joinpath("samples", sample.file).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 起動の番号


class Launches:
    """ブラウザに渡す URL（/?k=…）に載せる、一度だけ使える起動の番号（メモリにだけ持つ）。

    番号はクッキー（合言葉）と交換したら捨てる。LAUNCH_SECONDS たったものも使えない。
    使い終わった・期限が切れた・知らない番号で、クッキーも持たないページが開かれたら on_stale を呼ぶ
    （端末に、新しい URL の出し方を出す。間隔は STALE_REPORT_SECONDS 以上）。"""

    def __init__(self, seconds: float = LAUNCH_SECONDS, clock: Callable[[], float] = time.monotonic,
                 on_stale: Callable[[], None] | None = None) -> None:
        self.seconds = seconds
        self.clock = clock
        self.on_stale = on_stale
        self.lock = threading.Lock()
        self.open: dict[str, float] = {}  # 番号 → 使えなくなる時刻
        self.reported_at: float | None = None

    def issue(self) -> str:
        number = secrets.token_urlsafe(24)
        with self.lock:
            self._forget_expired()
            while len(self.open) >= LAUNCH_KEPT:
                del self.open[next(iter(self.open))]  # いちばん古いもの
            self.open[number] = self.clock() + self.seconds
        return number

    def redeem(self, number: str) -> bool:
        """番号が使えるなら捨てて True。比べるのにかかる時間は、どこまで合っているかによらない。"""
        given = number.encode("utf-8", "replace")
        with self.lock:
            self._forget_expired()
            hit = None
            for known in self.open:
                if hmac.compare_digest(given, known.encode("ascii")):
                    hit = known
            if hit is None:
                return False
            del self.open[hit]
            return True

    def stale(self) -> None:
        if self.on_stale is None:
            return
        with self.lock:
            now = self.clock()
            if self.reported_at is not None and now - self.reported_at < STALE_REPORT_SECONDS:
                return
            self.reported_at = now
        self.on_stale()

    def _forget_expired(self) -> None:
        now = self.clock()
        for number in [n for n, until in self.open.items() if until <= now]:
            del self.open[number]


# ------------------------------------------------------------------ 要求の確かめ


def token_ok(cookie: str | None, token: str) -> bool:
    """合言葉のクッキーが合っているか。比べるのにかかる時間は、どこまで合っているかによらない。"""
    if cookie is None:
        return False
    return hmac.compare_digest(cookie.encode("utf-8", "replace"), token.encode("utf-8"))


def request_problem(
    method: str, path: str, headers: Mapping[str, str], cookie: str | None, token: str
) -> tuple[int, str] | None:
    """要求を受け付けない理由（状態の番号、内部の説明）。受け付けるなら None。headers の名前は小文字。"""
    writing = method.upper() not in SAFE_METHODS
    if writing:
        origin = headers.get("origin")
        if origin is not None:
            own = "http://" + headers.get("host", "")
            if origin.lower() != own.lower():
                return 403, "cross-origin request refused"
        else:
            site = headers.get("sec-fetch-site")
            if site is not None and site.lower() != "same-origin":
                return 403, "cross-site request refused"
    if (path == "/api" or path.startswith("/api/")) and not token_ok(cookie, token):
        return 401, "open the app from the URL shown in the terminal"
    if writing:
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return 415, "content type must be application/json"
    return None


class Guard:
    """要求の確かめ（request_problem）を、すべての要求に掛ける。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        headers = {k.lower(): v for k, v in request.headers.items()}
        problem = request_problem(scope["method"], scope["path"], headers, request.cookies.get(COOKIE),
                                  self.token)
        if problem is not None:
            status, detail = problem
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ------------------------------------------------------------------ 仕様を開く


def notice(code: str, details: str | None = None, items: list[dict] | None = None, **params: Any) -> dict:
    return {"code": code, "params": params, "details": details, "items": items or []}


def schema_notice(file: str, places: list[dict], details: str) -> dict:
    shown = places[:SCHEMA_PLACES_SHOWN]
    return {
        **notice("N-OPEN-SCHEMA", details=details, items=shown, file=file, n=len(places)),
        "more": len(places) - len(shown),
    }


def schema_problem(err: dict) -> str:
    # 版が文字列でない（数値の 0.1 など）: 版の違いではなく、形の誤り（文字列が必要）として出す
    if err["type"] == "literal_error" and tuple(err["loc"]) == ("schema_version",):
        if not isinstance(err.get("input"), str):
            return "schema.string"
    return SCHEMA_PROBLEMS.get(err["type"], "schema.other")


def supported_versions() -> tuple[str, ...]:
    """このアプリが読める仕様の版。

    E1b が公開する circuit_sim.SUPPORTED_SCHEMA_VERSIONS（読める版の一覧）があればそれを、無ければ
    SCHEMA_VERSION だけ。呼ぶたびに読む（E1b の統合の後、ここを変えずに一覧が効く）。"""
    return tuple(getattr(circuit_sim, "SUPPORTED_SCHEMA_VERSIONS", (SCHEMA_VERSION,)))


def version_problem(file: str, data: object) -> dict | None:
    """版を確かめる、ただ1つの場所（4.2 の 6）。読めない版なら N-OPEN-VERSION、読めるか版が無ければ None。

    版が文字列でないもの（数値の 0.1 など）は、ここでは断らない
    （仕様の形の誤りとして、文字列が要ると案内する）。"""
    found = data.get("schema_version") if isinstance(data, dict) else None
    versions = supported_versions()
    if isinstance(found, str) and found not in versions:
        return notice("N-OPEN-VERSION", file=file, found=found, supported=", ".join(versions))
    return None


def check_spec(file: str, text: str) -> tuple[dict | None, dict | None]:
    """仕様JSONの文字列を確かめる（4.2 の 5〜8）。（開いた仕様の要約、開けない理由）の片方を返す。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, notice("N-OPEN-SYNTAX", details=exc.msg, file=file, line=exc.lineno, col=exc.colno)
    except RecursionError:
        # 入れ子が深すぎる（[ が何万も続くなど）。仕様JSONの形ではないので、形の誤りとして案内する
        return None, schema_notice(file, [{"path": "$", "problem": "schema.other"}], "nesting too deep")
    problem = version_problem(file, data)
    if problem is not None:
        return None, problem
    try:
        spec = Spec.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        places = [{"path": ".".join(str(p) for p in err["loc"]) or "$", "problem": schema_problem(err)}
                  for err in errors]
        details = "\n".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors)
        return None, schema_notice(file, places, details)
    except RecursionError:
        return None, schema_notice(file, [{"path": "$", "problem": "schema.other"}], "nesting too deep")
    if spec.template not in TEMPLATES:
        listed = ", ".join(sorted(TEMPLATES))
        return None, notice("N-OPEN-TEMPLATE", file=file, template=spec.template, list=listed)
    return {"file": file, "template": spec.template}, None


@dataclass
class Workspace:
    """作業の状態（D9）。アプリ本体が動いている間だけ持つ。ファイルには書かない。"""

    doc: dict | None = None
    dirty: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def unsaved(self) -> str | None:
        """保存していない変更があれば、そのファイル名。"""
        with self.lock:
            if self.doc is not None and self.dirty:
                return str(self.doc.get("file", ""))
            return None


class OpenRequest(BaseModel):
    file: str | None = None
    text: str | None = None
    sample: str | None = None


class SessionDoc(BaseModel):
    file: str
    template: str
    text: str
    sample: str | None = None


class SessionRequest(BaseModel):
    doc: SessionDoc | None = None
    dirty: bool = False


class SettingsRequest(BaseModel):
    settings: dict[str, str | None]


def page_with_prefs(page: str, prefs: Mapping[str, str]) -> str:
    """画面の入口（index.html）に、設定ファイルの好みを data-prefs として書き込む。

    boot.js が最初の描画の前に読む（番号が変わってブラウザの保存が空でも、暗いテーマで明るい画面が出ない）。
    """
    value = html.escape(json.dumps(dict(prefs), ensure_ascii=True, separators=(",", ":")), quote=True)
    return page.replace("<html ", f'<html data-prefs="{value}" ', 1)


def create_app(
    *,
    token: str,
    settings: Settings,
    static_dir: Path = STATIC_DIR,
    workspace: Workspace | None = None,
    launches: Launches | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    """token は合言葉（config.load_token）。launches は起動の番号（起動コマンドが発行して URL に載せる）。

    合言葉を確かめない形は持たない（試験のための抜け道を作らない）。"""
    if not token:
        raise ValueError("合言葉が要る")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.instance = secrets.token_hex(8)
    app.state.workspace = workspace or Workspace()
    app.state.settings = settings
    app.state.launches = launches = launches or Launches()
    ws: Workspace = app.state.workspace
    static_root = str(static_dir)
    # 外側から: 安全のヘッダー → Host の確かめ → 要求の確かめ（Guard）→ 本体
    app.add_middleware(Guard, token=token)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {"instance": app.state.instance, "version": VERSION}

    @app.get("/api/samples")
    def samples() -> dict:
        return {
            "samples": [
                {"id": s.id, "file": s.file, "template": s.template, "name": s.name, "desc": s.desc}
                for s in SAMPLES
                if s.template in TEMPLATES
            ]
        }

    @app.post("/api/open")
    def open_spec(req: OpenRequest) -> dict:
        if req.sample is not None:
            sample = next((s for s in SAMPLES if s.id == req.sample), None)
            if sample is None:
                return JSONResponse({"detail": "unknown sample"}, status_code=404)
            file, text = sample.file, sample_text(sample)
        elif req.file is not None and req.text is not None:
            file, text = req.file, req.text  # file は表示に使う名前だけ（その道のファイルは読まない）
        else:
            return JSONResponse({"detail": "file and text, or sample, are required"}, status_code=422)
        summary, problem = check_spec(file, text)
        if problem is not None:
            return {"ok": False, "notice": problem}
        doc = {**summary, "text": text, "sample": req.sample}
        with ws.lock:
            ws.doc, ws.dirty = doc, False
        return {"ok": True, "doc": doc}

    @app.get("/api/session")
    def get_session() -> dict:
        with ws.lock:
            return {"doc": ws.doc, "dirty": ws.dirty}

    @app.put("/api/session")
    def put_session(req: SessionRequest) -> dict:
        with ws.lock:
            ws.doc = req.doc.model_dump() if req.doc is not None else None
            ws.dirty = bool(req.dirty and req.doc is not None)
            return {"doc": ws.doc, "dirty": ws.dirty}

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {"settings": settings.read()}

    @app.put("/api/settings")
    def put_settings(req: SettingsRequest) -> Response:
        # 値が null の名前は消す。知らない名前と、形の違う値は書かない
        bad = [k for k, v in req.settings.items() if not (k in PREFS if v is None else valid_pref(k, v))]
        if bad:
            return JSONResponse({"detail": "unknown setting or value", "names": sorted(bad)}, status_code=422)
        try:
            return JSONResponse({"settings": settings.update(req.settings)})
        except OSError as exc:
            # 書けない場所（権限、空き容量、同じ名前のファイル）。画面は送り直しを止め、お知らせで知らせる
            return JSONResponse({"detail": "settings could not be written", "path": str(settings.path.parent),
                                 "reason": exc.strerror or str(exc)}, status_code=500)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def unknown_api(rest: str) -> Response:
        return JSONResponse({"detail": "not found"}, status_code=404)

    def index(request: Request) -> Response:
        if TOKEN_PARAM in request.query_params:
            # 起動したときにブラウザへ渡した URL: 起動の番号を合言葉のクッキーと交換し、番号の無い URL へ移す
            response = RedirectResponse("/", status_code=303)
            if launches.redeem(request.query_params[TOKEN_PARAM]):
                response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, path="/", httponly=True,
                                    samesite="strict")
            elif not token_ok(request.cookies.get(COOKIE), token):
                launches.stale()  # このページは使えない: 端末に、新しい URL の出し方を出す
            return response
        page = (static_dir / "index.html").read_text(encoding="utf-8")
        if token_ok(request.cookies.get(COOKIE), token):
            page = page_with_prefs(page, settings.read())
        return Response(page, media_type=MEDIA_TYPES[".html"])

    @app.api_route("/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static(request: Request) -> Response:
        path = request.scope["path"]
        if path in ("/", "/index.html"):
            return index(request)
        found = static_file(path, static_root)
        if found is None or not os.path.isfile(found):
            return JSONResponse({"detail": "not found"}, status_code=404)
        media = MEDIA_TYPES.get(os.path.splitext(found)[1].lower(), "application/octet-stream")
        return FileResponse(found, media_type=media)

    return app
