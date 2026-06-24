# Forex-EA control API (FastAPI/uvicorn).
#
# This image packages ONLY the backend the mobile app + dashboard talk to.
# The live MT5 trading bot is intentionally NOT here: the `MetaTrader5` package
# is Windows-only (note the sys_platform marker in requirements.txt — it's
# skipped on Linux), so the bot keeps running on the Windows VPS while this
# container serves the API on :8000.
FROM python:3.12-slim AS base

# - PYTHONUNBUFFERED: logs flush immediately so `docker logs` is live.
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image.
# - PIP_NO_CACHE_DIR: smaller layers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# No apt packages needed: the API-only deps (pandas/numpy/pyarrow + pure-Python
# web/auth libs) all install from manylinux wheels, with no system libraries or
# compiler toolchain required. (xgboost's libgomp1 dependency is gone with it.)
WORKDIR /app

# Dependencies first so the (slow) pip layer is cached until requirements change.
# We install the API-only subset (requirements-api.txt) rather than the full
# requirements.txt: the API never imports the ML/backtest/plotting stack, and
# dropping it (esp. xgboost's bundled CUDA libs) cuts the image by well over 1 GB.
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

# Then the application code (only what the API serves — not main.py/the bot).
COPY src/ ./src/

# SQLite lives here. Mount a volume over it (-v ./data:/app/data) so trades,
# users, and broker configs survive container restarts.
RUN mkdir -p /app/data /app/logs

# Run as an unprivileged user. Do it after mkdir so the dirs are writable.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Container-level liveness probe against the API's /health endpoint
# (returns {"status":"ok"}). Uses stdlib urllib so no extra deps.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# AUTH_SECRET must be supplied at runtime (--env-file .env or -e AUTH_SECRET=...).
# The API fails closed without a 32+ char secret, so a misconfigured deploy
# refuses to start rather than accepting any token.
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
