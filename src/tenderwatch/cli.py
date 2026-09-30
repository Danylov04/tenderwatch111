"""CLI: tenderwatch run | backfill | doctor | recall | digest | status | export | daemon | serve"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

from . import notify, recall
from .config import load_rules, load_settings
from .daemon import daemon, housekeeping, make_http, run_sources
from .db import connect
from .health import source_health
from .sources import build


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tenderwatch")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="одноразовий запуск джерел")
    r.add_argument("--mode", choices=["search", "sweep", "backfill"], default="search")
    r.add_argument("--days", type=int, default=None, help="глибина вікна (за замовч. LOOKBACK_DAYS)")
    r.add_argument("--sources", default="", help="через кому: ezam,ted,pz,bk")
    r.add_argument("--quiet", action="store_true", help="не слати сповіщення про знайдене (для першого наповнення)")

    b = sub.add_parser("backfill", help="історичне наповнення (search+sweep по великому вікну)")
    b.add_argument("--days", type=int, default=60)
    b.add_argument("--sources", default="")

    sub.add_parser("doctor", help="жива діагностика всіх джерел (схема, фільтри, доступність)")
    sub.add_parser("recall", help="оцінка повноти + канарки")
    d = sub.add_parser("digest", help="показати/надіслати дайджест")
    d.add_argument("--send", action="store_true")
    sub.add_parser("status", help="стан джерел і лічильники")
    e = sub.add_parser("export", help="експорт тендерів у CSV (stdout)")
    e.add_argument("--relevance", default="relevant,review")
    bk = sub.add_parser("backup", help="консистентна копія БД (безпечно під час роботи воркера)")
    bk.add_argument("path")
    sub.add_parser("daemon", help="фоновий планувальник")
    s = sub.add_parser("serve", help="веб-дашборд")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    dash = sub.add_parser("dashboard", help="зібрати статичну сторінку дашборду (для GitHub Pages)")
    dash.add_argument("--out", default="docs/index.html", help="куди записати HTML")
    dash.add_argument("--relevance", default="relevant,review")

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    settings = load_settings()
    rules = load_rules(settings.rules_path)
    conn = connect(settings.db_path)
    try:
        return _dispatch(args, conn, settings, rules)
    finally:
        # WAL-режим лишає частину записів у файлі -wal; переносимо їх в основний файл,
        # інакше при "точковому" збереженні лише *.sqlite3 (як у GitHub Actions) частина даних губиться.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        conn.close()


# Джерела, чий збій справді має валити GitHub Actions run (червоний статус, exit code 2).
# ted/pz/bk — best-effort: їхній стан і так видно в `tenderwatch status` та через алерти здоров'я
# (notify.alert_health читає таблицю runs незалежно від exit code), тож один тимчасовий збій
# TED/bazakonkurencyjnosci не повинен ховати те, що основне джерело (ezam) відпрацювало нормально.
CRITICAL_SOURCES = {"ezam"}


def _dispatch(args, conn, settings, rules) -> int:
    if args.cmd in ("run", "backfill"):
        sources = [x for x in (args.sources or "").split(",") if x] or None
        mode = "backfill" if args.cmd == "backfill" else args.mode
        days = args.days if args.days is not None else (60 if mode == "backfill" else settings.lookback_days)
        reports = run_sources(conn, settings, rules, mode, days, sources=sources)
        quiet = mode == "backfill" or getattr(args, "quiet", False)   # історичне наповнення не має засипати Telegram
        sender = notify.Sender(settings)
        hk = housekeeping(conn, settings, rules, sender, quiet=quiet)
        for rep in reports:
            print(rep.summary())
            for w in rep.warnings[:5]:
                print("  !", w)
        print("housekeeping:", json.dumps(hk, ensure_ascii=False))
        critical_failed = any(not r.ok for r in reports if r.source in CRITICAL_SOURCES)
        return 2 if critical_failed else 0

    if args.cmd == "doctor":
        http = make_http(settings)
        bad = 0
        try:
            for name in settings.enabled_sources:
                src = build(name, http, rules)
                for label, ok, msg in src.doctor():
                    print(f"[{'OK ' if ok else 'BAD'}] {label}: {msg}")
                    bad += 0 if ok else 1
        finally:
            http.close()
        return 1 if bad else 0

    if args.cmd == "recall":
        print(json.dumps(recall.recall_report(conn, rules), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "digest":
        text = notify.build_digest(conn, settings, rules)
        print(text)
        if args.send:
            print("надіслано" if notify.Sender(settings).send(text) else "НЕ надіслано (перевірте TELEGRAM_*)")
        return 0

    if args.cmd == "status":
        for h in source_health(conn):
            print(f"{h['status']:5} {h['source']}/{h['mode']}: останній {h['last_run']}, отримано {h['fetched']}, "
                  f"релевантних {h['relevant']}; {'; '.join(h['problems'])}")
        for row in conn.execute("SELECT relevance, lifecycle, COUNT(*) c FROM tenders GROUP BY 1,2 ORDER BY 1,2"):
            print(f"  {row['relevance']:9} {row['lifecycle']:10} {row['c']}")
        return 0

    if args.cmd == "export":
        rel = tuple(x for x in args.relevance.split(",") if x)
        q = ",".join("?" * len(rel))
        rows = conn.execute(f"SELECT * FROM tenders WHERE relevance IN ({q}) ORDER BY deadline", rel).fetchall()
        w = csv.writer(sys.stdout)
        cols = ["pk", "title", "buyer", "province", "category", "relevance", "lifecycle", "deadline", "value_amount",
                "value_currency", "cpv_main", "sources", "url", "user_status"]
        w.writerow(cols)
        for r_ in rows:
            w.writerow([r_[c] for c in cols])
        return 0

    if args.cmd == "backup":
        import sqlite3

        dst = sqlite3.connect(args.path)
        with dst:
            conn.backup(dst)
        dst.close()
        print(f"копію збережено: {args.path}")
        return 0

    if args.cmd == "daemon":
        daemon(conn, settings, rules)
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(settings, rules), host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "dashboard":
        from .web.staticsite import build as build_dashboard

        rel = tuple(x for x in args.relevance.split(",") if x)
        out = build_dashboard(conn, rules, args.out, relevance=rel)
        print(f"дашборд зібрано: {out}")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
