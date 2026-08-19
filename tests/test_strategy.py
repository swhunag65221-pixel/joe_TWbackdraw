"""策略單元測試（stdlib unittest，無外部相依）。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tw_backdraw import build_levels, build_plan, detect_setups
from tw_backdraw.setup import scan_episodes
from tw_backdraw.bars import Bar
from tw_backdraw.config import (
    DEFAULT_CONFIG, POST_CONFIG, PRESETS, EntryConfig, ExitConfig, SetupConfig,
    StrategyConfig,
)
from tw_backdraw.engine import (
    Engine, breakout_stop, ladder_weights, moving_average, position_size,
)
from tw_backdraw.leveraged import synth_leveraged_path
from tw_backdraw.status import render_status

# 下列多數測試驗的是「貼文版」的規則語意（分批加碼梯、38.2% 警戒線、15 日修復），
# 所以一律釘在 POST_CONFIG，不隨專案預設值改變而漂移。
POST = POST_CONFIG

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
        lv = build_levels(PEAK, TROUGH, POST.levels)
        self.assertAlmostEqual(lv.drop, 7809.0)
        self.assertAlmostEqual(lv.drop_pct, 0.1636, places=4)
        # 貼文說「補回一半」那條線大約在 43800
        self.assertAlmostEqual(lv.half_line, 43837.5, places=1)
        # 貼文的 42916 就是 38.2% 回補位
        self.assertAlmostEqual(lv.warn_line, 42916.0, places=0)
        self.assertEqual(lv.invalidation, TROUGH)

    def test_zones(self):
        lv = build_levels(PEAK, TROUGH, POST.levels)
        self.assertEqual(lv.zone(48000), "breakout")
        self.assertEqual(lv.zone(46000), "healthy")
        self.assertEqual(lv.zone(43700), "buffer")       # 主防線下方 0.5% 內
        self.assertEqual(lv.zone(43000), "caution")
        self.assertEqual(lv.zone(41000), "warning")
        self.assertEqual(lv.zone(39000), "invalidated")

    def test_repair_fraction(self):
        lv = build_levels(PEAK, TROUGH, POST.levels)
        self.assertAlmostEqual(lv.repair_fraction(TROUGH), 0.0)
        self.assertAlmostEqual(lv.repair_fraction(PEAK), 1.0)
        self.assertAlmostEqual(lv.repair_fraction(lv.half_line), 0.5)


class TestSetupDetection(unittest.TestCase):
    def test_fast_repair_triggers(self):
        # 47742 → 39933（下跌），12 根 K 補回 80%
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 12)
        setups = detect_setups(series(closes), POST.setup)
        self.assertEqual(len(setups), 1)
        s = setups[0]
        self.assertAlmostEqual(s.peak, PEAK)
        self.assertAlmostEqual(s.trough, TROUGH)
        self.assertLessEqual(s.bars_to_repair, 15)
        self.assertGreaterEqual(s.repair_fraction, 0.75)

    def test_slow_repair_rejected(self):
        # 同樣補回八成，但拖了 40 根 K —— 歷史成功率只剩五成，不進場
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 40)
        self.assertEqual(detect_setups(series(closes), POST.setup), [])

    def test_shallow_dip_rejected(self):
        # 只回檔 6%，不到 10% 門檻
        low = PEAK * 0.94
        closes = [PEAK] + ramp(PEAK, low, 10) + ramp(low, PEAK * 0.99, 6)
        self.assertEqual(detect_setups(series(closes), POST.setup), [])

    def test_lower_low_resets_the_clock(self):
        # 先反彈一半、再破底，計時必須從新谷底重算
        mid = TROUGH + 0.4 * 7809
        deeper = TROUGH - 800
        closes = ([PEAK] + ramp(PEAK, TROUGH, 15) + ramp(TROUGH, mid, 8)
                  + ramp(mid, deeper, 8) + ramp(deeper, deeper + 0.8 * (PEAK - deeper), 10))
        setups = detect_setups(series(closes), POST.setup)
        self.assertEqual(len(setups), 1)
        self.assertAlmostEqual(setups[0].trough, deeper)


class TestReanchor(unittest.TestCase):
    """修復視窗到期後，參考高點必須改錨到谷底之後的波段高。

    少了這一步，參考高點會一直釘在舊高直到指數重新站上為止 ——
    台股 2000 年頭部之後 17.3 年沒收復，中間所有訊號都會被遮蔽。
    """

    def _series(self) -> list[float]:
        # 舊高 10000 → 崩到 5000（慢速修復，不觸發）→ 在低檔形成新的 P/T 並快速修復
        deep = 5000.0
        slow = ramp(deep, 6500, 40)          # 40 根才爬回，遠超過 15 日視窗
        newp = slow[-1]
        dip = ramp(newp, newp * 0.87, 12)    # 自新高回檔 13%
        rebound = ramp(dip[-1], dip[-1] + 0.80 * (newp - dip[-1]), 8)
        return [10000.0] + ramp(10000, deep, 30) + slow + dip + rebound

    def test_reanchored_low_level_setup_is_detected(self):
        setups = detect_setups(series(self._series()), POST.setup)
        self.assertEqual(len(setups), 1, [s.describe() for s in setups])
        # 訊號的參考高點是改錨後的新高，不是 10000 那個舊高
        self.assertLess(setups[0].peak, 7000)

    def test_without_reanchor_the_detector_goes_blind(self):
        cfg = SetupConfig(reanchor_on_expiry=False)
        self.assertEqual(detect_setups(series(self._series()), cfg), [])

    def test_episodes_explain_every_rejection(self):
        eps = scan_episodes(series(self._series()), POST.setup)
        self.assertGreaterEqual(len(eps), 2)
        slow = eps[0]
        self.assertFalse(slow.fired)
        self.assertLess(slow.best_in_window, POST.setup.repair_fraction)
        self.assertIn("只補回", slow.reason(POST.setup))
        self.assertTrue(eps[-1].fired)

    def test_episodes_and_setups_agree(self):
        bars = series(self._series())
        fired = [e for e in scan_episodes(bars, POST.setup) if e.fired]
        self.assertEqual(len(fired), len(detect_setups(bars, POST.setup)))


class TestLadderNormalisation(unittest.TestCase):
    """底倉加上回檔加碼梯，必須剛好等於目標水位，不能超額。"""

    def _cfg(self, base_weight: float):
        return StrategyConfig(setup=POST.setup, levels=POST.levels,
                              entry=EntryConfig(base_weight=base_weight),
                              exit=POST.exit, sizing=POST.sizing, cost=POST.cost)

    def test_base_plus_ladder_always_equals_the_target(self):
        for bw in (0.0, 0.25, 0.4, 0.75, 1.0):
            with self.subTest(base_weight=bw):
                cfg = self._cfg(bw)
                total = bw + sum(w for _, w in ladder_weights(cfg, 1.0))
                self.assertAlmostEqual(total, 1.0, places=9)

    def test_full_base_leaves_no_ladder(self):
        self.assertEqual(ladder_weights(self._cfg(1.0), 1.0), [])

    def test_post_preset_ladder_is_unchanged(self):
        self.assertEqual([round(w, 6) for _, w in ladder_weights(POST, 1.0)], [0.3, 0.3])

    def test_engine_never_exceeds_the_target_weight(self):
        base = TROUGH + 0.80 * 7809
        # 訊號後連續下殺，把兩段加碼梯都觸發
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + [base * 0.965, base * 0.945, base * 0.95, base * 0.96])
        for bw in (0.4, 1.0):
            with self.subTest(base_weight=bw):
                cfg = self._cfg(bw)
                bars = series(closes)
                etf = synth_leveraged_path(bars, cfg.cost, cfg.sizing.leverage)
                t = Engine(cfg).run(bars, etf).trades[0]
                self.assertLessEqual(t.filled_weight, t.target_weight + 1e-9)


class TestSizing(unittest.TestCase):
    def test_further_stop_means_smaller_position(self):
        lv = build_levels(PEAK, TROUGH, POST.levels)
        near = position_size(44000, lv, POST)   # 離失效線近
        far = position_size(47000, lv, POST)    # 離失效線遠
        self.assertGreater(near, far)
        self.assertLessEqual(far, POST.sizing.max_weight)

    def test_risk_budget_is_respected(self):
        lv = build_levels(PEAK, TROUGH, POST.levels)
        entry = 46200.0
        w = position_size(entry, lv, POST)
        # 兩段式停損全部走完的預期損失 ≈ 風險預算
        to_warn = (entry - lv.warn_line) / entry
        to_trough = (entry - lv.invalidation) / entry
        loss = w * 2.0 * (0.5 * to_warn + 0.5 * to_trough)
        self.assertAlmostEqual(loss, POST.sizing.risk_per_trade, places=6)


class TestEngine(unittest.TestCase):
    def _run(self, closes: list[float], cfg: StrategyConfig | None = None):
        cfg = cfg or POST
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

    def _breakout_series(self) -> list[float]:
        base = TROUGH + 0.80 * 7809
        top = PEAK * 1.25
        # 訊號 → 創高 → 續漲兩成 → 緩跌觸發移動停利
        return ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                + ramp(base, top, 25) + ramp(top, top * 0.88, 12) + [top * 0.87] * 2)

    def test_prior_high_is_not_a_sell_signal_by_default(self):
        """預設不在前高賣出，出場全部交給移動停利。"""
        _, res = self._run(self._breakout_series())
        t = res.trades[0]
        self.assertTrue(t.reached_prior_high)
        sells = [f.reason for f in t.fills if f.side == "sell"]
        self.assertFalse(any("目標達陣" in r for r in sells), sells)
        self.assertTrue(any("移動停利" in r for r in sells), sells)
        self.assertGreater(t.ret, 0.0)

    def test_take_profit_at_prior_high_when_enabled(self):
        cfg = StrategyConfig(setup=POST.setup, levels=POST.levels,
                             entry=POST.entry,
                             exit=ExitConfig(target_take_fraction=1 / 3),
                             sizing=POST.sizing, cost=POST.cost)
        _, res = self._run(self._breakout_series(), cfg)
        sells = [f.reason for f in res.trades[0].fills if f.side == "sell"]
        self.assertTrue(any("目標達陣" in r for r in sells), sells)
        self.assertTrue(any("移動停利" in r for r in sells), sells)

    def test_breakout_fill_is_risk_scaled_down(self):
        """在更高的價位補倉，權重必須按停損距離縮小，才不會超出風險預算。"""
        _, res = self._run(self._breakout_series())
        t = res.trades[0]
        buys = [f for f in t.fills if f.side == "buy"]
        breakout = [f for f in buys if "突破補齊" in f.reason]
        self.assertTrue(breakout, [f.reason for f in buys])
        # 加碼梯名目權重是目標的 30%，實際成交必須更小
        self.assertLess(breakout[0].weight, 0.30 * t.target_weight)
        self.assertGreater(breakout[0].weight, 0.0)

    def test_derisk_then_reload_when_the_main_line_is_reclaimed(self):
        base = TROUGH + 0.80 * 7809
        lv = build_levels(PEAK, TROUGH, POST.levels)
        # 跌破 38.2% 警戒線 → 減碼；再收復 50% 主防線 → 回補一次
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + ramp(base, lv.warn_line - 200, 10)
                  + ramp(lv.warn_line - 200, lv.half_line + 400, 10) + [lv.half_line + 500] * 3)
        _, res = self._run(closes)
        t = res.trades[0]
        self.assertTrue(any("警戒減碼" in f.reason for f in t.fills if f.side == "sell"))
        self.assertTrue(any("回補" in f.reason for f in t.fills if f.side == "buy"))

    def test_no_adds_below_the_main_line(self):
        base = TROUGH + 0.80 * 7809
        lv = build_levels(PEAK, TROUGH, POST.levels)
        # 直接摜破主防線（−5% 以上），但 caution 區不得加碼
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + [lv.half_line - 300] * 5)
        _, res = self._run(closes)
        buys = [f.reason for f in res.trades[0].fills if f.side == "buy"]
        self.assertEqual(len(buys), 1, buys)          # 只有底倉
        self.assertIn("底倉", buys[0])

    def test_no_trade_when_repair_is_slow(self):
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, PEAK, 60)
        _, res = self._run(closes)
        self.assertEqual(res.trades, [])

    def test_faster_repair_threshold_is_configurable(self):
        closes = [PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, TROUGH + 0.80 * 7809, 30)
        strict = POST.setup
        loose = SetupConfig(min_drawdown=0.10, repair_fraction=0.75, max_repair_bars=35)
        self.assertEqual(detect_setups(series(closes), strict), [])
        self.assertEqual(len(detect_setups(series(closes), loose)), 1)


class TestPresets(unittest.TestCase):
    def test_default_is_the_calmar_tuned_preset(self):
        """專案預設是以「總報酬 ÷ 最大回檔」選出的那一組。"""
        self.assertEqual(DEFAULT_CONFIG, PRESETS["tuned"])
        self.assertNotEqual(DEFAULT_CONFIG, PRESETS["post"])

    def test_post_preset_still_matches_the_article(self):
        cfg = PRESETS["post"]
        self.assertEqual(cfg.setup.max_repair_bars, 15)
        self.assertAlmostEqual(cfg.setup.repair_fraction, 0.75)
        self.assertAlmostEqual(cfg.levels.warn_line_ratio, 0.382)
        self.assertAlmostEqual(cfg.entry.base_weight, 0.40)
        self.assertAlmostEqual(cfg.exit.warn_derisk_fraction, 0.50)

    def test_every_preset_runs(self):
        base = TROUGH + 0.80 * 7809
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + ramp(base, PEAK * 1.2, 30) + ramp(PEAK * 1.2, PEAK * 0.95, 20))
        bars = series(closes)
        for name, cfg in PRESETS.items():
            with self.subTest(preset=name):
                etf = synth_leveraged_path(bars, cfg.cost, cfg.sizing.leverage)
                Engine(cfg).run(bars, etf)          # 不應拋錯

    def test_winrate_preset_has_its_stops_disabled(self):
        """勝率最高的那組是靠關掉風控換來的，這一點必須留在程式碼裡看得見。"""
        cfg = PRESETS["winrate"]
        self.assertEqual(cfg.exit.warn_derisk_fraction, 0.0)
        self.assertFalse(cfg.exit.hard_stop_at_trough)

    def test_tuned_and_balanced_keep_their_stops(self):
        for name in ("tuned", "balanced"):
            with self.subTest(preset=name):
                cfg = PRESETS[name]
                self.assertGreater(cfg.exit.warn_derisk_fraction, 0.0)
                self.assertTrue(cfg.exit.hard_stop_at_trough)

    def test_balanced_uses_the_ma_ratchet(self):
        self.assertEqual(PRESETS["balanced"].exit.exit_mode, "ma_ratchet")
        self.assertEqual(PRESETS["balanced"].exit.ma_period, 40)


class TestPlan(unittest.TestCase):
    def test_plan_weights_sum_to_target(self):
        plan = build_plan(PEAK, TROUGH, 46200, POST, capital=1_000_000)
        total = sum(o.weight for o in plan.orders)
        self.assertAlmostEqual(total, plan.target_weight, places=6)

    def test_render_contains_the_key_levels(self):
        text = build_plan(PEAK, TROUGH, 46200, POST).render()
        for token in ("47,742", "43,838", "42,916", "39,933"):
            self.assertIn(token, text)

    def test_render_follows_the_config_not_hardcoded_post_rules(self):
        """輸出必須反映實際設定 —— 曾經寫死成貼文版，導致計畫書講「減碼一半」
        但引擎其實是全數出場。"""
        post = build_plan(PEAK, TROUGH, 46200, POST).render()
        self.assertIn("減碼 50%", post)
        self.assertIn("賣出 33% 落袋", post) if POST.exit.target_take_fraction else None
        self.assertIn("42,916", post)          # 38.2% 警戒線與主防線不同

        deflt = build_plan(PEAK, TROUGH, 46200, DEFAULT_CONFIG).render()
        self.assertIn("全部出場", deflt)
        self.assertNotIn("減碼一半", deflt)
        self.assertIn("不賣（前高不是賣出的理由）", deflt)
        self.assertIn("主防線 = 警戒線", deflt)   # 兩條線重合時只印一條

    def test_render_describes_the_configured_exit_mode(self):
        cfg = StrategyConfig(setup=DEFAULT_CONFIG.setup, levels=DEFAULT_CONFIG.levels,
                             entry=DEFAULT_CONFIG.entry,
                             exit=ExitConfig(exit_mode="ma_ratchet", ma_period=40),
                             sizing=DEFAULT_CONFIG.sizing, cost=DEFAULT_CONFIG.cost)
        text = build_plan(PEAK, TROUGH, 46200, cfg).render()
        self.assertIn("MA40", text)
        self.assertNotIn("移動停利", text)

    def test_no_ladder_means_no_timeout_line(self):
        self.assertEqual(len(build_plan(PEAK, TROUGH, 46200, DEFAULT_CONFIG).orders), 1)
        self.assertGreater(len(build_plan(PEAK, TROUGH, 46200, POST).orders), 1)


class TestMovingAverageExit(unittest.TestCase):
    def test_moving_average_warms_up(self):
        bars = series([10.0, 20.0, 30.0, 40.0])
        ma = moving_average(bars, 3)
        self.assertEqual(ma[:2], [None, None])
        self.assertAlmostEqual(ma[2], 20.0)
        self.assertAlmostEqual(ma[3], 30.0)

    def _cfg(self, **kw):
        return StrategyConfig(setup=POST.setup, levels=POST.levels,
                              entry=POST.entry, exit=ExitConfig(**kw),
                              sizing=POST.sizing, cost=POST.cost)

    def test_ratchet_uses_prior_high_until_the_ma_catches_up(self):
        cfg = self._cfg(exit_mode="ma_ratchet", ma_period=20)
        # MA 還在前高下方 → 出場線是前高本身
        stop, why = breakout_stop(cfg, prior_high=100.0, peak_since_breakout=130.0, ma=90.0)
        self.assertAlmostEqual(stop, 100.0)
        self.assertIn("前高", why)
        # MA 爬過前高 → 改看 MA
        stop, why = breakout_stop(cfg, prior_high=100.0, peak_since_breakout=130.0, ma=115.0)
        self.assertAlmostEqual(stop, 115.0)
        self.assertIn("MA20", why)

    def test_ratchet_before_warmup_falls_back_to_prior_high(self):
        cfg = self._cfg(exit_mode="ma_ratchet")
        stop, _ = breakout_stop(cfg, prior_high=100.0, peak_since_breakout=130.0, ma=None)
        self.assertAlmostEqual(stop, 100.0)

    def test_both_mode_takes_the_tighter_stop(self):
        cfg = self._cfg(exit_mode="both", ma_period=40, trail_drawdown=0.08)
        # trail = 130×0.92 = 119.6 > MA 115 → 取 trail
        stop, why = breakout_stop(cfg, prior_high=100.0, peak_since_breakout=130.0, ma=115.0)
        self.assertAlmostEqual(stop, 119.6)
        self.assertIn("移動停利", why)
        # MA 125 > trail 119.6 → 取 MA
        stop, why = breakout_stop(cfg, prior_high=100.0, peak_since_breakout=130.0, ma=125.0)
        self.assertAlmostEqual(stop, 125.0)
        self.assertIn("MA40", why)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            breakout_stop(self._cfg(exit_mode="nope"), 100.0, 130.0, 110.0)

    def test_ma_exit_fires_in_a_full_run(self):
        base = TROUGH + 0.80 * 7809
        top = PEAK * 1.30
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12)
                  + ramp(base, top, 60) + ramp(top, top * 0.80, 25) + [top * 0.79] * 3)
        bars = series(closes)
        cfg = self._cfg(exit_mode="ma_ratchet", ma_period=20)
        etf = synth_leveraged_path(bars, cfg.cost, 2.0)
        res = Engine(cfg).run(bars, etf)
        sells = [f.reason for f in res.trades[0].fills if f.side == "sell"]
        self.assertTrue(any("MA20" in r for r in sells), sells)


class TestStatus(unittest.TestCase):
    def test_open_position_status_lists_the_next_triggers(self):
        base = TROUGH + 0.80 * 7809
        closes = ([PEAK] + ramp(PEAK, TROUGH, 20) + ramp(TROUGH, base, 12) + [base * 0.99] * 3)
        bars = series(closes)
        etf = synth_leveraged_path(bars, POST.cost, 2.0)
        res = Engine(POST).run(bars, etf)
        text = render_status(res, bars, POST, capital=1_000_000)
        self.assertIn("底倉", text)
        self.assertIn("42,916", text)      # 警戒線
        self.assertIn("39,933", text)      # 失效線
        self.assertIn("回檔 3%", text)      # 尚未成交的加碼梯

    def test_status_without_a_position(self):
        bars = series([PEAK * (1 + 0.001 * i) for i in range(30)])
        etf = synth_leveraged_path(bars, POST.cost, 2.0)
        res = Engine(POST).run(bars, etf)
        text = render_status(res, bars, POST)
        self.assertIn("沒有進行中的部位", text)


class TestLeveraged(unittest.TestCase):
    def test_two_x_on_a_single_day(self):
        bars = series([100.0, 105.0])
        path = synth_leveraged_path(bars, POST.cost, 2.0, start_price=100.0)
        self.assertAlmostEqual(path[1] / path[0] - 1, 0.10 - POST.cost.daily_carry, places=6)

    def test_choppy_market_decays(self):
        # 指數來回震盪回到原點，槓桿 ETF 必然虧損
        closes = [100.0]
        for _ in range(30):
            closes += [closes[-1] * 1.03, closes[-1] * 1.03 / 1.03]
        bars = series(closes)
        path = synth_leveraged_path(bars, POST.cost, 2.0)
        self.assertAlmostEqual(bars[-1].close, 100.0, places=6)
        self.assertLess(path[-1], 100.0)


if __name__ == "__main__":
    unittest.main()
