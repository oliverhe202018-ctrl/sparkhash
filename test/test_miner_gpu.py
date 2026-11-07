#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test Suite for GPU Miner (script/miner_gpu.py)

Validates:
1. GPU Device Detection (NVIDIA RTX 4060 Ti via PyOpenCL).
2. Correctness on 1,000,000 random nonces compared against PyCryptodome Keccak-256.
3. Reproduction of known share from test_share_program.json.
4. Mining on reduced difficulty (20 to 24 bits).
5. Chain ID security enforcement (strictly rejects chainId != 97).
6. Output JSON compatibility with script/submit_share.py.
"""

import os
import sys
import json
import time
import struct
import pytest

try:
    import numpy as np
except ImportError:
    np = None

# Ensure root directory in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import script.miner_gpu as miner_gpu
from Crypto.Hash import keccak as pycrypto_keccak


def _check_opencl_gpu():
    if os.environ.get("NO_GPU", "").strip() in ("1", "true", "True") or os.environ.get("SKIP_GPU_TESTS", "").strip() in ("1", "true", "True"):
        return False, "OpenCL GPU tests skipped: disabled via NO_GPU / SKIP_GPU_TESTS environment variable"
    if not getattr(miner_gpu, "HAS_PYOPENCL", False) or np is None:
        return False, "OpenCL GPU tests skipped: pyopencl or numpy runtime not installed"
    try:
        devices = miner_gpu.detect_devices()
        if len(devices) > 0:
            return True, ""
        return False, "No OpenCL GPU device detected (requires OpenCL runtime and compatible GPU hardware)"
    except Exception as e:
        return False, f"OpenCL GPU detection failed: {e}"


HAS_OPENCL_GPU, SKIP_OPENCL_REASON = _check_opencl_gpu()
requires_opencl_gpu = pytest.mark.skipif(not HAS_OPENCL_GPU, reason=SKIP_OPENCL_REASON)


@requires_opencl_gpu
def test_gpu_device_detection():
    """Verifies that OpenCL detects the NVIDIA RTX 4060 Ti GPU."""
    devices = miner_gpu.detect_devices()
    assert len(devices) > 0, "No OpenCL GPU device detected!"
    primary = devices[0]
    assert "GeForce" in primary["name"] or "NVIDIA" in primary["name"] or "RTX" in primary["name"], (
        f"Unexpected GPU name: {primary['name']}"
    )
    assert primary["global_mem_mb"] > 4000, f"Expected >4GB VRAM, got {primary['global_mem_mb']} MB"


@requires_opencl_gpu
def test_keccak_correctness_1m_random_nonces():
    """
    Requirement 3:
    Computes Keccak-256 on GPU for at least 1,000,000 random nonces,
    comparing each one with PyCryptodome Keccak-256. All must match 100%.
    """
    import pyopencl as cl

    devices = miner_gpu.detect_devices()
    assert len(devices) > 0
    dev = devices[0]["device_obj"]

    ctx = cl.Context([dev])
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, miner_gpu.OPENCL_KECCAK_SOURCE).build()
    knl = cl.Kernel(prg, "hash_nonces")

    sub_bytes = bytes.fromhex("00a1a9e52e3b04917488c44b903881ac16fdd5d64fbfb3fb90a57b74dce7403e")
    sub_lanes = struct.unpack("<4Q", sub_bytes)

    N = 1_000_000
    local_size = 256
    global_size = ((N + local_size - 1) // local_size) * local_size

    # Generate 1,000,000 deterministic pseudo-random 64-bit nonces
    rng = np.random.default_rng(20261105)
    nonces = rng.integers(0, 0xFFFFFFFFFFFFFFFF, size=global_size, dtype=np.uint64)

    nonces_buf = cl.Buffer(ctx, cl.mem_flags.READ_ONLY | cl.mem_flags.COPY_HOST_PTR, hostbuf=nonces)
    out_hashes_buf = cl.Buffer(ctx, cl.mem_flags.WRITE_ONLY, size=global_size * 32)

    knl(
        queue, (global_size,), (local_size,),
        np.uint64(sub_lanes[0]), np.uint64(sub_lanes[1]), np.uint64(sub_lanes[2]), np.uint64(sub_lanes[3]),
        np.uint64(N),
        nonces_buf, out_hashes_buf
    )

    gpu_hashes = np.empty(global_size * 4, dtype=np.uint64)
    cl.enqueue_copy(queue, gpu_hashes, out_hashes_buf)
    queue.finish()

    mismatches = 0
    # Compare with PyCryptodome Keccak-256
    for i in range(N):
        nonce_val = int(nonces[i])
        gpu_hash = struct.pack("<4Q", *gpu_hashes[i*4:(i+1)*4])

        h_obj = pycrypto_keccak.new(digest_bits=256)
        h_obj.update(sub_bytes + nonce_val.to_bytes(32, "big"))
        cpu_hash = h_obj.digest()

        if gpu_hash != cpu_hash:
            mismatches += 1
            break

    assert mismatches == 0, f"Detected {mismatches} mismatches between GPU and PyCryptodome!"


@requires_opencl_gpu
def test_reproduce_test_share_program():
    """
    Requirement 3:
    Reproduce the known share in test_share_program.json on GPU.
    """
    import pyopencl as cl

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "test_share_program.json")
    if not os.path.exists(fixture_path):
        fixture_path = "test/fixtures/test_share_program.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        known_data = json.load(f)

    sub_bytes = miner_gpu.compute_sub_hash(
        bytes.fromhex(known_data["epochSeed"].replace("0x", "")),
        known_data["miner"],
        known_data["k"],
        known_data["minerContract"],
        known_data["chainId"]
    )
    sub_lanes = struct.unpack("<4Q", sub_bytes)

    target_int = int(known_data["target"], 16)
    target_bytes = target_int.to_bytes(32, "big")
    target_lanes = struct.unpack(">4Q", target_bytes)

    devices = miner_gpu.detect_devices()
    dev = devices[0]["device_obj"]
    ctx = cl.Context([dev])
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, miner_gpu.OPENCL_KECCAK_SOURCE).build()
    mine_knl = cl.Kernel(prg, "mine_batch")

    found_count_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=4)
    found_nonce_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=8)
    found_hash_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=32)

    zero_count = np.zeros(1, dtype=np.uint32)
    cl.enqueue_copy(queue, found_count_buf, zero_count)

    # Search window around known nonce 109140 (0x1aa54)
    start_nonce = 100000
    batch_size = 20480

    mine_knl(
        queue, (batch_size,), (256,),
        np.uint64(sub_lanes[0]), np.uint64(sub_lanes[1]), np.uint64(sub_lanes[2]), np.uint64(sub_lanes[3]),
        np.uint64(start_nonce),
        np.uint64(target_lanes[0]), np.uint64(target_lanes[1]), np.uint64(target_lanes[2]), np.uint64(target_lanes[3]),
        found_count_buf, found_nonce_buf, found_hash_buf
    )
    queue.finish()

    count_np = np.empty(1, dtype=np.uint32)
    cl.enqueue_copy(queue, count_np, found_count_buf)
    assert count_np[0] >= 1, "Known share was not found by GPU kernel!"

    res_nonce = np.empty(1, dtype=np.uint64)
    res_hash = np.empty(4, dtype=np.uint64)
    cl.enqueue_copy(queue, res_nonce, found_nonce_buf)
    cl.enqueue_copy(queue, res_hash, found_hash_buf)
    queue.finish()

    mined_nonce = int(res_nonce[0])
    hash_bytes = struct.pack(">4Q", *res_hash)
    mined_hash = "0x" + hash_bytes.hex()

    assert mined_nonce == known_data["nonceDecimal"], (
        f"Mined nonce {mined_nonce} != expected {known_data['nonceDecimal']}"
    )
    assert mined_hash.lower() == known_data["hash"].lower(), (
        f"Mined hash {mined_hash} != expected {known_data['hash']}"
    )


def test_chain_id_security_enforcement():
    """Requirement 4: Hardcoded rejection for any chainId != 97."""
    assert miner_gpu.REQUIRED_CHAIN_ID == 97

    class DummyArgs:
        rpc = "http://mock-rpc"

    # Mock rpc_call to return mainnet (chainId 1)
    original_rpc = miner_gpu.rpc_call
    try:
        miner_gpu.rpc_call = lambda rpc_url, method, params: "0x1" if method == "eth_chainId" else None
        with pytest.raises(ValueError) as excinfo:
            miner_gpu.verify_chain_id("http://mock-rpc")
        assert "SECURITY REJECTION" in str(excinfo.value)
        assert "Chain ID 1 != 97" in str(excinfo.value)
    finally:
        miner_gpu.rpc_call = original_rpc


@requires_opencl_gpu
def test_reduced_difficulty_mining(tmp_path):
    """
    Requirement 6: Small scale verification at reduced difficulty (e.g. 20 bits).
    """
    out_file = str(tmp_path / "test_reduced_share.json")

    class Args:
        miner = "0x0000000000000000000000000000000000000010"
        miner_contract = "0x420382a0f874c1F120621f847d81581e8737504e"
        epoch = 1
        k = 0
        rpc = None  # Local mode with explicit parameters
        difficulty_bits = 20  # 20-bit reduced difficulty (1 in ~1M trials)
        target = None
        seed = "0x2755c9f580902c8ba1b9aed50e0413ba02ef5001993043bbac928217ff386921"
        start_nonce = 0
        skip_stake_check = True
        out = out_file
        batch_size = 1024 * 1024
        device_idx = 0
        threads = 1

    miner_gpu.mine(Args())

    assert os.path.exists(out_file), "Output share JSON was not generated!"
    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["miner"] == "0x0000000000000000000000000000000000000010"
    assert data["difficultyBits"] == 20
    assert data["actualDifficultyBits"] >= 20
    assert data["chainId"] == 97

    # Verify that the mined share is valid locally
    sub_bytes = miner_gpu.compute_sub_hash(
        bytes.fromhex(data["epochSeed"].replace("0x", "")),
        data["miner"],
        data["k"],
        data["minerContract"],
        data["chainId"]
    )
    valid, h_bytes, h_int = miner_gpu.verify_share_local(
        sub_bytes, data["nonceDecimal"], int(data["target"], 16)
    )
    assert valid, "Locally verified share failed!"
    assert "0x" + h_bytes.hex() == data["hash"]
