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
