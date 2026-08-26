import math
import os

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

from strategy import Config, bs_delta, bs_price

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
MULT = 100.0
TRADING_HOURS_YEAR = 252.0 * 6.5
CALENDAR_HOURS_YEAR = 365.0 * 24.0
NY_TZ = "America/New_York"
DATA_START = "2023-11-01"
DATA_END = "2027-01-01"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
CBOE_VIX9D_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv"
DATA_SOURCES = {
    "daily": "yfinance:SPY:1d",
    "vix": CBOE_VIX_URL,
    "vix9d": CBOE_VIX9D_URL,
    "rth": "yfinance:SPY:1h",
}


def _flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def cache_paths():
    return {k: os.path.join(CACHE, f"cache_{k}.pkl") for k in DATA_SOURCES}


def _cboe_close(url):
    frame = pd.read_csv(url)
    frame.columns = [str(column).strip().upper() for column in frame.columns]
    if "DATE" not in frame or "CLOSE" not in frame:
        raise ValueError(f"unexpected CBOE schema from {url}")
    dates = pd.to_datetime(frame["DATE"], format="%m/%d/%Y", errors="coerce")
    values = pd.to_numeric(frame["CLOSE"], errors="coerce")
    series = pd.Series(values.to_numpy(), index=dates, name="Close").dropna().sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = series.loc[pd.Timestamp(DATA_START):pd.Timestamp(DATA_END)]
    if series.empty:
        raise ValueError(f"no usable CBOE history from {url}")
    series.attrs["source_url"] = url
    return series


def load_data(refresh=False):
    os.makedirs(CACHE, exist_ok=True)
    paths = cache_paths()
    if not refresh and all(os.path.exists(p) for p in paths.values()):
        cached = tuple(pd.read_pickle(paths[k]) for k in ("daily", "vix", "vix9d", "rth"))
        if (cached[1].attrs.get("source_url") == CBOE_VIX_URL
                and cached[2].attrs.get("source_url") == CBOE_VIX9D_URL):
            return cached
    daily = _flat(yf.download("SPY", start=DATA_START, end=DATA_END, interval="1d",
                              progress=False, auto_adjust=False))
    vix = _cboe_close(CBOE_VIX_URL)
    vix9d = _cboe_close(CBOE_VIX9D_URL)
    hourly = _flat(yf.download("SPY", period="730d", interval="1h", progress=False, auto_adjust=False))
    if daily.empty or hourly.empty:
        raise ValueError("Yahoo Finance returned empty SPY history")
    if hourly.index.tz is None:
        hourly.index = hourly.index.tz_localize(NY_TZ)
    else:
        hourly.index = hourly.index.tz_convert(NY_TZ)
    rth = hourly[((hourly.index.hour >= 9) & (hourly.index.hour <= 15))].sort_index()
    retrieved_utc = pd.Timestamp.now(tz="UTC").isoformat()
    for name, data in (("daily", daily), ("vix", vix), ("vix9d", vix9d), ("rth", rth)):
        data.attrs["source_url"] = DATA_SOURCES[name]
        data.attrs["retrieved_utc"] = retrieved_utc
    daily.to_pickle(paths["daily"])
    vix.to_pickle(paths["vix"])
    vix9d.to_pickle(paths["vix9d"])
    rth.to_pickle(paths["rth"])
    return daily, vix, vix9d, rth


def year_fraction(hours, clock):
    if clock == "trading":
        return max(hours, 0.001) / TRADING_HOURS_YEAR
    if clock == "calendar":
        return max(hours, 0.001) / CALENDAR_HOURS_YEAR
    raise ValueError(f"unsupported clock: {clock}")


def strike_on_grid(S, T, sigma, kind, target, r=0.04, q=0.012):
    if T <= 0.0 or sigma <= 0.0 or not math.isfinite(sigma) or not math.isfinite(T):
        return None
    lo = int(round(S * 0.85))
    hi = int(round(S * 1.15))
    ks = np.arange(lo, hi + 1, dtype=float)
    sq = sigma * math.sqrt(T)
    if sq <= 0.0 or not math.isfinite(sq):
        return None
    d1 = (np.log(S / ks) + (r - q + 0.5 * sigma * sigma) * T) / sq
    nd1 = 0.5 * (1.0 + np.vectorize(math.erf)(d1 / math.sqrt(2.0)))
    discount = math.exp(-q * T)
    if kind == "put":
        deltas = discount * (nd1 - 1.0)
        mask = ks < S
        score = np.where(mask, np.abs(deltas + target), np.inf)
    else:
        deltas = discount * nd1
        mask = ks > S
        score = np.where(mask, np.abs(deltas - target), np.inf)
    idx = int(np.argmin(score))
    if not np.isfinite(score[idx]):
        return None
    return int(ks[idx])


