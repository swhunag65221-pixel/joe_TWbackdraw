#!/usr/bin/env python3
"""第二輪：跨市場分散（TX 目前最佳版本 × UPRO × TQQQ）。

與 scripts/allocation_study.py 的差異：
  1. TX 腿換成目前最佳版本（混合半預算＋核心 0.5x），並列 step-down 變體。
  2. TQQQ 以 nq_robust 為主（nq_tuned 併列，config.py 註解說它是過擬合的尖峰）。
  3. 權重**只用預先宣告的固定組合**，不做任何最佳化（舊研究自己警告過權重會翻轉）。
  4. 再平衡兩種：每日（上界）與訊號再平衡（實務可達成）。
  5. 加上最長水下期間、最差月份、留一法報酬集中度。
"""
from __future__ import annotations
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent

import itertools
import math
import statistics as st
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(_LAB))
from A_giveback import CORE, Lab, load                          # noqa: E402

ROOT = Path(str(_ROOT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from tw_backdraw import load_csv                                # noqa: E402
from tw_backdraw.backtest import run_backtest                   # noqa: E402
from tw_backdraw.config import PRESETS                          # noqa: E402

START = date(2016, 1, 4)
SPLIT = date(2021, 1, 1)


# --------------------------------------------------------------------------
# 腿的建構
# --------------------------------------------------------------------------
def tx_leg(lab, entries, **kw):
    """回傳 (nav dict, 鎖定日集合, 逐筆交易)。

    「鎖定」= 策略部位在場（固定口數，中途不能砍）。
    核心 0.5x 依 §19 是**每日再平衡**的疊加，資金隨時可調度，因此不算鎖定。
    """
    nav, det, _ = lab.nav(entries, **kw)
    locked = set()
    for e in entries:
        b = e.exit_i if e.exit_i is not None else len(lab.dates) - 1
        locked.update(lab.dates[e.entry_i:b + 1])
    return dict(zip(lab.dates, nav)), locked, det


def us_leg(index_csv, etf_csv, preset):
    idx = load_csv(ROOT / 'data' / index_csv)
    ep = {b.d: b.close for b in load_csv(ROOT / 'data' / etf_csv)}
    bs = [b for b in idx if b.d in ep]
    r, _ = run_backtest(bs, PRESETS[preset], [ep[b.d] for b in bs])
    curve = dict(r.equity_curve)
    ecd = [d for d, _ in r.equity_curve]
    locked, trades = set(), []
    for t in r.trades:
        if t.entry_date is None:
            continue
        a = ecd.index(t.entry_date)
        b = ecd.index(t.exit_date) if t.exit_date in ecd else len(ecd) - 1
        locked.update(ecd[a:b + 1])
        trades.append(dict(entry_date=t.entry_date, exit_date=ecd[b],
                           ret=t.ret, a=a, b=b))
    return curve, locked, trades


# --------------------------------------------------------------------------
# 指標
# --------------------------------------------------------------------------
def path_of(rets):
    eq, p = 1.0, [1.0]
    for x in rets:
        eq *= 1 + x
        p.append(eq)
    return p


def metrics(rets, dates, years=None):
    """dates 對齊 rets（dates[i] 是 rets[i] 的日期）。"""
    p = path_of(rets)
    years = years or ((dates[-1] - dates[0]).days / 365.25)
    peak = dd = 0.0
    uw = uw_max = 0
    for x in p:
        if x >= peak:
            peak, uw = x, 0
        else:
            uw += 1
            uw_max = max(uw_max, uw)
        dd = min(dd, x / peak - 1.0) if peak else dd
    sd = st.pstdev(rets) if len(rets) > 1 else 0.0
    cagr = p[-1] ** (1 / years) - 1 if p[-1] > 0 else -1.0
    by_m: dict = {}
    for d, x in zip(dates, rets):
        k = (d.year, d.month)
        by_m[k] = by_m.get(k, 1.0) * (1 + x)
    worst_m = min(by_m.values()) - 1 if by_m else 0.0
    return dict(cagr=cagr, mdd=dd, vol=sd * math.sqrt(252),
                sharpe=st.fmean(rets) / sd * math.sqrt(252) if sd else 0.0,
                calmar=cagr / abs(dd) if dd else 0.0,
                uw=uw_max, worst_m=worst_m, total=p[-1] - 1)


HEAD = (f'{"配置":<30}{"CAGR":>8}{"MDD":>9}{"年化波動":>10}{"Sharpe":>8}'
        f'{"Calmar":>8}{"最長水下":>10}{"最差月":>9}')


def row(nm, m):
    return (f'{nm:<30}{m["cagr"]:>8.1%}{m["mdd"]:>9.1%}{m["vol"]:>10.1%}'
            f'{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}{m["uw"]:>8} 天'
            f'{m["worst_m"]:>9.1%}')


def corr(a, b):
    ma, mb = st.fmean(a), st.fmean(b)
    sa, sb = st.pstdev(a), st.pstdev(b)
    return (sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (sa * sb)
            if sa and sb else 0.0)


# --------------------------------------------------------------------------
# 再平衡引擎
# --------------------------------------------------------------------------
def daily_rebalance(R, w, names):
    """每日再平衡（上界）：每天的組合報酬 = Σ wᵢ × rᵢ。"""
    n = len(R[names[0]])
    return [sum(w[i] * R[nm][j] for i, nm in enumerate(names))
            for j in range(n)]


def signal_rebalance(R, LOCK, w, names, cap=False):
    """訊號再平衡：只在某腿的策略部位出場（鎖定解除）當日重算，持有中不動。

    cap=False（主口徑，SIG）：把「未鎖定各腿的資金」按目標權重的比例
        重新分配給未鎖定的腿。鎖定中的腿完全不碰，也不加碼。
    cap=True（SIG-cap）：目標改用**當下總資產** × wᵢ 封頂，超出的部分
        變成閒置現金（報酬 0），等下一次事件再投入 —— 這是「不砍跑中部位」
        在總資產口徑下的直接代價。
    """
    k = len(names)
    cap_i = [w[i] for i in range(k)]          # 各腿資金
    idle = 0.0
    out = []
    n = len(R[names[0]])
    prev_lock = [True] * k                    # 迫使第 0 天做一次初始配置
    for j in range(n):
        tot0 = sum(cap_i) + idle
        for i, nm in enumerate(names):
            cap_i[i] *= 1 + R[nm][j]
        tot1 = sum(cap_i) + idle
        out.append(tot1 / tot0 - 1 if tot0 > 0 else 0.0)

        lock = [LOCK[nm][j] for nm in names]
        event = any(prev_lock[i] and not lock[i] for i in range(k)) or j == 0
        prev_lock = lock
        if not event:
            continue

        free = [i for i in range(k) if not lock[i]]
        if not free:
            continue
        pool = sum(cap_i[i] for i in free) + idle
        wsum = sum(w[i] for i in free)
        if wsum <= 0:
            continue
        if not cap:
            for i in free:
                cap_i[i] = pool * w[i] / wsum
            idle = 0.0
        else:
            total = sum(cap_i) + idle
            tgt = {i: w[i] * total for i in free}
            need = sum(tgt.values())
            if need <= pool:
                for i in free:
                    cap_i[i] = tgt[i]
                idle = pool - need
            else:
                for i in free:
                    cap_i[i] = pool * tgt[i] / need
                idle = 0.0
    return out


def main():
    bars, fs = load()
    lab = Lab(bars, fs, 0.08)
    HY = lab.hybrid(5.0, 0.5)

    legs = {}
    legs["TX新"] = tx_leg(lab, HY, core=CORE)
    legs["TX新+step"] = tx_leg(lab, HY, core=CORE, step=(1.5, 1.0))
    legs["TX舊"] = tx_leg(lab, lab.base)              # allocation.md 用的那條
    legs["UPRO"] = us_leg("gspc.csv", "UPRO.csv", "us_tuned")
    legs["TQQQ"] = us_leg("ixic.csv", "TQQQ.csv", "nq_robust")
    legs["TQQQ_t"] = us_leg("ixic.csv", "TQQQ.csv", "nq_tuned")

    core_nav, _, _ = tx_leg(lab, [], core=CORE)       # 只有核心，供留一法用

    NAMES = ["TX新", "UPRO", "TQQQ"]
    common = sorted(set.intersection(*[set(legs[n][0]) for n in
                                       ["TX新", "UPRO", "TQQQ", "TQQQ_t"]]))
    common = [d for d in common if d >= START]
    D = common[1:]                                     # 報酬對齊的日期
    yrs = (common[-1] - common[0]).days / 365.25

    R, LOCK = {}, {}
    for nm, (curve, locked, _t) in legs.items():
        c = [curve[d] for d in common]
        R[nm] = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
        LOCK[nm] = [d in locked for d in common[1:]]
    cr = [core_nav[d] for d in common]
    R_core = [cr[i] / cr[i - 1] - 1 for i in range(1, len(cr))]

    print(f"共同期間 {common[0]} ~ {common[-1]}"
          f"（{len(common)} 天，{yrs:.1f} 年）\n")

    # ---------------------------------------------------------------- 1
    print("=" * 130)
    print("【1】各腿單獨表現（此視窗）\n")
    print(HEAD)
    for nm in ["TX新", "TX新+step", "TX舊", "UPRO", "TQQQ", "TQQQ_t"]:
        print(row(nm + ("（nq_tuned）" if nm == "TQQQ_t" else ""),
                  metrics(R[nm], D, yrs)))
    print("\n  對照：TX 腿的全期（1999–2026）數字")
    from A_giveback import metrics as fullm
    nav, det, expo = lab.nav(HY, core=CORE)
    m = fullm(lab.dates, nav, det, expo)
    print(f"    TX新 全期 CAGR {m['cagr']:.1%}　MDD {m['mdd']:.1%}"
          f"　Sharpe {m['sharpe']:.2f}　Calmar {m['calmar']:.2f}")
    nav2, det2, expo2 = lab.nav(lab.base)
    m2 = fullm(lab.dates, nav2, det2, expo2)
    print(f"    TX舊 全期 CAGR {m2['cagr']:.1%}　MDD {m2['mdd']:.1%}"
          f"　Sharpe {m2['sharpe']:.2f}　Calmar {m2['calmar']:.2f}")

    # ---------------------------------------------------------------- 2
    print("\n" + "=" * 130)
    print("【2】日報酬相關矩陣\n")
    CN = ["TX新", "TX新+step", "TX舊", "UPRO", "TQQQ", "TQQQ_t"]
    print("           " + "".join(f"{n:>11}" for n in CN))
    for a in CN:
        print(f"{a:<11}" + "".join(f"{corr(R[a], R[b]):>11.2f}" for b in CN))

    print("\n  在場（策略部位鎖定）比例與重疊")
    for nm in NAMES:
        print(f"    {nm:<8}{sum(LOCK[nm]) / len(D):>6.0%}")
    for a, b in itertools.combinations(NAMES, 2):
        both = sum(1 for j in range(len(D)) if LOCK[a][j] and LOCK[b][j])
        print(f"    {a} + {b} 同時在場 {both / len(D):>5.0%}")
    allin = sum(1 for j in range(len(D)) if all(LOCK[n][j] for n in NAMES))
    none = sum(1 for j in range(len(D)) if not any(LOCK[n][j] for n in NAMES))
    print(f"    三者同時在場 {allin/len(D):.0%}　三者皆無策略部位 {none/len(D):.0%}"
          f"（TX 這段時間仍有核心 0.5x）")

    # ---------------------------------------------------------------- 3
    MIXES = [("TX 100（對照）", (1.0, 0.0, 0.0)),
             ("US 50/50（對照）", (0.0, 0.5, 0.5)),
             ("等權 1/3", (1 / 3, 1 / 3, 1 / 3)),
             ("TX 50 / UPRO 25 / TQQQ 25", (0.5, 0.25, 0.25)),
             ("TX 40 / UPRO 30 / TQQQ 30", (0.4, 0.3, 0.3))]

    print("\n" + "=" * 130)
    print("【3】固定權重 × 兩種再平衡（權重預先宣告，未做任何最佳化）\n")
    for tag, fn in (("每日再平衡（上界，期貨整數口數做不到）",
                     lambda w: daily_rebalance(R, w, NAMES)),
                    ("訊號再平衡 SIG（只在策略部位出場時重配未鎖定的腿）",
                     lambda w: signal_rebalance(R, LOCK, w, NAMES)),
                    ("訊號再平衡 SIG-cap（總資產口徑封頂，超出留現金）",
                     lambda w: signal_rebalance(R, LOCK, w, NAMES, cap=True))):
        print(f"  {tag}")
        print("  " + HEAD)
        for nm, w in MIXES:
            print("  " + row(nm, metrics(fn(w), D, yrs)))
        print()

    # ---------------------------------------------------------------- 4
    print("=" * 130)
    print("【4】視窗內前後段（2016–2020 ／ 2021–2026）\n")
    jsplit = next(j for j, d in enumerate(D) if d >= SPLIT)
    for lo, hi, lab_p in ((0, jsplit, f"前段 {D[0]} ~ {D[jsplit-1]}"),
                          (jsplit, len(D), f"後段 {D[jsplit]} ~ {D[-1]}")):
        y = (D[hi - 1] - D[lo]).days / 365.25
        print(f"  {lab_p}（{y:.1f} 年）")
        print("  " + HEAD)
        for nm in ["TX新", "UPRO", "TQQQ"]:
            print("  " + row(f"單腿 {nm}", metrics(R[nm][lo:hi], D[lo:hi], y)))
        for nm, w in MIXES[2:]:
            r = signal_rebalance(R, LOCK, w, NAMES)[lo:hi]
            print("  " + row(nm + "（SIG）", metrics(r, D[lo:hi], y)))
        for nm, w in MIXES[2:]:
            r = daily_rebalance(R, w, NAMES)[lo:hi]
            print("  " + row(nm + "（每日）", metrics(r, D[lo:hi], y)))
        print()

    # ---------------------------------------------------------------- 5
    print("=" * 130)
    print("【5】留一法：剔除各腿最好的一筆交易（報酬集中度）\n")
    print("  剔除方式：該筆交易的在場日改成『沒有這個訊號』——")
    print("  TX 腿換成同期間的純核心 0.5x 報酬；美股腿換成現金（報酬 0）。\n")

    def drop_best(nm):
        """回傳剔除該腿最佳交易後的日報酬序列，以及被剔除的那筆。"""
        r = list(R[nm])
        dmap = {d: j for j, d in enumerate(D)}
        if nm == "TX新":
            best = max(legs[nm][2], key=lambda t: t["ret"])
            a, b = best["entry_date"], best["exit_date"]
            for j, d in enumerate(D):
                if a < d <= (b or D[-1]):
                    r[j] = R_core[j]
            return r, f'{a} → {b}　{best["ret"]:+.1%}'
        best = max(legs[nm][2], key=lambda t: t["ret"])
        a, b = best["entry_date"], best["exit_date"]
        for j, d in enumerate(D):
            if a < d <= b:
                r[j] = 0.0
        return r, f'{a} → {b}　{best["ret"]:+.1%}'

    base_w = (1 / 3, 1 / 3, 1 / 3)
    for wlab, w in (("等權 1/3", base_w), ("TX 50/25/25", (.5, .25, .25))):
        b_sig = metrics(signal_rebalance(R, LOCK, w, NAMES), D, yrs)
        b_day = metrics(daily_rebalance(R, w, NAMES), D, yrs)
        print(f"  {wlab}　基準：SIG Calmar {b_sig['calmar']:.2f}"
              f"（CAGR {b_sig['cagr']:.1%} / MDD {b_sig['mdd']:.1%}）"
              f"　每日 Calmar {b_day['calmar']:.2f}")
        for nm in NAMES:
            r2, desc = drop_best(nm)
            R2 = dict(R)
            R2[nm] = r2
            s = metrics(signal_rebalance(R2, LOCK, w, NAMES), D, yrs)
            dd = metrics(daily_rebalance(R2, w, NAMES), D, yrs)
            print(f"    剔除 {nm:<6} 最佳筆（{desc}）")
            print(f"        SIG　 CAGR {s['cagr']:>6.1%}"
                  f"　MDD {s['mdd']:>6.1%}　Calmar {s['calmar']:>5.2f}"
                  f"（{s['calmar']-b_sig['calmar']:+.2f}）")
            print(f"        每日　CAGR {dd['cagr']:>6.1%}"
                  f"　MDD {dd['mdd']:>6.1%}　Calmar {dd['calmar']:>5.2f}"
                  f"（{dd['calmar']-b_day['calmar']:+.2f}）")
        # 同時剔除三腿最佳
        R2 = dict(R)
        for nm in NAMES:
            R2[nm] = drop_best(nm)[0]
        s = metrics(signal_rebalance(R2, LOCK, w, NAMES), D, yrs)
        print(f"    三腿最佳筆全部剔除　SIG CAGR {s['cagr']:.1%}"
              f"　MDD {s['mdd']:.1%}　Calmar {s['calmar']:.2f}"
              f"（{s['calmar']-b_sig['calmar']:+.2f}）\n")

    # ---------------------------------------------------------------- 6
    print("=" * 130)
    print("【6】TX 腿換成 step-down 變體（其餘不變）\n")
    N2 = ["TX新+step", "UPRO", "TQQQ"]
    for tag, fn in (("每日", lambda w: daily_rebalance(R, w, N2)),
                    ("SIG", lambda w: signal_rebalance(R, LOCK, w, N2))):
        print(f"  {tag}")
        print("  " + HEAD)
        for nm, w in MIXES[2:]:
            print("  " + row(nm, metrics(fn(w), D, yrs)))
        print()

    print("  TQQQ 改用 nq_tuned（過擬合尖峰，僅供對照）")
    N3 = ["TX新", "UPRO", "TQQQ_t"]
    print("  " + HEAD)
    for nm, w in MIXES[2:]:
        print("  " + row(nm + "（SIG）",
                         metrics(signal_rebalance(R, LOCK, w, N3), D, yrs)))

    # ---------------------------------------------------------------- 7
    print("\n" + "=" * 130)
    print("【7】目標檢核：CAGR > 30% 且 Calmar > 4？\n")
    best = None
    for tag, fn in (("每日", lambda w, N: daily_rebalance(R, w, N)),
                    ("SIG", lambda w, N: signal_rebalance(R, LOCK, w, N)),
                    ("SIG-cap", lambda w, N: signal_rebalance(R, LOCK, w, N,
                                                             cap=True))):
        for N in (NAMES, N2, N3):
            for nm, w in MIXES:
                m = metrics(fn(w, N), D, yrs)
                lab_s = f"{nm}／{tag}／{N[0]}+{N[2]}"
                if best is None or m["calmar"] > best[1]["calmar"]:
                    best = (lab_s, m)
    print(f"  此視窗最高 Calmar：{best[0]}")
    print(f"    CAGR {best[1]['cagr']:.1%}　MDD {best[1]['mdd']:.1%}"
          f"　Sharpe {best[1]['sharpe']:.2f}　Calmar {best[1]['calmar']:.2f}")
    print(f"    距 CAGR 30%：{0.30 - best[1]['cagr']:+.1%}"
          f"　距 Calmar 4：{4 - best[1]['calmar']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
