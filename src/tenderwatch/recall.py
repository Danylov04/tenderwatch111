"""Оцінка повноти: чи багато релевантних тендерів ми ще НЕ бачимо?

Метод — capture–recapture (оцінка Чепмена) між незалежними каналами пошуку:
для двох каналів A, B: N ≈ (nA+1)(nB+1)/(m+1) − 1, де m — тендери, знайдені обома.
Канали не абсолютно незалежні (усі шукають схожі слова), тому оцінка — нижня межа «нечутого»,
але різка розбіжність між каналами — надійний сигнал про діру в одному з них.
Плюс «канарки»: ідентифікатори, які система зобов'язана знати.
"""
from __future__ import annotations

from itertools import combinations

from .config import Rules


def channel_of(source: str, strategy: str) -> str:
    fam = strategy.split(":", 1)[0] if strategy else "?"
    return f"{source}:{fam}"


def chapman(n1: int, n2: int, m: int) -> float:
    return (n1 + 1) * (n2 + 1) / (m + 1) - 1


def channel_sets(conn, relevance: tuple[str, ...] = ("relevant",)) -> dict[str, set[int]]:
    q = ",".join("?" * len(relevance))
    rows = conn.execute(
        f"SELECT h.tender_pk, h.source, h.strategy FROM hits h JOIN tenders t ON t.pk=h.tender_pk WHERE t.relevance IN ({q})",
        relevance,
    ).fetchall()
    out: dict[str, set[int]] = {}
    for r in rows:
        out.setdefault(channel_of(r["source"], r["strategy"]), set()).add(r["tender_pk"])
    return out


def recall_report(conn, rules: Rules | None = None) -> dict:
    sets = channel_sets(conn)
    union: set[int] = set().union(*sets.values()) if sets else set()
    per_channel = []
    for ch, s in sorted(sets.items()):
        others = set().union(*(v for k, v in sets.items() if k != ch)) if len(sets) > 1 else set()
        per_channel.append({"channel": ch, "found": len(s), "unique_only": len(s - others)})
    pairs = []
    for (a, sa), (b, sb) in combinations(sorted(sets.items()), 2):
        m = len(sa & sb)
        if not sa or not sb:
            continue
        est = chapman(len(sa), len(sb), m)
        pairs.append({"a": a, "b": b, "n_a": len(sa), "n_b": len(sb), "both": m, "estimate": round(est, 1)})
    est_total = max([p["estimate"] for p in pairs], default=float(len(union)))
    est_total = max(est_total, float(len(union)))
    report = {
        "union": len(union),
        "estimated_total": round(est_total, 1),
        "estimated_unseen": round(max(0.0, est_total - len(union)), 1),
        "channels": per_channel,
        "pairs": pairs,
        "note": "Оцінка Чепмена; канали корельовані, тож реальна кількість непомічених може бути більшою.",
    }
    if rules is not None:
        report["canaries"] = check_canaries(conn, rules)
    return report


def check_canaries(conn, rules: Rules) -> list[dict]:
    out = []
    for cid in rules.canaries:
        keys = [cid, f"ezam:{cid}", f"bzp:{cid}", f"ted:{cid}", f"pz:{cid}"]
        q = ",".join("?" * len(keys))
        row = conn.execute(f"SELECT tender_pk FROM tender_keys WHERE key IN ({q}) LIMIT 1", keys).fetchone()
        out.append({"id": cid, "found": bool(row), "tender_pk": row["tender_pk"] if row else None})
    return out
