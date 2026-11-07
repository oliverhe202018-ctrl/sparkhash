#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU Statistical Verification Test
Runs 50 independent trials at 28-bit and 30-bit difficulty on GPU.
Calculates mean hashes, ratio to 2^n, and 95% Student-t confidence interval.
"""

import sys, os, time, struct, math

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pyopencl as cl
except ImportError:
    cl = None

import pytest
if cl is None or np is None:
    pytestmark = pytest.mark.skip(reason="pyopencl or numpy runtime not installed")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import script.miner_gpu as miner_gpu

def run_gpu_statistical_test(diff_bits, num_trials=50, batch_size=16*1024*1024):
    devices = miner_gpu.detect_devices()
    dev = devices[0]["device_obj"]
    ctx = cl.Context([dev])
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, miner_gpu.OPENCL_KECCAK_SOURCE).build()
    knl = cl.Kernel(prg, "mine_batch")

    sub_bytes = bytes.fromhex("00a1a9e52e3b04917488c44b903881ac16fdd5d64fbfb3fb90a57b74dce7403e")
    sub_lanes = struct.unpack("<4Q", sub_bytes)

    target_int = 1 << (256 - diff_bits)
    target_bytes = target_int.to_bytes(32, "big")
    target_lanes = struct.unpack(">4Q", target_bytes)
    theoretical_mean = 2 ** diff_bits

    found_count_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=4)
    found_nonce_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=8)
    found_hash_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=32)
    zero = np.zeros(1, dtype=np.uint32)

    trial_results = []
    print(f"\n=======================================================")
    print(f"Starting GPU Statistical Test: {diff_bits}-bit Difficulty ({num_trials} Trials)")
    print(f"Target: 0x{target_bytes.hex()[:16]}... | Theoretical Mean 2^{diff_bits} = {theoretical_mean:,}")
    print(f"=======================================================")

    t_start = time.time()
    # Distinct non-overlapping start nonces for each trial
    stride = (2 ** diff_bits) * 4
    for trial_idx in range(num_trials):
        start_nonce = trial_idx * stride + 1
        curr = start_nonce
        hashes_in_trial = 0
        t0 = time.time()

        while True:
            cl.enqueue_copy(queue, found_count_buf, zero)
            knl(
                queue, (batch_size,), (256,),
                np.uint64(sub_lanes[0]), np.uint64(sub_lanes[1]), np.uint64(sub_lanes[2]), np.uint64(sub_lanes[3]),
                np.uint64(curr),
                np.uint64(target_lanes[0]), np.uint64(target_lanes[1]), np.uint64(target_lanes[2]), np.uint64(target_lanes[3]),
                found_count_buf, found_nonce_buf, found_hash_buf
            )
            count_np = np.empty(1, dtype=np.uint32)
            cl.enqueue_copy(queue, count_np, found_count_buf)
            queue.finish()

            if count_np[0] > 0:
                res_nonce = np.empty(1, dtype=np.uint64)
                res_hash = np.empty(4, dtype=np.uint64)
                cl.enqueue_copy(queue, res_nonce, found_nonce_buf)
                cl.enqueue_copy(queue, res_hash, found_hash_buf)
                queue.finish()

                winning_nonce = int(res_nonce[0])
                winning_hash = struct.pack(">4Q", *res_hash)
                # Hashes inspected in this trial
                hashes_in_trial += (winning_nonce - curr + 1)

                # Local CPU assertion to verify zero false positives
                valid, check_h, _ = miner_gpu.verify_share_local(sub_bytes, winning_nonce, target_int)
                assert valid, f"Local verification failed for nonce {winning_nonce}!"
                break

            hashes_in_trial += batch_size
            curr += batch_size

        t1 = time.time()
        ratio_i = hashes_in_trial / theoretical_mean
        trial_results.append(hashes_in_trial)

        if (trial_idx + 1) % 10 == 0 or trial_idx == 0:
            print(f"Trial {trial_idx+1:2d}/{num_trials}: start={start_nonce:,} hit={winning_nonce:,} "
                  f"hashes={hashes_in_trial:,} ({ratio_i:.3f}x 2^{diff_bits}) in {t1-t0:.3f}s")

    t_total = time.time() - t_start
    arr = np.array(trial_results, dtype=np.float64)
    sample_mean = np.mean(arr)
    sample_std = np.std(arr, ddof=1)
    mean_ratio = sample_mean / theoretical_mean
    std_ratio = sample_std / theoretical_mean

    # Student-t critical value for 95% confidence interval (df = n - 1 = 49)
    # t_crit for df=49 is 2.0096
    t_crit = 2.0096
    se = sample_std / math.sqrt(num_trials)
    se_ratio = se / theoretical_mean
    ci_low = mean_ratio - t_crit * se_ratio
    ci_high = mean_ratio + t_crit * se_ratio

    print(f"\n--- Summary for {diff_bits}-bit Difficulty ({num_trials} Trials) ---")
    print(f"Total Test Time:    {t_total:.2f} s")
    print(f"Theoretical Mean:   {theoretical_mean:,} hashes")
    print(f"Sample Mean:        {sample_mean:,.1f} hashes")
    print(f"Sample Std Dev:     {sample_std:,.1f} hashes")
    print(f"Mean / 2^{diff_bits} Ratio:  {mean_ratio:.4f}")
    print(f"Std / 2^{diff_bits} Ratio:   {std_ratio:.4f} (Theoretical: ~1.0000)")
    print(f"Standard Error SE:  {se_ratio:.4f}")
    print(f"95% Confidence Int: [{ci_low:.4f}, {ci_high:.4f}]")
    print(f"Theoretical 1.0 in 95% CI: {ci_low <= 1.0 <= ci_high}")
    print(f"-------------------------------------------------------")

    return {
        "diff_bits": diff_bits,
        "trials": num_trials,
        "theoretical_mean": theoretical_mean,
        "sample_mean": sample_mean,
        "sample_std": sample_std,
        "mean_ratio": mean_ratio,
        "std_ratio": std_ratio,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "t_total": t_total
    }

if __name__ == "__main__":
    res28 = run_gpu_statistical_test(28, num_trials=50)
    res30 = run_gpu_statistical_test(30, num_trials=50)
