"""台指期執行版：連續合約、換倉、固定口數的槓桿部位。

與 ETF 版的三個差異
-------------------
1. **換倉**：近月合約到期要換到次月。換倉當日以「近月收盤」平倉、
   「次月同日收盤」建倉，兩者的價差**不計入損益** —— 那是逆價差/正價差，
   不是賺賠。`build_continuous()` 就是在做這件事。

2. **槓桿由停損距離決定**：ETF 的槓桿是固定的（00631L 為 2 倍），期貨可以自己選。
   同樣的風險預算下，停損越近就能放越大：

       槓桿 L = 風險預算 ÷ 停損距離，上限 `max_leverage`

   注意這裡**不套用 `min_stop_distance` 下限** —— 那個下限是為了防止
   固定槓桿的 ETF 算出過大的水位；期貨改由槓桿上限承擔同樣的角色。
   若保留下限，L 會恆等於 8%/5% = 1.6 倍，槓桿上限永遠碰不到。

3. **進場後不調整口數**：權益變動時實際槓桿會自然漂移（賺錢時下降）。
   因此持有期間的權益是**線性**於期貨報酬，不是複利：

       權益(t) / 權益(進場) = 1 + L × (F(t)/F(進場) − 1)

4. **當日成交**：加權指數 13:30 收盤、台指期 13:45 收盤，中間有 15 分鐘。
   訊號以指數收盤判定後，還來得及在**當天的期貨收盤**成交，不必等到隔天。
   ETF 版沒有這個空間（同一時間收盤），只能次日成交。
   這個差別對停損特別關鍵 —— 隔夜跳空正是把「假設停損 1.5%」放大成
   「實際虧損 7.8%」的主因。以 `entries_from_trades(same_day=...)` 切換。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import StrategyConfig
from .levels import Levels

#: 台指期（TX）每點新台幣，小台（MTX）為 50
TX_POINT_VALUE = 200.0


@dataclass(frozen=True)
class FuturesCost:
    """期貨交易成本（以契約金額比例表示，單邊）。"""

    #: 期交稅：契約金額的十萬分之二
    tax_rate: float = 0.00002
    #: 手續費，每口新台幣。換算成比例時需要指數點位與每點價值
    commission_per_lot: float = 50.0
    point_value: float = TX_POINT_VALUE

    def one_way_rate(self, index_level: float) -> float:
        """單邊成本佔契約金額的比例。"""
        notional = index_level * self.point_value
        return self.tax_rate + self.commission_per_lot / notional


def build_continuous(dates: list[date], front_close: list[float],
                     contract: list[str],
                     next_close_on_roll: dict[date, float]) -> list[float]:
    """把近月連續序列接成「已還原換倉價差」的可交易序列。

    Args:
        dates / front_close / contract: 逐日的日期、近月收盤、該日所屬合約月。
        next_close_on_roll: {換倉日: 次月合約在該日的收盤}。換倉日指的是
            **舊合約的最後一天**，也就是 `contract` 即將改變的前一天。

    回傳與輸入等長的序列，起點對齊 `front_close[0]`。

    換倉日隔天的報酬改用「次日近月收盤 ÷ 次月在換倉日的收盤」計算，
    價差因此不落入損益。缺換倉價差資料時退回原始跳動，並在
    `missing_rolls()` 中可查出是哪幾天。
    """
    if not dates:
        return []
    out = [front_close[0]]
    for i in range(1, len(dates)):
        prev_is_roll = contract[i] != contract[i - 1]
        base = front_close[i - 1]
        if prev_is_roll:
            base = next_close_on_roll.get(dates[i - 1], front_close[i - 1])
        out.append(out[-1] * (front_close[i] / base) if base else out[-1])
    return out


def missing_rolls(dates: list[date], contract: list[str],
                  next_close_on_roll: dict[date, float]) -> list[date]:
    """列出缺少次月報價、只能沿用原始跳動的換倉日。"""
    return [dates[i - 1] for i in range(1, len(dates))
            if contract[i] != contract[i - 1] and dates[i - 1] not in next_close_on_roll]


def futures_leverage(entry_index: float, levels: Levels, cfg: StrategyConfig,
                     max_leverage: float = 5.0) -> float:
    """依停損距離決定槓桿倍數，上限 `max_leverage`。

    停損距離的定義與 ETF 版一致（依 `warn_derisk_fraction` 對停損線與失效線加權，
    停損線含主防線的假跌破緩衝 —— 用警戒線會低估距離、把槓桿放得過大），
    但**不套用 `min_stop_distance` 下限**，理由見模組說明。
    """
    to_warn = max(entry_index - levels.stop_line, 0.0) / entry_index
    to_trough = max(entry_index - levels.invalidation, 0.0) / entry_index
    f = cfg.exit.warn_derisk_fraction
    dist = f * to_warn + (1.0 - f) * to_trough
    if dist <= 0:
        return max_leverage
    return min(max_leverage, cfg.sizing.risk_per_trade / dist)


@dataclass(frozen=True)
class FuturesTrade:
    entry_date: date
    exit_date: date | None
    entry_index: float
    entry_futures: float
    exit_futures: float | None
    leverage: float
    stop_distance: float
    futures_return: float    # 期貨本身的報酬
    ret: float               # 權益報酬（已扣進出場成本）
    #: step-down 之後的槓桿（沒有觸發時等於 leverage）
    final_leverage: float | None = None
    #: step-down 觸發日（沒有觸發時為 None）
    step_date: date | None = None


@dataclass(frozen=True)
class FuturesEntry:
    """一筆待執行的期貨部位。索引對應 `dates` 的位置。"""

    entry_i: int
    exit_i: int | None       # None = 持有到資料最後一根
    leverage: float
    entry_index: float       # 進場當日的指數收盤
    stop_distance: float
    trade: object | None = None   # 產生這筆部位的引擎交易，供逐筆檢視用


def entries_from_trades(trades, bars, cfg: StrategyConfig,
                        max_leverage: float = 5.0,
                        same_day: bool = True) -> list["FuturesEntry"]:
    """把引擎的交易轉成期貨部位。

    引擎的 `Fill.d` 記的是**成交日**，而它的決策發生在前一根 K 的收盤。

    Args:
        same_day: True 代表當天期貨收盤成交（指數 13:30 收、期貨 13:45 收，
            中間 15 分鐘足夠下單），成交索引因此往前挪一根；
            False 則沿用引擎原本的隔日成交。
    """
    index_of = {b.d: i for i, b in enumerate(bars)}
    shift = 1 if same_day else 0
    out: list[FuturesEntry] = []
    for t in trades:
        if not t.fills:
            continue
        e_i = max(index_of[t.fills[0].d] - shift, 0)
        x_i = (max(index_of[t.fills[-1].d] - shift, 0)
               if len(t.fills) > 1 else None)
        if x_i is not None and x_i <= e_i:      # 同日進出場，視為未成立
            continue
        entry_index = t.setup.trigger_close
        out.append(FuturesEntry(
            entry_i=e_i, exit_i=x_i,
            leverage=futures_leverage(entry_index, t.levels, cfg, max_leverage),
            entry_index=entry_index,
            stop_distance=(entry_index - t.levels.stop_line) / entry_index,
            trade=t))
    return out


def _step_down(v: float, lev: float, anchor: float, f0: float, fut: float,
               rate: float, target: float) -> float:
    """把固定口數部位減到 `target` 倍：平掉多出來的名目，扣一次單邊成本。

    回傳扣完成本後的權益；呼叫端把錨點重設為 (v, 今日期貨, target)。
    """
    cur_notional = lev * anchor * fut / f0
    delta = max(cur_notional - target * v, 0.0)
    return v - delta * rate


def vehicle_series(dates: list[date], continuous: list[float],
                   entries: list[FuturesEntry], cost: FuturesCost,
                   index_close: list[float],
                   step_gain: float | None = None,
                   step_leverage: float = 1.0) -> tuple[list[float], list[FuturesTrade]]:
    """把「固定口數的槓桿期貨部位」攤成一條可餵給回測器的淨值序列。

    空手期間持平；持有期間依 `1 + L × (F/F_entry − 1)` **線性**變動
    （固定口數，不複利、不再平衡），並在進出場各扣一次單邊成本。

    成本按契約金額計算，所以扣在權益上的比例是 `L × 單邊成本率`。

    Args:
        step_gain: **step-down**（docs/strategy.md §22）。部位權益相對進場
            達 `1 + step_gain` 倍時（以期貨連續序列判定、當日期貨收盤成交），
            把口數減到 `step_leverage` 倍，之後不再加回。`None` 表示關閉。
            每筆最多觸發一次；出場日不觸發。
    """
    nav = [1.0] * len(dates)
    detail: list[FuturesTrade] = []
    by_entry = {e.entry_i: e for e in entries}

    equity = 1.0
    active: FuturesEntry | None = None
    eq_at_entry = anchor = f0 = lev = 0.0
    stepped = False
    step_d: date | None = None

    for i in range(len(dates)):
        if active is None:
            e = by_entry.get(i)
            if e is None:
                nav[i] = equity
                continue
            active = e
            eq_at_entry = equity
            lev = e.leverage
            anchor = equity * (1.0 - lev * cost.one_way_rate(index_close[i]))
            f0 = continuous[i]
            stepped, step_d = False, None
            nav[i] = anchor
            continue

        v = anchor * (1.0 + lev * (continuous[i] / f0 - 1.0))
        closing = active.exit_i is not None and i == active.exit_i
        if closing:
            v *= (1.0 - lev * cost.one_way_rate(index_close[i]))
        elif (step_gain is not None and not stepped
                and v >= eq_at_entry * (1.0 + step_gain) and lev > step_leverage):
            v = _step_down(v, lev, anchor, f0, continuous[i],
                           cost.one_way_rate(index_close[i]), step_leverage)
            anchor, f0, lev = v, continuous[i], step_leverage
            stepped, step_d = True, dates[i]
        nav[i] = v
        if closing:
            detail.append(FuturesTrade(
                entry_date=dates[active.entry_i], exit_date=dates[i],
                entry_index=active.entry_index, entry_futures=continuous[active.entry_i],
                exit_futures=continuous[i], leverage=active.leverage,
                stop_distance=active.stop_distance,
                futures_return=continuous[i] / continuous[active.entry_i] - 1.0,
                ret=v / eq_at_entry - 1.0,
                final_leverage=lev, step_date=step_d))
            equity, active = v, None

    if active is not None:      # 持有到最後一根，未平倉
        detail.append(FuturesTrade(
            entry_date=dates[active.entry_i], exit_date=None,
            entry_index=active.entry_index, entry_futures=continuous[active.entry_i],
            exit_futures=continuous[-1], leverage=active.leverage,
            stop_distance=active.stop_distance,
            futures_return=continuous[-1] / continuous[active.entry_i] - 1.0,
            ret=nav[-1] / eq_at_entry - 1.0,
            final_leverage=lev, step_date=step_d))
    return nav, detail


@dataclass(frozen=True)
class TradeDetail:
    """單筆期貨部位的完整檢視：報酬、期間極值，以及當初的進場條件。"""

    trade: FuturesTrade
    entry: FuturesEntry
    bars_held: int
    mfe: float            # 期間最大浮動獲利（權益，相對進場）
    mae: float            # 期間最大浮動虧損（權益，相對進場）
    max_drawdown: float   # 期間內從波段高點起算的最大回撤

    # --- 進場條件（來自訊號本身） ---
    @property
    def setup(self):
        return self.entry.trade.setup

    @property
    def levels(self):
        return self.entry.trade.levels

    @property
    def exit_reason(self) -> str:
        """出場說明。優先用出場那筆成交自己記的理由，比代碼具體。"""
        t = self.entry.trade
        if t.exit_reason == "open":
            return "尚未出場（持有中）"
        fills = t.fills
        if len(fills) > 1 and fills[-1].side == "sell":
            return fills[-1].reason
        return t.exit_reason

    @property
    def from_peak(self) -> float:
        """訊號日距離前高還有多遠（負值＝仍低於前高）。"""
        s = self.setup
        return s.trigger_close / s.peak - 1.0


def trade_details(dates: list[date], nav: list[float],
                  entries: list[FuturesEntry],
                  detail: list[FuturesTrade]) -> list[TradeDetail]:
    """把淨值序列切成逐筆部位，算出每筆的 MFE / MAE / 期間最大回撤。

    極值一律以**權益**衡量（已含槓桿與進場成本），所以數字就是帳戶當下看到的
    浮動盈虧，不是期貨本身的漲跌。
    """
    by_entry_date = {dates[e.entry_i]: e for e in entries}
    out: list[TradeDetail] = []
    for t in detail:
        e = by_entry_date.get(t.entry_date)
        if e is None:
            continue
        a = e.entry_i
        b = e.exit_i if e.exit_i is not None else len(dates) - 1
        seg = nav[a:b + 1]
        base = seg[0]
        if base <= 0:
            continue
        peak, dd = base, 0.0
        for x in seg:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1.0)
        out.append(TradeDetail(
            trade=t, entry=e, bars_held=b - a,
            mfe=max(seg) / base - 1.0,
            mae=min(seg) / base - 1.0,
            max_drawdown=dd))
    return out


def format_trade_details(details: list[TradeDetail]) -> str:
    """逐筆列出交易與當初的進場條件（中文，供 CLI 直接輸出）。"""
    lines: list[str] = []
    for n, d in enumerate(details, 1):
        t, s, lv = d.trade, d.setup, d.levels
        exit_txt = str(t.exit_date) if t.exit_date else "持有中（尚未平倉）"
        lines.append(f"[{n}] {t.entry_date} → {exit_txt}　持有 {d.bars_held} 個交易日")
        lines.append(
            f"    進場條件：{s.peak_date} 高點 {s.peak:,.0f} → {s.trough_date} 谷底 "
            f"{s.trough:,.0f}（回檔 {s.drop_pct:.1%}），"
            f"{s.bars_to_repair} 個交易日補回 {s.repair_fraction:.0%}")
        lines.append(
            f"              訊號日指數 {s.trigger_close:,.0f}（距前高 {d.from_peak:+.1%}）"
            f"　停損線 {lv.stop_line:,.0f}"
            + (f"（警戒線 {lv.warn_line:,.0f} 再扣假跌破緩衝）"
               if lv.stop_line < lv.warn_line else "（警戒線）")
            + f"　失效線 {lv.invalidation:,.0f}")
        lines.append(
            f"    距離停損 {t.stop_distance:.2%}　→　槓桿 "
            f"{t.leverage:.2f}x（風險預算 ÷ 停損距離，上限封頂）")
        lines.append(
            f"    期貨報酬 {t.futures_return:+.1%}　權益報酬 {t.ret:+.1%}"
            f"　最大報酬 {d.mfe:+.1%}　最大不利 {d.mae:+.1%}"
            f"　期間最大回撤 {d.max_drawdown:.1%}")
        lines.append(f"    出場原因：{d.exit_reason}")
        lines.append("")
    return "\n".join(lines)


def trend_filter(entries: list["FuturesEntry"], bars, ma_period: int = 200,
                 below: bool = True) -> list["FuturesEntry"]:
    """依進場日收盤與均線的位置過濾部位。

    `below=True` 只保留**收盤低於均線**的訊號。這與直覺相反，但這是逆勢策略：
    要求「站上均線才進場」會結構性排除最深的回檔，而深回檔正是報酬最好的場景
    （見 docs/strategy.md §18）。均線資料不足的日子一律排除。
    """
    from .engine import moving_average
    ma = moving_average(bars, ma_period)
    out = []
    for e in entries:
        m = ma[e.entry_i]
        if m is None:
            continue
        c = bars[e.entry_i].close
        if (c < m) if below else (c > m):
            out.append(e)
    return out


def fixed_leverage(entries: list["FuturesEntry"], leverage: float) -> list["FuturesEntry"]:
    """把所有部位改成同一個槓桿倍數，不再依停損距離決定。"""
    return [FuturesEntry(e.entry_i, e.exit_i, leverage, e.entry_index,
                         e.stop_distance, e.trade) for e in entries]


def hybrid_entries(entries: list["FuturesEntry"], bars, ma_period: int = 200,
                   below_leverage: float = 3.0, above_risk_scale: float = 0.5,
                   above_cap: float | None = None) -> list["FuturesEntry"]:
    """§20 混合注碼：按進場日與均線的相對位置切換槓桿規則。

        進場日收盤 < MA → 固定 `below_leverage`（深回檔是最好的機會，別壓小注）
        其餘           → 風險式槓桿 × `above_risk_scale`（反環境訊號只給一半預算），
                         可再以 `above_cap` 封頂

    均線資料不足的日子歸「其餘」—— 沒有濾網資訊時維持風險式，是保守的選擇。
    輸入的 `entries` 應來自 `entries_from_trades()`，其 `leverage` 即風險式槓桿。
    """
    from .engine import moving_average
    ma = moving_average(bars, ma_period)
    out = []
    for e in entries:
        m = ma[e.entry_i]
        if m is not None and bars[e.entry_i].close < m:
            lev = below_leverage
        else:
            lev = e.leverage * above_risk_scale
            if above_cap is not None:
                lev = min(lev, above_cap)
        out.append(FuturesEntry(e.entry_i, e.exit_i, lev, e.entry_index,
                                e.stop_distance, e.trade))
    return out


def entry_regime(bars, i: int, ma: list) -> str:
    """進場日 i 適用哪一種注碼：'below'（固定 3x）或 'above'（半預算風險式）。"""
    m = ma[i]
    return "below" if (m is not None and bars[i].close < m) else "above"


def core_state(index_close: list[float], ma: list[float | None],
               band: float = 0.0) -> list[bool]:
    """核心部位的開關序列（只看價格與均線，不看策略部位）。

    band = 0：收盤 > MA 即開、收盤 ≤ MA 即關（原始規則）。
    band > 0：**遲滯帶**——收盤 > MA×(1+band) 才開，之後要收盤 < MA×(1−band) 才關；
    介於兩者之間維持前一天的狀態。均線資料不足時一律關。
    docs/strategy.md §23：±2% 把 27 年的核心進出從 104 段減到 31 段，
    樣本內改善但未通過逐筆錨定 walk-forward，屬操作性選擇。
    """
    out: list[bool] = []
    on = False
    for c, m in zip(index_close, ma):
        if m is None:
            on = False
        elif band <= 0:
            on = c > m
        elif not on and c > m * (1.0 + band):
            on = True
        elif on and c < m * (1.0 - band):
            on = False
        out.append(on)
    return out


def core_overlay(dates: list[date], continuous: list[float],
                 entries: list["FuturesEntry"], cost: FuturesCost,
                 index_close: list[float], core_leverage: float,
                 ma: list[float | None],
                 step_gain: float | None = None,
                 step_leverage: float = 1.0,
                 band: float = 0.0) -> tuple[list[float], list[FuturesTrade], float]:
    """在 `vehicle_series` 之上，空手期間補一個低槓桿的核心部位。

    核心只在**策略沒有部位**且**收盤高於均線**時持有，策略一有訊號就先平掉核心。
    逆勢濾網（`trend_filter(below=True)`）只在收盤**低於**均線時進場，
    所以兩者天然互斥：低於均線做策略、高於均線抱核心，中間沒有重疊。

    時序與策略一致：`ma`/收盤在第 i 天收盤後判定，部位在當天期貨收盤建立，
    因此第 i 天的損益由**前一天**的判斷決定 —— 用當天判斷賺當天的報酬是前視偏誤。

    回傳 (淨值, 策略交易明細, 有部位的日子佔比)。核心部位不算成交易筆數。
    """
    on = core_state(index_close, ma, band)
    by_entry = {e.entry_i: e for e in entries}
    nav = [1.0] * len(dates)
    detail: list[FuturesTrade] = []
    equity, active, eq_at_entry, anchor, f0, lev = 1.0, None, 0.0, 0.0, 0.0, 0.0
    holding_core, exposed = False, 0
    stepped, step_d = False, None

    for i in range(len(dates)):
        if active is not None:
            v = anchor * (1.0 + lev * (continuous[i] / f0 - 1.0))
            exposed += 1
            closing = active.exit_i is not None and i == active.exit_i
            if closing:
                v *= 1.0 - lev * cost.one_way_rate(index_close[i])
            elif (step_gain is not None and not stepped
                    and v >= eq_at_entry * (1.0 + step_gain) and lev > step_leverage):
                v = _step_down(v, lev, anchor, f0, continuous[i],
                               cost.one_way_rate(index_close[i]), step_leverage)
                anchor, f0, lev = v, continuous[i], step_leverage
                stepped, step_d = True, dates[i]
            nav[i] = v
            if closing:
                f_entry = continuous[active.entry_i]
                detail.append(FuturesTrade(
                    entry_date=dates[active.entry_i], exit_date=dates[i],
                    entry_index=active.entry_index, entry_futures=f_entry,
                    exit_futures=continuous[i], leverage=active.leverage,
                    stop_distance=active.stop_distance,
                    futures_return=continuous[i] / f_entry - 1.0,
                    ret=v / eq_at_entry - 1.0,
                    final_leverage=lev, step_date=step_d))
                equity, active, holding_core = v, None, False
            continue

        if i > 0 and holding_core:                 # 昨收就持有核心 → 賺今天
            equity *= 1.0 + core_leverage * (continuous[i] / continuous[i - 1] - 1.0)
            exposed += 1

        e = by_entry.get(i)
        if e is not None:                          # 今收轉進策略部位
            if holding_core:
                equity *= 1.0 - core_leverage * cost.one_way_rate(index_close[i])
                holding_core = False
            active, eq_at_entry, lev = e, equity, e.leverage
            anchor = equity * (1.0 - lev * cost.one_way_rate(index_close[i]))
            f0 = continuous[i]
            stepped, step_d = False, None
            nav[i] = anchor
            exposed += 1
            continue

        if on[i] != holding_core:                  # 核心進出，各付一次成本
            equity *= 1.0 - core_leverage * cost.one_way_rate(index_close[i])
            holding_core = on[i]
        nav[i] = equity

    if active is not None:
        f_entry = continuous[active.entry_i]
        detail.append(FuturesTrade(
            entry_date=dates[active.entry_i], exit_date=None,
            entry_index=active.entry_index, entry_futures=f_entry,
            exit_futures=continuous[-1], leverage=active.leverage,
            stop_distance=active.stop_distance,
            futures_return=continuous[-1] / f_entry - 1.0,
            ret=nav[-1] / eq_at_entry - 1.0,
            final_leverage=lev, step_date=step_d))
    return nav, detail, exposed / len(dates)
