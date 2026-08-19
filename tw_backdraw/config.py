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
    # 從谷底起算，補回跌幅的多少比例才算「補回來了」（貼文：12 日補回近八成）
    repair_fraction: float = 0.75
    # 補回必須在幾個交易日內完成才算「最快的一組」（貼文：15 日內 → 89% 創高）
    max_repair_bars: int = 15
    # 訊號有效期；超過仍未創高也未失效就自然過期
    setup_expiry_bars: int = 120


@dataclass(frozen=True)
class LevelConfig:
    """由 P（前高）與 T（谷底）推出的關鍵價位。"""

    # 主防線：補回一半
    half_line_ratio: float = 0.50
    # 警戒線：38.2% 回補位（貼文台股 42916）
    warn_line_ratio: float = 0.382
    # 允許對主防線的假跌破緩衝（貼文：一半案例跌破不到 0.5%）
    half_line_buffer: float = 0.005


@dataclass(frozen=True)
class EntryConfig:
    """分批進場梯。

    核心前提：歷史上回檔中位數只有 2.9%、四分之三不超過 5%，
    所以「等深回檔」的期望值是負的 —— 底倉先上車，回檔才是加碼機會。
    """

    # 訊號確認後隔日開盤直接建立的底倉權重
    base_weight: float = 0.40
    # (自訊號後波段高點的回檔幅度, 加碼權重)
    pullback_ladder: tuple[tuple[float, float], ...] = ((0.03, 0.30), (0.05, 0.30))
    # 幾個交易日內若都沒等到回檔，就以市價補齊剩餘部位（不參與才是最大風險）
    fill_timeout_bars: int = 20
    # 突破前高後是否補齊剩餘部位。
    # 預設 False：突破即視為劇本完成，改為分批獲利 + 移動停利，不再往上追。
    breakout_fills_remainder: bool = False


@dataclass(frozen=True)
class ExitConfig:
    """出場與風控。"""

    # 收盤跌破警戒線 → 減碼比例（部位砍一半，保留船票）
    warn_derisk_fraction: float = 0.50
    # 收盤跌破谷底 → 全出（貼文中 11% 的失敗案例都是大熊市開場）
    hard_stop_at_trough: bool = True
    # 觸及前高後先落袋的比例
    target_take_fraction: float = 1.0 / 3.0
    # 剩餘部位改用移動停利：自最高收盤回檔多少（以指數計）
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


DEFAULT_CONFIG = StrategyConfig()
