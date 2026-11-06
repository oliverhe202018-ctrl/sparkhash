#!/usr/bin/env python3
"""
docs/simulation_gpu_resistance.py

PoolCoin Anti-GPU Parameter Optimization Simulation with Dynamic Difficulty Feedback
Simulates:
- Part A: Matrix Scan across CPU speeds (1.0, 2.5, 5.0 MH/s) and GPU speeds (1.3, 5.0, 10.0 GH/s)
- Part B: Dynamic Controller Feedback (MinerV1.sol:233-243) comparing [32, 35] vs [34, 36]
          across CAP = 4, 6, 8, 15 under configurable population scenarios:
          * Scenario 1 (Few GPUs): 100 CPUs @ 2.5 MH/s + 2 GPUs @ 1.3 GH/s
          * Scenario 2 (GPU Dominated): 50 CPUs @ 2.5 MH/s + 20 GPUs @ 1.3 GH/s
          * Scenario 3 (High-Power GPUs): 50 CPUs @ 2.5 MH/s + 10 GPUs @ 5.0 GH/s
- Part C: Sybil Attack & Gas Friction Modeling:
          1 GPU (1.3 GH/s) splitting into N addresses (N=1..8) vs 100 CPUs (4.8 MH/s)
          across CAP in [4, 6, 8, 15] and Difficulties 30 to 34 bits.
"""

import sys
import argparse
import numpy as np
from scipy.stats import poisson

T_EPOCH = 3600  # 1 hour
S_STAR_DEFAULT = 50000

CPU_SPEEDS = [1.0e6, 2.5e6, 5.0e6]
GPU_SPEEDS = [1.3e9, 5.0e9, 10.0e9]
CAPS = [4, 6, 8, 15]
DIFF_RANGES = {
    "[32, 35] (Proposed)": (32, 35),
    "[34, 36] (Current)": (34, 36),
}

def calc_exp_stats(speed, target_p, cap):
    """Calculates expected shares, weight, cap hit probability, and wasted hash fraction."""
    lam = speed * T_EPOCH * target_p
    k_max = max(int(lam + 5 * np.sqrt(max(lam, 1.0)) + 30), cap + 15)
    ks = np.arange(k_max)
    probs = poisson.pmf(ks, lam)
    exp_c = np.sum(probs * np.minimum(ks, cap))
    exp_w = np.sum(probs * np.sqrt(np.minimum(ks, cap)))
    prob_hit_cap = 1.0 - poisson.cdf(cap - 1, lam)
    useful_fraction = min(1.0, exp_c / lam) if lam > 0 else 1.0
    waste_pct = (1.0 - useful_fraction) * 100.0
    return exp_c, exp_w, prob_hit_cap, waste_pct

def run_feedback_simulation(n_cpus, cpu_spd, n_gpus, gpu_spd, cap, d_bounds, s_star=S_STAR_DEFAULT, epochs=80):
    """
    Simulates multi-epoch difficulty adjustment following MinerV1.sol:233-243:
      shares = (n >= 2) ? totalShares[n - 2] : S_STAR;
      ema = (ema + shares) / 2;
      nt = (ema == 0) ? (t * 125 / 100) : (t * S_STAR / ema);
      clamp to [t * 0.8, t * 1.25];
      clamp to [HARDEST, EASIEST];
    """
    easiest_d, hardest_d = d_bounds
    t_easiest = 1 << (256 - easiest_d)
    t_hardest = 1 << (256 - hardest_d)

    t_curr = t_easiest
    ema = s_star
    history_shares = [s_star, s_star]

    for ep in range(epochs):
        p = float(t_curr) / (2.0**256)
        c_cpu, w_cpu, _, _ = calc_exp_stats(cpu_spd, p, cap)
        c_gpu, w_gpu, _, _ = calc_exp_stats(gpu_spd, p, cap)

        tot_shares = n_cpus * c_cpu + n_gpus * c_gpu
        tot_weight = n_cpus * w_cpu + n_gpus * w_gpu

        delayed_shares = history_shares[-2]
        history_shares.append(tot_shares)

        ema = (ema + delayed_shares) / 2.0
        nt = (t_curr * 1.25) if ema == 0 else (t_curr * s_star / ema)
        if nt > t_curr * 1.25: nt = t_curr * 1.25
        if nt < t_curr * 0.8:  nt = t_curr * 0.8
        if nt > t_easiest: nt = t_easiest
        if nt < t_hardest: nt = t_hardest
        t_curr = nt

    final_d = 256.0 - np.log2(float(t_curr))
    final_p = float(t_curr) / (2.0**256)
    c_cpu, w_cpu, _, _ = calc_exp_stats(cpu_spd, final_p, cap)
    c_gpu, w_gpu, _, waste_gpu = calc_exp_stats(gpu_spd, final_p, cap)
    tot_weight = n_cpus * w_cpu + n_gpus * w_gpu

    cpu_group_pct = (n_cpus * w_cpu / tot_weight * 100.0) if tot_weight > 0 else 0.0
    gpu_adv = (w_gpu / w_cpu) if w_cpu > 0 else np.nan

    return {
        "final_diff": final_d,
        "w_cpu": w_cpu,
        "w_gpu": w_gpu,
        "cpu_group_pct": cpu_group_pct,
        "gpu_adv": gpu_adv,
        "waste_gpu": waste_gpu,
        "c_cpu": c_cpu,
        "c_gpu": c_gpu,
    }

