"""FastAPI backend — what the AntiGreed mobile app talks to.

The bot writes trade events into data/trades.db; this server reads from
that same SQLite file so the mobile UI reflects reality without needing
IPC with the bot process.

Endpoints:
  GET  /health
  GET  /status          bot running? last heartbeat?
  GET  /account         balance/equity/open positions/daily P&L
  GET  /trades          recent trade history (from SQLite journal)
  GET  /strategies      list enabled flags
  POST /strategies/{name}/toggle
  POST /bot/start
  POST /bot/stop

Run locally:
  uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.execution.journal import TradeJournal

app = FastAPI(title="Forex-EA Control API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _BotState:
    running: bool = False
    strategies: dict[str, bool] = {
        "ma_crossover": True,
        "rsi_mean_reversion": False,
        "donchian_breakout": False,
    }
    last_heartbeat: datetime | None = None
    balance: float = 10_000.0


state = _BotState()
journal = TradeJournal(Path("data/trades.db"))


class StatusResponse(BaseModel):
    running: bool
    mt5_connected: bool
    last_heartbeat: datetime | None
    open_positions: int


class AccountResponse(BaseModel):
    balance: float
    equity: float
    open_positions: int
    daily_pnl: float = 0.0


class StrategyResponse(BaseModel):
    name: str
    enabled: bool


class TradeResponse(BaseModel):
    id: int
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_price: float
    exit_price: float | None
    pnl: float
    opened_at: datetime
    closed_at: datetime | None


def _open_positions() -> int:
    rows = journal.recent(limit=200)
    return sum(1 for r in rows if r.get("status") == "OPEN")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(
        running=state.running,
        mt5_connected=False,
        last_heartbeat=state.last_heartbeat,
        open_positions=_open_positions(),
    )


@app.get("/account", response_model=AccountResponse)
def account() -> AccountResponse:
    today = journal.summary_today()
    return AccountResponse(
        balance=state.balance,
        equity=state.balance + today["pnl"],
        open_positions=_open_positions(),
        daily_pnl=today["pnl"],
    )


@app.get("/strategies", response_model=list[StrategyResponse])
def list_strategies() -> list[StrategyResponse]:
    return [StrategyResponse(name=n, enabled=e) for n, e in state.strategies.items()]


@app.post("/strategies/{name}/toggle", response_model=StrategyResponse)
def toggle_strategy(name: str) -> StrategyResponse:
    if name not in state.strategies:
        raise HTTPException(404, f"strategy '{name}' not found")
    state.strategies[name] = not state.strategies[name]
    return StrategyResponse(name=name, enabled=state.strategies[name])


@app.get("/trades", response_model=list[TradeResponse])
def trades(limit: int = 20) -> list[TradeResponse]:
    rows = journal.recent(limit=limit)
    out: list[TradeResponse] = []
    for r in rows:
        out.append(TradeResponse(
            id=r["id"],
            symbol=r["symbol"],
            side=r["side"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            pnl=r["pnl"] or 0.0,
            opened_at=datetime.fromisoformat(r["opened_at"]),
            closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
        ))
    return out


@app.post("/bot/start")
def start_bot() -> dict[str, str]:
    state.running = True
    state.last_heartbeat = datetime.utcnow()
    return {"status": "started"}


@app.post("/bot/stop")
def stop_bot() -> dict[str, str]:
    state.running = False
    return {"status": "stopped"}
