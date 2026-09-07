"""CLI: python -m mm {screen,record,replay,paper,costs}"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import MMConfig
from .costs import break_even_gain, net_pnl, round_trip_multiplier


def log(message: str, cfg: MMConfig | None = None) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} MM {message}"
    print(line, flush=True)
    if cfg is not None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with cfg.log_file.open("a") as fh:
            fh.write(line + "\n")


def cmd_screen(args: argparse.Namespace, cfg: MMConfig) -> int:
    from .screener import Screener
    from .sources import Sources
    screener = Screener(cfg, Sources(cfg))
    accepted, rejected = screener.run(write=not args.no_write)
    for cand in accepted:
        pool = f"DLMM {cand.dlmm_pool[:8]} tvl ${cand.dlmm_tvl_usd:,.0f} fee/tvl24h {cand.dlmm_fee_tvl_24h:.2f}%" if cand.dlmm_pool else "no DLMM pool"
        log(f"ACCEPT {cand.symbol:<10} age {cand.age_days or 0:5.0f}d liq ${cand.best_pool_liquidity_usd:>10,.0f} "
            f"traders24h {cand.traders_24h:>6} organic {cand.organic_score:3.0f} top {cand.top_holders_pct or 0:4.1f}% "
            f"impact@max {cand.impact_at_max_position_pct if cand.impact_at_max_position_pct is not None else float('nan'):.3f}% {pool}"
            + (f" WARN {'; '.join(cand.warnings)}" if cand.warnings else ""), cfg)
    for cand in rejected:
        log(f"REJECT {cand.symbol:<10} {cand.mint[:8]} " + " | ".join(r for _, r, _, _ in cand.rejects), cfg)
    log(f"screen complete: {len(accepted)} accepted, {len(rejected)} rejected, {screener.src.http.calls} HTTP calls", cfg)
    return 0


def cmd_record(args: argparse.Namespace, cfg: MMConfig) -> int:
    from .recorder import Recorder
    from .sources import Sources
    ticks = Recorder(cfg, Sources(cfg)).run(args.hours, log=lambda m: log(m, cfg))
    log(f"recorded {ticks} ticks", cfg)
    return 0


def cmd_replay(args: argparse.Namespace, cfg: MMConfig) -> int:
    from .replay import load_series, replay, write_report
    series = load_series(cfg, args.mints or None)
    if not series:
        print("no snapshots found; run `python -m mm record` or `paper` first", file=sys.stderr)
        return 1
    report = replay(cfg, series, starting_cash=args.cash)
    path = write_report(cfg, report)
    print(f"replayed {report['rows']} rows over {report['tokens']} tokens ({report['hours']}h); report at {path}")
    print(f"{'strategy':<20}{'final':>10}{'pnl':>10}{'ret%':>8}{'maxDD%':>8}{'fees':>9}{'costs':>9}{'trades':>7}")
    for name in report["ranking"]:
        m = report["strategies"][name]
        print(f"{name:<20}{m['final_nlv']:>10.2f}{m['net_pnl']:>10.2f}{m['return_pct']:>8.2f}{m['max_drawdown_pct']:>8.2f}"
              f"{m['fees_earned']:>9.3f}{m['costs_paid']:>9.3f}{m['trades']:>7}")
    return 0


def cmd_paper(args: argparse.Namespace, cfg: MMConfig) -> int:
    from .paper import PaperEngine
    engine = PaperEngine(cfg, log=lambda m: log(m, cfg))
    engine.run(args.hours)
    print(json.dumps(engine.report(), indent=1, default=str))
    return 0


def cmd_live(args: argparse.Namespace, cfg: MMConfig) -> int:
    from .live import LiveEngine
    log(f"MM LIVE: bankroll ${cfg.bankroll_usd:.2f}, max position ${cfg.max_position_usd:.2f}, "
        f"portfolio cap ${cfg.max_portfolio_exposure_usd:.2f}, daily loss limit ${cfg.daily_loss_limit_usd:.2f}", cfg)
    engine = LiveEngine(cfg, log=lambda m: log(m, cfg))
    engine.run(args.hours)
    print(json.dumps(engine.report(), indent=1, default=str))
    return 0


def cmd_costs(args: argparse.Namespace, cfg: MMConfig) -> int:
    q, d, c = args.notional, args.disadvantage, args.fixed
    print(f"notional ${q:.2f}, execution disadvantage {d:.2%} per side, fixed ${c:.2f}")
    print(f"{'fee/side':>9}{'BE fee-only':>13}{'BE all-in':>11}{'PnL @ +1%':>11}")
    for fee in (0.0025, 0.005, 0.01, 0.012):
        k_fee = round_trip_multiplier(fee, fee)
        k_all = round_trip_multiplier(fee, fee, d, d)
        print(f"{fee:>9.2%}{break_even_gain(q, k_fee):>13.4%}{break_even_gain(q, k_all, c):>11.4%}{net_pnl(q, 0.01, k_all, c):>11.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("screen", help="build the established-meme universe and log rejects")
    s.add_argument("--no-write", action="store_true")
    s.set_defaults(fn=cmd_screen)
    r = sub.add_parser("record", help="record snapshots for the current universe")
    r.add_argument("--hours", type=float, default=1.0)
    r.set_defaults(fn=cmd_record)
    p = sub.add_parser("replay", help="counterfactual replay over recorded snapshots")
    p.add_argument("--mints", nargs="*")
    p.add_argument("--cash", type=float, default=None)
    p.set_defaults(fn=cmd_replay)
    pa = sub.add_parser("paper", help="forward paper engine (screen + record + shadow strategies)")
    pa.add_argument("--hours", type=float, default=24.0)
    pa.set_defaults(fn=cmd_paper)
    lv = sub.add_parser("live", help="LIVE: real DLMM ranges and Jupiter swaps for adaptive_dlmm and momentum")
    lv.add_argument("--hours", type=float, default=876000.0)
    lv.set_defaults(fn=cmd_live)
    co = sub.add_parser("costs", help="print the round-trip break-even table")
    co.add_argument("--notional", type=float, default=50.0)
    co.add_argument("--disadvantage", type=float, default=0.001)
    co.add_argument("--fixed", type=float, default=0.02)
    co.set_defaults(fn=cmd_costs)
    args = parser.parse_args(argv)
    return args.fn(args, MMConfig.from_env())


if __name__ == "__main__":
    sys.exit(main())
