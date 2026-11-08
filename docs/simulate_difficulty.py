#!/usr/bin/env python3
"""
docs/simulate_difficulty.py

PoolCoin Difficulty Adaptation, S_STAR Calibration & Sybil Resistance Simulation.
Reproduces, verifies, and reconciles all analytical models documented in:
  - docs/difficulty-and-sybil-analysis.md
  - docs/s-star-calibration.md

Supports 4 operational modes via --mode:
  1. verify:      Verifies Poisson distribution models for CPU (2.5 MH/s) and GPU (1.3 GH/s),
                  capping behavior, and surge convergence dynamics.
  2. reconcile:   Reconciles on-chain measured gas (submitShares, sync, claim) and computes
                  the 16-scenario break-even valuation matrix (FDV* in BNB and USD).
  3. sensitivity: Evaluates sensitivity across initial deposits (0.01 to 0.50 BNB),
                  gas prices (0.05 to 3.0 Gwei), and daily token emission yields.
  4. calibration: Computes the 96-cell S_STAR calibration matrix across 8 address tiers
                  and 6 S_STAR candidates for CPU (s_bar=2) and GPU (s_bar=6).
"""

import sys
import math
import argparse
import numpy as np
from scipy.stats import poisson

if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------
# Core Constants (from MinerV1.sol, StakeVault.sol & Docs)
# ---------------------------------------------------------
BNB_PRICE_USD = 695.00                # Spot benchmark: $695.00 USD / BNB
DEFAULT_GAS_PRICE_GWEI = 0.05         # Baseline gas price: 0.05 Gwei
DEFAULT_GAS_PRICE_BNB = 5e-11         # 0.05 * 1e-9 BNB / gas
MINING_CAP = 900_000_000.0            # 900M Token hardcap (MINING_CAP)
ANNUAL_COST_OF_CAPITAL = 0.10         # 10% APY
DAILY_COST_OF_CAPITAL = 0.10 / 365.0  # r_daily = 0.027397%

# On-chain measured gas constants (Foundry & BSC Testnet benchmarks)
GAS_SUBMIT_BASE = 171402              # submitShares initial share
GAS_SUBMIT_EXTRA = 1650               # submitShares per additional share in batch
GAS_SYNC = 145676                     # sync per epoch step
GAS_CLAIM = 109646                    # claim per epoch per miner

CAP_SHARES = 6                        # CAP = 6 shares per miner per epoch
EPOCH_SECONDS = 3600                  # 1 hour per epoch
EPOCHS_PER_DAY = 24                   # 24 epochs per day
D0_TOKENS = 3_000_000.0               # Day 1 daily quota

# Hashrate profiles
CPU_HASHRATE = 2.5e6                  # 2.5 MH/s CPU
GPU_HASHRATE = 1.3e9                  # 1.3 GH/s RTX 4090 GPU

EASIEST_DIFF = 32                     # EASIEST = 1 << 224 (32 bits)
HARDEST_DIFF = 35                     # HARDEST = 1 << 221 (35 bits)
DEFAULT_S_STAR = 50000                # S_STAR genesis parameter


