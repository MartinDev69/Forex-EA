"""Economic-calendar blackout tests.

Covers: symbol→currency mapping, EventStore round-trip + window semantics,
ForexFactoryProvider parsing, BlackoutChecker decision logic, and RiskManager
integration (the signal-gating point operators care about)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.econ_calendar import (
    BlackoutChecker,
    BlackoutPolicy,
    CalendarEvent,
    EventStore,
    ForexFactoryProvider,
    StaticProvider,
    currencies_for_symbol,
)
from src.econ_calendar.provider import parse_forexfactory_payload
from src.econ_calendar.refresher import run_once


# ---------- symbols ----------

@pytest.mark.parametrize("symbol, expected", [
    ("EURUSD", {"EUR", "USD"}),
    ("eurusd", {"EUR", "USD"}),
    ("EUR/USD", {"EUR", "USD"}),
    ("GBPJPY", {"GBP", "JPY"}),
    ("XAUUSD", {"USD"}),
    ("BTCUSD", {"USD"}),
    ("US30", {"USD"}),
    ("DE40", {"EUR"}),
    ("UK100", {"GBP"}),
    ("EURUSDm", {"EUR", "USD"}),           # Exness "m" suffix
    ("EURUSD.pro", {"EUR", "USD"}),        # ICM/FBS suffix
    ("EURUSD-ECN", {"EUR", "USD"}),
    ("NOTAPAIR", set()),                   # unknown → empty
    ("", set()),
])
def test_currencies_for_symbol(symbol, expected):
    assert set(currencies_for_symbol(symbol)) == expected


# ---------- EventStore ----------

def _evt(ts, ccy="USD", impact="high", title="NFP", src="forexfactory"):
    return CalendarEvent(
        event_time=ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
        currency=ccy, impact=impact, title=title, source=src,
    )


def test_event_rejects_naive_datetime():
    with pytest.raises(ValueError):
        CalendarEvent(event_time=datetime(2026, 1, 1), currency="USD", impact="high", title="x")


def test_event_rejects_bad_impact():
    with pytest.raises(ValueError):
        CalendarEvent(
            event_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            currency="USD", impact="huge", title="x",
        )


def test_store_upsert_is_idempotent(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    e = _evt(datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc))
    assert store.upsert_many([e]) == 1
    assert store.upsert_many([e]) == 1  # same pk, no duplication
    assert store.count() == 1


def test_store_window_respects_times_and_impacts(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    t = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    store.upsert_many([
        _evt(t - timedelta(hours=2), title="CPI"),
        _evt(t, title="NFP"),
        _evt(t + timedelta(hours=1), title="FOMC", impact="medium"),
        _evt(t + timedelta(hours=2), title="OTHER", ccy="EUR"),
    ])
    found = store.events_in_window(
        currencies=["USD"],
        start=t - timedelta(minutes=30),
        end=t + timedelta(minutes=30),
        impacts=["high"],
    )
    assert [e.title for e in found] == ["NFP"]


def test_store_next_event(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    t = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    store.upsert_many([
        _evt(t + timedelta(hours=2), title="FOMC"),
        _evt(t + timedelta(hours=1), title="NFP"),
    ])
    nxt = store.next_event(["USD"], t, ["high"])
    assert nxt and nxt.title == "NFP"


def test_store_purge_before(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    t = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    store.upsert_many([
        _evt(t - timedelta(days=10), title="OLD"),
        _evt(t, title="NOW"),
    ])
    removed = store.purge_before(t - timedelta(days=5))
    assert removed == 1
    assert store.count() == 1


# ---------- BlackoutChecker ----------

@pytest.fixture
def store_with_nfp(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    t = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    store.upsert_many([_evt(t, title="Non-Farm Payrolls")])
    return store, t


def test_blackout_inside_window(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store, BlackoutPolicy(before_min=15, after_min=15))
    # 10 min before event → blocked
    event = checker.current_blackout("EURUSD", now=t - timedelta(minutes=10))
    assert event is not None and event.title == "Non-Farm Payrolls"


def test_blackout_outside_window(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store, BlackoutPolicy(before_min=15, after_min=15))
    # 20 min before event → allowed
    assert checker.current_blackout("EURUSD", now=t - timedelta(minutes=20)) is None
    # 20 min after event → allowed
    assert checker.current_blackout("EURUSD", now=t + timedelta(minutes=20)) is None


def test_blackout_only_affects_right_symbols(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store)
    # NFP is USD → EURGBP is not affected
    assert checker.current_blackout("EURGBP", now=t) is None
    assert checker.current_blackout("EURUSD", now=t) is not None
    assert checker.current_blackout("XAUUSD", now=t) is not None


def test_blackout_disabled_means_never_blocked(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store, BlackoutPolicy(enabled=False))
    assert checker.current_blackout("EURUSD", now=t) is None


def test_blackout_impact_filter(store_with_nfp):
    store, _ = store_with_nfp
    # Raise the bar to 'low' impact only — the high-impact NFP shouldn't match.
    checker = BlackoutChecker(store, BlackoutPolicy(impacts=frozenset({"low"})))
    assert checker.current_blackout("EURUSD") is None


def test_unknown_symbol_is_never_blocked(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store)
    assert checker.current_blackout("XYZABC", now=t) is None


def test_status_shape_for_dashboard(store_with_nfp):
    store, t = store_with_nfp
    checker = BlackoutChecker(store)
    s = checker.status("EURUSD", now=t + timedelta(minutes=1))  # inside blackout
    assert s["blackout"] is True
    assert s["current_event"]["title"] == "Non-Farm Payrolls"
    assert s["enabled"] is True
    assert s["before_min"] == 15


def test_policy_from_env():
    pol = BlackoutPolicy.from_env({
        "CALENDAR_BLACKOUT_ENABLED": "1",
        "CALENDAR_BLACKOUT_BEFORE_MIN": "30",
        "CALENDAR_BLACKOUT_AFTER_MIN": "45",
        "CALENDAR_BLACKOUT_IMPACTS": "high,medium",
    })
    assert pol.enabled is True
    assert pol.before_min == 30
    assert pol.after_min == 45
    assert pol.impacts == frozenset({"high", "medium"})


def test_policy_from_env_disabled():
    pol = BlackoutPolicy.from_env({"CALENDAR_BLACKOUT_ENABLED": "0"})
    assert pol.enabled is False


# ---------- ForexFactoryProvider parsing ----------

_SAMPLE_FF_PAYLOAD = [
    {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "date": "2026-05-02T08:30:00-04:00",
        "impact": "High",
        "forecast": "200K",
        "previous": "180K",
        "actual": None,
    },
    {
        "title": "Unemployment Rate",
        "country": "USD",
        "date": "2026-05-02T08:30:00-04:00",
        "impact": "Medium",
    },
    {
        "title": "ECB Press Conference",
        "country": "EUR",
        "date": "2026-05-03T12:45:00+00:00",
        "impact": "High",
    },
    # Malformed — missing date → dropped silently
    {"title": "Bad row", "country": "USD", "impact": "High"},
    # Bank holiday → dropped (not high/medium/low)
    {"title": "Holiday", "country": "USD", "impact": "Holiday", "date": "2026-05-04T00:00:00Z"},
]


def test_parse_forexfactory_payload_happy_path():
    events = parse_forexfactory_payload(_SAMPLE_FF_PAYLOAD)
    titles = [e.title for e in events]
    assert titles == ["Non-Farm Employment Change", "Unemployment Rate", "ECB Press Conference"]
    nfp = events[0]
    assert nfp.currency == "USD" and nfp.impact == "high"
    assert nfp.forecast == "200K"
    # Converted to UTC: 08:30 EDT = 12:30 UTC
    assert nfp.event_time == datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)


def test_parse_forexfactory_payload_drops_malformed_rows():
    events = parse_forexfactory_payload(_SAMPLE_FF_PAYLOAD)
    assert all(e.title not in ("Bad row", "Holiday") for e in events)


def test_parse_rejects_non_list():
    with pytest.raises(ValueError):
        parse_forexfactory_payload({"not": "a list"})


def test_forexfactory_provider_instantiation_is_cheap():
    # Just make sure we don't perform I/O in __init__.
    p = ForexFactoryProvider()
    assert p.url.startswith("https://")


# ---------- Refresher run_once ----------

def test_run_once_upserts_and_purges(tmp_path: Path):
    store = EventStore(tmp_path / "cal.db")
    t = datetime.now(timezone.utc)
    events = [
        CalendarEvent(event_time=t - timedelta(days=10), currency="USD", impact="high", title="OLD"),
        CalendarEvent(event_time=t + timedelta(hours=1), currency="USD", impact="high", title="NEW"),
    ]
    provider = StaticProvider(events)
    wrote = run_once(provider, store, purge_older_than_days=7)
    assert wrote == 2
    # Old event purged, new one stays
    assert store.count() == 1


# ---------- RiskManager integration ----------

def test_risk_manager_rejects_during_blackout(tmp_path: Path):
    from src.risk.risk_manager import RiskManager, RiskLimits

    store = EventStore(tmp_path / "cal.db")
    t_now = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    store.upsert_many([_evt(t_now + timedelta(minutes=5), title="NFP")])
    checker = BlackoutChecker(store, BlackoutPolicy(before_min=15, after_min=15))

    rm = RiskManager(RiskLimits(), blackout_checker=checker, clock=lambda: t_now)
    decision = rm.evaluate(
        account_balance=10_000,
        stop_distance_pips=20,
        symbol="EURUSD",
        lot_sizer=lambda **_: 0.1,
    )
    assert decision.approved is False
    assert "calendar" in decision.reason
    assert "USD" in decision.reason and "NFP" in decision.reason


def test_risk_manager_allows_outside_blackout(tmp_path: Path):
    from src.risk.risk_manager import RiskManager, RiskLimits

    store = EventStore(tmp_path / "cal.db")
    t_now = datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)
    # Event is 2 hours away — outside blackout.
    store.upsert_many([_evt(t_now + timedelta(hours=2), title="NFP")])
    checker = BlackoutChecker(store, BlackoutPolicy(before_min=15, after_min=15))

    rm = RiskManager(RiskLimits(), blackout_checker=checker, clock=lambda: t_now)
    decision = rm.evaluate(
        account_balance=10_000,
        stop_distance_pips=20,
        symbol="EURUSD",
        lot_sizer=lambda **_: 0.1,
    )
    assert decision.approved is True


def test_risk_manager_unchanged_without_checker(tmp_path: Path):
    """Existing code that doesn't pass a checker keeps the old behavior."""
    from src.risk.risk_manager import RiskManager, RiskLimits

    rm = RiskManager(RiskLimits())
    decision = rm.evaluate(
        account_balance=10_000,
        stop_distance_pips=20,
        symbol="EURUSD",
        lot_sizer=lambda **_: 0.1,
    )
    assert decision.approved is True


