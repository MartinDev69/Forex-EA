# Forex-EA

Automated Forex trading robot built in Python on top of MetaTrader 5, with a Flutter mobile companion app for monitoring and control.

## Stack

- **Bot:** Python 3.10–3.12, MetaTrader 5, pandas, numpy, ta, backtrader
- **ML:** scikit-learn, xgboost, tensorflow (Phase 3)
- **Mobile UI:** Flutter (in `mobile/`)
- **Alerts:** Telegram bot
- **Storage:** SQLite for trade journal, CSV/Parquet for historical data

## Project layout

```
forex-ea/
├── src/
│   ├── config/         # settings, credentials loading
│   ├── connection/     # MT5 client wrapper
│   ├── indicators/     # SMA, EMA, RSI, MACD, ATR, etc.
│   ├── strategies/     # MA crossover, RSI, breakout, etc.
│   ├── risk/           # position sizing, stop-loss, portfolio heat
│   ├── execution/      # order placement, trade management
│   ├── backtesting/    # backtrader integration, walk-forward
│   ├── ml/             # feature engineering, model training
│   ├── monitoring/     # logging, Telegram alerts, journal
│   └── utils/          # shared helpers
├── tests/              # pytest suite
├── logs/               # runtime logs (gitignored)
├── data/               # historical data (gitignored)
├── notebooks/          # research & analysis
├── mobile/             # Flutter app
└── docs/               # design notes, strategy specs
```

## Roadmap

Following the 12-week plan from `Forex_Robot_Guide.pdf`:

- **Week 1 (current):** Project scaffolding, MT5 demo account connection
- **Week 2:** Indicator library + first strategy (MA crossover)
- **Week 3:** Backtesting framework
- **Week 4:** Risk management module
- **Week 5:** Paper trading on demo account
- **Week 6:** Telegram alerts + trade journal
- **Week 7–8:** Additional strategies, portfolio management
- **Week 9–10:** ML signal layer
- **Week 11:** Flutter mobile dashboard
- **Week 12:** VPS deployment

## Setup — bot

```bash
python3.12 -m venv venv
source venv/bin/activate          # macOS/Linux
pip install -r requirements.txt
cp .env.example .env              # then fill in broker credentials
pytest                            # run the test suite
```

Python 3.12 is required — `MetaTrader5` only supports 3.8–3.12, and pandas wheels aren't ready for 3.14 yet.

⚠️ The `MetaTrader5` Python package runs only on Windows. On macOS, run the live bot inside a Windows VPS or a Windows VM — you can still develop, backtest, and test everything else locally.

## Setup — mobile app (AntiGreed)

```bash
cd mobile
flutter pub get
flutter run                       # launches on attached emulator/device
```

The app expects the FastAPI backend at `http://127.0.0.1:8000` (iOS sim) or `http://10.0.2.2:8000` (Android emulator). Start it with:

```bash
uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8000
```

Override the mobile base URL if running on a physical device:

```bash
flutter run --dart-define=API_BASE_URL=http://<your-mac-lan-ip>:8000
```
