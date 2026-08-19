"""策略單元測試（stdlib unittest，無外部相依）。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tw_backdraw import build_levels, build_plan, detect_setups
from tw_backdraw.bars import Bar
from tw_backdraw.config import DEFAULT_CONFIG, SetupConfig, StrategyConfig
from tw_backdraw.engine import Engine, position_size
from tw_backdraw.leveraged import synth_leveraged_path

# 貼文中的台股實例
PEAK, TROUGH = 47742.0, 39933.0


def series(closes: list[float], start: date = date(2025, 1, 2)) -> list[Bar]:
    """把收盤價序列轉成日線（跳過週末，日期只是為了可讀性）。"""
    bars, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append(Bar(d=d, open=c, high=c, low=c, close=c))
        d += timedelta(days=1)
    return bars


def ramp(a: float, b: float, n: int) -> list[float]:
    """a → b 的 n 段線性插值（不含起點）。"""
    return [a + (b - a) * (i + 1) / n for i in range(n)]


class TestLevels(unittest.TestCase):
    def test_matches_post_numbers(self):
        lv = build_levels(PEAK, TROUGH, DEFAULT_CONFIG.levels)
        self.assertAlmostEqual(lv.drop, 7809.0)
        self.assertAlmostEqual(lv.drop_pct, 0.1636, places=4)
        # 貼文說「補回一半」那條線大約在 43800
        self.assertAlmostEqual(lv.half_line, 43837.5, places=1)
        # 貼文的 42916 就是 38.2% 回補位
        self.assertAlmostEqual(lv.warn_line, 42916.0, places=0)
        self.assertEqual(lv.invalidation, TROUGH)

    def test_zones(self):
        lv = build_levels(PEAK, TROUGH, DEFAULT_CONFIG.levels)
        self.assertEqual(lv.zone(48000), "breakout")
        self.assertEqual(lv.zone(46000), "healthy")
        self.assertEqual(lv.zone(43700), "buffer")       # 主防線下方 0.5% 內
        self.assertEqual(lv.zone(43000), "caution")
        self.assertEqual(lv.zone(41000), "warning")
        self.assertEqual(lv.zone(39000), "invalidated")

    def test_repair_fraction(self):
        lv = build_levels(PEAK, TROUGH, DEFAULT_CONFIG.levels)
        self.assertAlmostEqual(lv.repair_fraction(TROUGH), 0.0)
        self.assertAlmostEqual(lv.repair_fraction(PEAK), 1.0)
        self.assertAlmostEqual(lv.repair_fraction(lv.half_line), 0.5)


class TestSetupDetection(unittest.TestCase):
    def test_fast_repair_triggers(self):
        # 47742 → 39933（下跌），12 根 K 補回 80%
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 12)
        setups = detect_setups(series(closes), DEFAULT_CONFIG.setup)
        self.assertEqual(len(setups), 1)
        s = setups[0]
        self.assertAlmostEqual(s.peak, PEAK)
        self.assertAlmostEqual(s.trough, TROUGH)
        self.assertLessEqual(s.bars_to_repair, 15)
        self.assertGreaterEqual(s.repair_fraction, 0.75)

    def test_slow_repair_rejected(self):
        # 同樣補回八成，但拖了 40 根 K —— 歷史成功率只剩五成，不進場
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 40)
        self.assertEqual(detect_setups(series(closes), DEFAULT_CONFIG.setup), [])

    def test_shallow_dip_rejected(self):
        # 只回檔 6%，不到 10% 門檻
        low = PEAK * 0.94
        closes = [PEAK] + ramp(PEAK, low, 10) + ramp(low, PEAK * 0.99, 6)
        self.assertEqual(detect_setups(series(closes), DEFAULT_CONFIG.setup), [])

    def test_lower_low_resets_the_clock(self):
        # 先反彈一半、再破底，計時必須從新谷底重算
        mid = TROUGH + 0.4 * 7809
        deeper = TROUGH - 800
        closes = ([PEAK] + ramp(PEAK, TROUGH, 15) + ramp(TROUGH, mid, 8)
                  + ramp(mid, deeper, 8) + ramp(deeper, deeper + 0.8 * (PEAK - deeper), 10))
        setups = detect_setups(series(closes), DEFAULT_CONFIG.setup)
        self.assertEqual(len(setups), 1)
        self.assertAlmostEqual(setups[0].trough, deeper)


class TestSizing(unittest.TestCase):
    def test_further_stop_means_smaller_position(self):
        lv = build_levels(PEAK, TROUGH, DEFAULT_CONFIG.levels)
        near = position_size(44000, lv, DEFAULT_CONFIG)   # 離失效線近
        far = position_size(47000, lv, DEFAULT_CONFIG)    # 離失效線遠
        self.assertGreater(near, far)
        self.assertLessEqual(far, DEFAULT_CONFIG.sizing.max_weight)

    def test_risk_budget_is_respected(self):
        lv = build_levels(PEAK, TROUGH, DEFAULT_CONFIG.levels)
        entry = 46200.0
        w = position_size(entry, lv, DEFAULT_CONFIG)
        # 兩段式停損全部走完的預期損失 ≈ 風險預算
        to_warn = (entry - lv.warn_line) / entry
        to_trough = (entry - lv.invalidation) / entry
        loss = w * 2.0 * (0.5 * to_warn + 0.5 * to_trough)
        self.assertAlmostEqual(loss, DEFAULT_CONFIG.sizing.risk_per_trade, places=6)


class TestEngine(unittest.TestCase):
    def _run(self, closes: list[float], cfg: StrategyConfig | None = None):
        cfg = cfg or DEFAULT_CONFIG
        bars = series(closes)
        etf = synth_leveraged_path(bars, cfg.cost, cfg.sizing.leverage)
        return bars, Engine(cfg).run(bars, etf)

    def test_base_tranche_fills_without_waiting_for_a_pullback(self):
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 12) + [46200] * 3
        _, res = self._run(closes)
        self.assertEqual(len(res.trades), 1)
        first = res.trades[0].fills[0]
        self.assertEqual(first.side, "buy")
        self.assertIn("底倉", first.reason)
        # 底倉 = 目標水位的 40%
        self.assertAlmostEqual(first.weight, res.trades[0].target_weight * 0.40, places=6)

    def test_pullback_ladder_adds(self):
        base = TROUGH + 0.80 * 7809          # ≈ 46180
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + [base * 0.965, base * 0.945, base * 0.98, base * 1.01])
        _, res = self._run(closes)
        reasons = [f.reason for f in res.trades[0].fills if f.side == "buy"]
        self.assertTrue(any("回檔加碼" in r for r in reasons), reasons)
        # 兩段回檔都吃到 → 三筆買進
        self.assertEqual(len(reasons), 3, reasons)

    def test_timeout_fills_the_remainder(self):
        base = TROUGH + 0.80 * 7809
        # 訊號後一路碎步走高（但沒突破前高），永遠等不到 -3%
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + [base * (1 + 0.0005 * i) for i in range(1, 26)])
        _, res = self._run(closes)
        reasons = [f.reason for f in res.trades[0].fills if f.side == "buy"]
        self.assertTrue(any("時間補齊" in r for r in reasons), reasons)

    def test_hard_stop_below_trough(self):
        base = TROUGH + 0.80 * 7809
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + ramp(base, TROUGH - 500, 15) + [TROUGH - 600] * 3)
        _, res = self._run(closes)
        t = res.trades[0]
        self.assertEqual(t.exit_reason, "stop_trough")
        sells = [f.reason for f in t.fills if f.side == "sell"]
        self.assertTrue(any("警戒減碼" in r for r in sells), sells)   # 先在 38.2% 減碼
        self.assertTrue(any("劇本失效" in r for r in sells), sells)   # 再於谷底清倉
        self.assertLess(t.ret, 0.0)

    def test_take_profit_at_prior_high_then_trail(self):
        base = TROUGH + 0.80 * 7809
        top = PEAK * 1.10
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + ramp(base, top, 20) + ramp(top, top * 0.90, 6) + [top * 0.89] * 2)
        _, res = self._run(closes)
        t = res.trades[0]
        self.assertTrue(t.reached_prior_high)
        sells = [f.reason for f in t.fills if f.side == "sell"]
        self.assertTrue(any("目標達陣" in r for r in sells), sells)
        self.assertTrue(any("移動停利" in r for r in sells), sells)
        self.assertGreater(t.ret, 0.0)

    def test_no_trade_when_repair_is_slow(self):
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, PEAK, 60)
        _, res = self._run(closes)
        self.assertEqual(res.trades, [])

    def test_faster_repair_threshold_is_configurable(self):
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 30)
        strict = DEFAULT_CONFIG.setup
        loose = SetupConfig(min_drawdown=0.10, repair_fraction=0.75, max_repair_bars=35)
        self.assertEqual(detect_setups(series(closes), strict), [])
        self.assertEqual(len(detect_setups(series(closes), loose)), 1)


class TestPlan(unittest.TestCase):
    def test_plan_weights_sum_to_target(self):
        plan = build_plan(PEAK, TROUGH, 46200, DEFAULT_CONFIG, capital=1_000_000)
        total = sum(o.weight for o in plan.orders)
        self.assertAlmostEqual(total, plan.target_weight, places=6)

    def test_render_contains_the_key_levels(self):
        text = build_plan(PEAK, TROUGH, 46200, DEFAULT_CONFIG).render()
        for token in ("47,742", "43,838", "42,916", "39,933"):
            self.assertIn(token, text)


class TestLeveraged(unittest.TestCase):
    def test_two_x_on_a_single_day(self):
        bars = series([100.0, 105.0])
        path = synth_leveraged_path(bars, DEFAULT_CONFIG.cost, 2.0, start_price=100.0)
        self.assertAlmostEqual(path[1] / path[0] - 1, 0.10 - DEFAULT_CONFIG.cost.daily_carry, places=6)

    def test_choppy_market_decays(self):
        # 指數來回震盪回到原點，槓桿 ETF 必然虧損
        closes = [100.0]
        for _ in range(30):
            closes += [closes[-1] * 1.03, closes[-1] * 1.03 / 1.03]
        bars = series(closes)
        path = synth_leveraged_path(bars, DEFAULT_CONFIG.cost, 2.0)
        self.assertAlmostEqual(bars[-1].close, 100.0, places=6)
        self.assertLess(path[-1], 100.0)


if __name__ == "__main__":
    unittest.main()
