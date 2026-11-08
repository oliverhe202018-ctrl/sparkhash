"""
Progress-Indexed Emission Simulation Script for PoolCoin MinerV2
Compares existing Calendar-based Decay vs Progress-Indexed Emission (q_e = lambda * R_e).
"""

from decimal import Decimal, getcontext
import math
import random

getcontext().prec = 50

MINING_CAP = 900_000_000.0  # 900M
D0 = 3_000_000.0            # Day 1 daily quota
DECAY = 0.9967              # Daily decay factor
DAYS_1394 = 1394
EPOCHS_PER_DAY = 24
N_1394 = DAYS_1394 * EPOCHS_PER_DAY # 33,456 epochs

# ----------------- Calendar-based Model (MinerV1) -----------------
def daily_quota_cal(d):
    if d == 0: return 0.0
    return D0 * (DECAY ** (d - 1))

def epoch_quota_cal(e):
    d = e // EPOCHS_PER_DAY + 1
    return daily_quota_cal(d) / EPOCHS_PER_DAY

def cumulative_daily_quota_cal(completed_days):
    if completed_days == 0: return 0.0
    return (10_000_000_000.0 * (1.0 - (DECAY ** completed_days))) / 11.0

def cumulative_quota_before_cal(e):
    cd = e // EPOCHS_PER_DAY
    fd = cumulative_daily_quota_cal(cd)
    ed = e % EPOCHS_PER_DAY
    return fd + ed * epoch_quota_cal(e)

def effective_quota_cal(e):
    cb = cumulative_quota_before_cal(e)
    if cb >= MINING_CAP: return 0.0
    rem = MINING_CAP - cb
    eq = epoch_quota_cal(e)
    return min(rem, eq)

# ----------------- Progress-indexed Model (MinerV2) -----------------
def calculate_lambda(p, n_epochs=N_1394):
    """
    (1 - lambda)^n = 1 - p  =>  lambda = 1 - (1 - p)^(1/n)
    Calculated using high-precision Decimal to avoid float64 precision loss.
    """
    p_dec = Decimal(str(p))
    lam = Decimal(1) - (Decimal(1) - p_dec) ** (Decimal(1) / Decimal(n_epochs))
    return float(lam)

CONTRACT_LAMBDA_WAD_99 = 137_639_085_754_731
EXACT_LAMBDA_WAD_99 = 137_639_085_754_731

LAMBDAS = {
    'p=98%': calculate_lambda(0.98),
    'p=99%': calculate_lambda(0.99),
    'p=99.9%': calculate_lambda(0.999)
}

def run_all_simulations(total_years=10, seed=42):
    random.seed(seed)
    total_days = total_years * 365
    total_epochs = total_days * EPOCHS_PER_DAY
    
    # Pre-generate fluctuating participation rates in [0.1, 1.0]
    fluctuating_u = [random.uniform(0.1, 1.0) for _ in range(total_epochs)]
    
    scenarios = {
        '100%': lambda e: 1.0,
        '50%': lambda e: 0.5,
        '10%': lambda e: 0.1,
        'Fluctuating': lambda e: fluctuating_u[e]
    }
    
    check_years = [1, 2, 3, 5, 10]
    check_epochs = {y: y * 365 * EPOCHS_PER_DAY for y in check_years}
    
    results = {}
    
    # 1. Calendar-based simulation
    for sc_name, sc_fn in scenarios.items():
        cum_cal = 0.0
        cal_history = {}
        for e in range(total_epochs):
            effQ = effective_quota_cal(e)
            u = sc_fn(e)
            minted = effQ * u
            cum_cal += minted
            if e + 1 in check_epochs.values():
                for y, ce in check_epochs.items():
                    if e + 1 == ce:
                        cal_history[y] = (cum_cal, cum_cal / MINING_CAP)
        results[('Calendar', sc_name)] = cal_history
        
    # 2. Progress-based simulations for each lambda tier
    for p_label, lam in LAMBDAS.items():
        for sc_name, sc_fn in scenarios.items():
            R = MINING_CAP
            cum_prog = 0.0
            prog_history = {}
            for e in range(total_epochs):
                qe = lam * R
                u = sc_fn(e)
                minted = qe * u
                R -= minted # unallocated quota stays in R; capped overflow adds back to R
                cum_prog += minted
                if e + 1 in check_epochs.values():
                    for y, ce in check_epochs.items():
                        if e + 1 == ce:
                            prog_history[y] = (cum_prog, cum_prog / MINING_CAP)
            results[(f'Progress ({p_label})', sc_name)] = prog_history
            
    return results

if __name__ == '__main__':
    print("=== Parameter Calculation for N = 33,456 epochs (1394 days) ===")
    for p, lam in LAMBDAS.items():
        lam_wad = int(round(lam * 1e18))
        q0 = MINING_CAP * lam
        d0 = q0 * 24
        extra = ""
        if p == "p=99%":
            extra = f" | Contract Wad = {CONTRACT_LAMBDA_WAD_99} (diff = {CONTRACT_LAMBDA_WAD_99 - lam_wad})"
        print(f"Tier {p:7s} | lambda = {lam:.12e} | Wad = {lam_wad:15d} | Epoch 0 Quota = {q0:10.2f} TTK | Day 1 Quota = {d0:10.2f} TTK{extra}")

    print("\nRunning 10-year hourly simulation (87,600 epochs)...")
    res = run_all_simulations()
    
    print("\n### Cumulative Emission Summary Table (% of 900M Cap)")
    print("| Model | Scenario | Year 1 | Year 2 | Year 3 | Year 5 | Year 10 |")
    print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |")
    
    models = ['Calendar', 'Progress (p=98%)', 'Progress (p=99%)', 'Progress (p=99.9%)']
    scenarios = ['100%', '50%', '10%', 'Fluctuating']
    
    for m in models:
        for sc in scenarios:
            h = res[(m, sc)]
            row = [f"{h[y][1]*100:.2f}%" for y in [1, 2, 3, 5, 10]]
            print(f"| {m} | {sc} | " + " | ".join(row) + " |")
    print("\n### Absolute Emission Summary Table (Million TTK / M)")
    print("| Model (Scenario) | Year 1 | Year 2 | Year 3 | Year 5 | Year 10 |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")

    key_scenarios = [
        ('Calendar', '100%'),
        ('Calendar', '50%'),
        ('Calendar', 'Fluctuating'),
        ('Progress (p=99%)', '100%'),
        ('Progress (p=99%)', '50%'),
        ('Progress (p=99%)', 'Fluctuating'),
    ]
    for m, sc in key_scenarios:
        h = res[(m, sc)]
        row = [f"{h[y][0]/1e6:.2f} M" for y in [1, 2, 3, 5, 10]]
        print(f"| {m} ({sc}) | " + " | ".join(row) + " |")
