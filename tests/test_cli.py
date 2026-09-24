"""CLI exit-code політика: критичний збій (ezam) валить run, best-effort джерела (ted/pz/bk) — ні."""
from __future__ import annotations

from tenderwatch import cli
from tenderwatch.pipeline import RunReport


def _report(source: str, ok: bool) -> RunReport:
    rep = RunReport(source, "search")
    rep.ok = ok
    return rep


def test_dispatch_ok_when_only_noncritical_source_fails(monkeypatch, conn, settings, rules):
    reports = [_report("ezam", True), _report("ted", False), _report("pz", True), _report("bk", False)]
    monkeypatch.setattr(cli, "run_sources", lambda *a, **kw: reports)
    monkeypatch.setattr(cli.notify, "flush_events", lambda *a, **kw: {"sent": 0, "skipped": 0, "enabled": False})
    monkeypatch.setattr(cli.notify, "alert_health", lambda *a, **kw: 0)

    args = cli.argparse.Namespace(cmd="run", mode="search", days=None, sources="", quiet=True, verbose=False)
    assert cli._dispatch(args, conn, settings, rules) == 0


def test_dispatch_fails_when_critical_source_fails(monkeypatch, conn, settings, rules):
    reports = [_report("ezam", False), _report("ted", True), _report("pz", True), _report("bk", True)]
    monkeypatch.setattr(cli, "run_sources", lambda *a, **kw: reports)
    monkeypatch.setattr(cli.notify, "flush_events", lambda *a, **kw: {"sent": 0, "skipped": 0, "enabled": False})
    monkeypatch.setattr(cli.notify, "alert_health", lambda *a, **kw: 0)

    args = cli.argparse.Namespace(cmd="run", mode="search", days=None, sources="", quiet=True, verbose=False)
    assert cli._dispatch(args, conn, settings, rules) == 2


def test_dispatch_ok_when_everything_succeeds(monkeypatch, conn, settings, rules):
    reports = [_report("ezam", True), _report("ted", True), _report("pz", True), _report("bk", True)]
    monkeypatch.setattr(cli, "run_sources", lambda *a, **kw: reports)
    monkeypatch.setattr(cli.notify, "flush_events", lambda *a, **kw: {"sent": 0, "skipped": 0, "enabled": False})
    monkeypatch.setattr(cli.notify, "alert_health", lambda *a, **kw: 0)

    args = cli.argparse.Namespace(cmd="run", mode="search", days=None, sources="", quiet=True, verbose=False)
    assert cli._dispatch(args, conn, settings, rules) == 0
