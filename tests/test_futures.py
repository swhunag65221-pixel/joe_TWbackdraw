"""台指期執行版的測試。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tw_backdraw.config import POST_CONFIG, DEFAULT_CONFIG
from tw_backdraw.futures import (
    FuturesCost, FuturesEntry, build_continuous, futures_leverage,
    missing_rolls, vehicle_series,
)
from tw_backdraw.levels import build_levels

FREE = FuturesCost(tax_rate=0.0, commission_per_lot=0.0)


def days(n: int) -> list[date]:
    return [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]


class TestContinuous(unittest.TestCase):
    def test_no_roll_is_passthrough(self):
        d = days(3)
        out = build_continuous(d, [100.0, 110.0, 121.0], ['A'] * 3, {})
        self.assertAlmostEqual(out[-1] / out[0], 1.21)

    def test_roll_gap_is_removed(self):
        """換倉價差不該變成損益。

        近月 100 →（換倉日次月報 102）→ 次日近月 102。
        真實報酬是 0%，若不還原會被記成 +2%。
        """
        d = days(3)
        front = [100.0, 100.0, 102.0]
        contract = ['202001', '202001', '202002']
        out = build_continuous(d, front, contract, {d[1]: 102.0})
        self.assertAlmostEqual(out[2] / out[1], 1.0, places=9)

    def test_without_spread_data_falls_back_to_raw_jump(self):
        d = days(3)
        front = [100.0, 100.0, 102.0]
        contract = ['202001', '202001', '202002']
        out = build_continuous(d, front, contract, {})       # 沒給價差
        self.assertAlmostEqual(out[2] / out[1], 1.02, places=9)
        self.assertEqual(missing_rolls(d, contract, {}), [d[1]])

    def test_backwardation_roll_is_a_gain_not_a_loss(self):
        """逆價差（次月比近月低）換倉後，續抱應該賺到收斂，而不是被記成虧損。"""
        d = days(3)
        front = [100.0, 100.0, 99.0]          # 次日近月（＝原次月）99
        contract = ['202001', '202001', '202002']
        out = build_continuous(d, front, contract, {d[1]: 99.0})
        self.assertAlmostEqual(out[2] / out[1], 1.0, places=9)


class TestLeverage(unittest.TestCase):
    def setUp(self):
        self.lv = build_levels(47742.0, 39933.0, DEFAULT_CONFIG.levels)

    def test_closer_stop_gives_more_leverage(self):
        near = futures_leverage(44000.0, self.lv, DEFAULT_CONFIG)
        far = futures_leverage(47000.0, self.lv, DEFAULT_CONFIG)
        self.assertGreater(near, far)

    def test_capped_at_max(self):
        # 進場價幾乎貼著警戒線 → 距離趨近 0 → 應被上限擋住
        entry = self.lv.warn_line * 1.0001
        self.assertAlmostEqual(futures_leverage(entry, self.lv, DEFAULT_CONFIG), 5.0)
        self.assertAlmostEqual(
            futures_leverage(entry, self.lv, DEFAULT_CONFIG, max_leverage=3.0), 3.0)

    def test_matches_risk_budget_when_not_capped(self):
        entry = 46200.0
        lev = futures_leverage(entry, self.lv, DEFAULT_CONFIG)
        dist = (entry - self.lv.warn_line) / entry      # warn_derisk=1.0 → 只算到警戒線
        self.assertAlmostEqual(lev * dist, DEFAULT_CONFIG.sizing.risk_per_trade, places=9)

    def test_min_stop_distance_floor_is_not_applied(self):
        """期貨版刻意不套 5% 下限；若套了，槓桿會恆為 1.6 倍。"""
        entry = 44929.0                                  # 實際距離約 2.4%
        lev = futures_leverage(entry, self.lv, DEFAULT_CONFIG)
        floored = DEFAULT_CONFIG.sizing.risk_per_trade / DEFAULT_CONFIG.sizing.min_stop_distance
        self.assertGreater(lev, floored)


class TestVehicle(unittest.TestCase):
    def test_fixed_contracts_are_linear_not_compounded(self):
        d = days(3)
        f = [100.0, 105.0, 110.0]
        nav, _ = vehicle_series(d, f, [FuturesEntry(0, 2, 3.0, 10000.0, 0.0267)],
                                FREE, [10000.0] * 3)
        self.assertAlmostEqual(nav[1], 1.15)      # 1 + 3×5%
        self.assertAlmostEqual(nav[2], 1.30)      # 1 + 3×10%，非 1.15×1.0476
        self.assertNotAlmostEqual(nav[2], 1.15 * (1 + 3 * (110 / 105 - 1)), places=4)

    def test_flat_while_out_of_market(self):
        d = days(5)
        f = [100.0, 110.0, 120.0, 130.0, 140.0]
        nav, _ = vehicle_series(d, f, [FuturesEntry(1, 2, 2.0, 10000.0, 0.04)],
                                FREE, [10000.0] * 5)
        self.assertAlmostEqual(nav[0], 1.0)
        self.assertAlmostEqual(nav[2], nav[3])    # 出場後持平
        self.assertAlmostEqual(nav[3], nav[4])

    def test_costs_scale_with_leverage(self):
        d = days(2)
        f = [100.0, 100.0]
        cost = FuturesCost(tax_rate=0.001, commission_per_lot=0.0)
        e = [FuturesEntry(0, 1, 4.0, 10000.0, 0.02)]
        nav, det = vehicle_series(d, f, e, cost, [10000.0] * 2)
        # 期貨沒動，只剩兩邊成本：4 × 0.1% × 2 ≈ -0.8%
        self.assertAlmostEqual(det[0].ret, (1 - 4 * 0.001) ** 2 - 1, places=9)

    def test_open_trade_is_reported(self):
        d = days(3)
        nav, det = vehicle_series(d, [100.0, 105.0, 110.0],
                                  [FuturesEntry(0, None, 2.0, 10000.0, 0.04)],
                                  FREE, [10000.0] * 3)
        self.assertEqual(len(det), 1)
        self.assertIsNone(det[0].exit_date)
        self.assertAlmostEqual(det[0].futures_return, 0.10)
        self.assertAlmostEqual(det[0].ret, 0.20)


class TestSameDayExecution(unittest.TestCase):
    """訊號日當天的期貨收盤成交 vs 隔日成交。"""

    class _Fill:
        def __init__(self, d):
            self.d = d

    class _Setup:
        trigger_close = 46000.0

    class _Trade:
        """最小的假交易物件；價位用真的 build_levels 產生。"""

        def __init__(self, fills):
            self.fills = fills
            self.levels = build_levels(47742.0, 39933.0, DEFAULT_CONFIG.levels)
            self.setup = TestSameDayExecution._Setup()

    class _Bar:
        def __init__(self, d):
            self.d = d

    def _bars(self, n):
        return [self._Bar(d) for d in days(n)]

    def test_same_day_shifts_execution_one_bar_earlier(self):
        from tw_backdraw.futures import entries_from_trades
        bars = self._bars(6)
        t = self._Trade([self._Fill(bars[2].d), self._Fill(bars[5].d)])
        same = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=True)[0]
        next_day = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=False)[0]
        self.assertEqual((same.entry_i, same.exit_i), (1, 4))
        self.assertEqual((next_day.entry_i, next_day.exit_i), (2, 5))

    def test_leverage_is_unaffected_by_execution_timing(self):
        from tw_backdraw.futures import entries_from_trades
        bars = self._bars(6)
        t = self._Trade([self._Fill(bars[2].d), self._Fill(bars[5].d)])
        a = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=True)[0]
        b = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=False)[0]
        self.assertAlmostEqual(a.leverage, b.leverage)

    def test_open_trade_keeps_none_exit(self):
        from tw_backdraw.futures import entries_from_trades
        bars = self._bars(6)
        t = self._Trade([self._Fill(bars[3].d)])
        e = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=True)[0]
        self.assertEqual(e.entry_i, 2)
        self.assertIsNone(e.exit_i)

    def test_degenerate_same_bar_trade_is_dropped(self):
        """成交日相鄰時，往前挪會讓進出場落在同一根 K —— 該筆不成立。"""
        from tw_backdraw.futures import entries_from_trades
        bars = self._bars(6)
        t = self._Trade([self._Fill(bars[2].d), self._Fill(bars[3].d)])
        same = entries_from_trades([t], bars, DEFAULT_CONFIG, same_day=True)
        self.assertEqual(len(same), 1)
        self.assertEqual((same[0].entry_i, same[0].exit_i), (1, 2))


class TestTradeDetails(unittest.TestCase):
    """逐筆明細的 MFE / MAE / 期間最大回撤。"""

    def _run(self, nav_path, exit_i):
        from tw_backdraw.futures import FuturesTrade, trade_details
        ds = days(len(nav_path))
        e = FuturesEntry(entry_i=0, exit_i=exit_i, leverage=2.0,
                         entry_index=100.0, stop_distance=0.05)
        t = FuturesTrade(entry_date=ds[0], exit_date=ds[exit_i or -1],
                         entry_index=100.0, entry_futures=100.0,
                         exit_futures=100.0, leverage=2.0, stop_distance=0.05,
                         futures_return=0.0, ret=nav_path[exit_i or -1] - 1.0)
        return trade_details(ds, nav_path, [e], [t])[0]

    def test_extremes_are_measured_from_entry_and_running_peak(self):
        # 1.0 → 1.5 → 0.9 → 1.2：MFE +50%、MAE −10%、自高點回撤 −40%
        d = self._run([1.0, 1.5, 0.9, 1.2], 3)
        self.assertAlmostEqual(d.mfe, 0.5)
        self.assertAlmostEqual(d.mae, -0.1)
        self.assertAlmostEqual(d.max_drawdown, -0.4)
        self.assertEqual(d.bars_held, 3)

    def test_monotonic_rise_has_no_drawdown(self):
        d = self._run([1.0, 1.1, 1.3], 2)
        self.assertAlmostEqual(d.max_drawdown, 0.0)
        self.assertAlmostEqual(d.mae, 0.0)

    def test_open_trade_runs_to_last_bar(self):
        d = self._run([1.0, 1.4, 0.8], None)
        self.assertEqual(d.bars_held, 2)
        self.assertAlmostEqual(d.mfe, 0.4)
        self.assertAlmostEqual(d.mae, -0.2)

    def test_extremes_ignore_bars_outside_the_holding_window(self):
        """出場之後的淨值不能算進這筆的極值。"""
        d = self._run([1.0, 1.2, 1.1, 5.0, 0.1], 2)
        self.assertAlmostEqual(d.mfe, 0.2)
        self.assertAlmostEqual(d.mae, 0.0)


if __name__ == "__main__":
    unittest.main()
