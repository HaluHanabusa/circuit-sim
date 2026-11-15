"""アプリ本体のサーバー（FastAPI）。画面（static/）と、画面が使う API を出す。

API（画面仕様 12章の番号）:
  GET  /api/health          D8 つながっているかの確かめ。起動ごとに変わる instance を返す（起動し直しを知る）
  GET  /api/samples         D1 サンプルの一覧（名前と説明は辞書のキー）
  POST /api/open            仕様JSONを開く（4.2 の 5〜8 を確かめる。1〜4 は画面）。開けたら作業の状態に入れる
  GET  /api/session         D9 作業の状態（ブラウザを再読み込みしても、開いている仕様が戻る）
  PUT  /api/session         作業の状態を送り直す（起動し直した後。保存していない変更があるかも受け取る）

エラーとお知らせは、文面ではなく番号と差し込む値で返す（文面は画面の辞書が持つ。12章）。
外部への通信はしない。受け付けるのは、このPCの中（127.0.0.1、localhost）からの要求だけ。
"""
from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import SCHEMA_VERSION, Spec
from ..templates import TEMPLATES

VERSION = "0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]

# 画面の部品が使うもの以外を読み込ませない（外部への通信を、ブラウザの側でも止める）
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
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


def sample_text(sample: Sample) -> str:
    return resources.files(__package__).joinpath("samples", sample.file).read_text(encoding="utf-8")


def notice(code: str, details: str | None = None, items: list[dict] | None = None, **params: Any) -> dict:
    return {"code": code, "params": params, "details": details, "items": items or []}


def check_spec(file: str, text: str) -> tuple[dict | None, dict | None]:
    """仕様JSONの文字列を確かめる（4.2 の 5〜8）。（開いた仕様の要約、開けない理由）の片方を返す。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, notice("N-OPEN-SYNTAX", details=exc.msg, file=file, line=exc.lineno, col=exc.colno)
    if isinstance(data, dict) and "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
        found = data["schema_version"]
        return None, notice("N-OPEN-VERSION", file=file, found=str(found), supported=SCHEMA_VERSION)
    try:
        spec = Spec.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        places = [
            {"path": ".".join(str(p) for p in err["loc"]) or "$", "problem": SCHEMA_PROBLEMS.get(err["type"],
                                                                                            "schema.other")}
            for err in errors
        ]
        details = "\n".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors)
        shown = places[:SCHEMA_PLACES_SHOWN]
        return None, {
            **notice("N-OPEN-SCHEMA", details=details, items=shown, file=file, n=len(places)),
            "more": len(places) - len(shown),
        }
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


def create_app(
    static_dir: Path = STATIC_DIR,
    workspace: Workspace | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.instance = secrets.token_hex(8)
    app.state.workspace = workspace or Workspace()
    ws: Workspace = app.state.workspace
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/") or request.url.path in ("/", "/index.html", "/boot.js"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
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
            file, text = req.file, req.text
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

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def unknown_api(rest: str) -> Response:
        return JSONResponse({"detail": "not found"}, status_code=404)

    index = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return FileResponse(index, media_type="text/html")

    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir), name="static")
    return app
