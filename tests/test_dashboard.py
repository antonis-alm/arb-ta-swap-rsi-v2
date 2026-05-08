from dashboard.ui import _build_rsi_config, render_custom_dashboard


def test_build_rsi_config_uses_strategy_thresholds() -> None:
    strategy_config = {
        "rsi_period": 21,
        "rsi_upper_band": 55,
        "rsi_lower_band": 45,
    }

    config = _build_rsi_config(strategy_config)

    assert config.indicator_name == "RSI"
    assert config.indicator_period == 21
    assert config.upper_threshold == 55
    assert config.lower_threshold == 45
    assert config.signal_type == "reversion"


def test_render_custom_dashboard_calls_ta_template(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_render(strategy_id, strategy_config, session_state, config):
        captured["strategy_id"] = strategy_id
        captured["strategy_config"] = strategy_config
        captured["session_state"] = session_state
        captured["config"] = config

    monkeypatch.setattr("dashboard.ui.render_ta_dashboard", fake_render)

    strategy_id = "arb_ta_test"
    strategy_config = {
        "rsi_period": 14,
        "rsi_upper_band": 55,
        "rsi_lower_band": 45,
        "base_token": "WETH",
        "quote_token": "USDC",
    }
    session_state = {"rsi_value": 52}

    render_custom_dashboard(strategy_id, strategy_config, api_client=None, session_state=session_state)

    assert captured["strategy_id"] == strategy_id
    assert captured["strategy_config"] == strategy_config
    assert captured["session_state"] == session_state
    config = captured["config"]
    assert config.indicator_name == "RSI"
    assert config.indicator_period == 14
    assert config.upper_threshold == 55
    assert config.lower_threshold == 45
