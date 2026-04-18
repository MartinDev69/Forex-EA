from src.config.settings import load_settings


def test_load_settings_defaults(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "99999")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "Demo-Server")
    monkeypatch.setenv("SYMBOLS", "EURUSD,GBPUSD")
    monkeypatch.setenv("RISK_PER_TRADE", "0.015")

    settings = load_settings()

    assert settings.mt5_login == 99999
    assert settings.symbols == ["EURUSD", "GBPUSD"]
    assert settings.risk_per_trade == 0.015
