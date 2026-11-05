import sys, os, time, struct, math
import numpy as np
import threading
import multiprocessing as mp

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import script.miner as miner

def run_cpu_trial(sub_bytes, target_bytes, b_targets, start_nonce, num_workers=5, chunk_size=1000000):
    stop_event = mp.Event()
    result_q = mp.Queue()
    stats_q = mp.Queue()

    stats_done = threading.Event()
    total_hashes_list = [0]
    def drain_stats():
        while not stats_done.is_set():
            try:
                val = stats_q.get(timeout=0.1)
                total_hashes_list[0] += val
            except Exception:
                pass
        while not stats_q.empty():
            try:
                total_hashes_list[0] += stats_q.get_nowait()
            except Exception:
                break

    drain_thread = threading.Thread(target=drain_stats, daemon=True)
    drain_thread.start()

    procs = []
    t0 = time.time()
    for wid in range(num_workers):
        p = mp.Process(
            target=miner.worker_proc,
            args=(
                wid,
                num_workers,
                start_nonce,
                sub_bytes,
                target_bytes,
                b_targets,
                stop_event,
                result_q,
                stats_q,
                chunk_size,
            ),
        )
        p.start()
        procs.append(p)

    found_nonce, found_hash = result_q.get()
    stop_event.set()
    for p in procs:
        p.join(timeout=2.0)
        if p.is_alive():
            p.terminate()
    t1 = time.time()

    stats_done.set()
    drain_thread.join(timeout=1.0)

    approx_hashes = found_nonce - start_nonce + 1
    hashes_checked = max(total_hashes_list[0], approx_hashes)

    return found_nonce, found_hash, hashes_checked, (t1 - t0)

def main():
    sub_bytes = bytes.fromhex("00a1a9e52e3b04917488c44b903881ac16fdd5d64fbfb3fb90a57b74dce7403e")
    diff_bits = 28
    target_int = 1 << (256 - diff_bits)
    target_bytes = target_int.to_bytes(32, "big")
    b_targets = (target_bytes[0], target_bytes[1], target_bytes[2], target_bytes[3], target_bytes[4])
    theoretical_mean = 2 ** diff_bits

    num_trials = 10
    num_workers = 5
    stride = (2 ** diff_bits) * 3

    print(f"=======================================================", flush=True)
    print(f"CPU Miner Statistical Test: {diff_bits}-bit Difficulty ({num_trials} Trials)", flush=True)
    print(f"Using {num_workers} worker processes (script/miner.py worker_proc)", flush=True)
    print(f"Target: 0x{target_bytes.hex()[:16]}... | Theoretical Mean 2^{diff_bits} = {theoretical_mean:,}", flush=True)
    print(f"=======================================================", flush=True)

    trial_hashes = []
    t_start = time.time()

    for i in range(num_trials):
        start_nonce = i * stride + 1
        nonce, h, hashes, dt = run_cpu_trial(sub_bytes, target_bytes, b_targets, start_nonce, num_workers=num_workers)
        
        valid, check_bytes, _ = miner.verify_share_local(sub_bytes, nonce, target_int)
        assert valid, f"Found nonce {nonce} failed local verification!"

        ratio = hashes / theoretical_mean
        trial_hashes.append(hashes)
        print(f"Trial {i+1:2d}/{num_trials}: start={start_nonce:,} hit={nonce:,} "
              f"hashes={hashes:,} ({ratio:.3f}x 2^{diff_bits}) in {dt:.1f}s", flush=True)

    t_total = time.time() - t_start
    arr = np.array(trial_hashes, dtype=np.float64)
    sample_mean = np.mean(arr)
    sample_std = np.std(arr, ddof=1)
    mean_ratio = sample_mean / theoretical_mean
    std_ratio = sample_std / theoretical_mean

    t_crit = 2.2622  # Student-t for df = 9 at 95% confidence
    se_ratio = (sample_std / math.sqrt(num_trials)) / theoretical_mean
    ci_low = mean_ratio - t_crit * se_ratio
    ci_high = mean_ratio + t_crit * se_ratio

    print(f"\n--- CPU Miner Summary for {diff_bits}-bit Difficulty ({num_trials} Trials) ---", flush=True)
    print(f"Total Test Time:    {t_total:.1f} s ({t_total/60:.2f} min)", flush=True)
    print(f"Theoretical Mean:   {theoretical_mean:,} hashes", flush=True)
    print(f"Sample Mean:        {sample_mean:,.1f} hashes", flush=True)
    print(f"Sample Std Dev:     {sample_std:,.1f} hashes", flush=True)
    print(f"Mean / 2^{diff_bits} Ratio:  {mean_ratio:.4f}", flush=True)
    print(f"Std / 2^{diff_bits} Ratio:   {std_ratio:.4f} (Theoretical: ~1.0000)", flush=True)
    print(f"Standard Error SE:  {se_ratio:.4f}", flush=True)
    print(f"95% Confidence Int: [{ci_low:.4f}, {ci_high:.4f}]", flush=True)
    print(f"Theoretical 1.0 in 95% CI: {ci_low <= 1.0 <= ci_high}", flush=True)
    print(f"=======================================================", flush=True)

if __name__ == "__main__":
    mp.freeze_support()
    main()