# =========================================================
# Mode 1: verify
# =========================================================
def run_verify():
    print("=" * 110)
    print("MODE: VERIFY - Poisson Distribution, CAP Capping & Surge Convergence Verification")
    print("=" * 110)

    print("\n1. CPU Miner Poisson Distribution Analysis (2.5 MH/s, 3600s, H = 9.0e9 hashes)")
    print("-" * 110)
    print(f"{'Difficulty':<18} | {'p (Hit Prob)':<14} | {'lambda (Exp)':<12} | {'Exp Eff (w)':<12} | {'Exp / CAP %':<12} | {'P(K >= 6)':<12} | {'P(K = 0)':<10} | {'P(K >= 1)':<10}")
    print("-" * 110)

    cpu_hashes = CPU_HASHRATE * EPOCH_SECONDS
    diff_results_cpu = {}
    for d in [32, 33, 34, 35]:
        p = 2.0 ** (-d)
        lam = cpu_hashes * p
        ks = np.arange(100)
        pmf = poisson.pmf(ks, lam)
        eff = float(np.sum(np.minimum(ks, CAP_SHARES) * pmf))
        ratio = eff / CAP_SHARES * 100.0
        p_cap = float(1.0 - poisson.cdf(CAP_SHARES - 1, lam)) * 100.0
        p_zero = float(pmf[0]) * 100.0
        p_ge1 = 100.0 - p_zero
        diff_results_cpu[d] = {
            "p": p, "lam": lam, "eff": eff, "ratio": ratio,
            "p_cap": p_cap, "p_zero": p_zero, "p_ge1": p_ge1
        }
        diff_name = f"{d}-bit" + (" (EASIEST)" if d == 32 else (" (HARDEST)" if d == 35 else ""))
        print(f"{diff_name:<18} | {p:<14.3e} | {lam:<12.3f} | {eff:<12.3f} | {ratio:<11.2f}% | {p_cap:<11.4f}% | {p_zero:<9.2f}% | {p_ge1:<9.2f}%")

    print("\n2. GPU Miner Saturation & Waste Analysis (RTX 4090, 1.3 GH/s, 3600s, H = 4.68e12 hashes)")
    print("-" * 110)
    gpu_hashes = GPU_HASHRATE * EPOCH_SECONDS
    p_32 = 2.0 ** (-32)
    lam_gpu = gpu_hashes * p_32
    eff_gpu = min(lam_gpu, float(CAP_SHARES))
    waste_gpu = (1.0 - eff_gpu / lam_gpu) * 100.0
    p_cap_gpu = 1.0 - poisson.cdf(CAP_SHARES - 1, lam_gpu)
    print(f"32-bit Difficulty (EASIEST):")
    print(f"  - Single hash hit prob:           {p_32:.6e}")
    print(f"  - Expected raw shares lambda:      {lam_gpu:.2f} shares / epoch")
    print(f"  - Expected effective shares:       {eff_gpu:.3f} shares (capped at {CAP_SHARES})")
    print(f"  - Expected shares / CAP ratio:     {(eff_gpu / CAP_SHARES)*100.0:.2f}%")
    print(f"  - Probability of hitting CAP >= 6: {p_cap_gpu * 100.0:.6f}% (> 99.9999%)")
    print(f"  - Hashrate wasted / truncated:     {waste_gpu:.2f}% (drives strong splitting incentive)")

    print("\n3. Controller Surge Convergence Steps (32-bit -> 35-bit with 0.8x rate-limiting)")
    print("-" * 110)
    t_ratio = (2.0 ** 221) / (2.0 ** 224)
    min_steps = math.log(t_ratio) / math.log(0.8)
    print(f"  - Target ratio: T_hardest / T_0 = 2^221 / 2^224 = {t_ratio} (1/8)")
    print(f"  - Max down-adjustment rate per step: 100 / 125 = 0.80x")
    print(f"  - Theoretical minimum consecutive reduction steps: {min_steps:.4f} => {math.ceil(min_steps)} steps")
    print(f"  - EMA feedback delay: n - 2 delay introduces 2-epoch settlement latency")
    print(f"  - Total surge convergence epochs: ~{math.ceil(min_steps) + 3} epochs (13 epochs verified in Foundry suite)")
    print("=" * 110)
    return diff_results_cpu


