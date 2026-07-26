"""Shared pytest setup.

This module is imported by pytest before any test module (and therefore before
``src.api.auth`` / ``src.api.broker_config`` compute their import-time hashes),
so the environment defaults below take effect for the whole run.

The two cost-factor knobs are the reason the suite used to take many minutes:
bcrypt at cost 12 and PBKDF2 at 200k iterations are deliberately slow for
production security, but the tests hash hundreds of passwords and derive the
broker-config key on every store init. Turning both down to the minimum keeps
the run fast without touching the secure production defaults — those still
apply whenever these env vars are unset.
"""
import os

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")
os.environ.setdefault("BCRYPT_ROUNDS", "4")          # bcrypt minimum
os.environ.setdefault("BROKER_KDF_ITERATIONS", "1000")

# --- Isolate the suite from the production .env -----------------------------
# src/api/server.py calls load_dotenv() at import time, so on the VPS importing
# it pulls C:\forex-ea\.env into os.environ. That made four tests fail here
# while passing on a dev machine: SYMBOLS=Volatility 10 (1s) Index made
# _active_symbols() filter the EURUSD/GBPUSD fixtures out of the correlation,
# drift and regime endpoints.
#
# load_dotenv() defaults to override=False, so anything pinned here survives it.
# Assignment (not setdefault) is deliberate -- these must win even when the real
# values are already exported in the environment.
#
# MT5_* and USE_MT5 are pinned for safety, not just determinism: a test that
# builds an MT5Client from ambient config could otherwise reach the live broker.
os.environ["SYMBOLS"] = ""          # empty => endpoints apply no symbol filter
os.environ["USE_MT5"] = "0"
for _leaky in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "PUBLIC_BASE_URL"):
    os.environ.pop(_leaky, None)
    os.environ[_leaky] = ""