# ---------- API endpoints ----------

@pytest.fixture
def calendar_api(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    store = EventStore(db)
    monkeypatch.setattr(server_module, "calendar_store", store)
    monkeypatch.setattr(server_module, "calendar_policy", BlackoutPolicy())
    monkeypatch.setattr(server_module, "calendar_checker", BlackoutChecker(store, BlackoutPolicy()))

    stub = lambda: {"username": "test-user", "role": "admin"}
    server_module.app.dependency_overrides[auth_module.current_user] = stub
    server_module.app.dependency_overrides[auth_module.require_admin] = stub

    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_calendar_events_endpoint_returns_upcoming(calendar_api):
    client, store = calendar_api
    now = datetime.now(timezone.utc)
    store.upsert_many([
        _evt(now + timedelta(hours=2), title="NFP", ccy="USD"),
        _evt(now + timedelta(hours=3), title="ECB", ccy="EUR"),
        _evt(now - timedelta(hours=1), title="PAST", ccy="USD"),  # excluded
    ])
    r = client.get("/calendar/events?hours_ahead=24")
    assert r.status_code == 200
    titles = [e["title"] for e in r.json()]
    assert "NFP" in titles and "ECB" in titles
    assert "PAST" not in titles


def test_calendar_events_filtered_by_symbol(calendar_api):
    client, store = calendar_api
    now = datetime.now(timezone.utc)
    store.upsert_many([
        _evt(now + timedelta(hours=2), title="NFP", ccy="USD"),
        _evt(now + timedelta(hours=3), title="BoJ",  ccy="JPY"),
    ])
    r = client.get("/calendar/events?symbol=EURUSD")
    titles = [e["title"] for e in r.json()]
    assert titles == ["NFP"]


def test_calendar_blackout_endpoint_inside_window(calendar_api):
    client, store = calendar_api
    now = datetime.now(timezone.utc)
    # Event 5 min in the future → within 15 min blackout
    store.upsert_many([_evt(now + timedelta(minutes=5), title="NFP", ccy="USD")])
    r = client.get("/calendar/blackout/EURUSD")
    assert r.status_code == 200
    body = r.json()
    assert body["blackout"] is True
    assert body["current_event"]["title"] == "NFP"


def test_calendar_endpoints_require_auth(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import server as server_module

    # No dependency_overrides → auth is enforced.
    client = TestClient(server_module.app)
    assert client.get("/calendar/events").status_code == 401
    assert client.get("/calendar/blackout/EURUSD").status_code == 401
