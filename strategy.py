import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.market.errors import (
    LiquidityDepthUnavailableError,
    MarketSnapshotError,
    PoolPriceUnavailableError,
    SlippageEstimateUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    LONG_WETH = "LONG_WETH"
    LONG_USDC = "LONG_USDC"
    NEUTRAL = "NEUTRAL"


@dataclass
class SwapPlan:
    from_token: str
    to_token: str
    amount: Decimal
    target_regime: Regime


def _safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Enum):
        return getattr(v, "value", str(v))
    return v


@almanak_strategy(
    name="arb_t_a_swap_r_s_i_v2",
    description="RSI regime flipper swap strategy for WETH/USDC 0.05% on Arbitrum",
    version="1.0.0",
    author="Generated",
    tags=["generated", "ta_swap", "rsi", "regime"],
    supported_chains=["arbitrum"],
    supported_protocols=["uniswap_v3"],
    intent_types=["SWAP", "HOLD"],
    default_chain="arbitrum",
)
class ArbTASwapRSIV2Strategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def get_config(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.protocol = str(get_config("protocol", "uniswap_v3"))
        self.base_token = str(get_config("base_token", "WETH"))
        self.quote_token = str(get_config("quote_token", "USDC"))
        self.pool_fee_tier_bps = int(get_config("pool_fee_tier_bps", 500))

        self.rsi_period = int(get_config("rsi_period", 14))
        self.rsi_lower_band = Decimal(str(get_config("rsi_lower_band", "45")))
        self.rsi_upper_band = Decimal(str(get_config("rsi_upper_band", "55")))
        self.timeframe = str(get_config("data_granularity", "5m"))

        self.allocation_pct = Decimal(str(get_config("allocation_pct", "0.95")))
        self.max_slippage_pct = Decimal(str(get_config("max_slippage_pct", "0.003")))
        self.max_price_impact_pct = Decimal(str(get_config("max_price_impact_pct", "0.01")))
        self.max_estimated_price_impact_bps = Decimal(
            str(get_config("max_estimated_price_impact_bps", "100"))
        )
        self.min_trade_value_usd = Decimal(str(get_config("min_trade_value_usd", "25")))
        self.max_gas_ratio = Decimal(str(get_config("max_gas_ratio", "0.05")))
        self.min_source_amount_weth = Decimal(str(get_config("min_source_amount_weth", "0.0005")))
        self.min_source_amount_usdc = Decimal(str(get_config("min_source_amount_usdc", "10")))

        self.cooldown_candles = int(get_config("cooldown_candles", 1))
        self.halt_on_repeated_failures = bool(get_config("halt_on_repeated_failures", False))
        self.max_consecutive_failed_swaps = int(get_config("max_consecutive_failed_swaps", 3))

        self.force_action = str(get_config("force_action", "") or "").lower()
        self.force_action_amount_usd = Decimal(str(get_config("force_action_amount_usd", "100")))

        self.current_regime = Regime.NEUTRAL
        self.prev_rsi_value: Decimal | None = None
        self.last_processed_candle_ts: int | None = None
        self.cooldown_candles_remaining = 0
        self.consecutive_failed_swaps = 0
        self.last_flip_ts: int | None = None
        self.pending_target_regime: Regime | None = None

    def _timestamp_to_epoch(self, timestamp: Any) -> int:
        if isinstance(timestamp, datetime):
            return int(timestamp.replace(tzinfo=timestamp.tzinfo or UTC).timestamp())
        return int(timestamp)

    def _candle_seconds(self) -> int:
        mapping = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        return mapping.get(self.timeframe, 300)

    def _extract_data(self, envelope: Any) -> Any:
        if hasattr(envelope, "data"):
            return envelope.data
        if hasattr(envelope, "value"):
            return envelope.value
        return envelope

    def _extract_pool_address(self, envelope: Any) -> str | None:
        data = self._extract_data(envelope)
        for attr in ("pool_address", "address", "pool"):
            value = getattr(data, attr, None)
            if isinstance(value, str) and value:
                return value
        if isinstance(data, dict):
            for key in ("pool_address", "address", "pool"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _extract_estimated_impact_bps(self, envelope: Any) -> Decimal:
        data = self._extract_data(envelope)
        for attr in ("price_impact_bps", "impact_bps"):
            value = getattr(data, attr, None)
            if value is not None:
                return Decimal(str(value))
        if hasattr(data, "price_impact") and getattr(data, "price_impact") is not None:
            return Decimal(str(getattr(data, "price_impact"))) * Decimal("10000")
        if isinstance(data, dict):
            if data.get("price_impact_bps") is not None:
                return Decimal(str(data["price_impact_bps"]))
            if data.get("price_impact") is not None:
                return Decimal(str(data["price_impact"])) * Decimal("10000")
        raise ValueError("missing price impact in slippage estimate")

    def _extract_min_output_ok(self, envelope: Any, min_output: Decimal) -> bool:
        data = self._extract_data(envelope)
        for attr in ("amount_out", "expected_amount_out", "output_amount"):
            value = getattr(data, attr, None)
            if value is not None:
                return Decimal(str(value)) >= min_output
        if isinstance(data, dict):
            for key in ("amount_out", "expected_amount_out", "output_amount"):
                if data.get(key) is not None:
                    return Decimal(str(data[key])) >= min_output
        return True

    def _validate_trade_sanity(self, market: MarketSnapshot, plan: SwapPlan, source_balance_usd: Decimal) -> str | None:
        if source_balance_usd < self.min_trade_value_usd:
            return (
                f"source balance ${source_balance_usd:.2f} below min trade value "
                f"${self.min_trade_value_usd:.2f}"
            )

        pool_quote = market.pool_price_by_pair(
            self.base_token,
            self.quote_token,
            chain=self.chain,
            protocol=self.protocol,
            fee_tier=self.pool_fee_tier_bps,
        )
        pool_address = self._extract_pool_address(pool_quote)
        if not pool_address:
            return "pool lookup did not return a pool address"

        market.liquidity_depth(pool_address, chain=self.chain)

        slippage_estimate = market.estimate_slippage(
            token_in=plan.from_token,
            token_out=plan.to_token,
            amount=plan.amount,
            chain=self.chain,
            protocol=self.protocol,
        )
        impact_bps = self._extract_estimated_impact_bps(slippage_estimate)
        if impact_bps > self.max_estimated_price_impact_bps:
            return (
                f"estimated price impact {impact_bps}bps exceeds "
                f"limit {self.max_estimated_price_impact_bps}bps"
            )

        min_output = plan.amount * (Decimal("1") - self.max_slippage_pct)
        if not self._extract_min_output_ok(slippage_estimate, min_output=min_output):
            return "estimated output is below minimum acceptable output"

        if not market.is_trade_worthwhile(
            amount_usd=source_balance_usd,
            chain=self.chain,
            max_gas_ratio=self.max_gas_ratio,
        ):
            gas_cost = market.estimate_swap_gas_cost_usd(self.chain)
            return (
                f"gas ${gas_cost} exceeds configured gas ratio "
                f"{self.max_gas_ratio:.2%} for trade value ${source_balance_usd:.2f}"
            )

        return None

    def _build_swap_plan(
        self,
        to_regime: Regime,
        weth_balance: Any,
        usdc_balance: Any,
    ) -> SwapPlan | None:
        if to_regime == Regime.LONG_WETH:
            amount = Decimal(str(usdc_balance.balance)) * self.allocation_pct
            if amount < self.min_source_amount_usdc:
                return None
            return SwapPlan(
                from_token=self.quote_token,
                to_token=self.base_token,
                amount=amount,
                target_regime=Regime.LONG_WETH,
            )

        if to_regime == Regime.LONG_USDC:
            amount = Decimal(str(weth_balance.balance)) * self.allocation_pct
            if amount < self.min_source_amount_weth:
                return None
            return SwapPlan(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount=amount,
                target_regime=Regime.LONG_USDC,
            )

        return None

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "buy":
            from_token = self.quote_token
            to_token = self.base_token
            target_regime = Regime.LONG_WETH
        elif self.force_action == "sell":
            from_token = self.base_token
            to_token = self.quote_token
            target_regime = Regime.LONG_USDC
        else:
            raise ValueError(f"Unknown force_action: {self.force_action!r}")

        self.pending_target_regime = target_regime
        logger.info(
            "force_action=%s amount_usd=%s from=%s to=%s",
            self.force_action,
            self.force_action_amount_usd,
            from_token,
            to_token,
        )
        return Intent.swap(
            from_token=from_token,
            to_token=to_token,
            amount_usd=self.force_action_amount_usd,
            max_slippage=self.max_slippage_pct,
            max_price_impact=self.max_price_impact_pct,
            protocol=self.protocol,
            chain=self.chain,
        )

    def decide(self, market: MarketSnapshot) -> Intent:
        if self.force_action:
            return self._forced_intent(market)

        if self.halt_on_repeated_failures and self.consecutive_failed_swaps >= self.max_consecutive_failed_swaps:
            return Intent.hold(reason="halted after repeated failed swaps")

        candle_seconds = self._candle_seconds()
        current_epoch = self._timestamp_to_epoch(market.timestamp)
        candle_close_ts = (current_epoch // candle_seconds) * candle_seconds
        if self.last_processed_candle_ts == candle_close_ts:
            return Intent.hold(reason="waiting for confirmed candle close")
        self.last_processed_candle_ts = candle_close_ts

        if self.cooldown_candles_remaining > 0:
            self.cooldown_candles_remaining -= 1
            return Intent.hold(reason=f"cooldown active ({self.cooldown_candles_remaining} candles remaining)")

        try:
            rsi = market.rsi(self.base_token, period=self.rsi_period, timeframe=self.timeframe)
            weth_balance = market.balance(self.base_token)
            usdc_balance = market.balance(self.quote_token)
        except (MarketSnapshotError, ValueError) as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        current_rsi = Decimal(str(rsi.value))
        if self.prev_rsi_value is None:
            self.prev_rsi_value = current_rsi
            logger.info("initialized RSI memory at %.2f", current_rsi)
            return Intent.hold(reason="waiting for prior RSI value")

        cross_up = self.prev_rsi_value <= self.rsi_upper_band and current_rsi > self.rsi_upper_band
        cross_down = self.prev_rsi_value >= self.rsi_lower_band and current_rsi < self.rsi_lower_band

        if cross_up:
            signal_regime = Regime.LONG_WETH
        elif cross_down:
            signal_regime = Regime.LONG_USDC
        else:
            signal_regime = Regime.NEUTRAL

        logger.info(
            "rsi=%.2f prev=%.2f signal=%s regime=%s",
            current_rsi,
            self.prev_rsi_value,
            signal_regime.value,
            self.current_regime.value,
        )

        self.prev_rsi_value = current_rsi

        if signal_regime == Regime.NEUTRAL:
            return Intent.hold(reason="neutral RSI band (45-55)")

        if signal_regime == self.current_regime:
            return Intent.hold(reason=f"already in target regime {signal_regime.value}")

        plan = self._build_swap_plan(signal_regime, weth_balance=weth_balance, usdc_balance=usdc_balance)
        if plan is None:
            return Intent.hold(reason="insufficient source balance for configured allocation")

        source_balance_usd = usdc_balance.balance_usd if plan.from_token == self.quote_token else weth_balance.balance_usd

        try:
            sanity_error = self._validate_trade_sanity(market, plan, source_balance_usd=Decimal(str(source_balance_usd)))
        except (
            PoolPriceUnavailableError,
            LiquidityDepthUnavailableError,
            SlippageEstimateUnavailableError,
            MarketSnapshotError,
            ValueError,
        ) as exc:
            logger.warning("sanity checks unavailable: %s", exc)
            return Intent.hold(reason=f"sanity checks unavailable: {exc}")

        if sanity_error:
            return Intent.hold(reason=f"sanity check failed: {sanity_error}")

        self.pending_target_regime = plan.target_regime
        logger.info(
            "flip %s->%s: swap %s %s -> %s",
            self.current_regime.value,
            plan.target_regime.value,
            plan.amount,
            plan.from_token,
            plan.to_token,
        )
        return Intent.swap(
            from_token=plan.from_token,
            to_token=plan.to_token,
            amount=plan.amount,
            max_slippage=self.max_slippage_pct,
            max_price_impact=self.max_price_impact_pct,
            protocol=self.protocol,
            chain=self.chain,
        )

    def on_intent_executed(self, intent, success: bool, result):
        intent_type = getattr(intent, "intent_type", None)
        if not intent_type or intent_type.value != "SWAP":
            return

        if success:
            if self.pending_target_regime:
                self.current_regime = self.pending_target_regime
                self.last_flip_ts = int(datetime.now(UTC).timestamp())
                self.cooldown_candles_remaining = self.cooldown_candles
                logger.info("swap succeeded; regime=%s cooldown=%s", self.current_regime.value, self.cooldown_candles)
            self.consecutive_failed_swaps = 0
        else:
            self.consecutive_failed_swaps += 1
            logger.warning("swap failed; consecutive_failed_swaps=%s", self.consecutive_failed_swaps)
        self.pending_target_regime = None

    def get_persistent_state(self):
        return {
            "current_regime": self.current_regime.value,
            "prev_rsi_value": str(self.prev_rsi_value) if self.prev_rsi_value is not None else None,
            "last_processed_candle_ts": self.last_processed_candle_ts,
            "cooldown_candles_remaining": self.cooldown_candles_remaining,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "last_flip_ts": self.last_flip_ts,
            "pending_target_regime": self.pending_target_regime.value if self.pending_target_regime else None,
        }

    def load_persistent_state(self, state):
        if not state:
            return
        self.current_regime = Regime(state.get("current_regime", Regime.NEUTRAL.value))
        prev_rsi = state.get("prev_rsi_value")
        self.prev_rsi_value = Decimal(prev_rsi) if prev_rsi is not None else None
        self.last_processed_candle_ts = state.get("last_processed_candle_ts")
        self.cooldown_candles_remaining = int(state.get("cooldown_candles_remaining", 0))
        self.consecutive_failed_swaps = int(state.get("consecutive_failed_swaps", 0))
        self.last_flip_ts = state.get("last_flip_ts")
        pending = state.get("pending_target_regime")
        self.pending_target_regime = Regime(pending) if pending else None

    def supports_teardown(self) -> bool:
        return True

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        if self.current_regime == Regime.LONG_WETH or self.pending_target_regime == Regime.LONG_WETH:
            positions.append(
                PositionInfo(
                    position_type=PositionType.TOKEN,
                    position_id="arb_t_a_swap_r_s_i_v2_weth",
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={"asset": self.base_token, "quote": self.quote_token},
                )
            )

        return TeardownPositionSummary(
            strategy_id=getattr(self, "strategy_id", "arb_t_a_swap_r_s_i_v2"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        from almanak.framework.teardown import TeardownMode

        should_unwind = (
            self.current_regime == Regime.LONG_WETH
            or self.pending_target_regime == Regime.LONG_WETH
        )
        if not should_unwind:
            return []

        max_slippage = Decimal("0.03") if mode == TeardownMode.HARD else self.max_slippage_pct
        return [
            Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount="all",
                max_slippage=max_slippage,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "arb_t_a_swap_r_s_i_v2",
            "chain": self.chain,
            "wallet": self.wallet_address[:10] + "..." if self.wallet_address else None,
            "regime": self.current_regime.value,
            "prev_rsi_value": _safe(self.prev_rsi_value),
            "cooldown_candles_remaining": self.cooldown_candles_remaining,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "pending_target_regime": _safe(self.pending_target_regime),
            "last_flip_ts": _safe(self.last_flip_ts),
        }


if __name__ == "__main__":
    print("=" * 60)
    print("ArbTASwapRSIV2Strategy")
    print("=" * 60)
    print(f"Strategy Name: {ArbTASwapRSIV2Strategy.STRATEGY_NAME}")
    print(f"Supported Chains: {ArbTASwapRSIV2Strategy.SUPPORTED_CHAINS}")
    print(f"Supported Protocols: {ArbTASwapRSIV2Strategy.SUPPORTED_PROTOCOLS}")
    print(f"Intent Types: {ArbTASwapRSIV2Strategy.INTENT_TYPES}")
    print("\nTo run this strategy:")
    print("  uv run almanak strat run --once")