class Day:
    __slots__ = ("date", "prev_date", "session_hours", "hours", "his", "los", "S0",
                 "daily_close", "hourly_close", "daily_high", "daily_low", "hourly_high",
                 "hourly_low", "sigma_base", "sma_prev", "vix_prev", "vix9_prev",
                 "expected_bars", "actual_bars")


def _by_date(series):
    return {x.date(): float(v) for x, v in series.items() if not pd.isna(v)}


def prepare_days(daily, vix, vix9d, rth, start, end, return_audit=False):
    d = daily.copy()
    d.index = d.index.tz_localize(None) if d.index.tz is not None else d.index
    d["_session_date"] = d.index.date
    d["SMA20"] = d["Close"].rolling(20).mean()
    rows = {x: g.iloc[-1] for x, g in d.groupby("_session_date")}
    vix_map = _by_date(vix)
    vix9_map = _by_date(vix9d)
    rth_by_date = {k: g for k, g in rth.groupby(rth.index.date)}
    available_end = min(max(rows), max(rth_by_date)) if rows and rth_by_date else None
    requested_end = pd.Timestamp(end).date()
    effective_end = min(requested_end, available_end) if available_end else requested_end
    if pd.Timestamp(start).date() <= effective_end:
        schedule = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=effective_end)
    else:
        schedule = pd.DataFrame()
    days = []
    audit = []

    ordered_dates = sorted(rows)
    prior_date = {ordered_dates[i]: ordered_dates[i - 1] for i in range(1, len(ordered_dates))}

    for session, sched in schedule.iterrows():
        dt = session.date()
        record = {"date": dt, "status": "excluded", "reason": "", "quality": "",
                  "expected_bars": 0, "actual_bars": 0, "close_gap": None,
                  "missing_high": None, "missing_low": None}
        daily_row = rows.get(dt)
        bars = rth_by_date.get(dt)
        if daily_row is None or bars is None:
            record["reason"] = "missing_daily_or_hourly"
            audit.append(record)
            continue
        market_open = sched["market_open"].tz_convert(NY_TZ)
        market_close = sched["market_close"].tz_convert(NY_TZ)
        bars = bars[(bars.index >= market_open) & (bars.index < market_close)]
        expected_idx = pd.date_range(market_open, market_close, freq="1h", inclusive="left")
        expected = len(expected_idx)
        record["expected_bars"] = expected
        record["actual_bars"] = len(bars)
        session_hours = (market_close - market_open).total_seconds() / 3600.0
        missing_half_day_fragment = False
        if session_hours < 6.0 and len(bars) == expected - 1 and expected > 1:
            # Only accept the known Yahoo half-day omission: the final 30-min
            # fragment (last hourly bar) is missing, but all earlier bars match
            # the NYSE schedule exactly. Any other single-bar gap remains excluded.
            missing_half_day_fragment = bars.index.equals(expected_idx[:-1])
        if len(bars) != expected and not missing_half_day_fragment:
            record["reason"] = "incomplete_session"
            audit.append(record)
            continue
        record["quality"] = (
            "daily_ohlc_guards_missing_half_day_fragment"
            if missing_half_day_fragment else "complete_hourly"
        )
        prev = prior_date.get(dt)
        if prev is None or prev >= dt:
            record["reason"] = "missing_prior_session"
            audit.append(record)
            continue
        v_prev = vix_map.get(prev)
        if v_prev is None:
            record["reason"] = "missing_prior_vix"
            audit.append(record)
            continue

        hourly_high = float(bars["High"].max())
        hourly_low = float(bars["Low"].min())
        hourly_close = float(bars["Close"].iloc[-1])
        daily_high = float(daily_row["High"])
        daily_low = float(daily_row["Low"])
        daily_close = float(daily_row["Close"])
        record["close_gap"] = round(abs(daily_close - hourly_close), 4)
        record["missing_high"] = round(max(daily_high - hourly_high, 0.0), 4)
        record["missing_low"] = round(max(hourly_low - daily_low, 0.0), 4)

        day = Day()
        day.date = dt
        day.prev_date = prev
        day.session_hours = session_hours
        day.hours = [max((market_close - ts).total_seconds() / 3600.0, 0.001) for ts in bars.index]
        day.his = [float(x) for x in bars["High"]]
        day.los = [float(x) for x in bars["Low"]]
        day.S0 = float(bars["Open"].iloc[0])
        day.daily_close = daily_close
        day.hourly_close = hourly_close
        day.daily_high = daily_high
        day.daily_low = daily_low
        day.hourly_high = hourly_high
        day.hourly_low = hourly_low
        day.sigma_base = float(v_prev) / 100.0
        day.vix_prev = float(v_prev)
        day.vix9_prev = vix9_map.get(prev)
        prev_sma = rows[prev]["SMA20"] if prev in rows else None
        day.sma_prev = None if prev_sma is None or pd.isna(prev_sma) else float(prev_sma)
        day.expected_bars = expected
        day.actual_bars = len(bars)
        days.append(day)
        record["status"] = "included"
        record["reason"] = ""
        audit.append(record)

    if return_audit:
        return days, pd.DataFrame(audit)
    return days


