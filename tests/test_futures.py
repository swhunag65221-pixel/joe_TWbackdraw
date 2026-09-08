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
        # 進場價幾乎貼著停損線 → 距離趨近 0 → 應被上限擋住
        entry = self.lv.stop_line * 1.0001
        self.assertAlmostEqual(futures_leverage(entry, self.lv, DEFAULT_CONFIG), 5.0)
        self.assertAlmostEqual(
            futures_leverage(entry, self.lv, DEFAULT_CONFIG, max_leverage=3.0), 3.0)

    def test_matches_risk_budget_when_not_capped(self):
        entry = 46200.0
        lev = futures_leverage(entry, self.lv, DEFAULT_CONFIG)
        # warn_derisk=1.0 → 只算到停損線（警戒線與主防線緩衝取低者）
        dist = (entry - self.lv.stop_line) / entry
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


class TestStopLine(unittest.TestCase):
    """真正會觸發出場的價位 = min(警戒線, 主防線的假跌破緩衝)。"""

    def test_default_config_stop_is_the_buffer_not_the_warn_line(self):
        from tw_backdraw.levels import build_levels
        lv = build_levels(9569.0, 8513.0, DEFAULT_CONFIG.levels)
        # 預設警戒線 = 主防線（皆為 50% 回補位），緩衝壓低 0.5%
        self.assertAlmostEqual(lv.warn_line, lv.half_line)
        self.assertAlmostEqual(lv.stop_line, lv.half_line * 0.995)
        self.assertLess(lv.stop_line, lv.warn_line)

    def test_low_warn_line_makes_the_warn_line_the_stop(self):
        from tw_backdraw.levels import build_levels
        lv = build_levels(9569.0, 8513.0, POST_CONFIG.levels)   # 警戒線 38.2%
        self.assertLess(lv.warn_line, lv.half_line_with_buffer)
        self.assertAlmostEqual(lv.stop_line, lv.warn_line)

    def test_zone_turns_warning_exactly_at_the_stop_line(self):
        from tw_backdraw.levels import build_levels
        lv = build_levels(9569.0, 8513.0, DEFAULT_CONFIG.levels)
        self.assertNotEqual(lv.zone(lv.stop_line + 1e-6), "warning")
        self.assertEqual(lv.zone(lv.stop_line - 1e-6), "warning")


class TestTrendFilterAndFixedLeverage(unittest.TestCase):
    """逆勢濾網與固定槓桿（docs/strategy.md §18）。"""

    def _bars(self, closes):
        from tw_backdraw.bars import Bar
        return [Bar(d=date(2020, 1, 1) + timedelta(days=i), open=c, high=c,
                    low=c, close=c) for i, c in enumerate(closes)]

    def _entries(self, idxs):
        return [FuturesEntry(entry_i=i, exit_i=i + 1, leverage=2.0,
                             entry_index=100.0, stop_distance=0.05) for i in idxs]

    def test_keeps_only_bars_below_the_moving_average(self):
        from tw_backdraw.futures import trend_filter
        closes = [100.0] * 5 + [90.0, 110.0]        # MA3 之後：第 5 根低、第 6 根高
        bars = self._bars(closes)
        kept = trend_filter(self._entries([5, 6]), bars, ma_period=3, below=True)
        self.assertEqual([e.entry_i for e in kept], [5])

    def test_above_mode_is_the_mirror_image(self):
        from tw_backdraw.futures import trend_filter
        bars = self._bars([100.0] * 5 + [90.0, 110.0])
        kept = trend_filter(self._entries([5, 6]), bars, ma_period=3, below=False)
        self.assertEqual([e.entry_i for e in kept], [6])

    def test_drops_entries_where_the_average_is_undefined(self):
        """均線暖身期一律排除 —— 不能用不存在的資料做判斷。"""
        from tw_backdraw.futures import trend_filter
        bars = self._bars([100.0] * 5)
        self.assertEqual(trend_filter(self._entries([0, 1]), bars, ma_period=3), [])

    def test_fixed_leverage_overrides_every_entry(self):
        from tw_backdraw.futures import fixed_leverage
        ents = [FuturesEntry(0, 1, 1.4, 100.0, 0.05),
                FuturesEntry(2, 3, 4.6, 200.0, 0.02)]
        out = fixed_leverage(ents, 3.0)
        self.assertEqual([e.leverage for e in out], [3.0, 3.0])
        # 其餘欄位不動
        self.assertEqual([e.entry_i for e in out], [0, 2])
        self.assertEqual([e.stop_distance for e in out], [0.05, 0.02])

    def test_fixed_leverage_makes_equity_linear_in_the_futures_move(self):
        from tw_backdraw.futures import fixed_leverage, vehicle_series
        ds = days(3)
        fut = [100.0, 100.0, 90.0]              # 期貨 −10%
        ent = fixed_leverage([FuturesEntry(0, 2, 1.0, 100.0, 0.05)], 3.0)
        nav, det = vehicle_series(ds, fut, ent, FREE, [100.0] * 3)
        self.assertAlmostEqual(det[0].ret, -0.30, places=9)


