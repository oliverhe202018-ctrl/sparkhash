#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brute-force Exhaustive Check
Range: nonce 0 to 2^28 - 1 (268,435,456 nonces)
Difficulty: 24 bits
Compares ALL hits found by CPU (PyCryptodome raw Keccak) vs GPU (OpenCL multi-hit).
Both hit sets must be 100% identical.
"""

import sys, os, time, struct
import multiprocessing as mp

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
try:
    from Crypto.Hash.keccak import _raw_keccak_lib
    from Crypto.Util._raw_api import ffi
except ImportError:
    _raw_keccak_lib = None
    ffi = None

if cl is None or np is None or ffi is None:
    pytestmark = pytest.mark.skip(reason="pyopencl, numpy, or cffi runtime not installed")

OPENCL_ALL_HITS_KERNEL = """
#define ROTL64(x, n) rotate((ulong)(x), (ulong)(n))

__constant ulong RC[24] = {
    0x0000000000000001UL, 0x0000000000008082UL, 0x800000000000808aUL, 0x8000000080008000UL,
    0x000000000000808bUL, 0x0000000080000001UL, 0x8000000080008081UL, 0x8000000000008009UL,
    0x000000000000008aUL, 0x0000000000000088UL, 0x0000000080008009UL, 0x000000008000000aUL,
    0x000000008000808bUL, 0x800000000000008bUL, 0x8000000000008089UL, 0x8000000000008003UL,
    0x8000000000008002UL, 0x8000000000000080UL, 0x000000000000800aUL, 0x800000008000000aUL,
    0x8000000080008081UL, 0x8000000000008080UL, 0x0000000080000001UL, 0x8000000080008008UL
};

inline ulong bswap64(ulong x) {
    return ((x & 0x00000000000000FFUL) << 56) |
           ((x & 0x000000000000FF00UL) << 40) |
           ((x & 0x0000000000FF0000UL) << 24) |
           ((x & 0x00000000FF000000UL) << 8)  |
           ((x & 0x000000FF00000000UL) >> 8)  |
           ((x & 0x0000FF0000000000UL) >> 24) |
           ((x & 0x00FF000000000000UL) >> 40) |
           ((x & 0xFF00000000000000UL) >> 56);
}

void keccak_f1600(ulong a[25]) {
    #pragma unroll
    for (int r = 0; r < 24; ++r) {
        ulong c0 = a[0] ^ a[5] ^ a[10] ^ a[15] ^ a[20];
        ulong c1 = a[1] ^ a[6] ^ a[11] ^ a[16] ^ a[21];
        ulong c2 = a[2] ^ a[7] ^ a[12] ^ a[17] ^ a[22];
        ulong c3 = a[3] ^ a[8] ^ a[13] ^ a[18] ^ a[23];
        ulong c4 = a[4] ^ a[9] ^ a[14] ^ a[19] ^ a[24];

        ulong d0 = c4 ^ ROTL64(c1, 1);
        ulong d1 = c0 ^ ROTL64(c2, 1);
        ulong d2 = c1 ^ ROTL64(c3, 1);
        ulong d3 = c2 ^ ROTL64(c4, 1);
        ulong d4 = c3 ^ ROTL64(c0, 1);

        ulong b[25];
        b[ 0] = ROTL64(a[ 0] ^ d0,  0);
        b[ 1] = ROTL64(a[ 6] ^ d1, 44);
        b[ 2] = ROTL64(a[12] ^ d2, 43);
        b[ 3] = ROTL64(a[18] ^ d3, 21);
        b[ 4] = ROTL64(a[24] ^ d4, 14);
        b[ 5] = ROTL64(a[ 3] ^ d3, 28);
        b[ 6] = ROTL64(a[ 9] ^ d4, 20);
        b[ 7] = ROTL64(a[10] ^ d0,  3);
        b[ 8] = ROTL64(a[16] ^ d1, 45);
        b[ 9] = ROTL64(a[22] ^ d2, 61);
        b[10] = ROTL64(a[ 1] ^ d1,  1);
        b[11] = ROTL64(a[ 7] ^ d2,  6);
        b[12] = ROTL64(a[13] ^ d3, 25);
        b[13] = ROTL64(a[19] ^ d4,  8);
        b[14] = ROTL64(a[20] ^ d0, 18);
        b[15] = ROTL64(a[ 4] ^ d4, 27);
        b[16] = ROTL64(a[ 5] ^ d0, 36);
        b[17] = ROTL64(a[11] ^ d1, 10);
        b[18] = ROTL64(a[17] ^ d2, 15);
        b[19] = ROTL64(a[23] ^ d3, 56);
        b[20] = ROTL64(a[ 2] ^ d2, 62);
        b[21] = ROTL64(a[ 8] ^ d3, 55);
        b[22] = ROTL64(a[14] ^ d4, 39);
        b[23] = ROTL64(a[15] ^ d0, 41);
        b[24] = ROTL64(a[21] ^ d1,  2);

        #pragma unroll
        for (int y = 0; y < 25; y += 5) {
            a[y + 0] = b[y + 0] ^ ((~b[y + 1]) & b[y + 2]);
            a[y + 1] = b[y + 1] ^ ((~b[y + 2]) & b[y + 3]);
            a[y + 2] = b[y + 2] ^ ((~b[y + 3]) & b[y + 4]);
            a[y + 3] = b[y + 3] ^ ((~b[y + 4]) & b[y + 0]);
            a[y + 4] = b[y + 4] ^ ((~b[y + 0]) & b[y + 1]);
        }

        a[0] ^= RC[r];
    }
}