def _estimated_reg_t_margin(S, legs):
    margins = []
    for leg in legs:
        if leg["kind"] == "put":
            otm = max(S - leg["K"], 0.0)
        else:
            otm = max(leg["K"] - S, 0.0)
        base = max(0.20 * S - otm, 0.10 * S)
        margins.append(base * MULT + leg["credit"] * MULT)
    if not margins:
        return 0.0
    max_idx = int(np.argmax(margins))
    if len(legs) > 1:
        extra_credit = sum(leg["credit"] for i, leg in enumerate(legs) if i != max_idx) * MULT
    else:
        extra_credit = 0.0
    return max(margins) + extra_credit


def simulate(cfg, days):
    trades = []
    equity = cfg.account
    curve = []
    for day in days:
        if cfg.vix_max and day.vix_prev > cfg.vix_max:
            continue
        if cfg.vix_min and day.vix_prev < cfg.vix_min:
            continue
        if cfg.skip_bwd:
            if day.vix9_prev is None:
                if cfg.fail_closed_vix9d:
                    continue
            elif day.vix9_prev > day.vix_prev:
                continue
        sigma = day.sigma_base * cfg.iv_scale
        T0 = year_fraction(day.session_hours, cfg.clock)
        kinds = []
        if cfg.structure in ("strangle", "put"):
            kinds.append("put")
        if cfg.structure in ("strangle", "call"):
            kinds.append("call")
        legs = []
        for kind in kinds:
            if cfg.trend_filter and day.sma_prev is not None:
                if kind == "put" and day.S0 < day.sma_prev:
                    continue
                if kind == "call" and day.S0 > day.sma_prev:
                    continue
            k = strike_on_grid(day.S0, T0, sigma, kind, cfg.delta, cfg.r, cfg.q)
            if k is None:
                continue
            model_mid = bs_price(day.S0, k, T0, sigma, kind, cfg.r, cfg.q)
            credit = model_mid * cfg.credit_haircut - cfg.slippage
            if credit <= 0.01:
                continue
            legs.append({"kind": kind, "K": k, "credit": credit, "model_mid": model_mid,
                         "entry_delta": bs_delta(day.S0, k, T0, sigma, kind, cfg.r, cfg.q),
                         "open": True, "exit": None, "reason": None})
        if not legs:
            continue
        estimated_margin = _estimated_reg_t_margin(day.S0, legs) * cfg.contracts
        if estimated_margin > equity:
            continue

        equity_before = equity
        worst_day_mtm = 0.0
        for i in range(len(day.hours)):
            T = year_fraction(day.hours[i], cfg.clock)
            hi, lo = day.his[i], day.los[i]
            if i == 0:
                if day.daily_high > day.hourly_high + 0.01:
                    hi = max(hi, day.daily_high)
                if day.daily_low < day.hourly_low - 0.01:
                    lo = min(lo, day.daily_low)
            mtm = 0.0
            for leg in legs:
                kind, k, credit = leg["kind"], leg["K"], leg["credit"]
                if leg["open"]:
                    adverse_S = lo if kind == "put" else hi
                    adverse = bs_price(adverse_S, k, T, sigma * cfg.exit_iv_mult,
                                       kind, cfg.r, cfg.q)
                    threshold = cfg.stop_mult * credit
                    if adverse >= threshold:
                        leg["exit"] = max(adverse, threshold) + cfg.slippage
                        leg["reason"] = "stop"
                        leg["open"] = False
                    elif cfg.tp_mult > 0:
                        favorable_S = hi if kind == "put" else lo
                        favorable = bs_price(favorable_S, k, T, sigma * cfg.exit_iv_mult,
                                             kind, cfg.r, cfg.q)
                        if favorable <= cfg.tp_mult * credit:
                            leg["exit"] = cfg.tp_mult * credit + cfg.slippage
                            leg["reason"] = "tp"
                            leg["open"] = False
                mark = leg["exit"] if not leg["open"] else bs_price(
                    lo if kind == "put" else hi, k, T, sigma * cfg.exit_iv_mult, kind, cfg.r, cfg.q)
                mtm += ((credit - mark) * MULT - 2.0 * cfg.commission) * cfg.contracts
            worst_day_mtm = min(worst_day_mtm, mtm)

        pnl = 0.0
        gross = 0.0
        for leg in legs:
            if leg["open"]:
                if leg["kind"] == "put":
                    daily_intrinsic = max(leg["K"] - day.daily_close, 0.0)
                    hourly_intrinsic = max(leg["K"] - day.hourly_close, 0.0)
                else:
                    daily_intrinsic = max(day.daily_close - leg["K"], 0.0)
                    hourly_intrinsic = max(day.hourly_close - leg["K"], 0.0)
                leg["exit"] = max(daily_intrinsic, hourly_intrinsic)
                leg["reason"] = "cash_settle_proxy"
            lp = ((leg["credit"] - leg["exit"]) * MULT - 2.0 * cfg.commission) * cfg.contracts
            leg["pnl"] = lp
            pnl += lp
            gross += leg["credit"] * MULT * cfg.contracts
        equity += pnl
        curve.append({"date": day.date, "equity": equity, "daily_pnl": pnl,
                      "intraday_equity_low": equity_before + worst_day_mtm})
        rec = {"date": day.date, "prior_data_date": day.prev_date,
               "structure": cfg.structure, "clock": cfg.clock, "S_open": round(day.S0, 2),
               "daily_close": round(day.daily_close, 2), "hourly_close": round(day.hourly_close, 2),
               "iv": round(sigma, 4), "exit_iv_mult": cfg.exit_iv_mult,
               "credit_haircut": cfg.credit_haircut,
               "gross_credit": round(gross, 2), "pnl": round(pnl, 2),
               "vix_prev": round(day.vix_prev, 2),
               "vix9_prev": None if day.vix9_prev is None else round(day.vix9_prev, 2),
               "estimated_margin": round(estimated_margin, 2)}
        for leg in legs:
            side = leg["kind"]
            rec[f"{side}_strike"] = leg["K"]
            rec[f"{side}_entry_delta"] = round(leg["entry_delta"], 4)
            rec[f"{side}_model_mid"] = round(leg["model_mid"], 4)
            rec[f"{side}_credit"] = round(leg["credit"], 4)
            rec[f"{side}_exit"] = round(leg["exit"], 4)
            rec[f"{side}_reason"] = leg["reason"]
            rec[f"{side}_pnl"] = round(leg["pnl"], 2)
        trades.append(rec)
    return pd.DataFrame(trades), pd.DataFrame(curve)