class TestCoreOverlay(unittest.TestCase):
    """空手期的核心部位 —— 時序錯了就是前視偏誤（docs/strategy.md §18）。"""

    def _run(self, fut, ma, core=1.0, entries=(), idx=None):
        from tw_backdraw.futures import core_overlay
        ds = days(len(fut))
        idx = idx if idx is not None else [105.0] * len(fut)
        return core_overlay(ds, fut, list(entries), FREE, idx, core, ma)

    def test_position_is_established_at_close_and_earns_from_the_next_bar(self):
        """第 0 天收盤才建倉，所以第 0→1 天的漲幅不算 —— 用當天判斷賺當天是前視。"""
        nav, _, _ = self._run([100.0, 110.0, 121.0], [100.0] * 3)
        self.assertAlmostEqual(nav[0], 1.0)
        self.assertAlmostEqual(nav[1], 1.10)      # D0 收盤建倉 → 賺 D1
        self.assertAlmostEqual(nav[2], 1.21)

    def test_no_position_while_below_the_average(self):
        nav, _, ex = self._run([100.0, 200.0, 400.0], [1000.0] * 3)
        self.assertEqual(nav, [1.0, 1.0, 1.0])
        self.assertEqual(ex, 0.0)

    def test_undefined_average_means_flat(self):
        nav, _, _ = self._run([100.0, 110.0], [None, None])
        self.assertEqual(nav, [1.0, 1.0])

    def test_core_is_daily_rebalanced_constant_leverage(self):
        """0.5x 每日再平衡：兩天各 +10% → 1.05² 而非 1 + 0.5×21%。"""
        nav, _, _ = self._run([100.0, 110.0, 121.0], [100.0] * 3, core=0.5)
        self.assertAlmostEqual(nav[2], 1.05 ** 2)

    def test_strategy_position_replaces_the_core(self):
        """有訊號時先平掉核心，改抱策略部位；核心不算成交易筆數。"""
        from tw_backdraw.futures import FuturesEntry
        e = FuturesEntry(entry_i=1, exit_i=3, leverage=2.0,
                         entry_index=105.0, stop_distance=0.05)
        fut = [100.0, 100.0, 110.0, 121.0]
        nav, det, _ = self._run(fut, [100.0] * 4, core=1.0, entries=[e])
        self.assertEqual(len(det), 1)
        # 進場時權益 1.0（D0→D1 期貨沒動），之後 2 倍吃 21%
        self.assertAlmostEqual(det[0].ret, 2.0 * 0.21, places=9)


if __name__ == "__main__":
    unittest.main()