__kernel void find_all_hits(
    ulong sub0, ulong sub1, ulong sub2, ulong sub3,
    ulong start_nonce,
    ulong target_hi0, ulong target_hi1, ulong target_hi2, ulong target_hi3,
    uint max_hits,
    __global volatile uint* found_count,
    __global ulong* hit_nonces
) {
    size_t gid = get_global_id(0);
    ulong nonce = start_nonce + (ulong)gid;

    ulong a[25];
    a[0] = sub0; a[1] = sub1; a[2] = sub2; a[3] = sub3;
    a[4] = 0; a[5] = 0; a[6] = 0;
    a[7] = bswap64(nonce);
    a[8] = 0x0000000000000001UL;
    a[9] = 0; a[10] = 0; a[11] = 0; a[12] = 0;
    a[13] = 0; a[14] = 0; a[15] = 0;
    a[16] = 0x8000000000000000UL;
    a[17] = 0; a[18] = 0; a[19] = 0; a[20] = 0;
    a[21] = 0; a[22] = 0; a[23] = 0; a[24] = 0;

    keccak_f1600(a);

    ulong h0 = bswap64(a[0]);
    ulong h1 = bswap64(a[1]);
    ulong h2 = bswap64(a[2]);
    ulong h3 = bswap64(a[3]);

    bool match = false;
    if (h0 < target_hi0) {
        match = true;
    } else if (h0 == target_hi0) {
        if (h1 < target_hi1) {
            match = true;
        } else if (h1 == target_hi1) {
            if (h2 < target_hi2) {
                match = true;
            } else if (h2 == target_hi2) {
                if (h3 <= target_hi3) {
                    match = true;
                }
            }
        }
    }

    if (match) {
        uint slot = atomic_inc(found_count);
        if (slot < max_hits) {
            hit_nonces[slot] = nonce;
        }
    }
}
"""

def cpu_search_chunk(sub_bytes, target_bytes, start_nonce, end_nonce, chunk_size, out_q):
    """CPU worker searching a sub-interval of nonces for all hits."""
    state_p = ffi.new("void **")
    _raw_keccak_lib.keccak_init(state_p, 64, 24)
    state = state_p[0]

    buf = ffi.new("uint8_t[64]")
    ffi.memmove(buf, sub_bytes, 32)
    out = ffi.new("uint8_t[32]")
    nonce_buf = ffi.buffer(buf + 32, 32)

    reset = _raw_keccak_lib.keccak_reset
    absorb = _raw_keccak_lib.keccak_absorb
    squeeze = _raw_keccak_lib.keccak_squeeze
    pack_into = struct.pack_into

    b0_t = target_bytes[0]
    b1_t = target_bytes[1]
    b2_t = target_bytes[2]

    hits = []
    curr = start_nonce
    while curr < end_nonce:
        chunk_end = min(curr + chunk_size, end_nonce)
        for nonce in range(curr, chunk_end):
            pack_into(">Q", nonce_buf, 24, nonce)
            reset(state)
            absorb(state, buf, 64)
            squeeze(state, out, 32, 0x01)

            # 24-bit difficulty prefix check
            if out[0] == 0 and out[1] == 0 and out[2] <= b2_t:
                h = bytes(ffi.buffer(out, 32))
                if h <= target_bytes:
                    hits.append(nonce)
        curr = chunk_end

    _raw_keccak_lib.keccak_destroy(state)
    out_q.put(hits)

def run_exhaustive_comparison():
    sub_bytes = bytes.fromhex("00a1a9e52e3b04917488c44b903881ac16fdd5d64fbfb3fb90a57b74dce7403e")
    diff_bits = 24
    target_int = 1 << (256 - diff_bits)
    target_bytes = target_int.to_bytes(32, "big")
    target_lanes = struct.unpack(">4Q", target_bytes)

    total_nonces = 2 ** 28  # 268,435,456
    print(f"============================================================")
    print(f"Brute-Force Exhaustive Check: Nonce 0 to 2^28 - 1 ({total_nonces:,})")
    print(f"Difficulty: {diff_bits} bits | Expected Hits: ~{total_nonces // (2**diff_bits)}")
    print(f"Target: 0x{target_bytes.hex()}")
    print(f"============================================================")

    # 1. Run GPU Exhaustive Search
    print("\n[GPU] Scanning 2^28 nonces across 16 batches...")
    devices = miner_gpu.detect_devices()
    dev = devices[0]["device_obj"]
    ctx = cl.Context([dev])
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, OPENCL_ALL_HITS_KERNEL).build()
    knl = cl.Kernel(prg, "find_all_hits")

    sub_lanes = struct.unpack("<4Q", sub_bytes)
    batch_size = 16 * 1024 * 1024  # 16,777,216 nonces / batch
    num_batches = total_nonces // batch_size
    max_hits_per_batch = 1024

    found_count_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=4)
    hit_nonces_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=max_hits_per_batch * 8)
    zero = np.zeros(1, dtype=np.uint32)

    gpu_hits = []
    t_gpu_0 = time.time()
    for b_idx in range(num_batches):
        start_nonce = b_idx * batch_size
        cl.enqueue_copy(queue, found_count_buf, zero)
        knl(
            queue, (batch_size,), (256,),
            np.uint64(sub_lanes[0]), np.uint64(sub_lanes[1]), np.uint64(sub_lanes[2]), np.uint64(sub_lanes[3]),
            np.uint64(start_nonce),
            np.uint64(target_lanes[0]), np.uint64(target_lanes[1]), np.uint64(target_lanes[2]), np.uint64(target_lanes[3]),
            np.uint32(max_hits_per_batch),
            found_count_buf, hit_nonces_buf
        )
        count_np = np.empty(1, dtype=np.uint32)
        cl.enqueue_copy(queue, count_np, found_count_buf)
        queue.finish()

        hit_cnt = int(count_np[0])
        if hit_cnt > 0:
            hits_arr = np.empty(hit_cnt, dtype=np.uint64)
            cl.enqueue_copy(queue, hits_arr, hit_nonces_buf)
            queue.finish()
            for h_nonce in hits_arr:
                gpu_hits.append(int(h_nonce))

    t_gpu_1 = time.time()
    gpu_hits.sort()
    print(f"[GPU] Finished {total_nonces:,} nonces in {t_gpu_1 - t_gpu_0:.3f} s!")
    print(f"[GPU] Found {len(gpu_hits)} hits: {gpu_hits}")

    # 2. Run CPU Exhaustive Search with 6 workers
    num_cpu_workers = 6
    chunk_per_worker = total_nonces // num_cpu_workers
    print(f"\n[CPU] Scanning 2^28 nonces using {num_cpu_workers} PyCryptodome raw workers...")

    out_q = mp.Queue()
    procs = []
    t_cpu_0 = time.time()
    for w_idx in range(num_cpu_workers):
        w_start = w_idx * chunk_per_worker
        w_end = (w_idx + 1) * chunk_per_worker if w_idx < num_cpu_workers - 1 else total_nonces
        p = mp.Process(
            target=cpu_search_chunk,
            args=(sub_bytes, target_bytes, w_start, w_end, 50000, out_q)
        )
        p.start()
        procs.append(p)

    cpu_hits = []
    for _ in range(num_cpu_workers):
        res = out_q.get()
        cpu_hits.extend(res)

    for p in procs:
        p.join()
    t_cpu_1 = time.time()
    cpu_hits.sort()

    print(f"[CPU] Finished {total_nonces:,} nonces in {t_cpu_1 - t_cpu_0:.3f} s!")
    print(f"[CPU] Found {len(cpu_hits)} hits: {cpu_hits}")

    # 3. Direct Set Equivalence Comparison
    print(f"\n==================== COMPARISON VERDICT ====================")
    print(f"Total Nonces Checked: {total_nonces:,}")
    print(f"GPU Hits Count:       {len(gpu_hits)}")
    print(f"CPU Hits Count:       {len(cpu_hits)}")

    missing_in_gpu = set(cpu_hits) - set(gpu_hits)
    missing_in_cpu = set(gpu_hits) - set(cpu_hits)

    print(f"Hits Missing in GPU:  {list(missing_in_gpu)}")
    print(f"Hits Missing in CPU:  {list(missing_in_cpu)}")

    assert len(gpu_hits) > 0, "No hits found!"
    assert gpu_hits == cpu_hits, f"Discrepancy detected between CPU and GPU hits!"
    print(f"VERDICT: SUCCESS! CPU AND GPU HITS ARE 100% IDENTICAL!")
    print(f"============================================================")

    # Detailed list of every hit with its Keccak hash
    print(f"\nExhaustive Hits Table ({len(gpu_hits)} hits in [0, 2^28)):")
    for idx, nonce in enumerate(gpu_hits):
        valid, h_bytes, _ = miner_gpu.verify_share_local(sub_bytes, nonce, target_int)
        assert valid
        print(f"  Hit {idx+1:2d}: Nonce={nonce:9d} (0x{nonce:07x}) | Hash=0x{h_bytes.hex()}")

if __name__ == "__main__":
    mp.freeze_support()
    run_exhaustive_comparison()
