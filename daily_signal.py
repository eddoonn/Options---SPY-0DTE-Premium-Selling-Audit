"""
signal.py — next-session synthetic indication (not a tradable order)

Uses only prior-session CBOE VIX/VIX9D + last SPY close as S0 proxy.
Strike/price are Black-Scholes estimates (engine.py:93 strike_on_grid,
strategy.py:9 bs_price) — no option quotes, no fills.
All Discord messages carry the synthetic warning.
"""
import argparse
import datetime as dt
import json
import os
import sys

import pandas as pd
import pandas_market_calendars as mcal
import requests

from engine import (
    DATA_SOURCES,
    NY_TZ,
    cache_paths,
    load_data,
    strike_on_grid,
    year_fraction,
)
from strategy import Config, bs_delta, bs_price

# Locked development-selected config (2024-2025 only)
# results/optimization_runs/corrected_development_v3_20260826/selected_config.json:1
SELECTED = Config(
    structure="call",
    delta=0.20,
    stop_mult=3.0,
    tp_mult=0.0,
    vix_max=25.0,
    skip_bwd=False,
    trend_filter=False,
    clock="trading",
    credit_haircut=1.0,
    exit_iv_mult=1.0,
    slippage=0.03,
    commission=0.65,
    account=25000.0,
    contracts=1,
)

BASELINE = Config(
    structure="strangle",
    delta=0.16,
    stop_mult=2.0,
    tp_mult=0.5,
    clock="trading",
)


def fmt_money(x):
    return f"${x:.2f}"


def next_session(today=None):
    now = pd.Timestamp.now(tz=NY_TZ) if today is None else pd.Timestamp(today).tz_localize(NY_TZ) if getattr(today, "tzinfo", None) is None else today.tz_convert(NY_TZ)
    start = now.date()
    end = start + dt.timedelta(days=10)
    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    if sched.empty:
        return None
    for sess_date, row in sched.iterrows():
        d = sess_date.date()
        if d >= start:
            return d, row
    return None


def prior_trading_day(target, daily_index):
    dates = sorted({d.date() for d in daily_index})
    # latest date < target
    cands = [d for d in dates if d < target]
    return max(cands) if cands else None


def build_leg(S0, T, sigma, kind, cfg):
    k = strike_on_grid(S0, T, sigma, kind, cfg.delta, cfg.r, cfg.q)
    if k is None:
        return None
    mid = bs_price(S0, k, T, sigma, kind, cfg.r, cfg.q)
    credit = mid * cfg.credit_haircut - cfg.slippage
    if credit <= 0.01:
        return None
    delta = bs_delta(S0, k, T, sigma, kind, cfg.r, cfg.q)
    stop = cfg.stop_mult * credit + cfg.slippage
    est_margin = max(0.20 * S0 - max(S0 - k if kind == "put" else k - S0, 0.0), 0.10 * S0) * 100 + credit * 100
    return {
        "kind": kind,
        "strike": k,
        "mid": round(mid, 4),
        "credit": round(credit, 4),
        "delta": round(delta, 4),
        "stop_trigger": round(stop, 4),
        "est_margin": round(est_margin, 2),
    }


