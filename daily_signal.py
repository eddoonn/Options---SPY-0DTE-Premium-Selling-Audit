"""
daily_signal.py — next-session synthetic indication (not a tradable order)

Fix1 (S0 live): re-solve strike at 09:30 live SPY open when available,
else fall back to prior-close proxy (S0_mode flags which).
Fix2 (directional gap): skip short-call when overnight up-gap
(S_open - S0_proxy) exceeds +0.5% OR +4pts — proxy stale, do not trade.
Strike/price are Black-Scholes estimates (engine.py strike_on_grid,
strategy.py bs_price) — no option quotes, no fills.
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
import yfinance as yf

from engine import (
    DATA_SOURCES,
    NY_TZ,
    cache_paths,
    load_data,
    strike_on_grid,
    year_fraction,
)
from strategy import Config, bs_delta, bs_price

# Fix2 thresholds (directional up-gap only — short calls hurt on up gaps)
GAP_PCT_THRESH = 0.005
GAP_PTS_THRESH = 4.0

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
        if d < start:
            continue
        if d == start:
            # After today's close, roll to next session (evening runs target next day)
            try:
                close = row["market_close"].tz_convert(NY_TZ)
                if now >= close:
                    continue
            except Exception:
                pass
        return d, row
    return None


def _flat_cols(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_live_open(symbol, target_date):
    """Fix1: 09:30 live open for `symbol` on target session via yfinance 1m. None if unavailable (pre-open)."""
    try:
        df = _flat_cols(yf.download(symbol, period="3d", interval="1m", prepost=False,
                                    progress=False, auto_adjust=False))
        if df.empty:
            return None
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize(NY_TZ)
        else:
            idx = idx.tz_convert(NY_TZ)
        df.index = idx
        day = df[df.index.date == target_date]
        if day.empty:
            return None
        # First regular-session print ≈ 09:30 ET open
        return float(day.iloc[0]["Open"])
    except Exception:
        return None


def fetch_live_vix():
    """Live VIX estimate via yfinance ^VIX 1m last print. None if unavailable."""
    try:
        df = _flat_cols(yf.download("^VIX", period="1d", interval="1m", prepost=False,
                                    progress=False, auto_adjust=False))
        if df.empty:
            return None
        return float(df["Close"].dropna().iloc[-1])
    except Exception:
        return None


def spx_closes():
    """Prior SPX daily closes for the XSP proxy (XSP = SPX/10). Returns {date: close}."""
    try:
        df = _flat_cols(yf.download("^SPX", period="1mo", interval="1d", progress=False,
                                    auto_adjust=False))
        if df.empty:
            return {}
        idx = df.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        return {d.date(): float(c) for d, c in zip(idx, df["Close"].values) if pd.notna(c)}
    except Exception:
        return {}


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

    # S0 proxy = prior SPY close (engine.py S0 is open, but pre-open we only have prior close)
    S0_proxy = float(daily.loc[daily.index.date == prior].iloc[-1]["Close"]) if prior in {d.date() for d in daily.index} else float(daily["Close"].iloc[-1])
    if last_daily == prior:
        S0_proxy = float(daily["Close"].iloc[-1])

    # Fix1: live 09:30 open + live VIX when available (post-open run)
    S0_live = fetch_live_open("SPY", target_date)
    vix_live = fetch_live_vix()
    if S0_live is not None:
        S0 = S0_live
        S0_mode = "live_open_0930"
    else:
        S0 = S0_proxy
        S0_mode = "proxy_prior_close"
    # Live VIX only replaces prior VIX when we also have live S0 (same post-open bar);
    # otherwise keep strictly-prior VIX to avoid mixing clocks.
    if S0_live is not None and vix_live is not None:
        vix_used = vix_live
        vix_mode = "live_intraday"
    else:
        vix_used = v_prev
        vix_mode = "prior_close"
    sigma = vix_used / 100.0

    # Fix2: directional overnight up-gap — short calls die on up gaps, proxy is stale
    gap_pts = S0 - S0_proxy
    gap_pct = gap_pts / S0_proxy if S0_proxy else 0.0

    # Use actual scheduled hours
    market_open = sched["market_open"].tz_convert(NY_TZ)
    market_close = sched["market_close"].tz_convert(NY_TZ)
    session_hours = (market_close - market_open).total_seconds() / 3600.0
    T = year_fraction(session_hours, SELECTED.clock)

    # VIX cap and backwardation checks
    skip_reasons = []
    if SELECTED.vix_max and vix_used > SELECTED.vix_max:
        skip_reasons.append(f"VIX {vix_used:.2f} > cap {SELECTED.vix_max}")
    gap_skip = (S0_mode == "live_open_0930"
                and gap_pts > 0
                and (gap_pct > GAP_PCT_THRESH or gap_pts > GAP_PTS_THRESH))
    if gap_skip:
        skip_reasons.append(
            f"overnight up-gap {gap_pts:+.2f} ({gap_pct:+.2%}) > +0.5%/+4pts — NO TRADE (proxy stale)"
        )

    legs_selected = []
    if not gap_skip:
        for kind in (["put", "call"] if SELECTED.structure == "strangle" else [SELECTED.structure]):
            leg = build_leg(S0, T, sigma, kind, SELECTED)
            if leg:
                legs_selected.append(leg)

    legs_baseline = []
    for kind in ["put", "call"]:
        leg = build_leg(S0, T, sigma, kind, BASELINE)
        if leg:
            legs_baseline.append(leg)

    # XSP (mini S&P 500: XSP = SPX/10, European cash-settled). Same rules,
    # same VIX (VIX is SPX volatility), strike solved off the SPX open/close.
    # $100 multiplier like SPY, so dollar math is identical.
    xsp_legs, xsp_skip_reasons = [], []
    XSP_S0 = XSP_mode = XSP_gap_pts = XSP_gap_pct = XSP_gap_skip = None
    spx_map = spx_closes()
    spx_prior = spx_map.get(prior)
    if spx_prior is None:
        xsp_skip_reasons.append("SPX data unavailable — no XSP ticket")
    else:
        XSP_proxy = spx_prior / 10.0
        spx_live = fetch_live_open("^SPX", target_date)
        XSP_live = spx_live / 10.0 if spx_live is not None else None
        if XSP_live is not None:
            XSP_S0, XSP_mode = XSP_live, "live_open_0930"
        else:
            XSP_S0, XSP_mode = XSP_proxy, "proxy_prior_close"
        XSP_gap_pts = XSP_S0 - XSP_proxy
        XSP_gap_pct = XSP_gap_pts / XSP_proxy if XSP_proxy else 0.0
        if SELECTED.vix_max and vix_used > SELECTED.vix_max:
            xsp_skip_reasons.append(f"VIX {vix_used:.2f} > cap {SELECTED.vix_max}")
        XSP_gap_skip = (XSP_mode == "live_open_0930" and XSP_gap_pts > 0
                        and (XSP_gap_pct > GAP_PCT_THRESH or XSP_gap_pts > GAP_PTS_THRESH))
        if XSP_gap_skip:
            xsp_skip_reasons.append(
                f"overnight up-gap {XSP_gap_pts:+.2f} ({XSP_gap_pct:+.2%}) > +0.5%/+4pts — NO TRADE (proxy stale)"
            )
        if not xsp_skip_reasons:
            for kind in (["put", "call"] if SELECTED.structure == "strangle" else [SELECTED.structure]):
                leg = build_leg(XSP_S0, T, sigma, kind, SELECTED)
                if leg:
                    xsp_legs.append(leg)
            if not xsp_legs and not xsp_skip_reasons:
                xsp_skip_reasons.append("credit ≤0.01 — no XSP ticket")

    # Data provenance
    provenance = {
        "as_of_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "as_of_ny": pd.Timestamp.now(tz=NY_TZ).isoformat(),
        "target_session": str(target_date),
        "prior_data_date": str(prior),
        "session_hours": round(session_hours, 2),
        "year_fraction_T": round(T, 6),
        "S0_proxy_prior_close": round(S0_proxy, 2),
        "S0_use": round(S0, 2),
        "S0_mode": S0_mode,
        "gap_pts": round(gap_pts, 2),
        "gap_pct": round(gap_pct, 4),
        "gap_filter": f"skip short-call if up-gap > +{GAP_PCT_THRESH:.1%}/+{GAP_PTS_THRESH:.0f}pts (live open only)",
        "gap_skipped": bool(gap_skip),
        "S0_note": ("09:30 live open re-solve (Fix1)" if S0_mode == "live_open_0930"
                    else "Prior close proxy; strike must be re-solved at 09:30 open"),
        "vix_prev": round(v_prev, 2),
        "vix_live": None if vix_live is None else round(vix_live, 2),
        "vix_used": round(vix_used, 2),
        "vix_mode": vix_mode,
        "vix9_prev": None if v9_prev is None else round(v9_prev, 2),
        "iv_sigma": round(sigma, 4),
        "XSP_S0_use": None if XSP_S0 is None else round(XSP_S0, 2),
        "XSP_S0_mode": XSP_mode,
        "XSP_gap_pts": None if XSP_gap_pts is None else round(XSP_gap_pts, 2),
        "XSP_gap_pct": None if XSP_gap_pct is None else round(XSP_gap_pct, 4),
        "XSP_gap_skipped": bool(XSP_gap_skip),
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
        "xsp_legs": xsp_legs,
        "xsp_skip_reasons": xsp_skip_reasons,
        "baseline_legs": legs_baseline,
        "baseline_config": {"structure": BASELINE.structure, "delta": BASELINE.delta, "stop": BASELINE.stop_mult, "tp": BASELINE.tp_mult},
    }


def to_discord_payload(sig):
    if "error" in sig:
        return {"content": f"⚠️ Signal error: {sig['error']} — {sig.get('target','')}", "embeds": []}

    prov = sig["provenance"]
    target = prov["target_session"]

    # Beginner-friendly dates: "Sep 18" and "Friday, September 18, 2026"
    tdate = dt.date.fromisoformat(target)
    exp_short = tdate.strftime("%b %d").replace(" 0", " ")
    exp_long = tdate.strftime("%A, %B %d, %Y")

    # Plain-English order ticket: ticker, side, strike, expiry, price, stop
    def trade_card(ticker, l):
        side = l["kind"].upper()
        wins_if = (f"you profit if {ticker} stays below {l['strike']} when it expires"
                   if l["kind"] == "call"
                   else f"you profit if {ticker} stays above {l['strike']} when it expires")
        upfront = l["credit"] * 100
        card = (
            f"SELL TO OPEN — 1 contract\n"
            f"Ticker: {ticker}\n"
            f"Option type: {side} ({wins_if})\n"
            f"Strike price: {l['strike']}\n"
            f"Expiry date: {exp_long} — expires TODAY at market close (0DTE)\n"
            f"Sell price: ${l['credit']:.2f} (you collect about ${upfront:.0f} upfront)\n"
            f"Stop loss: buy it back at ${l['stop_trigger']:.2f} (this caps your loss)"
        )
        if ticker == "XSP":
            card += "\nNote: XSP is cash-settled — no shares change hands, just cash"
        return card

    def ref_line(l):
        return (f"SELL SPY {l['strike']} {l['kind'].upper()} @ ${l['credit']:.2f}, "
                f"stop ${l['stop_trigger']:.2f}, expires {exp_short}")

    sel = sig["selected_legs"]
    xsp = sig.get("xsp_legs", [])
    base = sig["baseline_legs"]
    skip = ", ".join(sig["selected_skip_reasons"]) if sig["selected_skip_reasons"] else "none"
    xskip = ", ".join(sig.get("xsp_skip_reasons", [])) if sig.get("xsp_skip_reasons") else "none"

    if prov["S0_mode"] == "live_open_0930":
        spy_ref = (f"SPY at ${prov['S0_use']} (live open price)")
    else:
        spy_ref = (f"SPY at ${prov['S0_use']} (yesterday's close — live open not out yet)")
    if prov.get("XSP_S0_use") is None:
        xsp_ref = "XSP n/a (SPX data missing)"
    elif prov.get("XSP_S0_mode") == "live_open_0930":
        xsp_ref = (f"XSP at ${prov['XSP_S0_use']} (live open price)")
    else:
        xsp_ref = (f"XSP at ${prov['XSP_S0_use']} (yesterday's close — live open not out yet)")
    session_line = (f"Session: {exp_short} (using data up to {prov['prior_data_date']}) · "
                    f"{spy_ref} · {xsp_ref} · VIX {prov['vix_used']}")
    skip_line = (f"Skip check — SPY: {skip if skip != 'none' else 'trade ON'} · "
                 f"XSP: {xskip if xskip != 'none' else 'trade ON'}")

    blocks = []
    for ticker, legs, reasons in (("SPY", sel, skip), ("XSP", xsp, xskip)):
        if legs:
            blocks.extend(trade_card(ticker, l) for l in legs)
        else:
            blocks.append(f"{ticker}: NO TRADE today. Reason: {reasons}")
    desc = (
        "\n\n".join(blocks)
        + f"\n\n{session_line}\n"
        + skip_line
        + f"\n\nReference model (not the trade, for testing only):\n"
        + ("\n".join(ref_line(l) for l in base) if base else "no reference legs today")
    )

    # Discord 2000 char limit
    if len(desc) > 4000:
        desc = desc[:4000]

    traded = ([f"SPY {l['strike']} {l['kind'].upper()} @ ${l['credit']:.2f} (STOP ${l['stop_trigger']:.2f})"
               for l in sel]
              + [f"XSP {l['strike']} {l['kind'].upper()} @ ${l['credit']:.2f} (STOP ${l['stop_trigger']:.2f})"
                 for l in xsp])
    if not traded:
        content = f"NO TRADE — SPY/XSP {exp_short} (SPY: {skip} · XSP: {xskip})"
    else:
        content = f"SELL {' + '.join(traded)} — expires {exp_short} (today)"
    return {
        "content": content,
        "embeds": [
            {
                "title": f"SPY/XSP 0DTE Signal — {exp_short}",
                "description": desc,
                "color": 9807270 if not (sel or xsp) else 15158332,
                "footer": {"text": "Not financial advice"},
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
