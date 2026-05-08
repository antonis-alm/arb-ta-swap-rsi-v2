from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from almanak.framework.teardown import TeardownMode
from strategy import ArbTASwapRSIV2Strategy, Regime


def _cfg(**overrides):
    cfg = {
        "chain": "arbitrum",
        "protocol": "uniswap_v3",
        "base_token": "WETH",
        "quote_token": "USDC",
        "pool_fee_tier_bps": 500,
        "data_granularity": "5m",
        "rsi_period": 14,
        "rsi_lower_band": 45,
        "rsi_upper_band": 55,
        "allocation_pct": "0.95",
        "max_slippage_pct": "0.003",
        "max_price_impact_pct": "0.01",
        "max_estimated_price_impact_bps": 100,
        "min_trade_value_usd": "25",
        "max_gas_ratio": "0.05",
        "min_source_amount_weth": "0.0005",
        "min_source_amount_usdc": "10",
        "cooldown_candles": 1,
        "halt_on_repeated_failures": False,
        "max_consecutive_failed_swaps": 3,
        "force_action": "",
    }
    cfg.update(overrides)
    return cfg


def _strategy(**overrides):
    return ArbTASwapRSIV2Strategy(
        config=_cfg(**overrides),
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    ts: datetime,
    rsi: Decimal,
    weth_balance=Decimal("1.0"),
    weth_balance_usd=Decimal("3500"),
    usdc_balance=Decimal("1000"),
    usdc_balance_usd=Decimal("1000"),
    worthwhile=True,
    impact_bps=Decimal("25"),
):
    class M:
        def __init__(self):
            self.timestamp = ts
            self.chain = "arbitrum"

        def rsi(self, token, period, timeframe):
            assert token == "WETH"
            assert period == 14
            assert timeframe == "5m"
            return SimpleNamespace(value=rsi)

        def balance(self, token):
            if token == "WETH":
                return SimpleNamespace(balance=weth_balance, balance_usd=weth_balance_usd)
            if token == "USDC":
                return SimpleNamespace(balance=usdc_balance, balance_usd=usdc_balance_usd)
            raise ValueError("unknown token")

        def pool_price_by_pair(self, token_a, token_b, chain, protocol, fee_tier):
            assert token_a == "WETH" and token_b == "USDC"
            assert chain == "arbitrum" and protocol == "uniswap_v3" and fee_tier == 500
            return SimpleNamespace(data=SimpleNamespace(pool_address="0xpool"))

        def liquidity_depth(self, pool_address, chain):
            assert pool_address == "0xpool"
            assert chain == "arbitrum"
            return SimpleNamespace(data=SimpleNamespace())

        def estimate_slippage(self, token_in, token_out, amount, chain, protocol):
            assert chain == "arbitrum"
            assert protocol == "uniswap_v3"
            return SimpleNamespace(data=SimpleNamespace(price_impact_bps=impact_bps, amount_out=amount))

        def is_trade_worthwhile(self, amount_usd, chain, max_gas_ratio):
            assert chain == "arbitrum"
            assert max_gas_ratio == Decimal("0.05")
            return worthwhile

        def estimate_swap_gas_cost_usd(self, chain):
            assert chain == "arbitrum"
            return Decimal("2")

    return M()


def _intent_type(intent):
    return getattr(intent.intent_type, "value", str(intent.intent_type))


def test_waits_for_previous_rsi_before_cross_logic():
    s = _strategy()
    m = _market(ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC), rsi=Decimal("52"))

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"
    assert s.prev_rsi_value == Decimal("52")


def test_cross_above_upper_flips_to_long_weth():
    s = _strategy()
    s.prev_rsi_value = Decimal("54")
    m = _market(ts=datetime(2026, 1, 1, 0, 5, tzinfo=UTC), rsi=Decimal("56"))

    out = s.decide(m)

    assert _intent_type(out) == "SWAP"
    assert out.from_token == "USDC"
    assert out.to_token == "WETH"
    assert out.amount == Decimal("950")


def test_cross_below_lower_flips_to_long_usdc():
    s = _strategy()
    s.prev_rsi_value = Decimal("46")
    m = _market(ts=datetime(2026, 1, 1, 0, 10, tzinfo=UTC), rsi=Decimal("44"))

    out = s.decide(m)

    assert _intent_type(out) == "SWAP"
    assert out.from_token == "WETH"
    assert out.to_token == "USDC"
    assert out.amount == Decimal("0.95")


def test_neutral_band_holds_without_swap():
    s = _strategy()
    s.prev_rsi_value = Decimal("50")
    m = _market(ts=datetime(2026, 1, 1, 0, 15, tzinfo=UTC), rsi=Decimal("51"))

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"


def test_prevents_repeated_swap_when_already_in_target_regime():
    s = _strategy()
    s.current_regime = Regime.LONG_WETH
    s.prev_rsi_value = Decimal("55")
    m = _market(ts=datetime(2026, 1, 1, 0, 20, tzinfo=UTC), rsi=Decimal("56"))

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"


def test_candle_close_gate_blocks_second_decision_same_candle():
    s = _strategy()
    s.prev_rsi_value = Decimal("50")
    t = datetime(2026, 1, 1, 0, 25, tzinfo=UTC)

    first = s.decide(_market(ts=t, rsi=Decimal("51")))
    second = s.decide(_market(ts=t, rsi=Decimal("56")))

    assert _intent_type(first) == "HOLD"
    assert _intent_type(second) == "HOLD"