def generate_signal(refresh=False):
    daily, vix, vix9d, rth = load_data(refresh=refresh)
    # daily index is naive; convert to date
    last_daily = daily.index.max().date()
    # vix index is naive datetime
    vix_map = {d.date(): float(v) for d, v in vix.items() if pd.notna(v)}
    vix9_map = {d.date(): float(v) for d, v in vix9d.items() if pd.notna(v)}

    nxt = next_session()
    if nxt is None:
        return {"error": "No NYSE session in next 10 days", "as_of": pd.Timestamp.now(tz=NY_TZ).isoformat()}

    target_date, sched = nxt
    # Need prior trading day that has VIX
    prior = prior_trading_day(target_date, daily.index)
    if prior is None:
        return {"error": f"No prior daily before {target_date}", "target": str(target_date)}

    # Prior VIX is strictly prior session (engine.py:180)
    v_prev = vix_map.get(prior)
    if v_prev is None:
        return {"error": f"Missing CBOE VIX for prior {prior}", "target": str(target_date), "prior": str(prior)}

    v9_prev = vix9_map.get(prior)
    sigma = v_prev / 100.0

    # S0 proxy = last SPY close (engine.py:203 S0 is open, but for pre-open signal we use prior close)
    S0 = float(daily.loc[daily.index.date == prior].iloc[-1]["Close"]) if prior in {d.date() for d in daily.index} else float(daily["Close"].iloc[-1])
    # Try to use most recent close if target is next day and we have last_daily close as better proxy
    if last_daily == prior:
        S0 = float(daily["Close"].iloc[-1])

    session_hours = (sched["market_close"].tz_convert(NY_TZ) - sched["market_close"].tz_convert(NY_TZ).normalize() - pd.Timedelta(hours=0)).total_seconds()/3600
    # Use actual scheduled hours
    market_open = sched["market_open"].tz_convert(NY_TZ)
    market_close = sched["market_close"].tz_convert(NY_TZ)
    session_hours = (market_close - market_open).total_seconds() / 3600.0
    T = year_fraction(session_hours, SELECTED.clock)

    # VIX cap and backwardation checks
    skip_reasons = []
    if SELECTED.vix_max and v_prev > SELECTED.vix_max:
        skip_reasons.append(f"VIX {v_prev:.2f} > cap {SELECTED.vix_max}")

    legs_selected = []
    for kind in (["put", "call"] if SELECTED.structure == "strangle" else [SELECTED.structure]):
        leg = build_leg(S0, T, sigma, kind, SELECTED)
        if leg:
            legs_selected.append(leg)

    legs_baseline = []
    for kind in ["put", "call"]:
        leg = build_leg(S0, T, sigma, kind, BASELINE)
        if leg:
            legs_baseline.append(leg)

    # Data provenance
    provenance = {
        "as_of_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "as_of_ny": pd.Timestamp.now(tz=NY_TZ).isoformat(),
        "target_session": str(target_date),
        "prior_data_date": str(prior),
        "session_hours": round(session_hours, 2),
        "year_fraction_T": round(T, 6),
        "S0_proxy_prior_close": round(S0, 2),
        "S0_note": "Prior close proxy; actual 09:30 open will differ — strike must be re-solved at open",
        "vix_prev": round(v_prev, 2),
        "vix9_prev": None if v9_prev is None else round(v9_prev, 2),
        "iv_sigma": round(sigma, 4),
        "data_sources": {k: daily.attrs.get("source_url", DATA_SOURCES[k]) if k == "daily" else vix.attrs.get("source_url", DATA_SOURCES[k]) if k == "vix" else vix9d.attrs.get("source_url", DATA_SOURCES[k]) if k == "vix9d" else rth.attrs.get("source_url", DATA_SOURCES[k]) for k in DATA_SOURCES},
        "source_ranges": {
            "daily": [str(daily.index.min()), str(daily.index.max())],
            "vix": [str(vix.index.min()), str(vix.index.max())],
            "vix9d": [str(vix9d.index.min()), str(vix9d.index.max())],
        },
        "warning": "SYNTHETIC Black-Scholes/VIX proxy — NOT option quotes, NOT financial advice, NOT an edge",
    }

    return {
        "provenance": provenance,
        "selected_config": {"structure": SELECTED.structure, "delta": SELECTED.delta, "stop": SELECTED.stop_mult, "tp": SELECTED.tp_mult, "vix_max": SELECTED.vix_max, "clock": SELECTED.clock},
        "selected_legs": legs_selected,
        "selected_skip_reasons": skip_reasons,
        "baseline_legs": legs_baseline,
        "baseline_config": {"structure": BASELINE.structure, "delta": BASELINE.delta, "stop": BASELINE.stop_mult, "tp": BASELINE.tp_mult},
    }