# =========================================================
# Mode 2: reconcile
# =========================================================
def run_reconcile():
    print("=" * 125)
    print("MODE: RECONCILE - On-Chain Gas Itemization & Break-Even Valuation Matrix")
    print("=" * 125)

    gas_submit_6 = GAS_SUBMIT_BASE + 5 * GAS_SUBMIT_EXTRA
    daily_submit_gas = EPOCHS_PER_DAY * gas_submit_6
    daily_claim_gas = EPOCHS_PER_DAY * GAS_CLAIM
    daily_total_gas_per_addr = daily_submit_gas + daily_claim_gas
    daily_gas_bnb_per_addr = daily_total_gas_per_addr * DEFAULT_GAS_PRICE_BNB
    daily_gas_usd_per_addr = daily_gas_bnb_per_addr * BNB_PRICE_USD

    print("\n1. On-Chain Gas Itemization (0.05 Gwei, 24 Epochs / Day):")
    print(f"  - submitShares (first share):          {GAS_SUBMIT_BASE:,} gas")
    print(f"  - submitShares (each additional):      {GAS_SUBMIT_EXTRA:,} gas")
    print(f"  - submitShares (full 6 shares):        {gas_submit_6:,} gas")
    print(f"  - sync (single epoch step):            {GAS_SYNC:,} gas")
    print(f"  - claim (single epoch claim):          {GAS_CLAIM:,} gas")
    print(f"  - Single address daily submit gas:     {daily_submit_gas:,} gas (24 x {gas_submit_6:,})")
    print(f"  - Single address daily claim gas:      {daily_claim_gas:,} gas (24 x {GAS_CLAIM:,})")
    print(f"  - Single address daily total gas:      {daily_total_gas_per_addr:,} gas / day")
    print(f"  - Single address daily gas cost (BNB): {daily_gas_bnb_per_addr:.8f} BNB / day")
    print(f"  - Single address daily gas cost (USD): ${daily_gas_usd_per_addr:.4f} USD / day (@ ${BNB_PRICE_USD}/BNB)")

    print("\n2. Sybil Break-Even Valuation Matrix (16 Scenarios):")
    print("-" * 125)
    header = f"{'Scenario Name':<32} | {'N':<4} | {'Dep':<5} | {'R (Token)':<11} | {'C_gas':<8} | {'C_opp':<8} | {'C_tot':<8} | {'C_USD':<7} | {'P* (BNB)':<10} | {'FDV (BNB)':<10} | {'FDV (USD)':<10}"
    print(header)
    print("-" * 125)

    scenarios = [
        ("100地址 / 0.01 BNB / 历史高额份额", 100, 0.01, 1850847),
        ("100地址 / 0.01 BNB / 稳态合理份额", 100, 0.01, 1000000),
        ("100地址 / 0.01 BNB / 首日爬坡折减", 100, 0.01, 609375),
        ("100地址 / 0.01 BNB / 激烈竞争稀释", 100, 0.01, 500000),
        ("100地址 / 0.02 BNB / 稳态合理份额", 100, 0.02, 1000000),
        ("100地址 / 0.05 BNB / 稳态合理份额", 100, 0.05, 1000000),
        ("100地址 / 0.10 BNB / 稳态合理份额", 100, 0.10, 1000000),
        ("100地址 / 0.50 BNB / 稳态合理份额", 100, 0.50, 1000000),
        ("200地址 / 0.01 BNB / 历史高额份额", 200, 0.01, 1850847),
        ("200地址 / 0.01 BNB / 稳态适度份额", 200, 0.01, 1000000),
        ("200地址 / 0.01 BNB / 仿真标准生态产出", 200, 0.01, 720000),
        ("200地址 / 0.01 BNB / 仿真首日爬坡折减", 200, 0.01, 438750),
        ("200地址 / 0.01 BNB / 激烈竞争稀释", 200, 0.01, 500000),
        ("200地址 / 0.02 BNB / 仿真标准生态产出", 200, 0.02, 720000),
        ("200地址 / 0.05 BNB / 仿真标准生态产出", 200, 0.05, 720000),
        ("200地址 / 0.10 BNB / 仿真标准生态产出", 200, 0.10, 720000),
        ("200地址 / 0.50 BNB / 仿真标准生态产出", 200, 0.50, 720000),
    ]

    reconcile_data = []
    for name, N, dep, R in scenarios:
        c_gas = N * daily_gas_bnb_per_addr
        c_opp = N * dep * DAILY_COST_OF_CAPITAL
        c_tot = c_gas + c_opp
        c_tot_usd = c_tot * BNB_PRICE_USD
        p_star = c_tot / R
        fdv_bnb = p_star * MINING_CAP
        fdv_usd = fdv_bnb * BNB_PRICE_USD
        reconcile_data.append({
            "name": name, "N": N, "dep": dep, "R": R,
            "c_gas": c_gas, "c_opp": c_opp, "c_tot": c_tot,
            "c_tot_usd": c_tot_usd, "p_star": p_star,
            "fdv_bnb": fdv_bnb, "fdv_usd": fdv_usd
        })
        print(f"{name:<32} | {N:<4} | {dep:<5.2f} | {R:<11,d} | {c_gas:<8.5f} | {c_opp:<8.5f} | {c_tot:<8.5f} | ${c_tot_usd:<6.2f} | {p_star:<10.3e} | {fdv_bnb:<10.2f} | ${fdv_usd:<9,.0f}")
    print("=" * 125)
    return reconcile_data


