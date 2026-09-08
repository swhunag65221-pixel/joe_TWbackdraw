import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
import A2_diversify as A2
from A2_diversify import *
from A_giveback import CORE, Lab, load
from datetime import date

bars, fs = load(); lab = Lab(bars, fs, 0.08); HY = lab.hybrid(5.0, 0.5)
legs = {}
legs["TX新"] = A2.tx_leg(lab, HY, core=CORE)
legs["TX新+step"] = A2.tx_leg(lab, HY, core=CORE, step=(1.5,1.0))
legs["TX舊"] = A2.tx_leg(lab, lab.base)
legs["UPRO"] = A2.us_leg("gspc.csv","UPRO.csv","us_tuned")
legs["TQQQ"] = A2.us_leg("ixic.csv","TQQQ.csv","nq_robust")
legs["TQQQ_t"] = A2.us_leg("ixic.csv","TQQQ.csv","nq_tuned")
core_nav,_,_ = A2.tx_leg(lab, [], core=CORE)
common = sorted(set.intersection(*[set(legs[n][0]) for n in ["TX新","UPRO","TQQQ","TQQQ_t"]]))
common = [d for d in common if d >= A2.START]
D = common[1:]; yrs = (common[-1]-common[0]).days/365.25
R, LOCK = {}, {}
for nm,(c,l,_t) in legs.items():
    cc=[c[d] for d in common]; R[nm]=[cc[i]/cc[i-1]-1 for i in range(1,len(cc))]
    LOCK[nm]=[d in l for d in common[1:]]
cr=[core_nav[d] for d in common]; R_core=[cr[i]/cr[i-1]-1 for i in range(1,len(cr))]
NAMES=["TX新","UPRO","TQQQ"]

print("【A】TX 單腿的報酬集中度（與組合對照）")
def drop(nm, R0):
    r=list(R0[nm]); best=max(legs[nm][2], key=lambda t:t["ret"])
    a,b=best["entry_date"],best["exit_date"]
    for j,d in enumerate(D):
        if a < d <= (b or D[-1]): r[j]= R_core[j] if nm.startswith("TX") else 0.0
    return r,best
m0=metrics(R["TX新"],D,yrs)
print(f"  TX新 基準 CAGR {m0['cagr']:.1%} MDD {m0['mdd']:.1%} Calmar {m0['calmar']:.2f}")
r2,b=drop("TX新",R); m1=metrics(r2,D,yrs)
print(f"  剔除最佳筆（{b['entry_date']}→{b['exit_date']} {b['ret']:+.1%}）"
      f" CAGR {m1['cagr']:.1%} MDD {m1['mdd']:.1%} Calmar {m1['calmar']:.2f}"
      f"（{m1['calmar']-m0['calmar']:+.2f}）")
# 剔除前二
r3=list(R["TX新"]); tt=sorted(legs["TX新"][2], key=lambda t:-t["ret"])[:2]
for t in tt:
    for j,d in enumerate(D):
        if t["entry_date"] < d <= (t["exit_date"] or D[-1]): r3[j]=R_core[j]
m2=metrics(r3,D,yrs)
print(f"  剔除最佳兩筆　CAGR {m2['cagr']:.1%} MDD {m2['mdd']:.1%} Calmar {m2['calmar']:.2f}"
      f"（{m2['calmar']-m0['calmar']:+.2f}）")
print(f"    被剔除：" + "、".join(f'{t["entry_date"]}({t["ret"]:+.0%})' for t in tt))

print("\n【B】分散有沒有改善？逐指標前後段方向一致性（基準＝TX新 單腿，SIG 再平衡）")
jsp = next(j for j,d in enumerate(D) if d >= A2.SPLIT)
segs=[("前段2016–2020",0,jsp),("後段2021–2026",jsp,len(D))]
MIX=[("等權1/3",(1/3,1/3,1/3)),("TX50/25/25",(.5,.25,.25)),("TX40/30/30",(.4,.3,.3))]
keys=[("cagr","CAGR",1),("mdd","MDD",1),("vol","年化波動",-1),("sharpe","Sharpe",1),
      ("calmar","Calmar",1),("uw","最長水下",-1),("worst_m","最差月",1)]
for wl,w in MIX:
    print(f"  {wl}")
    print(f"    {'指標':<10}{'前段 TX新':>12}{'前段 組合':>12}{'後段 TX新':>12}{'後段 組合':>12}   判定")
    for k,lb,sign in keys:
        vals=[]
        for _,lo,hi in segs:
            y=(D[hi-1]-D[lo]).days/365.25
            vals.append(metrics(R["TX新"][lo:hi],D[lo:hi],y)[k])
            vals.append(metrics(signal_rebalance(R,LOCK,w,NAMES)[lo:hi],D[lo:hi],y)[k])
        d1=(vals[1]-vals[0])*sign; d2=(vals[3]-vals[2])*sign
        verdict = "一致改善" if d1>0 and d2>0 else ("一致變差" if d1<0 and d2<0 else "⚠️方向不一致")
        if abs(d1)<1e-9 or abs(d2)<1e-9: verdict = "一致（含持平）" if d1>=0 and d2>=0 else "⚠️方向不一致"
        f=(lambda v: f"{v:>12.0f}" if k=="uw" else f"{v:>12.1%}" if k in("cagr","mdd","vol","worst_m") else f"{v:>12.2f}")
        print(f"    {lb:<10}"+"".join(f(v) for v in vals)+f"   {verdict}")
    print()

print("【C】最佳『分散』組合（排除 TX100 / US50-50 兩個對照）")
best=None
for tag,fn in (("每日",lambda w,N: daily_rebalance(R,w,N)),
               ("SIG",lambda w,N: signal_rebalance(R,LOCK,w,N)),
               ("SIG-cap",lambda w,N: signal_rebalance(R,LOCK,w,N,cap=True))):
    for N in (NAMES,["TX新+step","UPRO","TQQQ"],["TX新","UPRO","TQQQ_t"]):
        for wl,w in MIX:
            m=metrics(fn(w,N),D,yrs)
            if best is None or m["calmar"]>best[1]["calmar"]: best=(f"{wl}／{tag}／{N[0]}+{N[2]}",m)
print(f"  {best[0]}")
print(f"    CAGR {best[1]['cagr']:.1%}  MDD {best[1]['mdd']:.1%}  Sharpe {best[1]['sharpe']:.2f}"
      f"  Calmar {best[1]['calmar']:.2f}  最長水下 {best[1]['uw']} 天  最差月 {best[1]['worst_m']:.1%}")
print(f"    距 CAGR 30%: {0.30-best[1]['cagr']:+.1%}   距 Calmar 4: {4-best[1]['calmar']:+.2f}")

print("\n【D】視窗折算：TX 腿在此視窗 vs 全期")
from A_giveback import metrics as fm
for nm,es,kw in (("TX新",HY,dict(core=CORE)),("TX新+step",HY,dict(core=CORE,step=(1.5,1.0))),("TX舊",lab.base,dict())):
    nav,det,ex=lab.nav(es,**kw); f=fm(lab.dates,nav,det,ex); win=metrics(R[nm],D,yrs)
    print(f"  {nm:<10} 視窗 CAGR {win['cagr']:>6.1%} / Calmar {win['calmar']:.2f}"
          f"　全期 CAGR {f['cagr']:>6.1%} / Calmar {f['calmar']:.2f}"
          f"　倍數 CAGR ×{win['cagr']/f['cagr']:.2f} Calmar ×{win['calmar']/f['calmar']:.2f}")
