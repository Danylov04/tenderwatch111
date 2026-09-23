"""Веб-дашборд (FastAPI). Один HTML без збірки; дані — JSON API. Базова автентифікація, якщо задано DASHBOARD_USER/PASSWORD."""
from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .. import recall as recall_mod
from .. import store
from ..config import Rules, Settings
from ..db import connect, jload
from ..health import source_health

STATIC = Path(__file__).parent / "static"
JSON_COLS = ("tags", "reasons", "cpv_all", "sources", "contractors", "links")
SORTS = {
    "deadline": "(deadline IS NULL), deadline ASC",
    "published": "published_at DESC",
    "score": "score DESC, deadline ASC",
    "changed": "last_changed DESC",
    "value": "value_amount DESC",
}


class StatusBody(BaseModel):
    status: str
    note: str | None = None


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in JSON_COLS:
        d[c] = jload(d.get(c), {} if c == "links" else [])
    return d


def create_app(settings: Settings, rules: Rules) -> FastAPI:
    app = FastAPI(title="tenderwatch", docs_url=None, redoc_url=None)
    security = HTTPBasic(auto_error=False)

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not (settings.dashboard_user and settings.dashboard_password):
            return
        ok = bool(creds) and secrets.compare_digest(creds.username, settings.dashboard_user) and secrets.compare_digest(
            creds.password, settings.dashboard_password
        )
        if not ok:
            raise HTTPException(401, "Потрібна автентифікація", headers={"WWW-Authenticate": 'Basic realm="tenderwatch"'})

    def db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path, init=False)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", dependencies=[Depends(auth)])
    def index() -> Response:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/api/tenders", dependencies=[Depends(auth)])
    def tenders(
        conn: sqlite3.Connection = Depends(db),
        relevance: str = "relevant,review",
        lifecycle: str = "",
        status: str = "",
        q: str = "",
        province: str = "",
        category: str = "",
        source: str = "",
        sort: str = "deadline",
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        where, args = [], []

        def in_clause(col: str, raw: str) -> None:
            vals = [v for v in raw.split(",") if v]
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args.extend(vals)

        in_clause("relevance", relevance)
        in_clause("lifecycle", lifecycle)
        in_clause("user_status", status)
        in_clause("province", province)
        in_clause("category", category)
        if source:
            where.append("sources LIKE ?")
            args.append(f'%"{source}"%')
        if q:
            where.append("(title LIKE ? OR buyer LIKE ? OR reference LIKE ? OR description LIKE ?)")
            args.extend([f"%{q}%"] * 4)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = SORTS.get(sort, SORTS["deadline"])
        total = conn.execute(f"SELECT COUNT(*) c FROM tenders {clause}", args).fetchone()["c"]
        rows = conn.execute(f"SELECT * FROM tenders {clause} ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"total": total, "items": [_row(r) for r in rows]}

    @app.get("/api/tenders/{pk}", dependencies=[Depends(auth)])
    def tender(pk: int, conn: sqlite3.Connection = Depends(db)) -> dict:
        r = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
        if not r:
            raise HTTPException(404, "Тендер не знайдено")
        d = _row(r)
        d["notices"] = [dict(x) for x in conn.execute("SELECT * FROM notices WHERE tender_pk=? ORDER BY published_at", (pk,))]
        d["events"] = [
            {**dict(x), "detail": jload(x["detail"], {})}
            for x in conn.execute("SELECT * FROM events WHERE tender_pk=? ORDER BY pk DESC LIMIT 100", (pk,))
        ]
        d["hits"] = [dict(x) for x in conn.execute("SELECT source, strategy, first_seen FROM hits WHERE tender_pk=?", (pk,))]
        d["snapshots"] = [
            dict(x) for x in conn.execute(
                "SELECT source, source_id, seen_at, hash FROM snapshots WHERE tender_pk=? ORDER BY pk DESC LIMIT 50", (pk,)
            )
        ]
        d["keys"] = [x["key"] for x in conn.execute("SELECT key FROM tender_keys WHERE tender_pk=?", (pk,))]
        return d

    @app.post("/api/tenders/{pk}/status", dependencies=[Depends(auth)])
    def set_status(pk: int, body: StatusBody, conn: sqlite3.Connection = Depends(db)) -> dict:
        if not conn.execute("SELECT 1 FROM tenders WHERE pk=?", (pk,)).fetchone():
            raise HTTPException(404, "Тендер не знайдено")
        try:
            store.set_user_status(conn, pk, body.status, body.note)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"ok": True}

    @app.get("/api/health", dependencies=[Depends(auth)])
    def health(conn: sqlite3.Connection = Depends(db)) -> dict:
        counts = [dict(r) for r in conn.execute(
            "SELECT relevance, lifecycle, COUNT(*) AS n FROM tenders GROUP BY relevance, lifecycle")]
        return {"sources": source_health(conn), "counts": counts}

    @app.get("/api/recall", dependencies=[Depends(auth)])
    def recall(conn: sqlite3.Connection = Depends(db)) -> dict:
        return recall_mod.recall_report(conn, rules)

    @app.get("/api/facets", dependencies=[Depends(auth)])
    def facets(conn: sqlite3.Connection = Depends(db)) -> dict:
        def col(c: str) -> list[dict]:
            return [dict(r) for r in conn.execute(
                f"SELECT {c} AS v, COUNT(*) AS n FROM tenders WHERE relevance!='noise' AND {c}!='' GROUP BY {c} ORDER BY n DESC")]
        weekly = [dict(r) for r in conn.execute(
            "SELECT strftime('%Y-%W', first_seen) AS week, COUNT(*) AS n FROM tenders WHERE relevance='relevant' "
            "GROUP BY week ORDER BY week DESC LIMIT 12")]
        return {"province": col("province"), "category": col("category"), "weekly": list(reversed(weekly))}

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_error(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse({"detail": f"Помилка БД: {exc}"}, status_code=503)

    return app