# =========================================================
# Mode 3: sensitivity
# =========================================================
def run_sensitivity():
    print("=" * 115)
    print("MODE: SENSITIVITY - Parameter Sensitivity across Deposits, Gas Prices & Token Shares")
    print("=" * 115)

    gas_submit_6 = GAS_SUBMIT_BASE + 5 * GAS_SUBMIT_EXTRA
    daily_gas_per_addr = EPOCHS_PER_DAY * (gas_submit_6 + GAS_CLAIM)

    # 1. Deposit Sensitivity
    print("\n1. Deposit Sensitivity (0.05 Gwei, APY=10%, Token Supply=900M)")
    print("-" * 115)
    print(f"{'Cluster':<10} | {'Deposit (BNB)':<14} | {'C_gas (BNB)':<13} | {'C_opp (BNB)':<13} | {'C_total (BNB)':<14} | {'Gas Share %':<12} | {'FDV* (BNB)':<12} | {'FDV* (USD)':<12}")
    print("-" * 115)

    deposits = [0.01, 0.02, 0.05, 0.10, 0.50]
    for N, R, label in [(100, 1000000, "100 Addrs (R=1.0M)"), (200, 720000, "200 Addrs (R=720k)")]:
        for dep in deposits:
            c_gas = N * daily_gas_per_addr * DEFAULT_GAS_PRICE_BNB
            c_opp = N * dep * DAILY_COST_OF_CAPITAL
            c_tot = c_gas + c_opp
            gas_pct = (c_gas / c_tot) * 100.0
            p_star = c_tot / R
            fdv_bnb = p_star * MINING_CAP
            fdv_usd = fdv_bnb * BNB_PRICE_USD
            print(f"{label:<10} | {dep:<14.2f} | {c_gas:<13.5f} | {c_opp:<13.5f} | {c_tot:<14.5f} | {gas_pct:<11.2f}% | {fdv_bnb:<12.2f} | ${fdv_usd:<11,.0f}")

    # 2. Gas Price Sensitivity
    print("\n2. Gas Price Sensitivity (N=100, Deposit=0.01 BNB, R=1,000,000 Token/day)")
    print("-" * 115)
    print(f"{'Gas Price (Gwei)':<18} | {'Daily Gas Cost (BNB)':<22} | {'Daily Total Cost (USD)':<24} | {'Break-even FDV (BNB)':<22} | {'Break-even FDV (USD)':<22}")
    print("-" * 115)

    gas_prices_gwei = [0.05, 0.10, 0.50, 1.00, 3.00]
    for gp in gas_prices_gwei:
        gp_bnb = gp * 1e-9
        c_gas = 100 * daily_gas_per_addr * gp_bnb
        c_opp = 100 * 0.01 * DAILY_COST_OF_CAPITAL
        c_tot = c_gas + c_opp
        c_tot_usd = c_tot * BNB_PRICE_USD
        fdv_bnb = (c_tot / 1000000) * MINING_CAP
        fdv_usd = fdv_bnb * BNB_PRICE_USD
        print(f"{gp:<18.2f} | {c_gas:<22.5f} | ${c_tot_usd:<23.2f} | {fdv_bnb:<22.2f} | ${fdv_usd:<21,.0f}")

    # 3. Dilution / Participation Yield Sensitivity
    print("\n3. Token Yield / Dilution Sensitivity (N=100, Deposit=0.01 BNB, 0.05 Gwei Gas)")
    print("-" * 115)
    print(f"{'Yield Scenario':<28} | {'Daily Tokens R':<16} | {'Break-even P* (BNB)':<22} | {'Break-even FDV (BNB)':<22} | {'Break-even FDV (USD)':<22}")
    print("-" * 115)
    c_tot = 100 * daily_gas_per_addr * DEFAULT_GAS_PRICE_BNB + 100 * 0.01 * DAILY_COST_OF_CAPITAL
    yield_scenarios = [
        ("Monopoly / Historic Upper", 1850847),
        ("Steady-State Reasonable", 1000000),
        ("Standard Simulation", 720000),
        ("Ramp Warmup First Day", 609375),
        ("High Competition Dilution", 500000),
        ("Severe Competition Dilution", 300000),
    ]
    for sc_name, R in yield_scenarios:
        p_star = c_tot / R
        fdv_bnb = p_star * MINING_CAP
        fdv_usd = fdv_bnb * BNB_PRICE_USD
        print(f"{sc_name:<28} | {R:<16,d} | {p_star:<22.3e} | {fdv_bnb:<22.2f} | ${fdv_usd:<21,.0f}")
    print("=" * 115)


