from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tenderwatch import daemon, notify, pipeline, recall, store
from tenderwatch.classify import classify
from tenderwatch.daemon import run_sources
from tenderwatch.db import connect, kv_get, kv_set
from tenderwatch.health import source_health
from tenderwatch.sources import EzamSource
from tenderwatch.sources.ezam import to_notice as ezam_notice
from tenderwatch.web.app import create_app

from .conftest import FakeEzam, fixture_text, iso_days, make_http, make_item


def small(rules, kws=("schron", "MDS")):
    rules.raw["search"]["title_keywords"] = list(kws)
    rules.raw["search"]["cpv_codes"] = ["45216129"]
    rules.raw["search"]["pz_keywords"] = []
    return rules


def dataset():
    return [
        make_item("Budowa schronu w Szkole Podstawowej", cpvCode="45216129-4 (Schrony)"),
        make_item("Adaptacja piwnic na MDS", cpvCode="45000000-7 (Roboty budowlane)"),
        make_item("Budowa schroniska dla zwierząt"),                      # шум із title-пошуку
        make_item("Remont piwnic w Szkole nr 3", cpvCode="45000000-7 (Roboty budowlane)"),   # знайдеться лише sweep+тіло
        make_item("Dostawa papieru biurowego", cpvCode="30197643-5 (Papier)"),
    ]


def bodies(items):
    schools = next(i for i in items if i["orderObject"].startswith("Remont piwnic"))
    return {
        schools["objectId"]: "<div>4.2.2.) Krótki opis przedmiotu zamówienia Adaptacja piwnic na miejsce doraźnego schronienia dla uczniów. 4.2.6.) Główny kod CPV: 45000000-7 SEKCJA V</div>"
    }


def test_full_run_search_then_sweep_finds_body_only_tender(conn, settings, rules):
    rules = small(rules)
    items = dataset()
    http = make_http(ezam=FakeEzam(items, bodies(items)))
    r1 = run_sources(conn, settings, rules, "search", 3, http, sources=["ezam"])[0]
    assert r1.ok and r1.new_tenders >= 2
    rel = {r["title"] for r in conn.execute("SELECT title FROM tenders WHERE relevance='relevant'")}
    assert "Budowa schronu w Szkole Podstawowej" in rel and "Adaptacja piwnic na MDS" in rel
    assert "Remont piwnic w Szkole nr 3" not in rel             # title-пошук його не бачить — це і є діра
    r2 = run_sources(conn, settings, rules, "sweep", 3, http, sources=["ezam"])[0]
    assert r2.ok and r2.bodies >= 1
    rel = {r["title"] for r in conn.execute("SELECT title FROM tenders WHERE relevance='relevant'")}
    assert "Remont piwnic w Szkole nr 3" in rel                 # sweep + аналіз тіла закрив діру
    assert not conn.execute("SELECT 1 FROM tenders WHERE title LIKE 'Dostawa papieru%'").fetchone()


def test_second_run_is_quiet(conn, settings, rules):
    rules = small(rules)
    http = make_http(ezam=FakeEzam(dataset()))
    run_sources(conn, settings, rules, "search", 3, http, sources=["ezam"])
    conn.execute("UPDATE events SET notified=1")
    before = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    r = run_sources(conn, settings, rules, "search", 3, http, sources=["ezam"])[0]
    assert r.new_tenders == 0
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == before


def test_failing_source_is_isolated_and_recorded(conn, settings, rules):
    rules = small(rules)
    http = make_http(ezam=FakeEzam(dataset(), fail_status=500), pz=lambda r: __import__("httpx").Response(200, text=fixture_text("pz_listing.html")))
    rules.raw["search"]["pz_keywords"] = ["schron"]
    reps = run_sources(conn, settings, rules, "search", 3, http, sources=["ezam", "pz"])
    by = {r.source: r for r in reps}
    assert not by["ezam"].ok and "500" in by["ezam"].error
    assert by["pz"].ok and by["pz"].fetched >= 2
    h = {(x["source"], x["mode"]): x for x in source_health(conn)}
    assert h[("ezam", "search")]["status"] == "fail"


def test_filter_ignored_run_ingests_nothing_and_alerts(conn, settings, rules):
    rules = small(rules)
    junk = [make_item(f"Inny przetarg {i}") for i in range(10)]
    r = run_sources(conn, settings, rules, "search", 3, make_http(ezam=FakeEzam(junk, ignore_filters=True)), sources=["ezam"])[0]
    assert not r.ok and "фільтр" in r.error
    assert conn.execute("SELECT COUNT(*) c FROM tenders").fetchone()["c"] == 0
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)
    assert notify.alert_health(conn, sender) == 1
    assert "FAIL" in sent[0]["text"]
    assert notify.alert_health(conn, sender) == 0                # cooldown


