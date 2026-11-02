"""
Synthetic data generator for "AI-Optimized MBIST Test Time Reduction".

Produces three linked tables (all values are SYNTHETIC, for method development only):
  memories.csv     - memory inventory of one SoC (~1,200 instances, 64 controllers)
  die_results.csv  - per-die parametric + MBIST outcomes across lots/wafers
  fail_events.csv  - per failing memory: fault type and which March SS elements detect it

Usage:  python generate_mbist_data.py --seed 7 --lots 20 --wafers 12 --out data
"""
import argparse, os
import numpy as np
import pandas as pd

# candidate algorithms, cheapest first: name -> operations per address (xN)
ALGOS = {"MATS+": 5, "MarchC-": 10, "MarchC-_RD": 15, "MarchLR": 14, "MarchSS": 22}
# MarchC-_RD = March C- plus one targeted read-disturb element (r,r,w,r) -- the Section 6.3 example

# fault type -> probability each candidate algorithm detects it (same order as ALGOS)
FAULT_DETECT = {
    "SAF":  [1.00, 1.00, 1.00, 1.00, 1.00],   # stuck-at
    "TF":   [0.50, 1.00, 1.00, 1.00, 1.00],   # transition
    "ADF":  [0.85, 1.00, 1.00, 1.00, 1.00],   # address decoder
    "CFid": [0.20, 0.999, 0.999, 1.00, 1.00], # idempotent coupling
    "CFin": [0.30, 0.999, 0.999, 1.00, 1.00], # inversion coupling
    "RDF":  [0.05, 0.15, 0.99, 0.60, 1.00],   # read-destructive (needs repeated reads)
    "DRDF": [0.00, 0.05, 0.85, 0.40, 1.00],   # deceptive read-destructive (dynamic)
    "WDF":  [0.00, 0.20, 0.60, 0.50, 1.00],   # write-disturb
}
FAULT_MIX = {  # relative frequency by bitcell flavour: HC = mature high-current, HD = high-density
    "HC": {"SAF": .45, "TF": .22, "ADF": .08, "CFid": .12, "CFin": .08, "RDF": .040, "DRDF": .001, "WDF": .002},
    "HD": {"SAF": .30, "TF": .18, "ADF": .06, "CFid": .12, "CFin": .08, "RDF": .13, "DRDF": .08, "WDF": .05},
}
BACKGROUNDS = ["solid", "checkerboard", "row_stripe", "col_stripe"]
CORNERS = ["Vmin_hot", "Vmin_cold", "Vnom_hot", "Vmax_cold"]


def make_memories(rng, n=1200, n_ctrl=64):
    kinds = rng.choice(["SRAM_SP", "SRAM_DP", "RF_2P", "ROM"], n, p=[.58, .10, .27, .05])
    rows = []
    # controller sites on an 8x8 grid over a 10 x 10 mm die
    cx, cy = np.meshgrid(np.linspace(0.6, 9.4, 8), np.linspace(0.6, 9.4, 8))
    sites = np.c_[cx.ravel(), cy.ravel()]
    for i, k in enumerate(kinds):
        if k in ("SRAM_SP", "SRAM_DP"):
            words = int(rng.choice([512, 1024, 2048, 4096, 8192], p=[.24, .36, .26, .11, .03]))
            bits = int(rng.choice([32, 39, 64, 72, 128, 144], p=[.20, .15, .25, .20, .12, .08]))
            ports = 1 if k == "SRAM_SP" else 2
            bitcell = rng.choice(["HD", "HC"], p=[.65, .35])
            clk = int(rng.choice([800, 1200, 1600]))
        elif k == "RF_2P":
            words = int(rng.choice([32, 64, 128, 256, 512])); bits = int(rng.choice([16, 32, 64, 72]))
            ports, bitcell, clk = 2, "HC", int(rng.choice([1600, 2000]))
        else:
            words = int(rng.choice([1024, 4096, 8192])); bits = 32; ports, bitcell, clk = 1, "ROM", 800
        x, y = rng.uniform(0.2, 9.8, 2)
        ctrl = int(np.argmin(((sites - [x, y]) ** 2).sum(1)))
        total_bits = words * bits
        repair = k in ("SRAM_SP", "SRAM_DP") and total_bits >= 64 * 1024
        rows.append(dict(
            mem_id=f"MEM{i:04d}", mem_type=k, bitcell=bitcell, words=words, width_bits=bits,
            total_bits=total_bits, ports=ports, test_clock_mhz=clk,
            test_power_mw=round(0.0045 * np.sqrt(total_bits) * clk / 1000 * ports * rng.uniform(.85, 1.15), 3),
            x_mm=round(x, 3), y_mm=round(y, 3),
            power_domain=f"PD{int(x // 2.5) + 4 * int(y // 5)}",
            irdrop_zone=f"Z{int(x // 2.5)}{int(y // 2.5)}",
            controller_id=f"CTRL{ctrl:02d}",
            has_repair=repair, spare_rows=2 if repair else 0, spare_cols=2 if repair and total_bits >= 256 * 1024 else (1 if repair else 0),
        ))
    m = pd.DataFrame(rows)
    # baseline March SS cycles per memory (22N x backgrounds x port passes)
    m["march_ss_cycles"] = 22 * m.words * 2 * m.ports
    m["march_ss_time_us"] = (m.march_ss_cycles / m.test_clock_mhz).round(2)
    return m


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