def to_discord_payload(sig):
    if "error" in sig:
        return {"content": f"⚠️ Signal error: {sig['error']} — {sig.get('target','')}", "embeds": []}

    prov = sig["provenance"]
    # Build embed
    def leg_line(l):
        return f"{l['kind'].upper()} {l['strike']} | Δ {l['delta']:+.2f} | mid {l['mid']:.2f} → credit {l['credit']:.2f} | stop {l['stop_trigger']:.2f} | margin ~${l['est_margin']:.0f}"

    sel = sig["selected_legs"]
    base = sig["baseline_legs"]
    skip = ", ".join(sig["selected_skip_reasons"]) if sig["selected_skip_reasons"] else "none"

    desc = (
        f"**SYNTHETIC — NOT tradable quotes**\n"
        f"Target: **{prov['target_session']}** (prior data {prov['prior_data_date']}) · S0 proxy {prov['S0_proxy_prior_close']} · VIX {prov['vix_prev']} → σ {prov['iv_sigma']}\n"
        f"T={prov['year_fraction_T']} ({prov['session_hours']}h, {sig['selected_config']['clock']})\n"
        f"Skip check: {skip}\n\n"
        f"**Selected (dev 2024-25 only, call 20Δ 3x no-TP):**\n"
        + ("\n".join(leg_line(l) for l in sel) if sel else "_no leg (credit ≤0.01 or VIX cap)_")
        + "\n\n**Baseline (guide-literal 16Δ strangle 2x/50%):**\n"
        + ("\n".join(leg_line(l) for l in base) if base else "_no leg_")
        + f"\n\n_Data: CBOE VIX/VIX9D + yfinance SPY · {prov['source_ranges']['daily'][1]} as_of {prov['as_of_ny']}_"
        + "\n⚠️ Paper model only — re-solve strike at 09:30 open with live VIX/price; forward test required."
    )

    # Discord 2000 char limit
    if len(desc) > 4000:
        desc = desc[:4000]

    return {
        "content": f"SPY 0DTE Synthetic Signal — {prov['target_session']} — {prov['S0_proxy_prior_close']} @ VIX {prov['vix_prev']}",
        "embeds": [
            {
                "title": "SPY 0DTE Premium-Selling Audit — Synthetic Signal",
                "description": desc,
                "color": 15158332,
                "footer": {"text": "Synthetic Black-Scholes/VIX proxy — not financial advice"},
                "timestamp": prov["as_of_utc"],
            }
        ],
    }


def post_discord(webhook, payload):
    resp = requests.post(webhook, json=payload, timeout=20)
    # Discord returns 204 on success, 200 with body on some
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook failed {resp.status_code}: {resp.text[:500]}")
    return resp.status_code


def main():
    ap = argparse.ArgumentParser(description="Generate next-session synthetic signal")
    ap.add_argument("--refresh", action="store_true", help="force refresh CBOE/yfinance caches")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"), help="Discord webhook URL (or env DISCORD_WEBHOOK_URL)")
    ap.add_argument("--dry-run", action="store_true", help="print payload, don't POST")
    ap.add_argument("--json-out", help="write full signal JSON to path")
    args = ap.parse_args()

    sig = generate_signal(refresh=args.refresh)
    payload = to_discord_payload(sig)

    print(json.dumps(sig, indent=2))
    print("\n--- DISCORD PAYLOAD ---")
    print(json.dumps(payload, indent=2))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(sig, f, indent=2)

    if args.dry_run:
        print("\nDry run — not posting to Discord")
        return 0

    if not args.webhook:
        print("\nNo webhook provided — set DISCORD_WEBHOOK_URL env or --webhook", file=sys.stderr)
        print("To post, add repository secret DISCORD_WEBHOOK_URL in GitHub Settings > Secrets and variables > Actions", file=sys.stderr)
        return 2

    code = post_discord(args.webhook, payload)
    print(f"Posted to Discord: {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