# =========================================================
# Mode 4: calibration
# =========================================================
def run_calibration():
    print("=" * 115)
    print("MODE: CALIBRATION - 96-Cell S_STAR Calibration Matrix & Equilibrium Difficulty")
    print("=" * 115)

    A_list = [100, 500, 1000, 2000, 5000, 10000, 20000, 50000]
    S_STAR_list = [1000, 5000, 10000, 20000, 50000, 100000]

    def calc_d_eq(A, s_bar, s_star):
        s_pot = A * s_bar
        if s_pot <= s_star:
            return 32.00
        d = 32.00 + math.log2(s_pot / s_star)
        return min(35.00, max(32.00, d))

    print("\nMatrix A: Average s_bar = 2 Shares / Address (CPU User Dominated)")
    print("-" * 115)
    header = f"{'S_STAR':<8} | " + " | ".join([f"A={a:<5}" for a in A_list]) + " | Critical A"
    print(header)
    print("-" * 115)

    matrix_a_data = {}
    for s_star in S_STAR_list:
        row_vals = [calc_d_eq(a, 2, s_star) for a in A_list]
        matrix_a_data[s_star] = row_vals
        row_str = " | ".join([f"{v:5.2f}b" for v in row_vals])
        crit = s_star // 2
        print(f"{s_star:<8d} | {row_str} | A > {crit:<6,d}")

    print("\nMatrix B: Average s_bar = 6 Shares / Address (GPU / Mining Farm Capping)")
    print("-" * 115)
    header = f"{'S_STAR':<8} | " + " | ".join([f"A={a:<5}" for a in A_list]) + " | Critical A"
    print(header)
    print("-" * 115)

    matrix_b_data = {}
    for s_star in S_STAR_list:
        row_vals = [calc_d_eq(a, 6, s_star) for a in A_list]
        matrix_b_data[s_star] = row_vals
        row_str = " | ".join([f"{v:5.2f}b" for v in row_vals])
        crit = math.ceil(s_star / 6.0)
        print(f"{s_star:<8d} | {row_str} | A > {crit:<6,d}")

    print("\nKey Takeaway for S_STAR = 50,000:")
    print("  - For CPU network (s_bar=2): Difficulty stays strictly at 32.00b until A > 25,000.")
    print("  - For GPU network (s_bar=6): Difficulty stays at 32.00b until A > 8,334; reaches 35.00b only at A > 66,667.")
    print("=" * 115)
    return {"matrix_a": matrix_a_data, "matrix_b": matrix_b_data}


def main():
    parser = argparse.ArgumentParser(
        description="PoolCoin Difficulty Adaptation, S_STAR Calibration & Sybil Defense Simulation"
    )
    parser.add_argument(
        "--mode",
        choices=["verify", "reconcile", "sensitivity", "calibration", "all"],
        default="all",
        help="Simulation mode to run: verify, reconcile, sensitivity, calibration, or all (default: all)"
    )
    args = parser.parse_args()

    if args.mode in ["verify", "all"]:
        run_verify()
    if args.mode in ["reconcile", "all"]:
        run_reconcile()
    if args.mode in ["sensitivity", "all"]:
        run_sensitivity()
    if args.mode in ["calibration", "all"]:
        run_calibration()


if __name__ == "__main__":
    main()
