"""Запис у БД: upsert тендера, версії, події змін, злиття дублів."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import dedup
from .classify import Classification
from .db import add_event, jdump, jload
from .models import CANCELLED, CLOSED, OPEN, Notice
from .util import norm_buyer, norm_title, now_iso

SOURCE_RANK = {"ezam": 0, "pz": 1, "bk": 2, "ted": 3}
RAW_CAP = 30_000


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@dataclass
class IngestResult:
    tender_pk: int | None
    created: bool = False
    stored: bool = True
    events: list[str] = field(default_factory=list)
    linked_via: str = ""


def _raw_json(n: Notice) -> str:
    payload = dict(n.raw)
    if n.description:
        payload["_description"] = n.description[:4000]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return text[:RAW_CAP]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _upsert_seen(conn, n: Notice, cls: Classification, body_scanned: bool) -> None:
    conn.execute(
        """INSERT INTO seen_notices(source, source_id, verdict, score, body_scanned, seen_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(source, source_id) DO UPDATE SET verdict=excluded.verdict, score=excluded.score,
             body_scanned=MAX(body_scanned, excluded.body_scanned), seen_at=excluded.seen_at""",
        (n.source, n.source_id, cls.relevance, cls.score, int(body_scanned), now_iso()),
    )


def already_scanned(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    row = conn.execute(
        "SELECT body_scanned FROM seen_notices WHERE source=? AND source_id=?", (source, source_id)
    ).fetchone()
    return bool(row and row["body_scanned"])


def is_seen(conn: sqlite3.Connection, source: str, source_id: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_notices WHERE source=? AND source_id=?", (source, source_id)).fetchone() is not None


def ingest(conn: sqlite3.Connection, n: Notice, cls: Classification, *, persist_noise: bool, body_scanned: bool = False) -> IngestResult:
    """Головна точка запису. Ідемпотентна: повторний виклик з тими ж даними не створює подій."""
    keys = list(dict.fromkeys(n.keys))
    pks = dedup.find_by_keys(conn, keys)
    linked_via = "key" if pks else ""
    if not pks and cls.relevance != "noise":
        m = dedup.find_fuzzy(conn, n)
        if m:
            pks, linked_via = [m], "fuzzy"
    if not pks and cls.relevance == "noise" and not persist_noise:
        _upsert_seen(conn, n, cls, body_scanned)
        return IngestResult(None, stored=False)

    with tx(conn):
        _upsert_seen(conn, n, cls, body_scanned)
        if not pks:
            pk = _create(conn, n, cls)
            res = IngestResult(pk, created=True, events=["new"], linked_via="")
        else:
            pk = pks[0]
            events: list[str] = []
            if len(pks) > 1:
                _merge(conn, pks)
                events.append("merged")
            events += _update(conn, pk, n, cls, linked_via)
            res = IngestResult(pk, events=events, linked_via=linked_via)
        _attach(conn, pk, n)
    return res


# ---------- create / attach ----------

def _create(conn, n: Notice, cls: Classification) -> int:
    now = now_iso()
    cpv_main = n.cpv[0] if n.cpv else ""
    cur = conn.execute(
        """INSERT INTO tenders(title,title_norm,buyer,buyer_norm,buyer_nip,province,city,kind,category,tags,relevance,score,
             reasons,lifecycle,cpv_main,cpv_all,value_amount,value_currency,deadline,deadline_src,published_at,last_notice_at,
             first_seen,last_seen,last_changed,url,sources,lots,description,reference,contact_email,contractors,links)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            n.title, norm_title(n.title), n.buyer, norm_buyer(n.buyer), n.buyer_nip, n.province, n.city, n.kind, cls.category,
            jdump(cls.tags), cls.relevance, cls.score, jdump(cls.reasons), n.lifecycle, cpv_main, jdump(n.cpv),
            n.value_amount, n.value_currency, n.deadline, n.source if n.deadline else "", n.published_at,
            n.published_at, now, now, now, n.url, jdump([n.source]), n.lots, n.description, n.reference, n.contact_email,
            jdump(n.contractors), jdump({n.source: n.url} if n.url else {}),
        ),
    )
    pk = int(cur.lastrowid or 0)
    add_event(conn, pk, "new", {"relevance": cls.relevance, "category": cls.category, "source": n.source, "title": n.title})
    return pk