def summarize(trades, curve, cfg):
    if len(trades) == 0 or len(curve) == 0:
        return {"trades": 0, "pnl": 0.0, "ret_pct": 0.0, "wr": 0.0, "pf": 0.0,
                "maxdd": 0.0, "pnl_over_dd": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    pnl = trades["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    running_peak = cfg.account
    maxdd = 0.0
    for row in curve.itertuples(index=False):
        maxdd = min(maxdd, float(row.intraday_equity_low) - running_peak)
        running_peak = max(running_peak, float(row.equity))
    gross_w = float(wins.sum())
    gross_l = abs(float(losses.sum()))
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    net = float(pnl.sum())
    return {"trades": len(trades), "pnl": round(net, 2),
            "ret_pct": round(100.0 * net / cfg.account, 2),
            "wr": round(100.0 * len(wins) / len(trades), 1),
            "pf": round(pf, 2), "maxdd": round(maxdd, 2),
            "pnl_over_dd": round(net / abs(maxdd), 2) if maxdd < 0 else float("inf"),
            "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0}


def monthly_breakdown(trades, cfg):
    if len(trades) == 0:
        return pd.DataFrame()
    table = trades.copy()
    table["month"] = pd.to_datetime(table["date"]).dt.to_period("M").astype(str)
    rows = []
    for month, group in table.groupby("month"):
        wins = int((group["pnl"] > 0).sum())
        pnl = float(group["pnl"].sum())
        rows.append({"month": month, "trades": len(group), "wins": wins,
                     "losses": len(group) - wins,
                     "win_rate_pct": round(100.0 * wins / len(group), 1),
                     "gross_premium": round(float(group["gross_credit"].sum()), 2),
                     "net_pnl": round(pnl, 2),
                     "ret_on_initial_account_pct": round(100.0 * pnl / cfg.account, 2)})
    return pd.DataFrame(rows)
