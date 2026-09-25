"""Built-in chain table. Every field can be overridden with EVM_<CHAIN>_<FIELD> (for example
EVM_ROBINHOOD_RPC_URL); the defaults are the public endpoints and the Uniswap / PancakeSwap
deployments on each chain."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class Chain:
    key: str
    chain_id: int
    rpc_url: str
    native_symbol: str
    wrapped_native: str            # WETH9 / WBNB
    stable: str                    # USDC / USDT used to price the native token
    stable_decimals: int
    block_seconds: float
    v3_quoter: str                 # QuoterV2 (Uniswap V3 or PancakeSwap V3)
    v3_router: str                 # SwapRouter02 (Uniswap) / SmartRouter (Pancake)
    v3_fees: tuple[int, ...]
    v2_router: str                 # UniswapV2Router02 / PancakeRouter
    explorer: str
    gas_reserve_native: float      # keep this much native for gas
    price_chain: str = ""          # price the native token on this chain instead (same asset)
    # Uniswap V4: the singleton PoolManager, a V4Quoter bound to it, the Universal Router most
    # swaps go through, and Permit2. Empty: no V4 routing on this chain.
    v4_pool_manager: str = ""
    v4_quoter: str = ""
    universal_router: str = ""
    permit2: str = ""
    extra: dict[str, str] = field(default_factory=dict)


ZERO = "0x0000000000000000000000000000000000000000"

DEFAULTS: dict[str, Chain] = {
    # Robinhood Chain: Arbitrum Orbit L2, gas in ETH. Uniswap V2/V3/V4 deployed at launch;
    # memecoins launch on bonding curves (Pons, Bags, pools.trade) and graduate into Uniswap V4
    # pools, so the Uniswap Trading API is the route that reaches them; the on-chain V3/V2
    # routers below are the key-less fallback.
    "robinhood": Chain(
        key="robinhood", chain_id=4663, rpc_url="https://rpc.mainnet.chain.robinhood.com",
        native_symbol="ETH", wrapped_native="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        stable="0x5fc5360d0400a0fd4f2af552add042d716f1d168", stable_decimals=6, block_seconds=0.1,    # USDG; ~10 blocks a second (measured)
        v3_quoter="0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7", v3_router="0xcaf681a66d020601342297493863e78c959e5cb2",
        v3_fees=(100, 500, 2500, 3000, 10000), v2_router="0x89e5db8b5aa49aa85ac63f691524311aeb649eba",
        explorer="https://robinhoodchain.blockscout.com/tx/", gas_reserve_native=0.002,
        # Every memecoin pool we checked (Uniswap V4 and Pons, which is V4 with a hook) lives in
        # this PoolManager; the router is the one most of its swaps come through.
        v4_pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        v4_quoter="0x6492C2e9340A6Cc1b12963D4723D819Af5B3CC5F",
        universal_router="0x8876789976dEcBfCbBbe364623C63652db8C0904",
        permit2="0x000000000022D473030F116dDEE9F6B43aC78BA3",
    ),
    "base": Chain(
        key="base", chain_id=8453, rpc_url="https://mainnet.base.org,https://base-rpc.publicnode.com",
        native_symbol="ETH", wrapped_native="0x4200000000000000000000000000000000000006",
        stable="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", stable_decimals=6, block_seconds=2.0,
        v3_quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a", v3_router="0x2626664c2603336E57B271c5C0b26F421741e481",
        v3_fees=(500, 3000, 10000), v2_router="0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
        explorer="https://basescan.org/tx/", gas_reserve_native=0.002,
    ),
    # BNB Chain: PancakeSwap V3 (QuoterV2 + SmartRouter share the Uniswap SwapRouter02 ABI) and
    # PancakeSwap V2 carry nearly all memecoin liquidity.
    "bnb": Chain(
        key="bnb", chain_id=56, rpc_url="https://bsc-dataseed.binance.org,https://bsc-rpc.publicnode.com",
        native_symbol="BNB", wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        stable="0x55d398326f99059fF775485246999027B3197955", stable_decimals=18, block_seconds=0.75,
        v3_quoter="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997", v3_router="0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        v3_fees=(100, 500, 2500, 10000), v2_router="0x10ED43C718714eb63d5aA57B78B54704E256024E",
        explorer="https://bscscan.com/tx/", gas_reserve_native=0.005,
    ),
}


ALCHEMY_NETWORKS = {"base": "base-mainnet", "bnb": "bnb-mainnet"}


def alchemy_key() -> str:
    """The Alchemy key already used for Solana (RPC_URL) or ALCHEMY_API_KEY, so the EVM chains
    Alchemy serves get a keyed endpoint ahead of the public ones without more configuration."""
    explicit = os.getenv("ALCHEMY_API_KEY", "").strip()
    if explicit:
        return explicit
    for name in ("RPC_URL", "RPC_URLS"):
        match = re.search(r"alchemy\.com/v2/([A-Za-z0-9_-]{8,})", os.getenv(name, ""))
        if match:
            return match.group(1)
    return ""


def load_chains(keys: list[str]) -> dict[str, Chain]:
    chains: dict[str, Chain] = {}
    key_alchemy = alchemy_key()
    for key in keys:
        key = key.strip().lower()
        if not key:
            continue
        base = DEFAULTS.get(key)
        if base is None:
            raise SystemExit(f"unknown chain '{key}' (known: {', '.join(DEFAULTS)})")
        chain = Chain(**{**base.__dict__})
        if key_alchemy and key in ALCHEMY_NETWORKS:
            chain.rpc_url = f"https://{ALCHEMY_NETWORKS[key]}.g.alchemy.com/v2/{key_alchemy}," + chain.rpc_url
        prefix = f"EVM_{key.upper()}_"
        for name in ("rpc_url", "v3_quoter", "v3_router", "v2_router", "wrapped_native", "stable",
                     "v4_pool_manager", "v4_quoter", "universal_router", "permit2"):
            value = os.getenv(prefix + name.upper())
            if value:
                setattr(chain, name, value.strip())
        chains[key] = chain
    return chains