def test_alert_health_backs_off_the_longer_a_source_stays_broken(conn, settings):
    """Регресія: без відкату давно зламане джерело пінгувало б Telegram кожні cooldown_hours нескінченно."""
    old = (datetime.now(UTC) - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,error,fetched) VALUES('ted','search',?,?,0,'boom',0)", (old, old))
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)

    assert notify.alert_health(conn, sender, cooldown_hours=6, max_cooldown_hours=48) == 1   # 1-й алерт: одразу
    # 13г тому — з count=1 наступний поріг 6*2=12г, 13г > 12г -> має спрацювати знову
    kv_set(conn, "alert:ted:search", (datetime.now(UTC) - timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert notify.alert_health(conn, sender, cooldown_hours=6, max_cooldown_hours=48) == 1   # 2-й алерт
    # тепер count=2, поріг 6*4=24г; 20г тому — ще зарано
    kv_set(conn, "alert:ted:search", (datetime.now(UTC) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert notify.alert_health(conn, sender, cooldown_hours=6, max_cooldown_hours=48) == 0
    # а 25г тому — вже пора (25г > 24г)
    kv_set(conn, "alert:ted:search", (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert notify.alert_health(conn, sender, cooldown_hours=6, max_cooldown_hours=48) == 1   # 3-й алерт (count=3)

    # джерело одужало — успішний запуск скидає лічильник відкату
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,fetched) VALUES('ted','search',?,?,1,5)", (now, now))
    assert notify.alert_health(conn, sender, cooldown_hours=6, max_cooldown_hours=48) == 0   # здорове — алерту нема
    assert kv_get(conn, "alert_count:ted:search") == "0"


def test_silent_zero_is_flagged(conn):
    for i in range(4):
        conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,fetched) VALUES('ezam','search',?,?,1,40)", (iso_days(-0.5 + i * 0.1)[:19] + "Z",) * 2)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,fetched) VALUES('ezam','search',?,?,1,0)", (now, now))
    h = source_health(conn)[0]
    assert h["status"] == "warn" and "0 записів" in h["problems"][0]


def test_stale_source_is_failed(conn):
    old = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,fetched) VALUES('ted','search',?,?,1,5)", (old, old))
    assert source_health(conn)[0]["status"] == "fail"


def test_lifecycle_refresh_and_reminders(conn, settings, rules):
    n = ezam_notice(make_item("Budowa schronu", submittingOffersDate=iso_days(2)), "t")
    pk = store.ingest(conn, n, classify(n, rules), persist_noise=True).tender_pk
    conn.execute("UPDATE events SET notified=1")
    assert pipeline.make_reminders(conn, rules) == 1
    assert pipeline.make_reminders(conn, rules) == 0            # ідемпотентно
    assert conn.execute("SELECT kind FROM events WHERE kind LIKE 'deadline_%'").fetchone()["kind"] == "deadline_3d"
    conn.execute("UPDATE tenders SET deadline=? WHERE pk=?", ((datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), pk))
    assert pipeline.refresh_lifecycle(conn) == 1
    assert conn.execute("SELECT lifecycle FROM tenders WHERE pk=?", (pk,)).fetchone()["lifecycle"] == "closed"


def test_notifications_flow(conn, settings, rules):
    n = ezam_notice(make_item("Budowa schronu w <Testowie> & okolicach", cpvCode="45216129-4 (Schrony)"), "t")
    store.ingest(conn, n, classify(n, rules), persist_noise=True)
    review = ezam_notice(make_item("Ochronn konstrukcja"), "t")
    store.ingest(conn, review, classify(review, rules), persist_noise=True)
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)
    out = notify.flush_events(conn, sender, settings)
    assert out["sent"] == 1 and len(sent) == 1                   # review не спамить миттєво
    text = sent[0]["text"]
    assert "НОВИЙ ТЕНДЕР" in text and "&lt;Testowie&gt;" in text and "tw.example/#t" in text
    assert notify.flush_events(conn, sender, settings)["sent"] == 0


def test_telegram_disabled_marks_events_skipped(conn, settings, rules):
    settings.telegram_token = ""
    n = ezam_notice(make_item("Budowa schronu", cpvCode="45216129-4 (Schrony)"), "t")
    store.ingest(conn, n, classify(n, rules), persist_noise=True)
    out = notify.flush_events(conn, notify.Sender(settings), settings)
    assert out["enabled"] is False
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE notified=0").fetchone()["c"] == 0


def test_recall_estimator_and_canaries(conn, rules):
    assert round(recall.chapman(10, 10, 5), 1) == 19.2     # (11*11)/6 - 1
    items = [make_item(f"Budowa schronu {i}", cpvCode="45216129-4 (Schrony)") for i in range(6)]
    for i, it in enumerate(items):
        strategies = ["title:schron"] if i < 4 else []
        strategies += ["cpv:45216129"] if i >= 2 else []
        for s in strategies:
            n = ezam_notice(it, s)
            store.ingest(conn, n, classify(n, rules), persist_noise=True)
    rules.raw["canaries"]["ids"] = [items[0]["tenderId"], "ocds-148610-nie-istnieje"]
    rep = recall.recall_report(conn, rules)
    assert rep["union"] == 6
    ch = {c["channel"]: c for c in rep["channels"]}
    assert ch["ezam:title"]["found"] == 4 and ch["ezam:cpv"]["found"] == 4
    assert ch["ezam:title"]["unique_only"] == 2
    assert rep["estimated_total"] >= 6
    assert [c["found"] for c in rep["canaries"]] == [True, False]


def test_quiet_backfill_suppresses_reminders_and_health_alerts(conn, settings, rules):
    """Регресія: --quiet мав ловити лише події, що існували ДО housekeeping. Нагадування (make_reminders)
    і алерти про здоров'я джерел (alert_health) виникають/шлються ВСЕРЕДИНІ housekeeping — і раніше проривались
    у Telegram навіть при --quiet (це сталось наживо на першому backfill: 17 нагадувань + 2 алерти пішли попри quiet)."""
    n = ezam_notice(make_item("Budowa schronu", cpvCode="45216129-4 (Schrony)", submittingOffersDate=iso_days(2)), "t")
    store.ingest(conn, n, classify(n, rules), persist_noise=True)
    conn.execute("INSERT INTO runs(source,mode,started_at,finished_at,ok,fetched,error) VALUES"
                 "('ted','search',?,?,0,0,'FilterIgnored')", (iso_days(0)[:19] + "Z",) * 2)
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)
    hk = daemon.housekeeping(conn, settings, rules, sender, quiet=True)
    assert hk["reminders"] == 1                       # нагадування таки створене...
    assert sent == []                                 # ...але нічого не надіслано
    assert hk["notified"]["sent"] == 0 and hk["alerts"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE notified=0").fetchone()["c"] == 0


def test_daemon_tick_schedule(conn, settings, rules):
    rules = small(rules)
    settings.enabled_sources = ["ezam"]
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)
    http = make_http(ezam=FakeEzam(dataset()))
    t0 = datetime(2026, 9, 21, 5, 30, tzinfo=UTC)               # 07:30 Київ, 07:30 Варшава (літо) → дайджест, не sweep
    d1 = daemon.tick(conn, settings, rules, sender, now=t0, http=http)
    assert d1 == ["search", "digest"]
    d2 = daemon.tick(conn, settings, rules, sender, now=t0 + timedelta(minutes=10), http=http)
    assert d2 == []                                             # інтервал не минув, дайджест уже був
    d3 = daemon.tick(conn, settings, rules, sender, now=t0 + timedelta(minutes=61), http=http)
    assert d3 == ["search"]
    t_night = datetime(2026, 9, 22, 0, 30, tzinfo=UTC)          # 02:30 Варшава → sweep
    assert "sweep" in daemon.tick(conn, settings, rules, sender, now=t_night, http=http)
    assert kv_get(conn, "job:digest_date") == "2026-09-21"


