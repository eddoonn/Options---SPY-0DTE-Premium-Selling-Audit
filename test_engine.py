import math
import unittest
from datetime import date

import pandas as pd

from engine import (CBOE_VIX9D_URL, CBOE_VIX_URL, Day, load_data, prepare_days, simulate,
                    strike_on_grid, summarize, year_fraction)
from strategy import Config, bs_delta


class EngineIntegrityTests(unittest.TestCase):
    def test_put_and_call_strikes_match_requested_delta(self):
        S = 685.71
        sigma = 0.15
        for clock in ("trading", "calendar"):
            T = year_fraction(6.5, clock)
            for kind in ("put", "call"):
                K = strike_on_grid(S, T, sigma, kind, 0.20)
                delta = bs_delta(S, K, T, sigma, kind)
                self.assertLess(abs(abs(delta) - 0.20), 0.07)
                self.assertLess(K, S) if kind == "put" else self.assertGreater(K, S)

    def test_delta_distance_is_monotonic(self):
        S = 685.71
        sigma = 0.15
        T = year_fraction(6.5, "trading")
        put10 = strike_on_grid(S, T, sigma, "put", 0.10)
        put20 = strike_on_grid(S, T, sigma, "put", 0.20)
        call10 = strike_on_grid(S, T, sigma, "call", 0.10)
        call20 = strike_on_grid(S, T, sigma, "call", 0.20)
        self.assertLess(put10, put20)
        self.assertGreater(call10, call20)

    def test_backwardation_filter_fails_closed_without_vix9d(self):
        day = Day()
        day.date = date(2026, 1, 2)
        day.prev_date = date(2025, 12, 31)
        day.session_hours = 6.5
        day.hours = [6.5]
        day.his = [100.0]
        day.los = [100.0]
        day.S0 = 100.0
        day.daily_close = 100.0
        day.hourly_close = 100.0
        day.daily_high = 100.0
        day.daily_low = 100.0
        day.hourly_high = 100.0
        day.hourly_low = 100.0
        day.sigma_base = 0.20
        day.sma_prev = None
        day.vix_prev = 20.0
        day.vix9_prev = None
        day.expected_bars = 1
        day.actual_bars = 1
        closed = Config(structure="call", skip_bwd=True, fail_closed_vix9d=True)
        open_cfg = Config(structure="call", skip_bwd=True, fail_closed_vix9d=False)
        self.assertEqual(len(simulate(closed, [day])[0]), 0)
        self.assertEqual(len(simulate(open_cfg, [day])[0]), 1)

    def test_drawdown_includes_starting_equity_and_intraday_low(self):
        cfg = Config(account=25000.0)
        trades = pd.DataFrame([{"pnl": -100.0}])
        curve = pd.DataFrame([{"date": date(2026, 1, 2), "equity": 24900.0,
                               "daily_pnl": -100.0, "intraday_equity_low": 24800.0}])
        self.assertEqual(summarize(trades, curve, cfg)["maxdd"], -200.0)

    def test_cached_inputs_are_prior_only_and_sessions_complete(self):
        daily, vix, vix9d, rth = load_data()
        days = prepare_days(daily, vix, vix9d, rth, "2026-01-01", "2026-02-28")
        self.assertGreater(len(days), 20)
        self.assertEqual(vix.attrs.get("source_url"), CBOE_VIX_URL)
        self.assertEqual(vix9d.attrs.get("source_url"), CBOE_VIX9D_URL)
        self.assertGreater(len(vix9d), 500)
        for day in days:
            self.assertLess(day.prev_date, day.date)
            self.assertEqual(day.actual_bars, day.expected_bars)
            self.assertIsNotNone(day.vix9_prev)

    def test_half_day_with_only_missing_close_fragment_is_not_dropped(self):
        daily, vix, vix9d, rth = load_data()
        _, audit = prepare_days(
            daily, vix, vix9d, rth, "2025-11-28", "2025-11-28", return_audit=True
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit.iloc[0]["status"], "included")
        self.assertIn(
            audit.iloc[0]["quality"],
            ("complete_hourly", "daily_ohlc_guards_missing_half_day_fragment"),
        )


if __name__ == "__main__":
    unittest.main()