def make_dies(rng, lots, wafers, radius=10):
    gx, gy = np.meshgrid(np.arange(-radius, radius + 1), np.arange(-radius, radius + 1))
    on = gx ** 2 + gy ** 2 <= radius ** 2
    gx, gy = gx[on], gy[on]
    frames = []
    for L in range(lots):
        lot_p, lot_d = rng.normal(0, .6), rng.normal(0, .35)
        for W in range(wafers):
            n = len(gx)
            waf_p, waf_d = lot_p + rng.normal(0, .3), lot_d + rng.normal(0, .25)
            r = np.sqrt(gx ** 2 + gy ** 2) / radius
            # 0-2 defect clusters (scratch/particle blobs) per wafer
            cluster = np.zeros(n)
            for _ in range(rng.poisson(1.0)):
                c = rng.uniform(-radius * .8, radius * .8, 2); s = rng.uniform(1.2, 3.0)
                cluster += rng.uniform(1.0, 2.2) * np.exp(-((gx - c[0]) ** 2 + (gy - c[1]) ** 2) / (2 * s ** 2))
            z_proc = waf_p + 0.35 * r ** 2 + rng.normal(0, .5, n)          # + = fast/leaky
            d = waf_d + 1.4 * r ** 6 + cluster + rng.normal(0, .65, n)     # latent defectivity
            frames.append(pd.DataFrame(dict(
                lot_id=f"LOT{L + 1:02d}", wafer_id=f"LOT{L + 1:02d}-W{W + 1:02d}",
                die_x=gx, die_y=gy, radial_pos=r.round(3), _zp=z_proc, _d=d)))
    df = pd.concat(frames, ignore_index=True)
    n = len(df)
    zp, d = df.pop("_zp").values, df.pop("_d").values

    # parametric measurements
    df["ro_speed_mhz"] = (520 + 22 * zp + rng.normal(0, 6, n)).round(1)
    df["leakage_na"] = np.exp(4.2 + 0.45 * zp + rng.normal(0, .25, n)).round(1)
    df["iddq_ua"] = (0.9 * df.leakage_na + rng.normal(0, 8, n)).clip(5).round(1)
    df["bitcell_vmin_mv"] = (612 - 14 * zp + 11 * d + rng.normal(0, 9, n)).round(1)
    df["scan_pass"] = rng.random(n) > sigmoid(-4.6 + 0.9 * d)

    zl = (np.log(df.leakage_na) - 4.2) / 0.5
    zv = (df.bitcell_vmin_mv - 612) / 18
    # ground-truth MBIST outcomes
    n_march = rng.poisson(np.exp(-3.1 + 1.05 * d))                       # failing memories found by March
    ret_fail = rng.random(n) < sigmoid(-11.4 + 3.4 * zl + 1.2 * zv + 0.4 * d)   # weak cells leak -> retention
    dyn_fail = rng.random(n) < sigmoid(-10.8 + 2.6 * zv + 0.8 * d)              # needs repeated-read elements
    corner_fail = rng.random(n) < sigmoid(-11.2 + 3.6 * zv)                     # fails only at Vmin corner
    unrep = (n_march > 0) & (rng.random(n) < 1 - np.exp(-0.18 * n_march ** 1.6))

    df["march_failing_mems"] = n_march
    df["early_mats_fails"] = rng.binomial(n_march, 0.7)                  # wafer-sort MATS+ (partial view)
    df["repaired_rows"] = np.where(unrep, 0, rng.binomial(n_march * 2, 0.6))
    df["repaired_cols"] = np.where(unrep, 0, rng.binomial(n_march, 0.5))
    df["retention_fail"] = ret_fail
    df["dynamic_fault_fail"] = dyn_fail
    df["vmin_corner_only_fail"] = corner_fail
    df["unrepairable"] = unrep

    # neighbour feature: failing neighbours (any MBIST fail at wafer sort view)
    seen = (df.early_mats_fails > 0) | ~df.scan_pass
    key = dict(zip(zip(df.wafer_id, df.die_x, df.die_y), seen))
    df["neighbor_fail_cnt"] = [
        sum(key.get((w, x + a, y + b), False) for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b)
        for w, x, y in zip(df.wafer_id, df.die_x, df.die_y)]

    def bin_of(row):
        if not row.scan_pass: return "FAIL_SCAN"
        if row.unrepairable: return "FAIL_UNREPAIRABLE"
        if row.retention_fail: return "FAIL_RETENTION"
        if row.dynamic_fault_fail: return "FAIL_DYNAMIC"
        if row.vmin_corner_only_fail: return "FAIL_VMIN_CORNER"
        if row.march_failing_mems > 0: return "PASS_REPAIRED"
        return "PASS"
    df["final_bin"] = df.apply(bin_of, axis=1)

    # baseline (full-flow) MBIST time per die at final test, ms
    df["t_march_ms"] = (310 * rng.normal(1, .01, n)).round(1)
    df["t_retention_ms"] = (200 * rng.normal(1, .005, n)).round(1)
    df["t_repair_ms"] = (60 + 30 * (n_march > 0) + 6 * np.minimum(n_march, 5)).round(1)
    df["t_setup_readout_ms"] = (60 * rng.normal(1, .02, n)).round(1)
    df["t_total_baseline_ms"] = df[["t_march_ms", "t_retention_ms", "t_repair_ms", "t_setup_readout_ms"]].sum(axis=1).round(1)
    df.insert(0, "die_id", [f"D{i:06d}" for i in range(n)])
    return df