def test_web_api(settings, rules, conn):
    n = ezam_notice(make_item("Budowa schronu w gminie", cpvCode="45216129-4 (Schrony)"), "title:schron")
    pk = store.ingest(conn, n, classify(n, rules), persist_noise=True).tender_pk
    settings.dashboard_user, settings.dashboard_password = "u", "p"
    client = TestClient(create_app(settings, rules))
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/tenders").status_code == 401
    auth = ("u", "p")
    assert client.get("/").status_code == 401 and client.get("/", auth=auth).status_code == 200
    lst = client.get("/api/tenders", auth=auth).json()
    assert lst["total"] == 1 and lst["items"][0]["title"] == "Budowa schronu w gminie"
    assert client.get("/api/tenders?q=nieistnieje", auth=auth).json()["total"] == 0
    assert client.get("/api/tenders?source=ezam&lifecycle=open", auth=auth).json()["total"] == 1
    d = client.get(f"/api/tenders/{pk}", auth=auth).json()
    assert d["hits"][0]["strategy"] == "title:schron" and d["events"][0]["kind"] == "new" and d["keys"]
    assert client.post(f"/api/tenders/{pk}/status", json={"status": "watch"}, auth=auth).status_code == 200
    assert client.post(f"/api/tenders/{pk}/status", json={"status": "zzz"}, auth=auth).status_code == 422
    assert client.get("/api/tenders?status=watch", auth=auth).json()["total"] == 1
    assert client.get("/api/tenders/9999", auth=auth).status_code == 404
    assert "sources" in client.get("/api/health", auth=auth).json()
    assert client.get("/api/recall", auth=auth).json()["union"] == 1
    assert client.get("/api/facets", auth=auth).json()["category"]
    # без налаштованого пароля — відкрито (локальний режим)
    settings.dashboard_user = settings.dashboard_password = ""
    assert TestClient(create_app(settings, rules)).get("/api/tenders").status_code == 200
    connect(settings.db_path).close()