class TestHybridEntriesAndStepDown(unittest.TestCase):
    """§20 混合注碼與 §22 step-down（最終套件）。"""

    def _bars(self, closes):
        from tw_backdraw.bars import Bar
        return [Bar(d=date(2020, 1, 1) + timedelta(days=i), open=c, high=c,
                    low=c, close=c) for i, c in enumerate(closes)]

    def test_below_average_gets_fixed_leverage_above_gets_half_budget(self):
        from tw_backdraw.futures import hybrid_entries
        bars = self._bars([100.0] * 5 + [90.0, 110.0])   # MA3 後：第 5 根低於、第 6 根高於
        ents = [FuturesEntry(5, 6, 4.0, 90.0, 0.02), FuturesEntry(6, 7, 4.0, 110.0, 0.02)]
        out = hybrid_entries(ents, bars, ma_period=3, below_leverage=3.0,
                             above_risk_scale=0.5)
        self.assertEqual([e.leverage for e in out], [3.0, 2.0])
        self.assertEqual([e.entry_i for e in out], [5, 6])           # 不丟訊號

    def test_undefined_average_is_treated_as_above(self):
        """均線暖身期沒有濾網資訊 → 保守地維持風險式（砍半），不給 3x。"""
        from tw_backdraw.futures import hybrid_entries
        bars = self._bars([100.0] * 3)
        out = hybrid_entries([FuturesEntry(0, 2, 4.0, 100.0, 0.02)], bars, ma_period=3)
        self.assertEqual(out[0].leverage, 2.0)

    def test_above_cap_only_lowers(self):
        from tw_backdraw.futures import hybrid_entries
        bars = self._bars([100.0] * 5 + [110.0])
        out = hybrid_entries([FuturesEntry(5, 6, 4.0, 110.0, 0.02)], bars, 3,
                             above_risk_scale=0.5, above_cap=1.5)
        self.assertEqual(out[0].leverage, 1.5)

    def test_step_down_triggers_at_the_gain_and_reanchors_at_one_x(self):
        """3x 部位：期貨 +50% → 權益 +150% → 當天降到 1x，之後權益只隨期貨 1:1 變動。"""
        from tw_backdraw.futures import vehicle_series
        ds = days(4)
        fut = [100.0, 150.0, 165.0, 165.0]
        ent = [FuturesEntry(0, 3, 3.0, 100.0, 0.05)]
        nav, det = vehicle_series(ds, fut, ent, FREE, [100.0] * 4,
                                  step_gain=1.5, step_leverage=1.0)
        self.assertAlmostEqual(nav[1], 2.5, places=9)                # 觸發日：+150%
        self.assertAlmostEqual(nav[2], 2.5 * (1 + 1.0 * 0.10), places=9)  # 之後 1x
        self.assertEqual(det[0].step_date, ds[1])
        self.assertEqual(det[0].final_leverage, 1.0)
        self.assertEqual(det[0].leverage, 3.0)                       # 進場槓桿保留

    def test_step_down_is_off_by_default_and_never_on_exit_day(self):
        from tw_backdraw.futures import vehicle_series
        ds = days(3)
        fut = [100.0, 150.0, 160.0]
        ent = [FuturesEntry(0, 2, 3.0, 100.0, 0.05)]
        nav0, det0 = vehicle_series(ds, fut, ent, FREE, [100.0] * 3)
        self.assertIsNone(det0[0].step_date)
        self.assertAlmostEqual(nav0[2], 1 + 3 * 0.6, places=9)
        # 出場日恰好達標：不減碼，照原槓桿結算
        nav1, det1 = vehicle_series(ds, [100.0, 120.0, 150.0], ent, FREE, [100.0] * 3,
                                    step_gain=1.5)
        self.assertIsNone(det1[0].step_date)
        self.assertAlmostEqual(det1[0].ret, 1.5, places=9)

    def test_step_down_pays_one_way_cost_on_the_closed_notional(self):
        """減碼平掉的名目要付一次單邊成本，其餘部位不付。"""
        from tw_backdraw.futures import vehicle_series
        cost = FuturesCost(tax_rate=0.001, commission_per_lot=0.0)
        ds = days(3)
        # 觸發門檻以「進場前權益」計，含進場成本時期貨 +50% 還差一點，用 +51%
        fut = [100.0, 151.0, 151.0]
        ent = [FuturesEntry(0, 2, 3.0, 100.0, 0.05)]
        nav, _ = vehicle_series(ds, fut, ent, cost, [100.0] * 3, step_gain=1.5)
        anchor = 1 - 3 * 0.001                       # 進場成本
        v = anchor * (1 + 3 * 0.51)                  # 觸發日權益（未扣減碼成本）
        closed = 3 * anchor * 1.51 - 1.0 * v         # 平掉的名目：原名目 − 目標名目
        self.assertAlmostEqual(nav[1], v - closed * 0.001, places=12)

    def test_core_overlay_applies_the_same_step_down(self):
        from tw_backdraw.futures import core_overlay, vehicle_series
        ds = days(5)
        fut = [100.0, 150.0, 165.0, 165.0, 170.0]
        ent = [FuturesEntry(0, 3, 3.0, 100.0, 0.05)]
        ma = [None] * 5                              # 核心永不啟動 → 與 vehicle_series 相同
        nav_c, det_c, _ = core_overlay(ds, fut, ent, FREE, [100.0] * 5, 0.5, ma,
                                       step_gain=1.5)
        nav_v, det_v = vehicle_series(ds, fut, ent, FREE, [100.0] * 5, step_gain=1.5)
        self.assertEqual(nav_c, nav_v)
        self.assertEqual(det_c[0].step_date, det_v[0].step_date)


