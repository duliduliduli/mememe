"""Environment-driven configuration for the market-making research track.

Every knob is prefixed MM_ so it cannot collide with the graduation executor's
variables. Percentages accept either 0.25 or 25 (legacy fraction spelling)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCdbjhBoWCo"
QUOTE_MINTS = {WSOL, USDC, USDT}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Majors, stables, wrapped and staked assets never belong to the meme universe.
EXCLUDED_SYMBOLS = {
    "SOL", "WSOL", "USDC", "USDT", "USDS", "USD1", "PYUSD", "USDE", "USDG", "CASH", "EURC",
    "CBBTC", "WBTC", "ZBTC", "TBTC", "WETH", "ETH", "BTC", "ZEC", "HYPE", "JUP", "JLP",
    "JITOSOL", "MSOL", "BSOL", "JUPSOL", "INF", "HSOL", "LST", "RAY", "ORCA", "PYTH", "W",
    "WIF", "RENDER", "RNDR", "HNT", "MOBILE", "IOT", "ONDO", "KMNO", "DRIFT", "CLOUD", "TNSR",
}
EXCLUDED_TAGS = {"lst", "stablecoin", "bridged", "wrapped", "perp", "token-2022-stable"}


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _pct(name: str, default: float) -> float:
    value = _float(name, default)
    return value * 100 if 0 < value <= 1 else value


@dataclass
class MMConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")))
    rpc_urls: tuple[str, ...] = ("https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com")
    jupiter_base: str = "https://lite-api.jup.ag"
    meteora_base: str = "https://dlmm.datapi.meteora.ag"
    dexscreener_base: str = "https://api.dexscreener.com"
    request_timeout: float = 20.0
    jupiter_min_interval: float = 1.1          # seconds between lite-api calls (public quota ~60/min)

    # Universe construction
    universe_size: int = 60
    quotes: frozenset[str] = frozenset({WSOL, USDC})
    min_token_age_days: float = 30.0
    min_pool_liquidity_usd: float = 100_000.0
    preferred_pool_liquidity_usd: float = 200_000.0
    min_traders_24h: int = 300
    min_organic_score: float = 40.0
    max_top_holders_pct: float = 50.0
    max_dev_balance_pct: float = 10.0
    max_flow_imbalance: float = 0.70          # |buy-sell|/(buy+sell) over 24h
    max_transfer_fee_bps: int = 0
    require_dlmm_pool: bool = False           # momentum candidates may live on any pool

    # Sizing and exit tests (dollars are per-token unless stated)
    bankroll_usd: float = 87.0
    max_position_usd: float = 40.0
    max_token_exposure_usd: float = 40.0
    max_portfolio_exposure_usd: float = 80.0
    daily_loss_limit_usd: float = 10.0
    routine_exit_impact_pct: float = 0.25
    emergency_exit_impact_pct: float = 3.0
    exit_depth_multiple: float = 20.0         # depth at emergency band must cover this many max positions
    gas_reserve_sol: float = 0.02

    # Strategy
    horizon_hours: float = 6.0
    range_width_sigma: float = 2.0            # half-width = sigma_hourly * sqrt(horizon) * this
    min_range_half_width_pct: float = 3.0
    max_range_half_width_pct: float = 25.0
    lp_fee_share: float = 0.90                # Meteora standard DLMM position share of trading fee
    model_error_pct: float = 0.10             # per-entry allowance subtracted from projected edge
    tx_fee_usd: float = 0.01                  # base + priority per landed transaction, USD
    lp_setup_transactions: int = 3            # create position, add liquidity, later remove + close
    target_token_fraction: float = 0.50
    inventory_band: float = 0.15              # REDUCE when token fraction exceeds target + band
    reentry_cooldown_minutes: float = 30.0
    live_slippage_bps: int = 100
    lp_position_rent_sol: float = 0.06        # refundable DLMM position rent kept free per open
    sidecar_url: str = ""                     # empty: spawn mm/sidecar/server.js on sidecar_port
    sidecar_port: int = 8787
    stale_data_seconds: float = 180.0
    poll_seconds: float = 60.0
    universe_refresh_minutes: float = 60.0

    # Regime thresholds (fractions of price / volume over the lookback)
    lookback_points: int = 30
    jump_sigma: float = 4.0
    decline_drift_pct: float = -8.0           # cumulative move over lookback
    rally_drift_pct: float = 8.0
    crash_pct: float = -20.0
    volume_decay_ratio: float = 0.35          # recent volume / earlier volume
    liquidity_drop_pct: float = 30.0
    participation_drop_ratio: float = 0.40

    # Momentum challenger
    momentum_lookback: int = 12
    momentum_breakout_pct: float = 5.0
    momentum_trail_pct: float = 12.0
    momentum_volume_confirm: float = 1.5
    momentum_fee_pct_each_side: float = 0.30  # pool + router fee assumption for the replay

    @classmethod
    def from_env(cls) -> "MMConfig":
        cfg = cls()
        urls = os.getenv("MM_RPC_URLS") or os.getenv("RPC_URLS") or os.getenv("RPC_URL")
        if urls:
            cfg.rpc_urls = tuple(u.strip() for u in urls.replace("\n", ",").split(",") if u.strip())
        cfg.jupiter_base = os.getenv("MM_JUPITER_BASE", cfg.jupiter_base).rstrip("/")
        cfg.meteora_base = os.getenv("MM_METEORA_BASE", cfg.meteora_base).rstrip("/")
        cfg.jupiter_min_interval = _float("MM_JUPITER_MIN_INTERVAL", cfg.jupiter_min_interval)
        cfg.universe_size = _int("MM_UNIVERSE_SIZE", cfg.universe_size)
        if os.getenv("MM_QUOTES"):
            cfg.quotes = frozenset(q.strip() for q in os.environ["MM_QUOTES"].split(",") if q.strip())
        cfg.min_token_age_days = _float("MM_MIN_TOKEN_AGE_DAYS", cfg.min_token_age_days)
        cfg.min_pool_liquidity_usd = _float("MM_MIN_POOL_LIQUIDITY_USD", cfg.min_pool_liquidity_usd)
        cfg.preferred_pool_liquidity_usd = _float("MM_PREFERRED_POOL_LIQUIDITY_USD", cfg.preferred_pool_liquidity_usd)
        cfg.min_traders_24h = _int("MM_MIN_TRADERS_24H", cfg.min_traders_24h)
        cfg.min_organic_score = _float("MM_MIN_ORGANIC_SCORE", cfg.min_organic_score)
        cfg.max_top_holders_pct = _pct("MM_MAX_TOP_HOLDERS_PCT", cfg.max_top_holders_pct)
        cfg.max_dev_balance_pct = _pct("MM_MAX_DEV_BALANCE_PCT", cfg.max_dev_balance_pct)
        cfg.max_flow_imbalance = _float("MM_MAX_FLOW_IMBALANCE", cfg.max_flow_imbalance)
        cfg.max_transfer_fee_bps = _int("MM_MAX_TRANSFER_FEE_BPS", cfg.max_transfer_fee_bps)
        cfg.require_dlmm_pool = os.getenv("MM_REQUIRE_DLMM_POOL", "0") == "1"
        cfg.bankroll_usd = _float("MM_BANKROLL_USD", cfg.bankroll_usd)
        cfg.max_position_usd = _float("MM_MAX_POSITION_USD", cfg.max_position_usd)
        cfg.max_token_exposure_usd = _float("MM_MAX_TOKEN_EXPOSURE_USD", cfg.max_token_exposure_usd)
        cfg.max_portfolio_exposure_usd = _float("MM_MAX_PORTFOLIO_EXPOSURE_USD", cfg.max_portfolio_exposure_usd)
        cfg.daily_loss_limit_usd = _float("MM_DAILY_LOSS_LIMIT_USD", cfg.daily_loss_limit_usd)
        cfg.routine_exit_impact_pct = _pct("MM_ROUTINE_EXIT_IMPACT_PCT", cfg.routine_exit_impact_pct)
        cfg.emergency_exit_impact_pct = _pct("MM_EMERGENCY_EXIT_IMPACT_PCT", cfg.emergency_exit_impact_pct)
        cfg.exit_depth_multiple = _float("MM_EXIT_DEPTH_MULTIPLE", cfg.exit_depth_multiple)
        cfg.horizon_hours = _float("MM_HORIZON_HOURS", cfg.horizon_hours)
        cfg.range_width_sigma = _float("MM_RANGE_WIDTH_SIGMA", cfg.range_width_sigma)
        cfg.min_range_half_width_pct = _pct("MM_MIN_RANGE_HALF_WIDTH_PCT", cfg.min_range_half_width_pct)
        cfg.max_range_half_width_pct = _pct("MM_MAX_RANGE_HALF_WIDTH_PCT", cfg.max_range_half_width_pct)
        cfg.lp_fee_share = _float("MM_LP_FEE_SHARE", cfg.lp_fee_share)
        cfg.model_error_pct = _pct("MM_MODEL_ERROR_PCT", cfg.model_error_pct)
        cfg.tx_fee_usd = _float("MM_TX_FEE_USD", cfg.tx_fee_usd)
        cfg.target_token_fraction = _float("MM_TARGET_TOKEN_FRACTION", cfg.target_token_fraction)
        cfg.inventory_band = _float("MM_INVENTORY_BAND", cfg.inventory_band)
        cfg.reentry_cooldown_minutes = _float("MM_REENTRY_COOLDOWN_MINUTES", cfg.reentry_cooldown_minutes)
        cfg.live_slippage_bps = _int("MM_LIVE_SLIPPAGE_BPS", cfg.live_slippage_bps)
        cfg.lp_position_rent_sol = _float("MM_LP_POSITION_RENT_SOL", cfg.lp_position_rent_sol)
        cfg.sidecar_url = os.getenv("MM_SIDECAR_URL", cfg.sidecar_url)
        cfg.sidecar_port = _int("MM_SIDECAR_PORT", cfg.sidecar_port)
        cfg.stale_data_seconds = _float("MM_STALE_DATA_SECONDS", cfg.stale_data_seconds)
        cfg.poll_seconds = _float("MM_POLL_SECONDS", cfg.poll_seconds)
        cfg.universe_refresh_minutes = _float("MM_UNIVERSE_REFRESH_MINUTES", cfg.universe_refresh_minutes)
        cfg.lookback_points = _int("MM_LOOKBACK_POINTS", cfg.lookback_points)
        cfg.momentum_lookback = _int("MM_MOMENTUM_LOOKBACK", cfg.momentum_lookback)
        cfg.momentum_breakout_pct = _pct("MM_MOMENTUM_BREAKOUT_PCT", cfg.momentum_breakout_pct)
        cfg.momentum_trail_pct = _pct("MM_MOMENTUM_TRAIL_PCT", cfg.momentum_trail_pct)
        cfg.momentum_fee_pct_each_side = _pct("MM_MOMENTUM_FEE_PCT", cfg.momentum_fee_pct_each_side)
        return cfg

    # Derived paths -----------------------------------------------------
    @property
    def universe_file(self) -> Path:
        return self.data_dir / "mm_universe.csv"

    @property
    def rejects_file(self) -> Path:
        return self.data_dir / "mm_rejects.csv"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "mm_snapshots"

    @property
    def paper_dir(self) -> Path:
        return self.data_dir / "mm_paper"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "mm_live"

    @property
    def stop_flag(self) -> Path:
        return self.data_dir / "mm.stop"

    @property
    def panic_flag(self) -> Path:
        return self.data_dir / "mm.panic"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "mm.log"