def test_ezam_source_doctor_reports_broken_filter(rules):
    junk = [make_item(f"Inny {i}") for i in range(10)]
    src = EzamSource(make_http(ezam=FakeEzam(junk, ignore_filters=True)), rules)
    res = src.doctor()
    assert any(not ok for _, ok, _ in res)


def test_pagination_cap_fails_loudly_instead_of_truncating(rules, monkeypatch):
    from datetime import UTC, datetime, timedelta

    import tenderwatch.sources.ezam as ez
    from tenderwatch.sources.base import SourceError, Window

    monkeypatch.setattr(ez, "MAX_PAGES", 2)
    items = [make_item(f"Budowa schronu nr {i}") for i in range(35)]
    src = EzamSource(make_http(ezam=FakeEzam(items)), small(rules, ("schron",)))
    import pytest
    with pytest.raises(SourceError, match="неповний"):
        list(src.search(Window(datetime.now(UTC) - timedelta(days=3), datetime.now(UTC))))


def test_mass_events_are_summarized_not_spammed(conn, settings, rules):
    for i in range(45):
        n = ezam_notice(make_item(f"Budowa schronu nr {i} w gminie Gmina{i}", organizationName=f"Gmina Nr{i}", cpvCode="45216129-4 (Schrony)"), "t")
        store.ingest(conn, n, classify(n, rules), persist_noise=True)
    sent = []
    sender = notify.Sender(settings, post=lambda url, payload: sent.append(payload) or True)
    out = notify.flush_events(conn, sender, settings)
    assert out.get("summarized") and len(sent) == 1 and out["sent"] == 45
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE notified=0").fetchone()["c"] == 0


def test_review_tenders_do_not_trigger_immediate_messages(conn, settings, rules):
    base = make_item("Remont magazynu obrony cywilnej", cpvCode="45000000-7 (Roboty budowlane)", publicationDate=iso_days(-3))
    n = ezam_notice(base, "t")
    c = classify(n, rules)
    assert c.relevance == "review"
    store.ingest(conn, n, c, persist_noise=True)
    conn.execute("UPDATE events SET notified=1")
    amended = dict(base, objectId="08df0000-0000-0000-0000-dddddddddddd", publicationDate=iso_days(0), submittingOffersDate=iso_days(20))
    a = ezam_notice(amended, "t")
    r = store.ingest(conn, a, classify(a, rules), persist_noise=True)
    assert "deadline_changed" in r.events
    sent = []
    notify.flush_events(conn, notify.Sender(settings, post=lambda u, p: sent.append(p) or True), settings)
    assert sent == []


def test_amendment_emits_single_meaningful_event(conn, rules):
    base = make_item("Budowa schronu", cpvCode="45216129-4 (Schrony)", publicationDate=iso_days(-3))
    n = ezam_notice(base, "t")
    pk = store.ingest(conn, n, classify(n, rules), persist_noise=True).tender_pk
    amended = dict(base, objectId="08df0000-0000-0000-0000-eeeeeeeeeeee", publicationDate=iso_days(-1), submittingOffersDate=iso_days(30))
    a = ezam_notice(amended, "t")
    r = store.ingest(conn, a, classify(a, rules), persist_noise=True)
    assert r.tender_pk == pk and r.events == ["deadline_changed"]     # без дубля new_notice
