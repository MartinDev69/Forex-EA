"""FastAPI backend — what the Flutter mobile app talks to.

Endpoints (stubbed for now; wire to real bot state in Week 11):
  GET  /health
  GET  /status          bot running? connected to MT5? last heartbeat?
  GET  /account         balance/equity/open positions
  GET  /trades          recent trade history
  GET  /strategies      list + enabled flag
  POST /strategies/{name}/toggle
  POST /bot/start
  POST /bot/stop

Run locally:
  uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Forex-EA Control API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- In-memory state (swap for real bot bus in Week 11) ----

class _BotState:
    running: bool = False
    strategies: dict[str, bool] = {
        "ma_crossover": True,
        "rsi_mean_reversion": False,
        "donchian_breakout": False,
    }
    last_heartbeat: datetime | None = None
    balance: float = 10_000.0
    equity: float = 10_000.0
    open_positions: int = 0


state = _BotState()


# ---- Schemas ----

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


class Trade(BaseModel):
    id: int
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_price: float
    exit_price: float | None
    pnl: float
    opened_at: datetime
    closed_at: datetime | None


# ---- Endpoints ----

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(
        running=state.running,
        mt5_connected=False,  # real check in Week 11
        last_heartbeat=state.last_heartbeat,
        open_positions=state.open_positions,
    )


@app.get("/account", response_model=AccountResponse)
def account() -> AccountResponse:
    return AccountResponse(
        balance=state.balance,
        equity=state.equity,
        open_positions=state.open_positions,
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


@app.get("/trades", response_model=list[Trade])
def trades(limit: int = 20) -> list[Trade]:
    # Placeholder — plug into SQLite journal in Week 6
    return []


@app.post("/bot/start")
def start_bot() -> dict[str, str]:
    state.running = True
    state.last_heartbeat = datetime.utcnow()
    return {"status": "started"}


@app.post("/bot/stop")
def stop_bot() -> dict[str, str]:
    state.running = False
    return {"status": "stopped"}
