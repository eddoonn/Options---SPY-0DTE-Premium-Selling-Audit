# SPY 0DTE Premium-Selling Audit

This repository translates the qualitative rules in
`../options trading guide - mobile.pdf` into an executable SPY model.

## Verdict

No reliable tradable edge has been established.

- This is a synthetic Black-Scholes/VIX scenario, not a historical
  options-chain backtest.
- The guide-literal 2026-to-date translation loses money under both tested
  option-time conventions.
- A development-selected candidate is profitable under the trading-time model,
  but becomes strongly negative under calendar time and combined adverse
  pricing assumptions.
- All available data through August 26, 2026 has been inspected. There is no
  untouched out-of-sample period in this repository.
- The former `+$27,144` / `+108.6%` result is invalid. It used an incorrect put
  delta and selected the final winner using 2026 performance.

The model can test code behavior and sensitivity. It cannot support live
trading without historical option quotes or prospectively recorded quotes and
fills.

## Guide Translation

The guide specifies selling OTM SPX/SPY options with mandatory stops, but does
not define delta, DTE, stop size, profit target, or entry time. The default
translation is therefore an assumption, not a rule copied from the guide:

- Enter one SPY 0DTE 16-delta strangle at the regular-session open.
- Estimate strikes and prices with Black-Scholes and the prior session's VIX
  close.
- Stop each leg at 2x entry credit and take profit at 50% of entry credit.
- Use $0.03 slippage and $0.65 commission per contract per fill.
- Use a $25,000 initial account and a conservative Reg-T margin estimate.
- Settle remaining legs with a SPY close/intrinsic-value proxy.

## Integrity Controls

- Put delta is `exp(-qT) * (N(d1) - 1)`, not `-N(d1)`.
- VIX and VIX9D are loaded from official CBOE history:
  - `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`
  - `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv`
- Signals use only the strictly prior SPY session's SMA, VIX, and VIX9D values.
- Missing VIX9D fails the backwardation filter closed by default.
- NYSE schedules determine session length and prevent future dates from being
  reported as missing observations.
- Stop checks use each hourly bar's adverse extreme before its favorable
  extreme. Missing daily extremes are applied at the first bar, which is
  intentionally pessimistic.
- A known missing final 30-minute Yahoo bar on scheduled half-days is guarded
  only when the missing bar is exactly the final hourly fragment; any other
  intraday gap remains excluded, with daily high/low applied pessimistically
  to the first bar.
- Config values are validated (delta, stop, slippage, haircut, IV multiplier,
  clock, etc.); invalid inputs fail before simulation.
- Every run has an immutable directory containing trades, session exclusions,
  monthly results, configuration, source ranges, source URLs, code hashes, and
  cached-input hashes. Optimization and stress runs also record source ranges.

## Corrected 2026-To-Date Results

Period evaluated: January 2 through August 26, 2026. Of 163 elapsed NYSE
sessions, 161 have usable intraday data. January 30 has 1 of 7 expected bars and
February 2 has 3 of 7, so both are explicitly excluded.

All returns below are net P&L divided by the initial $25,000, without
compounding.

| Scenario | Trades | Net P&L | Return | PF | Max drawdown |
|---|---:|---:|---:|---:|---:|
| Guide default, trading-time clock | 161 | -$4,359.17 | -17.44% | 0.50 | -$4,981.88 |
| Guide default, calendar-time clock | 88 | -$10,874.91 | -43.50% | 0.00 | -$10,874.93 |
| Former 20-delta strangle candidate, trading clock | 137 | +$14,606.24 | +58.42% | 4.04 | -$1,412.09 |
| Former candidate, calendar clock | 92 | -$10,682.22 | -42.73% | 0.07 | -$10,763.77 |
| Former candidate, adverse credit/exit-IV assumptions | 30 | -$13,142.43 | -52.57% | 0.00 | -$13,142.41 |

The large sign reversal is the central result. The apparent profit depends on a
specific synthetic time-decay and option-marking convention.

Audited artifacts:

- `results/runs/final_baseline_trading_20260826/`
- `results/runs/final_baseline_calendar_20260826/`
- `results/runs/final_former_candidate_trading_20260826/`
- `results/runs/final_former_candidate_calendar_20260826/`
- `results/runs/final_former_candidate_adverse_20260826/`

## Development Search

`optimize.py` evaluates 972 configurations on 2024 and 2025 development data.
It requires at least 190 trades, positive P&L, PF at least 1.30, and drawdown no
worse than $3,500 in each year. It ranks eligible configurations by the lower
annual P&L/max-drawdown ratio, then worst-year P&L, then total P&L. There is no
fallback when nothing qualifies.

The corrected run had 49 eligible configurations and selected:

```text
structure=call, delta=0.20, stop=3x, take_profit=off,
prior_vix_max=25, backwardation_filter=off, trend_filter=off
```

| Period | Status | Trades | Net P&L | Return | PF | Max drawdown |
|---|---|---:|---:|---:|---:|---:|
| 2024 | Development | 248 | +$8,320.92 | +33.28% | 3.25 | -$636.22 |
| 2025 | Development | 230 | +$13,430.33 | +53.72% | 6.47 | -$484.25 |
| 2026 to Aug 26 | Previously inspected diagnostic | 147 | +$9,355.20 | +37.42% | 5.13 | -$563.23 |