def _attach(conn, pk: int, n: Notice) -> None:
    """Ключі, запис оповіщення, знімок, hit — однаково для нового й наявного тендера."""
    for k in n.keys:
        conn.execute("INSERT OR IGNORE INTO tender_keys(key, tender_pk) VALUES(?,?)", (k, pk))
    now = now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO notices(tender_pk, source, source_id, notice_type, published_at, url, first_seen)
           VALUES(?,?,?,?,?,?,?)""",
        (pk, n.source, n.source_id, n.notice_type, n.published_at, n.url, now),
    )
    raw = _raw_json(n)
    conn.execute(
        "INSERT OR IGNORE INTO snapshots(tender_pk, source, source_id, seen_at, hash, raw) VALUES(?,?,?,?,?,?)",
        (pk, n.source, n.source_id, now, _hash(raw), raw),
    )
    if n.strategy:
        conn.execute(
            "INSERT OR IGNORE INTO hits(tender_pk, source, strategy, first_seen) VALUES(?,?,?,?)",
            (pk, n.source, n.strategy, now),
        )


# ---------- update ----------

def _changed(old, new) -> bool:
    return (old or None) != (new or None)


def _update(conn, pk: int, n: Notice, cls: Classification, linked_via: str) -> list[str]:
    t = conn.execute("SELECT * FROM tenders WHERE pk=?", (pk,)).fetchone()
    now = now_iso()
    sources = jload(t["sources"], [])
    same_source = n.source in sources
    newer = (n.published_at or "") >= (t["last_notice_at"] or "")
    events: list[tuple[str, dict]] = []
    upd: dict[str, object] = {"last_seen": now}

    known_notice = conn.execute(
        "SELECT 1 FROM notices WHERE source=? AND source_id=?", (n.source, n.source_id)
    ).fetchone() is not None

    # назва (лише в межах одного джерела; між джерелами формат назви відрізняється)
    if newer and same_source and n.title and norm_title(n.title) != t["title_norm"]:
        if dedup.title_similarity(norm_title(n.title), t["title_norm"]) < 0.98:
            events.append(("title_changed", {"old": t["title"], "new": n.title}))
        upd.update(title=n.title, title_norm=norm_title(n.title))

    # життєвий цикл
    if newer and n.lifecycle != "unknown" and n.lifecycle != t["lifecycle"]:
        if n.lifecycle == OPEN and t["lifecycle"] in (CLOSED, CANCELLED, "awarded") and linked_via == "fuzzy":
            events.append(("republished", {"old_lifecycle": t["lifecycle"], "attempt": t["attempts"] + 1}))
            upd["attempts"] = t["attempts"] + 1
        else:
            events.append(("lifecycle_changed", {"old": t["lifecycle"], "new": n.lifecycle}))
        upd["lifecycle"] = n.lifecycle

    # дедлайн: точніше джерело виграє; змінами вважаємо лише зміни в межах одного джерела
    if n.deadline:
        rank_new = SOURCE_RANK.get(n.source, 9)
        rank_old = SOURCE_RANK.get(t["deadline_src"], 9) if t["deadline_src"] else 99
        if t["deadline"] != n.deadline and (newer or rank_new < rank_old or not t["deadline"]):
            if t["deadline"] and t["deadline_src"] == n.source and (upd.get("lifecycle", t["lifecycle"]) == OPEN):
                events.append(("deadline_changed", {"old": t["deadline"], "new": n.deadline}))
            if rank_new <= rank_old or newer:
                upd.update(deadline=n.deadline, deadline_src=n.source)

    # вартість
    if n.value_amount is not None and _changed(t["value_amount"], n.value_amount):
        if t["value_amount"] is not None and same_source:
            events.append(("value_changed", {"old": t["value_amount"], "new": n.value_amount, "cur": n.value_currency}))
        if newer or t["value_amount"] is None:
            upd.update(value_amount=n.value_amount, value_currency=n.value_currency)

    # прості «заповнити порожнє»
    fill = {
        "buyer": n.buyer, "buyer_nip": n.buyer_nip, "province": n.province, "city": n.city, "kind": n.kind,
        "description": n.description, "reference": n.reference, "contact_email": n.contact_email,
    }
    for col, val in fill.items():
        if val and (not t[col] or (newer and col in ("description",) and val != t[col])):
            upd[col] = val
    if n.buyer and not t["buyer_norm"]:
        upd["buyer_norm"] = norm_buyer(n.buyer)
    if n.lots and not t["lots"]:
        upd["lots"] = n.lots
    if n.contractors:
        upd["contractors"] = jdump(n.contractors)

    cpvs = list(dict.fromkeys(jload(t["cpv_all"], []) + n.cpv))
    if cpvs != jload(t["cpv_all"], []):
        upd["cpv_all"] = jdump(cpvs)
        if not t["cpv_main"] and cpvs:
            upd["cpv_main"] = cpvs[0]

    if n.published_at and (not t["published_at"] or n.published_at < t["published_at"]):
        upd["published_at"] = n.published_at
    if n.published_at and n.published_at > (t["last_notice_at"] or ""):
        upd["last_notice_at"] = n.published_at
        if n.url:
            upd["url"] = n.url

    # джерела / посилання
    if not same_source:
        sources.append(n.source)
        upd["sources"] = jdump(sources)
        events.append(("source_added", {"source": n.source}))
    links = jload(t["links"], {})
    if n.url and (n.source not in links or newer):
        links[n.source] = n.url
        upd["links"] = jdump(links)

    # нове оголошення для відомої процедури (зміна, результат тощо)
    if not known_notice and same_source and n.notice_type and not any(
        e[0] in ("lifecycle_changed", "republished", "deadline_changed", "value_changed", "title_changed") for e in events
    ):
        events.append(("new_notice", {"notice_type": n.notice_type, "source": n.source}))

    # релевантність: береться максимум (тіло/друге джерело можуть лише підвищити)
    if cls.score > t["score"] or _rank(cls.relevance) > _rank(t["relevance"]):
        new_rel = cls.relevance if _rank(cls.relevance) >= _rank(t["relevance"]) else t["relevance"]
        if new_rel != t["relevance"]:
            events.append(("relevance_changed", {"old": t["relevance"], "new": new_rel}))
        old_tags = jload(t["tags"], [])
        upd.update(
            relevance=new_rel,
            score=max(cls.score, t["score"]),
            reasons=jdump(cls.reasons),
            tags=jdump(list(dict.fromkeys(old_tags + cls.tags))),
            category=cls.category if cls.category != "other" else t["category"],
        )

    if events:
        upd["last_changed"] = now
    sets = ", ".join(f"{k}=?" for k in upd)
    conn.execute(f"UPDATE tenders SET {sets} WHERE pk=?", [*upd.values(), pk])
    for kind, detail in events:
        add_event(conn, pk, kind, detail)
    return [k for k, _ in events]


def _rank(rel: str) -> int:
    return {"noise": 0, "review": 1, "relevant": 2}.get(rel, 0)


# ---------- merge ----------

def _merge(conn, pks: list[int]) -> None:
    keep, others = pks[0], pks[1:]
    for o in others:
        rows = {r["pk"]: r for r in conn.execute("SELECT * FROM tenders WHERE pk IN (?,?)", (keep, o))}
        a, b = rows[keep], rows[o]
        conn.execute("UPDATE OR IGNORE tender_keys SET tender_pk=? WHERE tender_pk=?", (keep, o))
        conn.execute("UPDATE notices SET tender_pk=? WHERE tender_pk=?", (keep, o))
        conn.execute("UPDATE OR IGNORE snapshots SET tender_pk=? WHERE tender_pk=?", (keep, o))
        conn.execute("UPDATE OR IGNORE hits SET tender_pk=? WHERE tender_pk=?", (keep, o))
        conn.execute("UPDATE events SET tender_pk=? WHERE tender_pk=?", (keep, o))
        sources = list(dict.fromkeys(jload(a["sources"], []) + jload(b["sources"], [])))
        links = {**jload(b["links"], {}), **jload(a["links"], {})}
        status = a["user_status"] if a["user_status"] != "new" else b["user_status"]
        note = a["user_note"] or b["user_note"]
        first = min(a["first_seen"], b["first_seen"])
        conn.execute(
            "UPDATE tenders SET sources=?, links=?, user_status=?, user_note=?, first_seen=?, attempts=MAX(attempts, ?) WHERE pk=?",
            (jdump(sources), jdump(links), status, note, first, b["attempts"], keep),
        )
        conn.execute("DELETE FROM tenders WHERE pk=?", (o,))
        add_event(conn, keep, "merged", {"absorbed": o, "title": b["title"]})


def set_user_status(conn: sqlite3.Connection, pk: int, status: str, note: str | None = None) -> None:
    if status not in ("new", "watch", "applied", "skip"):
        raise ValueError(f"bad status {status!r}")
    if note is None:
        conn.execute("UPDATE tenders SET user_status=? WHERE pk=?", (status, pk))
    else:
        conn.execute("UPDATE tenders SET user_status=?, user_note=? WHERE pk=?", (status, note, pk))
    add_event(conn, pk, "user_status", {"status": status})
    # користувацькі події не розсилаємо
    conn.execute("UPDATE events SET notified=1 WHERE tender_pk=? AND kind='user_status'", (pk,))
