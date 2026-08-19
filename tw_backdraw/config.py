"""策略參數。

所有可調參數集中於此，方便做敏感度測試。
預設值對應貼文中的歷史統計（12 國、近百年、174 次樣本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SetupConfig:
    """「快速修復」劇本的辨識條件。"""

    # 先要有一段像樣的回檔才談得上修復，貼文樣本為 -16%
    min_drawdown: float = 0.10
    # 從谷底起算，補回跌幅的多少比例才算「補回來了」。
    # 貼文的原始值是 0.75；預設改用 grid search 在總報酬上選出的 0.60。
    repair_fraction: float = 0.60
    # 補回必須在幾個交易日內完成。
    # 貼文的原始值是 15（對應「89% 創高」那一組）；預設改用總報酬最佳的 30。
    max_repair_bars: int = 30
    # 訊號有效期；超過仍未創高也未失效就自然過期
    setup_expiry_bars: int = 120
    # 修復視窗過期後，把參考高點重新錨定到「谷底之後的波段高」。
    # 關掉的話，參考高點會一直釘在舊高，直到指數重新站上它為止 ——
    # 台股 2000 年頭部之後花了 17.3 年才收復 10,202，等於中間完全看不到訊號。
    reanchor_on_expiry: bool = True


@dataclass(frozen=True)
class LevelConfig:
    """由 P（前高）與 T（谷底）推出的關鍵價位。"""

    # 主防線：補回一半
    half_line_ratio: float = 0.50
    # 警戒線：貼文的原始值是 0.382（台股 42916）。
    # 預設改用總報酬最佳的 0.500 —— 這會讓警戒線與主防線重合，
    # 等於「跌破補回一半的線（含 0.5% 緩衝）就全部出場」，是明顯更緊的停損。
    warn_line_ratio: float = 0.500
    # 允許對主防線的假跌破緩衝（貼文：一半案例跌破不到 0.5%）
    half_line_buffer: float = 0.005


@dataclass(frozen=True)
class EntryConfig:
    """分批進場梯。

    核心前提：歷史上回檔中位數只有 2.9%、四分之三不超過 5%，
    所以「等深回檔」的期望值是負的 —— 底倉先上車，回檔才是加碼機會。
    """

    # 訊號確認後隔日直接建立的底倉權重。
    # 貼文原意是 0.40（保留三成回檔加碼空間）；預設改用總報酬最佳的 1.00，
    # 也就是訊號一確認就把目標水位一次買足，不留加碼梯。
    base_weight: float = 1.00
    # (自訊號後波段高點的回檔幅度, 加碼權重)
    pullback_ladder: tuple[tuple[float, float], ...] = ((0.03, 0.30), (0.05, 0.30))
    # 幾個交易日內若都沒等到回檔，就以市價補齊剩餘部位（不參與才是最大風險）
    fill_timeout_bars: int = 20
    # 突破前高後補齊剩餘部位。
    # 預設 True：訊號觸發時距前高通常只剩 3~4%，等不到回檔梯就先創高是常態，
    # 若此時取消加碼，實際部署會長期停在底倉水位（實測平均僅 24%）。
    # 往上買的單位風險較高，引擎會按停損距離自動縮小這一段的權重。
    breakout_fills_remainder: bool = True


@dataclass(frozen=True)
class ExitConfig:
    """出場與風控。"""

    # 收盤跌破警戒線 → 減碼比例。
    # 貼文原意是 0.50（砍一半、保留船票）；預設改用總報酬最佳的 1.00，
    # 也就是跌破就全部出場。這會降低勝率、提高總報酬（見 docs/strategy.md §11）。
    warn_derisk_fraction: float = 1.00
    # 收盤跌破谷底 → 全出（貼文中 11% 的失敗案例都是大熊市開場）
    hard_stop_at_trough: bool = True
    # 觸及前高後先落袋的比例。
    # 預設 0：「離前高很近，本身不是賣出的理由」——前高只是統計上的高機率目標，
    # 不是出場訊號；獲利全部交給停利機制處理。設 1/3 可改回分批落袋。
    target_take_fraction: float = 0.0

    # 創高之後用哪一種停利：
    #   "trail"       自創高後最高收盤回檔 trail_drawdown → 出場
    #   "ma_ratchet"  停利價 = max(前高, MA)。創高後先把前高當出場線，
    #                 等 MA 爬過前高，就改看 MA 跌破 —— 均線只會把出場線往上推。
    #   "both"        兩者取較緊（較高）的那一條
    exit_mode: str = "trail"
    # ma_ratchet / both 使用的均線天期（以加權指數收盤計）
    ma_period: int = 20
    # trail / both 使用的回檔幅度（以指數計）
    trail_drawdown: float = 0.08


@dataclass(frozen=True)
class SizingConfig:
    """部位規模（標的為 2 倍槓桿 ETF，必須以指數停損距離反推）。"""

    # 單筆交易願意承受的權益風險（兩段式停損全走完的預期損失）
    risk_per_trade: float = 0.08
    # 標的槓桿倍數（台灣50正2 = 2）
    leverage: float = 2.0
    # 最高持股水位（佔權益比例）
    max_weight: float = 1.0
    # 停損距離至少視為這麼大，避免訊號日離谷底太近而算出過大部位
    min_stop_distance: float = 0.05


@dataclass(frozen=True)
class CostConfig:
    """台股 ETF 交易成本與槓桿 ETF 的內扣損耗。"""

    # 券商手續費 0.1425% × 折扣
    fee_rate: float = 0.001425
    fee_discount: float = 0.60
    # ETF 賣出證交稅 0.1%
    tax_rate: float = 0.001
    # 槓桿 ETF 年化內扣（管理費 + 期貨轉倉/避險成本）
    annual_carry: float = 0.012
    trading_days: int = 252

    @property
    def buy_cost(self) -> float:
        return self.fee_rate * self.fee_discount

    @property
    def sell_cost(self) -> float:
        return self.fee_rate * self.fee_discount + self.tax_rate

    @property
    def daily_carry(self) -> float:
        return self.annual_carry / self.trading_days


@dataclass(frozen=True)
class StrategyConfig:
    setup: SetupConfig = field(default_factory=SetupConfig)
    levels: LevelConfig = field(default_factory=LevelConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    cost: CostConfig = field(default_factory=CostConfig)


# ---------------------------------------------------------------------------
# 預設組（PRESETS）
#
# 以下各組是 648,000 組 grid search + 樣本外驗證（scripts/grid_search.py、
# scripts/walk_forward.py）的產物。**預設是 tuned**，也就是以總報酬為目標
# 選出來的那一組；忠於貼文的原始設定保留在 `post`。
# 兩者的取捨與已知風險記在 docs/strategy.md §11，換組合前請先讀。
# ---------------------------------------------------------------------------

#: 以總報酬為目標選出的參數（本專案的預設）。放寬訊號定義（30 日 / 補回 60%）、
#: 訊號確認即滿倉、跌破主防線全數出場。
#: 樣本內 21 筆、勝率 52%、總報酬 +5151%、最大回檔 -44%。
TUNED_CONFIG = StrategyConfig()

DEFAULT_CONFIG = TUNED_CONFIG

#: 忠於貼文的原始設定：15 日內補回 75%、底倉四成留加碼梯、
#: 38.2% 回補位減碼一半。樣本內 8 筆、勝率 50%、總報酬 +31.4%、最大回檔 -20.4%。
POST_CONFIG = StrategyConfig(
    setup=SetupConfig(repair_fraction=0.75, max_repair_bars=15),
    levels=LevelConfig(warn_line_ratio=0.382),
    entry=EntryConfig(base_weight=0.40),
    exit=ExitConfig(warn_derisk_fraction=0.50),
)

#: 調校過的訊號 + MA40 棘輪出場。用報酬換勝率與較小的回檔：
#: 樣本內 21 筆、勝率 67%、總報酬 +459%、回檔 -35%。
BALANCED_CONFIG = StrategyConfig(
    exit=ExitConfig(warn_derisk_fraction=1.0, exit_mode="ma_ratchet", ma_period=40),
)

#: 全網格勝率最高的一組（92%，11/12）。**它是靠關掉兩道停損換來的**，
#: 總報酬只有 +33%、最大回檔 -35%。列在這裡是為了讓「最大化勝率」的
#: 後果可以被重現，不是建議值。
WINRATE_CONFIG = StrategyConfig(
    setup=SetupConfig(min_drawdown=0.13),
    levels=LevelConfig(warn_line_ratio=0.382),
    entry=EntryConfig(fill_timeout_bars=40),
    exit=ExitConfig(warn_derisk_fraction=0.0, hard_stop_at_trough=False,
                    exit_mode="ma_ratchet", ma_period=40),
)

PRESETS: dict[str, StrategyConfig] = {
    "tuned": TUNED_CONFIG,
    "post": POST_CONFIG,
    "balanced": BALANCED_CONFIG,
    "winrate": WINRATE_CONFIG,
}