def test_cooldown_holds_after_successful_flip():
    s = _strategy()
    s.prev_rsi_value = Decimal("54")
    first = s.decide(_market(ts=datetime(2026, 1, 1, 0, 30, tzinfo=UTC), rsi=Decimal("56")))
    assert _intent_type(first) == "SWAP"

    s.on_intent_executed(first, success=True, result=SimpleNamespace())
    hold = s.decide(_market(ts=datetime(2026, 1, 1, 0, 35, tzinfo=UTC), rsi=Decimal("56")))
    assert _intent_type(hold) == "HOLD"


def test_insufficient_source_balance_holds():
    s = _strategy()
    s.prev_rsi_value = Decimal("54")
    m = _market(
        ts=datetime(2026, 1, 1, 0, 40, tzinfo=UTC),
        rsi=Decimal("56"),
        usdc_balance=Decimal("5"),
        usdc_balance_usd=Decimal("5"),
    )

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"


def test_high_estimated_price_impact_holds():
    s = _strategy()
    s.prev_rsi_value = Decimal("54")
    m = _market(ts=datetime(2026, 1, 1, 0, 45, tzinfo=UTC), rsi=Decimal("56"), impact_bps=Decimal("250"))

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"


def test_not_worthwhile_gas_holds():
    s = _strategy()
    s.prev_rsi_value = Decimal("54")
    m = _market(ts=datetime(2026, 1, 1, 0, 50, tzinfo=UTC), rsi=Decimal("56"), worthwhile=False)

    out = s.decide(m)

    assert _intent_type(out) == "HOLD"


def test_halt_after_repeated_failures_option():
    s = _strategy(halt_on_repeated_failures=True, max_consecutive_failed_swaps=2)
    s.consecutive_failed_swaps = 2

    out = s.decide(_market(ts=datetime(2026, 1, 1, 0, 55, tzinfo=UTC), rsi=Decimal("56")))

    assert _intent_type(out) == "HOLD"


def test_force_action_buy_and_sell_emit_swaps():
    buy_s = _strategy(force_action="buy")
    buy = buy_s.decide(_market(ts=datetime(2026, 1, 1, 1, 0, tzinfo=UTC), rsi=Decimal("50")))
    assert _intent_type(buy) == "SWAP"
    assert buy.from_token == "USDC" and buy.to_token == "WETH"

    sell_s = _strategy(force_action="sell")
    sell = sell_s.decide(_market(ts=datetime(2026, 1, 1, 1, 5, tzinfo=UTC), rsi=Decimal("50")))
    assert _intent_type(sell) == "SWAP"
    assert sell.from_token == "WETH" and sell.to_token == "USDC"


def test_on_intent_executed_updates_regime_and_failures():
    s = _strategy()
    s.pending_target_regime = Regime.LONG_WETH
    intent = SimpleNamespace(intent_type=SimpleNamespace(value="SWAP"))

    s.on_intent_executed(intent, success=True, result=SimpleNamespace())
    assert s.current_regime == Regime.LONG_WETH
    assert s.consecutive_failed_swaps == 0

    s.pending_target_regime = Regime.LONG_USDC
    s.on_intent_executed(intent, success=False, result=SimpleNamespace())
    assert s.consecutive_failed_swaps == 1
    assert s.pending_target_regime is None


def test_persistent_state_round_trip():
    s = _strategy()
    s.current_regime = Regime.LONG_WETH
    s.prev_rsi_value = Decimal("57.2")
    s.last_processed_candle_ts = 123
    s.cooldown_candles_remaining = 1
    s.consecutive_failed_swaps = 2

    state = s.get_persistent_state()

    fresh = _strategy()
    fresh.load_persistent_state(state)

    assert fresh.current_regime == Regime.LONG_WETH
    assert fresh.prev_rsi_value == Decimal("57.2")
    assert fresh.last_processed_candle_ts == 123
    assert fresh.cooldown_candles_remaining == 1
    assert fresh.consecutive_failed_swaps == 2


def test_teardown_generates_unwind_swap_when_weth_exposure_exists():
    s = _strategy()

    class MarketForTeardown:
        def balance(self, token):
            if token == "WETH":
                return SimpleNamespace(balance=Decimal("0.2"), balance_usd=Decimal("700"))
            raise ValueError("unknown token")

    s.current_regime = Regime.LONG_WETH
    soft = s.generate_teardown_intents(TeardownMode.SOFT, market=MarketForTeardown())
    hard = s.generate_teardown_intents(TeardownMode.HARD, market=MarketForTeardown())

    assert len(soft) == 1
    assert _intent_type(soft[0]) == "SWAP"
    assert soft[0].from_token == "WETH" and soft[0].to_token == "USDC"
    assert hard[0].max_slippage >= soft[0].max_slippage


def test_teardown_empty_when_no_weth_exposure():
    s = _strategy()

    class MarketForTeardown:
        def balance(self, token):
            return SimpleNamespace(balance=Decimal("0"), balance_usd=Decimal("0"))

    intents = s.generate_teardown_intents(TeardownMode.SOFT, market=MarketForTeardown())

    assert intents == []
