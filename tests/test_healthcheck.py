"""Healthcheck unit tests — every check function in isolation.

The real script also hits a live API; we cover that path by spinning up the
FastAPI test client. Everything else uses tmp_path so no real disk / DB / time
is touched.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Import the script as a module. The file lives in deploy/ and isn't a package.
import importlib.util
import sys as _sys

_hc_path = Path(__file__).resolve().parents[1] / "deploy" / "healthcheck.py"
_spec = importlib.util.spec_from_file_location("healthcheck", _hc_path)
assert _spec and _spec.loader
hc = importlib.util.module_from_spec(_spec)
# Register before exec_module so @dataclass can look the module up via sys.modules.
_sys.modules["healthcheck"] = hc
_spec.loader.exec_module(hc)


# ---------------------------------------------------------------- API check


def test_check_api_ok(monkeypatch):
    import urllib.request

    class _FakeResp:
        status = 200
        def read(self): return b'{"status":"ok"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout: _FakeResp())

    res = hc.check_api("http://ignored/health")
    assert res.ok
    assert "ok" in res.detail


def test_check_api_bad_status(monkeypatch):
    import urllib.request

    class _FakeResp:
        status = 500
        def read(self): return b"err"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout: _FakeResp())
    res = hc.check_api("http://x/health")
    assert not res.ok
    assert "500" in res.detail


def test_check_api_against_real_fastapi():
    """Sanity check the real FastAPI /health route returns ok — catches schema drift."""
    from src.api import server
    client = TestClient(server.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_check_api_unreachable(monkeypatch):
    import urllib.request

    def boom(url, timeout):
        raise ConnectionRefusedError("no service")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    res = hc.check_api("http://127.0.0.1:1/health")
    assert not res.ok
    assert "unreachable" in res.detail


# ---------------------------------------------------------------- Journal


def _make_journal(path: Path, rows: list[tuple]) -> None:
    """Build a minimal trades table matching TradeJournal's schema."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            symbol TEXT, side TEXT, lot_size REAL, entry_price REAL,
            exit_price REAL, stop_loss REAL, take_profit REAL,
            strategy TEXT, status TEXT,
            opened_at TEXT, closed_at TEXT,
            pnl REAL DEFAULT 0, close_reason TEXT, broker_ticket INTEGER
        );
    """)
    conn.executemany(
        "INSERT INTO trades (id,symbol,side,lot_size,entry_price,stop_loss,take_profit,strategy,status,opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_check_journal_missing(tmp_path: Path):
    res = hc.check_journal(tmp_path / "nope.db")
    assert not res.ok
    assert "does not exist" in res.detail


def test_check_journal_counts(tmp_path: Path):
    db = tmp_path / "trades.db"
    _make_journal(db, [
        (1, "EURUSD", "BUY", 0.1, 1.1, 1.09, 1.12, "ma", "OPEN", "2024-01-01T00:00:00+00:00"),
    ])
    res = hc.check_journal(db)
    assert res.ok
    assert "1 trades" in res.detail


def test_check_journal_bad_file(tmp_path: Path):
    # Non-sqlite garbage.
    db = tmp_path / "trades.db"
    db.write_bytes(b"this is not a sqlite db")
    res = hc.check_journal(db)
    assert not res.ok


# ---------------------------------------------------------------- Trade recency


def test_trade_recency_empty_is_ok(tmp_path: Path):
    db = tmp_path / "trades.db"
    _make_journal(db, [])
    res = hc.check_trade_recency(db, max_age_h=1.0)
    assert res.ok
    assert "no trades" in res.detail


def test_trade_recency_fresh(tmp_path: Path):
    db = tmp_path / "trades.db"
    ts = datetime.now(timezone.utc).isoformat()
    _make_journal(db, [
        (1, "EURUSD", "BUY", 0.1, 1.1, 1.09, 1.12, "ma", "CLOSED", ts),
    ])
    res = hc.check_trade_recency(db, max_age_h=24.0)
    assert res.ok


def test_trade_recency_stale(tmp_path: Path):
    db = tmp_path / "trades.db"
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    _make_journal(db, [
        (1, "EURUSD", "BUY", 0.1, 1.1, 1.09, 1.12, "ma", "CLOSED", ts),
    ])
    res = hc.check_trade_recency(db, max_age_h=1.0)
    assert not res.ok
    assert "ago" in res.detail


# ---------------------------------------------------------------- Log freshness


def test_log_freshness_ok(tmp_path: Path):
    logs = tmp_path / "logs"; logs.mkdir()
    (logs / "bot.log").write_text("tick")
    res = hc.check_log_freshness(logs, max_age_min=5)
    assert res.ok


def test_log_freshness_stale(tmp_path: Path):
    logs = tmp_path / "logs"; logs.mkdir()
    f = logs / "bot.log"; f.write_text("tick")
    # Back-date the mtime 30 minutes.
    old = time.time() - 30 * 60
    os.utime(f, (old, old))
    res = hc.check_log_freshness(logs, max_age_min=5)
    assert not res.ok


def test_log_freshness_no_files(tmp_path: Path):
    logs = tmp_path / "logs"; logs.mkdir()
    res = hc.check_log_freshness(logs, max_age_min=5)
    assert not res.ok


def test_log_freshness_missing_dir(tmp_path: Path):
    res = hc.check_log_freshness(tmp_path / "nope", max_age_min=5)
    assert not res.ok


# ---------------------------------------------------------------- Disk


def test_disk_free_reports_real_number(tmp_path: Path):
    res = hc.check_disk_free(tmp_path, min_free_gb=0.0)
    assert res.ok  # any disk has >0 GB free
    assert "GB free" in res.detail


# ---------------------------------------------------------------- CLI


def test_main_exits_nonzero_when_any_check_fails(tmp_path: Path, monkeypatch, capsys):
    # Point everything at empty dirs so journal + logs both fail.
    rc = hc.main([
        "--repo-root", str(tmp_path),
        "--api-url", "http://127.0.0.1:1/health",  # unreachable
        "--min-free-gb", "0",
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_main_json_output(tmp_path: Path, capsys):
    rc = hc.main([
        "--repo-root", str(tmp_path),
        "--api-url", "http://127.0.0.1:1/health",
        "--min-free-gb", "0",
        "--json",
    ])
    out = capsys.readouterr().out
    # Valid JSON array with one object per check.
    import json
    data = json.loads(out)
    assert isinstance(data, list) and len(data) >= 4
    for item in data:
        assert set(item.keys()) == {"name", "ok", "detail"}