def print_speed_sweep_table():
    print("=" * 95)
    print("PART A: Matrix Scan Across CPU Speeds (1, 2.5, 5 MH/s) and GPU Speeds (1.3, 5, 10 GH/s)")
    print("=" * 95)
    print(f"{'Diff':<5} | {'CAP':<4} | {'CPU MH/s':<9} | {'CPU W':<8} | {'GPU GH/s':<9} | {'GPU W':<8} | {'GPU/CPU Adv':<12} | {'GPU Waste %':<11}")
    print("-" * 95)
    for d in [32, 34, 35]:
        p = 2**(-d)
        for cap in [4, 6, 8, 15]:
            for cs in CPU_SPEEDS:
                _, w_c, _, _ = calc_exp_stats(cs, p, cap)
                for gs in GPU_SPEEDS:
                    _, w_g, _, waste_g = calc_exp_stats(gs, p, cap)
                    adv = w_g / w_c if w_c > 0 else np.nan
                    print(f"{d:<5} | {cap:<4} | {cs/1e6:<7.1f}M  | {w_c:<8.3f} | {gs/1e9:<7.1f}G  | {w_g:<8.3f} | {adv:<10.2f}x | {waste_g:<10.1f}%")

def print_population_scenario_table():
    print("\n" + "=" * 105)
    print("PART B: Dynamic Controller Feedback & Population Scenarios (MinerV1.sol:233-243)")
    print("Scenario 1: Few GPUs (100 CPUs @ 2.5 MH/s + 2 GPUs @ 1.3 GH/s)")
    print("Scenario 2: GPU Dominated (50 CPUs @ 2.5 MH/s + 20 GPUs @ 1.3 GH/s)")
    print("Scenario 3: High-Power GPUs (50 CPUs @ 2.5 MH/s + 10 GPUs @ 5.0 GH/s)")
    print("=" * 105)

    scenarios = [
        ("Few GPUs (100 CPU @ 2.5M, 2 GPU @ 1.3G)", 100, 2.5e6, 2, 1.3e9),
        ("GPU Dominated (50 CPU @ 2.5M, 20 GPU @ 1.3G)", 50, 2.5e6, 20, 1.3e9),
        ("High-Power GPUs (50 CPU @ 2.5M, 10 GPU @ 5.0G)", 50, 2.5e6, 10, 5.0e9),
    ]

    for sc_name, n_c, c_spd, n_g, g_spd in scenarios:
        print(f"\n>>> Scenario: {sc_name}")
        print(f"{'Range':<22} | {'CAP':<4} | {'Settled D':<10} | {'CPU W':<8} | {'GPU W':<8} | {'CPU Group %':<12} | {'GPU Adv':<10} | {'GPU Waste %':<11}")
        print("-" * 105)
        for r_name, bounds in DIFF_RANGES.items():
            for cap in CAPS:
                res = run_feedback_simulation(n_c, c_spd, n_g, g_spd, cap, bounds)
                print(f"{r_name:<22} | {cap:<4} | {res['final_diff']:<10.2f} | {res['w_cpu']:<8.3f} | {res['w_gpu']:<8.3f} | {res['cpu_group_pct']:<11.1f}% | {res['gpu_adv']:<8.2f}x | {res['waste_gpu']:<10.1f}%")

def print_sybil_sweep_table():
    print("\n" + "=" * 105)
    print("PART C: Sybil Attack Modeling (1 GPU @ 1.3 GH/s split into N addresses vs 100 CPUs @ 4.8 MH/s)")
    print("Simulates: N in [1, 2, 4, 8], Difficulties 30..34 bits, CAP in [4, 6, 8, 15]")
    print("=" * 105)
    print(f"{'Diff':<5} | {'CAP':<4} | {'N (Addrs)':<10} | {'Single CPU W':<13} | {'Sybil Tot W':<12} | {'Sybil / CPU Adv':<16} | {'Sybil Pool % (vs 100 CPUs)':<28}")
    print("-" * 105)
    for d in [30, 32, 34]:
        p = 2**(-d)
        for cap in [4, 6, 8, 15]:
            _, w_c, _, _ = calc_exp_stats(4.8e6, p, cap)
            for N in [1, 2, 4, 8]:
                spd_per_addr = 1.3e9 / N
                _, w_per_addr, _, _ = calc_exp_stats(spd_per_addr, p, cap)
                w_sybil_tot = N * w_per_addr
                tot_pool_w = 100 * w_c + w_sybil_tot
                sybil_pct = (w_sybil_tot / tot_pool_w * 100.0) if tot_pool_w > 0 else 0.0
                adv = w_sybil_tot / w_c if w_c > 0 else np.nan
                print(f"{d:<5} | {cap:<4} | {N:<10} | {w_c:<13.3f} | {w_sybil_tot:<12.3f} | {adv:<14.2f}x | {sybil_pct:<26.2f}%")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PoolCoin Anti-GPU Parameter Simulation')
    parser.add_argument('--part', choices=['all', 'a', 'b', 'c'], default='all', help='Which simulation part to run')
    args = parser.parse_args()

    if args.part in ['all', 'a']:
        print_speed_sweep_table()
    if args.part in ['all', 'b']:
        print_population_scenario_table()
    if args.part in ['all', 'c']:
        print_sybil_sweep_table()