The 2026 row was not used by the rewritten optimizer, but it was inspected by
the earlier workflow and cannot be rehabilitated as an untouched test.

Sensitivity of the same locked configuration:

| Variant | 2024 | 2025 | Reused 2026 |
|---|---:|---:|---:|
| Baseline trading clock | +$8,321 | +$13,430 | +$9,355 |
| Calendar clock | -$10,588 | -$10,601 | -$7,767 |
| $0.20 slippage | -$1,010 | +$5,606 | +$4,461 |
| 15% entry-credit haircut | +$4,807 | +$9,464 | +$5,711 |
| 1.25x exit IV | +$2,983 | +$4,756 | +$3,035 |
| Combined adverse assumptions | -$15,465 | -$14,312 | -$12,226 |

Artifacts (latest, matching current code hash):

- `results/optimization_runs/corrected_development_v3_20260826/`
- `results/stress_runs/corrected_selected_v3_20260826/`

Prior runs `corrected_development_v2_20260826` and `corrected_selected_v2_20260826`
are retained as audit trail and differ only by code-hash and manifest
`source_ranges` enrichment; P&L figures are identical because the selected
configuration has no take-profit and the half-day fix did not change its
included sessions.

## Limitations

- There are no historical option bids, asks, trades, skews, smiles, Greeks, or
  NBBO timestamps. VIX is not the IV of the selected SPY contract.
- The two Black-Scholes clocks keep the same VIX-derived IV but use different
  year fractions (252*6.5 vs 365*24), so a 6.5-hour 0DTE has ~5.3x more variance
  under the trading clock than under the calendar clock. The "calendar" variant
  is therefore a different volatility model, not just a time-decay convention;
  the sign reversal shows model fragility, not a clock nuance, and alone
  invalidates an edge claim. TP checks also use the stressed exit IV, so take
  profits cannot trigger below the stressed mark.
- Hourly underlying OHLC cannot reconstruct option stop order, spread changes,
  volatility jumps, or fills inside the bar.
- SPY options are American-style. Early assignment, dividends, pin risk, and
  broker liquidation are not simulated.
- The margin check is an estimate (Reg-T-style: max single-leg requirement
  plus the other leg's premium for strangles), not a broker-specific
  historical margin series. Falling equity can reduce later trade count.
- Two 2026 sessions have material Yahoo intraday gaps and are excluded.
- The grid is multiple-comparison research on already observed data. Its winner
  is a candidate for prospective collection, not validation.
- A real test requires locked rules followed by new option-chain quotes and
  executable fills collected without retuning. Any outcome-driven rule change
  restarts that prospective period.

## Usage

```bash
pip install yfinance pandas numpy matplotlib pandas-market-calendars
python -m unittest -v test_engine.py
python backtest.py --refresh
python backtest.py --delta 0.20 --stop 3 --tp 0 --skip-bwd
python optimize.py
python optimize.py --evaluate-reused-2026
python stress.py --config results/optimization_runs/corrected_development_v2_20260826/selected_config.json --include-reused-2026
```

`optimize.py` always loads the cached inputs (which contain 2026 history for
continuity) but never evaluates or ranks on 2026 unless `--evaluate-reused-2026`
is explicitly provided. `stress.py` never ranks configurations.

## Live signal (Fix1 + Fix2 directional, locked)

`daily_signal.py` posts one synthetic indication per NYSE session to Discord
via `.github/workflows/signal.yml` (09:35 ET post-open, `DISCORD_WEBHOOK_URL`
secret — never committed).

- Fix1: strike re-solved at the live 09:30 SPY open (`S0_mode=live_open_0930`)
  with live VIX; pre-open fallback uses the prior close (`proxy_prior_close`).
- Fix2 directional: short-call skipped when the overnight up-gap
  `(S_open - S0_proxy)` exceeds +0.5% or +4pts — proxy stale, NO TRADE.
- Each signal carries two tickets: SPY and XSP (mini S&P 500, XSP = SPX/10,
  European cash-settled). Same 20Δ/3x rules, same VIX, per-ticker gap skip.
  Beginner-friendly format: ticker, side, strike, expiry date, price, stop —
  no jargon.
- Backtest engine is unchanged (strictly prior VIX/SMA). The live path uses
  data available at signal time only; `S0_mode`/`vix_mode`/`gap_pts` are in
  every payload and `signal.json` artifact.

```bash
python daily_signal.py --refresh --dry-run --json-out signal.json
python daily_signal.py --refresh  # posts via DISCORD_WEBHOOK_URL
```

## Files

- `strategy.py`: Black-Scholes pricing and strategy configuration.
- `engine.py`: source loading, session audit, simulation, and summaries.
- `daily_signal.py`: locked live-signal (Fix1 open re-solve + Fix2 gap skip).
- `backtest.py`: one-config audited run and immutable artifacts.
- `optimize.py`: development-only deterministic grid selection.
- `stress.py`: sensitivity analysis of one locked configuration.
- `test_engine.py`: integrity tests for delta, prior-only inputs, missing-data
  behavior, half-days, and drawdown.
- `results/LEGACY_RESULTS_INVALID.md`: quarantine notice for invalid old output.

This is research software, not financial advice.
