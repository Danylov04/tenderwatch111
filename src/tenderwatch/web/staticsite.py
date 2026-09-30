"""Статична збірка дашборду для GitHub Pages.

На відміну від web/app.py (живий FastAPI з авторизацією, для майбутнього VPS), тут дані один раз
запікаються в HTML під час білду (workflow dashboard.yml) — сторінка повністю публічна, без бекенда
й без пароля. Тому свідомо НЕ включаємо raw-знімки (snapshots.raw) і нотатки користувача (user_note) —
решта того самого, що й у живому /api/tenders(/{pk}), просто одним JSON-блобом у <script>.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import recall as recall_mod
from .. import store
from ..config import Rules
from ..db import jload
from ..health import source_health

TEMPLATE = Path(__file__).parent / "static" / "dashboard_template.html"
PLACEHOLDER = "/*__TENDERWATCH_DATA__*/"
DEFAULT_RELEVANCE = ("relevant", "review")


def _facets(conn: sqlite3.Connection) -> dict:
    def col(c: str) -> list[dict]:
        return [dict(r) for r in conn.execute(
            f"SELECT {c} AS v, COUNT(*) AS n FROM tenders WHERE relevance!='noise' AND {c}!='' GROUP BY {c} ORDER BY n DESC")]
    weekly = [dict(r) for r in conn.execute(
        "SELECT strftime('%Y-%W', first_seen) AS week, COUNT(*) AS n FROM tenders WHERE relevance='relevant' "
        "GROUP BY week ORDER BY week DESC LIMIT 12")]
    return {"province": col("province"), "category": col("category"), "weekly": list(reversed(weekly))}


def _detail(conn: sqlite3.Connection, pk: int) -> dict:
    notices = [dict(x) for x in conn.execute(
        "SELECT source, source_id, notice_type, published_at, url FROM notices WHERE tender_pk=? ORDER BY published_at", (pk,)
    )]
    events = [
        {"at": x["at"], "kind": x["kind"], "detail": jload(x["detail"], {})}
        for x in conn.execute("SELECT at, kind, detail FROM events WHERE tender_pk=? ORDER BY pk DESC LIMIT 50", (pk,))
    ]
    hits = [dict(x) for x in conn.execute("SELECT source, strategy FROM hits WHERE tender_pk=?", (pk,))]
    keys = [x["key"] for x in conn.execute("SELECT key FROM tender_keys WHERE tender_pk=?", (pk,))]
    return {"notices": notices, "events": events, "hits": hits, "keys": keys}


def collect(conn: sqlite3.Connection, rules: Rules | None, relevance: tuple[str, ...] = DEFAULT_RELEVANCE,
            limit: int = 3000) -> dict:
    """Один знімок усіх даних для сторінки. Без пагінації — це разовий білд, не живий API."""
    q = ",".join("?" * len(relevance))
    rows = conn.execute(
        f"SELECT * FROM tenders WHERE relevance IN ({q}) ORDER BY (deadline IS NULL), deadline ASC LIMIT ?",
        [*relevance, limit],
    ).fetchall()
    items = []
    for r in rows:
        d = store.row_dict(r)
        d.pop("user_note", None)
        d.update(_detail(conn, d["pk"]))
        items.append(d)
    counts = [dict(r) for r in conn.execute(
        "SELECT relevance, lifecycle, COUNT(*) AS n FROM tenders GROUP BY relevance, lifecycle")]
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "items": items,
        "total": len(items),
        "truncated": len(items) >= limit,
        "health": {"sources": source_health(conn), "counts": counts},
        "facets": _facets(conn),
    }
    if rules is not None:
        data["recall"] = recall_mod.recall_report(conn, rules)
    return data


def render(data: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise RuntimeError(f"шаблон {TEMPLATE} не містить плейсхолдера {PLACEHOLDER}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # JSON може містити "</script>" (напр. у description) — розбиваємо, щоб не закрити тег завчасно.
    payload = payload.replace("</script", "<\\/script")
    return html.replace(PLACEHOLDER, payload)


def build(conn: sqlite3.Connection, rules: Rules | None, out_path: str,
          relevance: tuple[str, ...] = DEFAULT_RELEVANCE) -> Path:
    data = collect(conn, rules, relevance)
    html = render(data)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