def make_fail_events(rng, dies, mems):
    sram = mems[mems.mem_type != "ROM"].reset_index(drop=True)
    w = sram.total_bits / sram.total_bits.sum()
    hd = sram[sram.bitcell == "HD"].reset_index(drop=True)
    w_hd = hd.total_bits / hd.total_bits.sum()
    rows = []
    for die_id, k, dyn in zip(dies.die_id, dies.march_failing_mems, dies.dynamic_fault_fail):
        n_ev = int(k) + int(dyn)
        for j in range(n_ev):
            # dynamic-fault events occur in high-density bitcells; others are spread by bit count
            mem = hd.iloc[rng.choice(len(hd), p=w_hd)] if j == int(k) else sram.iloc[rng.choice(len(sram), p=w)]
            mix = FAULT_MIX["HD" if mem.bitcell == "HD" else "HC"]
            if j == int(k):  # the dynamic-fault event
                ft = rng.choice(["DRDF", "RDF"], p=[.7, .3])
            else:
                pr = np.array(list(mix.values())); ft = rng.choice(list(mix), p=pr / pr.sum())
            u = rng.random()  # one draw per event: a fault "hard" enough to escape a weak test escapes weaker ones too
            det = {al: u < pr for al, pr in zip(ALGOS, FAULT_DETECT[ft])}
            det["MarchSS"] = True  # baseline catches every logged event by construction
            cheapest = min((al for al in ALGOS if det[al]), key=ALGOS.get)
            rows.append(dict(
                die_id=die_id, mem_id=mem.mem_id, mem_type=mem.mem_type, bitcell=mem.bitcell,
                fault_type=ft, cheapest_detecting_algo=cheapest,
                detect_background=rng.choice(BACKGROUNDS, p=[.5, .3, .1, .1] if ft in ("SAF", "TF") else [.2, .4, .2, .2]),
                detect_corner=rng.choice(CORNERS, p=[.45, .2, .25, .1] if ft in ("DRDF", "RDF") else [.25, .2, .4, .15]),
                **{f"det_{al.replace('+', 'plus').replace('-', 'm')}": bool(v) for al, v in det.items()},
            ))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--lots", type=int, default=20)
    ap.add_argument("--wafers", type=int, default=12)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    os.makedirs(a.out, exist_ok=True)
    mems = make_memories(rng)
    dies = make_dies(rng, a.lots, a.wafers)
    fails = make_fail_events(rng, dies, mems)
    mems.to_csv(f"{a.out}/memories.csv", index=False)
    dies.to_csv(f"{a.out}/die_results.csv", index=False)
    fails.to_csv(f"{a.out}/fail_events.csv", index=False)
    print(f"memories: {len(mems)}  ({mems.total_bits.sum() / 2**20:.1f} Mbit, {mems.controller_id.nunique()} controllers)")
    print(f"dies: {len(dies)}  fail events: {len(fails)}")
    print(dies.final_bin.value_counts(normalize=True).mul(100).round(2).to_string())


if __name__ == "__main__":
    main()
