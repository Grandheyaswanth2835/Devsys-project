"""
Demonstrates the document's techniques on the synthetic data:
  A. Risk-based adaptive test flow (Section 7)   - lot-split model, tier sweep, time vs DPPM
  B. March element pruning by set cover (Section 6)
  C. Power-capped session scheduling (Section 5) - naive vs length-balanced packing
Usage: python validate_mbist_methods.py --data data
"""
import argparse, itertools
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

ap = argparse.ArgumentParser(); ap.add_argument("--data", default="data"); a = ap.parse_args()
dies = pd.read_csv(f"{a.data}/die_results.csv")
fails = pd.read_csv(f"{a.data}/fail_events.csv")
mems = pd.read_csv(f"{a.data}/memories.csv")
rng = np.random.default_rng(1)
out = []
p = out.append

# ---------------- A. adaptive test flow ----------------
FEAT = ["radial_pos", "ro_speed_mhz", "leakage_na", "iddq_ua", "bitcell_vmin_mv", "early_mats_fails",
        "repaired_rows", "repaired_cols", "neighbor_fail_cnt"]
d = dies[dies.scan_pass & ~dies.unrepairable].copy()           # die still in flow at final-test MBIST
d["y"] = d.retention_fail | d.dynamic_fault_fail | d.vmin_corner_only_fail   # faults a reduced flow can miss
lots = sorted(d.lot_id.unique()); train_lots = lots[:12]
tr, te = d[d.lot_id.isin(train_lots)], d[~d.lot_id.isin(train_lots)].copy()
clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=0)
clf.fit(tr[FEAT], tr.y)
te["score"] = clf.predict_proba(te[FEAT])[:, 1]
auc = roc_auc_score(te.y, te.score)
p("A. RISK-BASED ADAPTIVE FLOW")
p(f"   train lots {train_lots[0]}..{train_lots[-1]}  ({len(tr)} die), test lots {lots[12]}..{lots[-1]} ({len(te)} die)")
p(f"   target prevalence (test): {te.y.mean()*100:.2f}%   ROC AUC on held-out lots: {auc:.3f}")

base_ms = dies.t_total_baseline_ms.mean()
P_SHORT_PAUSE_DETECT, P_PRUNED_DYN_DETECT = 0.5, 0.5
te["audit"] = rng.random(len(te)) < 0.02
rows = []
for low_frac in [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90]:
    q = te.score.rank(method="first", pct=True).values
    tier = np.where(te.audit, "audit", np.where(q <= low_frac, "low", np.where(q > 0.96, "high", "medium")))
    lo = tier == "low"
    t = te.t_march_ms + te.t_retention_ms + te.t_repair_ms + te.t_setup_readout_ms
    t = np.where(lo, 0.60 * te.t_march_ms + 0.30 * te.t_retention_ms + 0.70 * te.t_repair_ms + 0.67 * te.t_setup_readout_ms, t)
    t = np.where(tier == "high", t + 0.15 * (te.t_march_ms + te.t_retention_ms), t)
    esc = (lo & te.retention_fail) * (1 - P_SHORT_PAUSE_DETECT) + (lo & te.dynamic_fault_fail) * (1 - P_PRUNED_DYN_DETECT) \
        + (lo & te.vmin_corner_only_fail & ~te.retention_fail & ~te.dynamic_fault_fail) * 1.0
    shipped = (~te.y | lo).sum()
    rows.append(dict(low_tier_pct=round(100 * lo.mean(), 1), mean_ms=round(np.mean(t), 1),
                     saving_pct=round(100 * (1 - np.mean(t) / te.t_total_baseline_ms.mean()), 1),
                     expected_escapes=round(esc.sum(), 1), added_dppm=round(1e6 * esc.sum() / shipped, 0)))
sweep = pd.DataFrame(rows)
p("   tier sweep (audit 2%, high = top 4%; low tier: pruned March, short retention pause, fewer corners):")
p("   " + sweep.to_string(index=False).replace("\n", "\n   "))
ok = sweep[sweep.added_dppm <= 50]
if len(ok):
    best = ok.iloc[-1]
    p(f"   -> largest low tier within 50 DPPM budget: {best.low_tier_pct}% of die, {best.saving_pct}% MBIST time saved")
p(f"   (baseline full flow: {base_ms:.1f} ms/die; this model saves only on adaptive levers, not scheduling)")

# ---------------- B. element pruning ----------------
p("\nB. ALGORITHM PRUNING PER MEMORY CLASS (cheapest algorithm whose 95% upper escape bound <= 0.5%)")
ALGOS = {"MATSplus": 5, "MarchCm": 10, "MarchLR": 14, "MarchCm_RD": 15, "MarchSS": 22}
for cls, g in fails.groupby("bitcell"):
    n = len(g)
    for al, ops in ALGOS.items():
        miss = (~g[f"det_{al}"]).sum()
        ub = (miss + 2) / (n + 4) + 1.645 * np.sqrt((miss + 2) * (n - miss + 2) / (n + 4) ** 3)  # Agresti-Coull style bound
        if ub <= 0.005:
            missed = g[~g[f"det_{al}"]].fault_type.value_counts().to_dict()
            p(f"   {cls}: {n} fail events -> {al} = {ops}N vs MarchSS 22N ({100*(1-ops/22):.0f}% fewer ops); "
              f"observed escapes {miss} ({100*miss/n:.2f}%), 95% upper bound {100*ub:.2f}%, missed {missed or 'none'}")
            p(f"      implied escape upper bound: {1e6*ub*n/len(dies):.0f} DPPM (shrinks as more fail data is logged)")
            break
    tab = {al: f"{100*g[f'det_{al}'].mean():.2f}%" for al in ALGOS}
    p(f"      coverage of each candidate: {tab}")

# ---------------- C. scheduling ----------------
p("\nC. POWER-CAPPED SESSION SCHEDULING (March SS time per memory, one memory per controller per session)")
m = mems.copy(); cap = 0.08 * m.test_power_mw.sum()
def schedule(order):
    sessions = []  # each: [power, set(ctrl), max_time]
    for _, r in order.iterrows():
        for s in sessions:
            if s[0] + r.test_power_mw <= cap and r.controller_id not in s[1]:
                s[0] += r.test_power_mw; s[1].add(r.controller_id); s[2] = max(s[2], r.march_ss_time_us); break
        else:
            sessions.append([r.test_power_mw, {r.controller_id}, r.march_ss_time_us])
    return len(sessions), sum(s[2] for s in sessions) / 1000
n1, t1 = schedule(m.sort_values("mem_id"))
n2, t2 = schedule(m.sort_values("march_ss_time_us", ascending=False))
p(f"   power cap {cap:.0f} mW (8% of all-on power)")
p(f"   naive (inventory order) first-fit : {n1} sessions, {t1:.1f} ms")
p(f"   length-sorted first-fit decreasing: {n2} sessions, {t2:.1f} ms  ({100*(1-t2/t1):.0f}% shorter)")
p("   (a learned GNN/RL policy + ILP refinement, Section 5, would start from the second schedule)")

text = "\n".join(out); print(text)
open(f"{a.data}/../validation_report.txt", "w").write(text + "\n")
