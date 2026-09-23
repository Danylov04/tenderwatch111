"""Стан джерел: помилки, «тихі» нулі, застарілі запуски."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from .util import utcnow

# як довго можна не мати УСПІШНОГО запуску, перш ніж бити на сполох
STALE = {"search": timedelta(hours=4), "sweep": timedelta(hours=36), "backfill": timedelta(days=365)}


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def source_health(conn, sources: list[str] | None = None) -> list[dict]:
    rows = conn.execute("SELECT DISTINCT source, mode FROM runs ORDER BY source, mode").fetchall()
    out = []
    now = utcnow()
    for r in rows:
        src, mode = r["source"], r["mode"]
        if sources and src not in sources:
            continue
        runs = conn.execute(
            "SELECT * FROM runs WHERE source=? AND mode=? AND finished_at IS NOT NULL ORDER BY pk DESC LIMIT 30", (src, mode)
        ).fetchall()
        if not runs:
            continue
        last = runs[0]
        last_ok = next((x for x in runs if x["ok"]), None)
        fails = 0
        for x in runs:
            if x["ok"]:
                break
            fails += 1
        prev_ok = [x["fetched"] for x in runs[1:] if x["ok"]]
        med = statistics.median(prev_ok) if prev_ok else None
        problems: list[str] = []
        status = "ok"
        if not last["ok"]:
            status = "fail"
            problems.append(f"останній запуск впав ({fails} поспіль): {last['error'][:200]}")
        elif med is not None and med >= 3 and last["fetched"] == 0:
            status = "warn"
            problems.append(f"0 записів при типовій медіані {med:g} — можливий тихий збій джерела")
        elif med is not None and med >= 20 and last["fetched"] < 0.2 * med:
            status = "warn"
            problems.append(f"різке падіння: {last['fetched']} проти медіани {med:g}")
        if last_ok:
            age = now - _parse(last_ok["finished_at"])
            if age > STALE.get(mode, timedelta(hours=24)):
                status = "fail"
                problems.append(f"останній успішний запуск {int(age.total_seconds() // 3600)} год тому")
        else:
            status = "fail"
            problems.append("жодного успішного запуску")
        out.append({
            "source": src, "mode": mode, "status": status, "problems": problems,
            "last_run": last["finished_at"], "last_ok": last_ok["finished_at"] if last_ok else None,
            "fetched": last["fetched"], "median": med, "consecutive_failures": fails,
            "relevant": last["relevant"], "new_tenders": last["new_tenders"],
        })
    return out