class TestCoreBand(unittest.TestCase):
    """§23 核心遲滯帶：站上 MA×(1+b) 才開、跌破 MA×(1−b) 才關。"""

    def test_band_zero_is_the_plain_cross(self):
        from tw_backdraw.futures import core_state
        ma = [100.0] * 4
        self.assertEqual(core_state([101.0, 100.0, 99.0, 100.5], ma, 0.0),
                         [True, False, False, True])

    def test_band_needs_a_margin_to_open_and_to_close(self):
        from tw_backdraw.futures import core_state
        ma = [100.0] * 6
        closes = [101.0, 102.5, 99.0, 98.5, 97.0, 101.0]
        # 101 不夠（需 >102）；102.5 開；99、98.5 仍在帶內維持；97 (<98) 關；101 不夠再開
        self.assertEqual(core_state(closes, ma, 0.02),
                         [False, True, True, True, False, False])

    def test_undefined_average_forces_off(self):
        from tw_backdraw.futures import core_state
        self.assertEqual(core_state([105.0, 105.0], [None, 100.0], 0.02), [False, True])

    def test_core_overlay_uses_the_band(self):
        from tw_backdraw.futures import core_overlay
        ds = days(4)
        fut = [100.0, 110.0, 121.0, 133.1]
        ma = [100.0] * 4
        # 帶寬 0：第 0 天收盤 100 不大於 MA → 第 1 天才開（收 110）；
        # 帶寬 5%：第 1 天 110 > 105 才開。兩者第 2 天起都持有 → 差在第 1→2 天的報酬
        nav0, _, _ = core_overlay(ds, fut, [], FREE, fut, 0.5, ma)
        nav5, _, _ = core_overlay(ds, fut, [], FREE, fut, 0.5, ma, band=0.05)
        self.assertEqual(nav0, nav5)                       # 這個例子兩者開在同一天
        nav_wide, _, _ = core_overlay(ds, fut, [], FREE, fut, 0.5, ma, band=0.15)
        # 帶寬 15%：第 1 天 110 < 115 未開，第 2 天 121 > 115 才開 → 少賺第 2→3 天之前那段
        self.assertLess(nav_wide[-1], nav0[-1])
