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
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch, MagicMock

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


class MiningStoppedAtGpuBoundary(Exception):
    """
    Test sentinel exception raised at the deterministic boundary
    immediately after prechecks/hashing and before real OpenCL GPU initialization.
    """
    pass


class TestDynamicVaultAndStakeEligibility(unittest.TestCase):
    """
    Offline regression tests for dynamic StakeVault resolution and eligibility checks.
    Fully mocked: no real RPC, no real GPU, no transactions.
    """
    MINER_PROXY_A = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
    VAULT_B = "0xBaDC0de000000000000000000000000000000002"
    MINER_ADDR = "0x2e9e19f7F1D3a2b01ba5BbA7c37E58398568d82c"

    def _encode_address_ret(self, addr_hex: str) -> str:
        clean = addr_hex.lower().replace("0x", "")
        return "0" * 24 + clean

    def _encode_status_ret(self, active: bool, since: int) -> str:
        w0 = (1 if active else 0).to_bytes(32, "big").hex()
        w1 = since.to_bytes(32, "big").hex()
        return w0 + w1

    def test_dynamic_vault_resolution(self):
        """A. Dynamic Vault Resolution: queries vault() on miner proxy to find Vault B instead of DEFAULT_VAULT_CONTRACT."""
        calls = []

        def mock_eth_call(rpc_url, to, data):
            calls.append((to.lower(), data))
            if to.lower() == self.MINER_PROXY_A.lower() and data == miner_gpu.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_B)
            if to.lower() == self.VAULT_B.lower() and data.startswith(miner_gpu.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=0)
            return ""

        with patch("script.miner_gpu.eth_call", side_effect=mock_eth_call):
            vault_res, active, since = miner_gpu.verify_stake(
                "http://mock-rpc", self.MINER_PROXY_A, self.MINER_ADDR, cur_epoch=1
            )
            self.assertEqual(vault_res.lower(), self.VAULT_B.lower())
            self.assertTrue(active)
            self.assertEqual(since, 0)

            # Assert status query went to Vault B, never to DEFAULT_VAULT_CONTRACT
            queried_addresses = [c[0] for c in calls]
            self.assertIn(self.MINER_PROXY_A.lower(), queried_addresses)
            self.assertIn(self.VAULT_B.lower(), queried_addresses)
            self.assertNotIn(miner_gpu.DEFAULT_VAULT_CONTRACT.lower(), queried_addresses)

    def test_normal_eligibility(self):
        """B. Normal eligibility: active=true, depositEpoch=0, epoch=1 passes."""
        def mock_eth_call(rpc_url, to, data):
            if data == miner_gpu.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_B)
            if data.startswith(miner_gpu.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=0)
            return ""

        with patch("script.miner_gpu.eth_call", side_effect=mock_eth_call):
            vault, active, since = miner_gpu.verify_stake(
                "http://mock-rpc", self.MINER_PROXY_A, self.MINER_ADDR, cur_epoch=1
            )
            self.assertTrue(active)
            self.assertEqual(since, 0)
            self.assertEqual(vault.lower(), self.VAULT_B.lower())

    def test_not_staked_fails_before_gpu(self):
        """C. Not staked: active=false raises error and exits before GPU starts."""
        def mock_eth_call(rpc_url, to, data):
            if data == miner_gpu.SELECTOR_EPOCH:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data == miner_gpu.SELECTOR_PAUSED:
                return "0x" + "0" * 64
            if data == miner_gpu.SELECTOR_LAST_SYNCED:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data.startswith(miner_gpu.SELECTOR_COUNTED):
                return "0x" + "0" * 64
            if data == miner_gpu.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_B)
            if data.startswith(miner_gpu.SELECTOR_STATUS):
                return self._encode_status_ret(active=False, since=0)
            return ""

        with patch("script.miner_gpu.eth_call", side_effect=mock_eth_call):
            with self.assertRaises(RuntimeError) as ctx:
                miner_gpu.verify_stake(
                    "http://mock-rpc", self.MINER_PROXY_A, self.MINER_ADDR, cur_epoch=1
                )
            self.assertIn("does not have active stake", str(ctx.exception))

        # Test within mine() lifecycle: verify GPU is never reached
        class DummyArgs:
            miner = self.MINER_ADDR
            miner_contract = self.MINER_PROXY_A
            rpc = "http://mock-rpc"
            epoch = 1
            skip_stake_check = False
            k = 0
            seed = "0x" + "12" * 32
            target = hex(1 << 224)
            difficulty_bits = 32
            start_nonce = 0
            out = None
            batch_size = 1024
            device_idx = 0
            threads = 1

        mock_cl = MagicMock()
        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("GPU boundary reached")

        f = io.StringIO()
        with patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=mock_eth_call), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]) as mock_detect, \
             patch("script.miner_gpu.cl", mock_cl), \
             redirect_stdout(f):
            with self.assertRaises(RuntimeError) as ctx_mine:
                miner_gpu.mine(DummyArgs())
            self.assertIn("does not have active stake", str(ctx_mine.exception))
            mock_detect.assert_called_once()
            mock_cl.Context.assert_not_called()
            mock_cl.CommandQueue.assert_not_called()
            mock_cl.Program.assert_not_called()
            mock_cl.Kernel.assert_not_called()

    def test_cooldown_warmup_rule(self):
        """D. Cooldown/warmup: depositEpoch=1, epoch=1 fails before GPU; epoch=2 passes."""
        def mock_eth_call(rpc_url, to, data):
            if data == miner_gpu.SELECTOR_EPOCH:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data == miner_gpu.SELECTOR_PAUSED:
                return "0x" + "0" * 64
            if data == miner_gpu.SELECTOR_LAST_SYNCED:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data.startswith(miner_gpu.SELECTOR_COUNTED):
                return "0x" + "0" * 64
            if data == miner_gpu.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_B)
            if data.startswith(miner_gpu.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=1)
            return ""

        with patch("script.miner_gpu.eth_call", side_effect=mock_eth_call):
            # epoch 1: warmup requirement not met -> raises RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                miner_gpu.verify_stake(
                    "http://mock-rpc", self.MINER_PROXY_A, self.MINER_ADDR, cur_epoch=1
                )
            self.assertIn("warmup requires epoch >=", str(ctx.exception))

            # epoch 2: warmup requirement met (2 >= 1 + 1) -> passes
            vault, active, since = miner_gpu.verify_stake(
                "http://mock-rpc", self.MINER_PROXY_A, self.MINER_ADDR, cur_epoch=2
            )
            self.assertTrue(active)
            self.assertEqual(since, 1)

        # Test within mine() lifecycle: cooldown insufficient fails before GPU
        class CooldownArgs:
            miner = self.MINER_ADDR
            miner_contract = self.MINER_PROXY_A
            rpc = "http://mock-rpc"
            epoch = 1
            skip_stake_check = False
            k = 0
            seed = "0x" + "12" * 32
            target = hex(1 << 224)
            difficulty_bits = 32
            start_nonce = 0
            out = None
            batch_size = 1024
            device_idx = 0
            threads = 1

        mock_cl = MagicMock()
        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("GPU boundary reached")

        f = io.StringIO()
        with patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=mock_eth_call), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]) as mock_detect, \
             patch("script.miner_gpu.cl", mock_cl), \
             redirect_stdout(f):
            with self.assertRaises(RuntimeError) as ctx_mine:
                miner_gpu.mine(CooldownArgs())
            self.assertIn("warmup requires epoch >=", str(ctx_mine.exception))
            mock_detect.assert_called_once()
            mock_cl.Context.assert_not_called()
            mock_cl.CommandQueue.assert_not_called()
            mock_cl.Program.assert_not_called()
            mock_cl.Kernel.assert_not_called()

    def test_vault_abi_errors(self):
        """E. vault() ABI errors: RPC error, empty, malformed length, non-hex, whitespace, zero address, non-zero high bytes fail without fallback."""
        # 1. RPC failure
        with self.subTest("RPC failure"):
            with patch("script.miner_gpu.eth_call", side_effect=RuntimeError("network error")):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                self.assertIn("RPC call failed", str(ctx.exception))

        # 2. Empty return
        for empty_val in ("", None):
            with self.subTest(f"Empty return: {empty_val}"):
                with patch("script.miner_gpu.eth_call", return_value=empty_val):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                    self.assertIn("empty response", str(ctx.exception))

        # 3. Malformed length
        for malformed in ("1234", "0" * 63, "0" * 65, "0" * 128):
            with self.subTest(f"Malformed length: {len(malformed)}"):
                with patch("script.miner_gpu.eth_call", return_value=malformed):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                    self.assertIn("malformed ABI data length", str(ctx.exception))

        # 4. Valid address with 0x prefix
        with self.subTest("Valid address with 0x prefix"):
            valid_with_0x = "0x" + "0" * 24 + self.VAULT_B[2:].lower()
            with patch("script.miner_gpu.eth_call", return_value=valid_with_0x):
                resolved = miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                self.assertEqual(resolved.lower(), self.VAULT_B.lower())

        # 5. Non-hex characters
        with self.subTest("Non-hex characters"):
            with patch("script.miner_gpu.eth_call", return_value="zz" * 32):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                self.assertIn("invalid hex encoding", str(ctx.exception))

        # 6. Exact length 64 containing whitespace (space, tab, newline, carriage return)
        whitespace_cases = {
            "leading space": " " + "0" * 63,
            "trailing space": "0" * 63 + " ",
            "middle space": "0" * 31 + " " + "0" * 32,
            "tab": "0" * 63 + "\t",
            "newline": "0" * 63 + "\n",
            "carriage return": "0" * 63 + "\r",
        }
        for desc, bad_val in whitespace_cases.items():
            with self.subTest(f"Whitespace in vault(): {desc}"):
                with patch("script.miner_gpu.eth_call", return_value=bad_val):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                    self.assertIn("invalid hex encoding", str(ctx.exception))

        # 7. Zero address
        with self.subTest("Zero address"):
            with patch("script.miner_gpu.eth_call", return_value="0" * 64):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                self.assertIn("zero address", str(ctx.exception))

        # 8. Non-zero high bytes
        with self.subTest("Non-zero high bytes"):
            bad_high = "0" * 22 + "01" + self.VAULT_B[2:].lower()
            with patch("script.miner_gpu.eth_call", return_value=bad_high):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_miner_vault("http://mock-rpc", self.MINER_PROXY_A)
                self.assertIn("high 12 bytes are non-zero", str(ctx.exception))

    def test_status_abi_errors(self):
        """F. status() ABI errors: RPC error, empty, malformed length, non-hex, whitespace, invalid bool fail without fallback."""
        # 1. RPC failure
        with self.subTest("RPC failure"):
            with patch("script.miner_gpu.eth_call", side_effect=RuntimeError("network error")):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                self.assertIn("RPC call failed", str(ctx.exception))

        # 2. Empty return
        for empty_val in ("", None):
            with self.subTest(f"Empty return: {empty_val}"):
                with patch("script.miner_gpu.eth_call", return_value=empty_val):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                    self.assertIn("empty response", str(ctx.exception))

        # 3. Malformed length
        for bad_len in ("0" * 64, "0" * 126, "0" * 130):
            with self.subTest(f"Malformed length: {len(bad_len)}"):
                with patch("script.miner_gpu.eth_call", return_value=bad_len):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                    self.assertIn("malformed ABI data length", str(ctx.exception))

        # 4. Valid status with 0x prefix
        with self.subTest("Valid status with 0x prefix"):
            valid_with_0x = "0x" + self._encode_status_ret(active=True, since=0)
            with patch("script.miner_gpu.eth_call", return_value=valid_with_0x):
                active, since = miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                self.assertTrue(active)
                self.assertEqual(since, 0)

        # 5. Non-hex characters
        for non_hex in ("zz" * 64, "0" * 116 + "deadbeefbad!"):
            self.assertEqual(len(non_hex), 128)
            with self.subTest(f"Non-hex in status()"):
                with patch("script.miner_gpu.eth_call", return_value=non_hex):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                    self.assertIn("invalid hex encoding", str(ctx.exception))

        # 6. Exact length 128 containing whitespace (space, tab, newline, carriage return)
        whitespace_cases = {
            "leading space": " " + "0" * 127,
            "trailing space": "0" * 127 + " ",
            "middle space": "0" * 63 + " " + "0" * 64,
            "tab": "0" * 127 + "\t",
            "newline": "0" * 127 + "\n",
            "carriage return": "0" * 127 + "\r",
        }
        for desc, bad_val in whitespace_cases.items():
            with self.subTest(f"Whitespace in status(): {desc}"):
                with patch("script.miner_gpu.eth_call", return_value=bad_val):
                    with self.assertRaises(RuntimeError) as ctx:
                        miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                    self.assertIn("invalid hex encoding", str(ctx.exception))

        # 7. Invalid bool: value 2
        with self.subTest("Invalid bool value 2"):
            bad_bool_2 = (2).to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex()
            with patch("script.miner_gpu.eth_call", return_value=bad_bool_2):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                self.assertIn("invalid ABI bool", str(ctx.exception))

        # 8. Invalid bool: high byte non-zero
        with self.subTest("Invalid bool high byte non-zero"):
            bad_bool_high = "01" + "00" * 31 + (0).to_bytes(32, "big").hex()
            with patch("script.miner_gpu.eth_call", return_value=bad_bool_high):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.get_stake_status("http://mock-rpc", self.VAULT_B, self.MINER_ADDR)
                self.assertIn("invalid ABI bool", str(ctx.exception))

    def test_skip_stake_check(self):
        """G. --skip-stake-check: skips stake queries, prints unverified warning, reaches GPU boundary without calling real OpenCL."""
        calls = []
        def mock_eth_call(rpc_url, to, data):
            calls.append((to, data))
            if data == miner_gpu.SELECTOR_EPOCH:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data == miner_gpu.SELECTOR_PAUSED:
                return "0x" + "0" * 64
            if data == miner_gpu.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_B)
            if data == miner_gpu.SELECTOR_LAST_SYNCED:
                return "0x" + (1).to_bytes(32, "big").hex()
            if data.startswith(miner_gpu.SELECTOR_COUNTED):
                return "0x" + "0" * 64
            return "0x1"

        class DummyArgs:
            miner = self.MINER_ADDR
            miner_contract = self.MINER_PROXY_A
            rpc = "http://mock-rpc"
            epoch = 1
            skip_stake_check = True
            seed = "0x" + "12" * 32
            target = hex(1 << 224)
            k = 0
            difficulty_bits = 32
            start_nonce = 0
            out = None
            batch_size = 1024
            device_idx = 0
            threads = 1

        mock_cl = MagicMock()
        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("Stopped at OpenCL Context boundary")

        f = io.StringIO()
        with patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=mock_eth_call), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.cl", mock_cl), \
             redirect_stdout(f):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(DummyArgs())

        # Assert deterministic stopping point reached and real OpenCL never invoked
        mock_cl.Context.assert_called_once()
        mock_cl.CommandQueue.assert_not_called()
        mock_cl.Program.assert_not_called()
        mock_cl.Kernel.assert_not_called()

        output = f.getvalue()
        self.assertIn("未验证质押资格", output)
        self.assertNotIn("Stake Status:     Active=True", output)
        for to, data in calls:
            self.assertFalse(data.startswith(miner_gpu.SELECTOR_STATUS))

    def test_offline_mode_without_rpc(self):
        """H. Offline mode without RPC: retains existing offline mining capability, reaching GPU boundary with zero network calls."""
        class OfflineArgs:
            miner = self.MINER_ADDR
            miner_contract = self.MINER_PROXY_A
            rpc = None
            epoch = 1
            skip_stake_check = False
            seed = "0x" + "12" * 32
            target = hex(1 << 224)
            k = 0
            difficulty_bits = 32
            start_nonce = 0
            out = None
            batch_size = 1024
            device_idx = 0
            threads = 1

        calls = []
        def mock_eth_call(rpc_url, to, data):
            calls.append((to, data))
            return ""

        mock_cl = MagicMock()
        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("Stopped at OpenCL Context boundary")

        f = io.StringIO()
        with patch("script.miner_gpu.eth_call", side_effect=mock_eth_call), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.cl", mock_cl), \
             redirect_stdout(f):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(OfflineArgs())

        # Assert deterministic stopping point reached and real OpenCL never invoked
        mock_cl.Context.assert_called_once()
        mock_cl.CommandQueue.assert_not_called()
        mock_cl.Program.assert_not_called()
        mock_cl.Kernel.assert_not_called()

        # Zero network calls
        self.assertEqual(len(calls), 0)

    def test_error_message_redaction(self):
        """Error messages redact sensitive RPC URLs and credentials."""
        sensitive_rpc = "https://user:password_secret@bsc-testnet.rpc:8545"
        with patch("script.miner_gpu.eth_call", side_effect=Exception(f"failed {sensitive_rpc}")):
            with self.assertRaises(RuntimeError) as ctx1:
                miner_gpu.get_miner_vault(sensitive_rpc, self.MINER_PROXY_A)
            self.assertNotIn("password_secret", str(ctx1.exception))
            self.assertNotIn(sensitive_rpc, str(ctx1.exception))

            with self.assertRaises(RuntimeError) as ctx2:
                miner_gpu.get_stake_status(sensitive_rpc, self.VAULT_B, self.MINER_ADDR)
            self.assertNotIn("password_secret", str(ctx2.exception))
            self.assertNotIn(sensitive_rpc, str(ctx2.exception))


class TestMinerGpuPrechecksAndSecurity(unittest.TestCase):
    MINER_ADDR = "0x2e9e19f7F1D3a2b01ba5BbA7c37E58398568d82c"
    MINER_CONTRACT = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
    VAULT_CONTRACT = "0x40b92431de886Ea46117407c8f7123a31FB3F744"
    TEST_SEED = "0x" + "cd" * 32

    def _make_args(self, **overrides):
        from types import SimpleNamespace
        base = dict(
            miner=self.MINER_ADDR,
            miner_contract=self.MINER_CONTRACT,
            rpc="http://mock-rpc",
            epoch=None,
            k=0,
            seed=None,
            target=None,
            difficulty_bits=None,
            start_nonce=0,
            skip_stake_check=False,
            allow_backfill=False,
            max_seed_steps=24,
            out=None,
            batch_size=1024,
            device_idx=0,
            threads=1,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _make_eth_call(self, *, miner_paused=0, vault_paused=0, onchain_epoch=10, last_synced=10,
                       counted=0, seed=None, target=None, used_mask=0, stake_active=True, stake_since=1):
        seed_hex = seed if seed is not None else self.TEST_SEED
        target_hex = target if target is not None else hex(1 << 223)

        def mock_eth_call(rpc_url, to, data):
            if data == miner_gpu.SELECTOR_EPOCH:
                return hex(onchain_epoch)
            if data == miner_gpu.SELECTOR_PAUSED:
                if to.lower() == self.MINER_CONTRACT.lower():
                    return hex(miner_paused)
                return hex(vault_paused)
            if data == miner_gpu.SELECTOR_VAULT:
                return "0x" + "0" * 24 + self.VAULT_CONTRACT[2:].lower()
            if data == miner_gpu.SELECTOR_LAST_SYNCED:
                return hex(last_synced)
            if data.startswith(miner_gpu.SELECTOR_COUNTED):
                return hex(counted)
            if data.startswith(miner_gpu.SELECTOR_STATUS):
                return ("1" if stake_active else "0").zfill(64) + hex(stake_since)[2:].zfill(64)
            if data.startswith(miner_gpu.SELECTOR_EPOCH_SEED):
                return seed_hex
            if data.startswith(miner_gpu.SELECTOR_TARGET):
                return target_hex
            if data.startswith(miner_gpu.SELECTOR_USED_MASK):
                return hex(used_mask)
            raise AssertionError(f"Unexpected eth_call: to={to}, data={data}")

        return mock_eth_call

    def test_zero_seed_without_allow_backfill_no_send_no_name_error(self):
        mock_cl = MagicMock()
        pk_dummy = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
        with patch.dict(os.environ, {"MINER_PRIVATE_KEY": pk_dummy, "PRIVATE_KEY": pk_dummy}, clear=False), \
             patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(seed="0x0")), \
             patch("script.miner_gpu.rpc_call") as mock_rpc, \
             patch("script.miner_gpu.cl", mock_cl, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                miner_gpu.mine(self._make_args(allow_backfill=False))
            self.assertIn("Keeper", str(ctx.exception))
            mock_rpc.assert_not_called()
            mock_cl.Context.assert_not_called()

    def test_zero_seed_with_allow_backfill_calls_defined_backfill_function(self):
        txs_sent = []

        def mock_rpc_call(rpc_url, method, params):
            if method == "eth_getTransactionCount":
                return "0x2"
            if method == "eth_gasPrice":
                return "0x5f5e100"
            if method == "eth_sendRawTransaction":
                txs_sent.append(params[0])
                return "0xhash_gpu_bf"
            raise AssertionError(f"Unexpected rpc_call: {method}")

        pk_dummy = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
        mock_cl = MagicMock()
        with patch.dict(os.environ, {"MINER_PRIVATE_KEY": pk_dummy}, clear=False), \
             patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(seed="0x0")), \
             patch("script.miner_gpu.rpc_call", side_effect=mock_rpc_call), \
             patch("time.sleep"), \
             patch("random.uniform", return_value=1.25), \
             patch("script.miner_gpu.cl", mock_cl, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                miner_gpu.mine(self._make_args(allow_backfill=True))
            self.assertIn("not initialized", str(ctx.exception))
            self.assertEqual(len(txs_sent), 1)

    def test_onchain_target_zero_exits_and_difficulty_bits_defaults_to_none(self):
        with patch.object(sys, "argv", ["miner_gpu.py", "--miner", self.MINER_ADDR]):
            with patch("script.miner_gpu.mine") as mock_mine:
                miner_gpu.main()
                parsed_args = mock_mine.call_args[0][0]
                self.assertIsNone(parsed_args.difficulty_bits)
                self.assertFalse(parsed_args.allow_backfill)

        mock_cl = MagicMock()
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(target="0x0")), \
             patch("script.miner_gpu.cl", mock_cl, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                miner_gpu.mine(self._make_args())
            self.assertIn("target", str(ctx.exception).lower())
            mock_cl.Context.assert_not_called()

        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("GPU boundary")
        f = io.StringIO()
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(target=hex(1 << 224))), \
             patch("script.miner_gpu.cl", mock_cl, create=True), \
             redirect_stdout(f):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(self._make_args())
            self.assertIn("Target Difficulty: 32.00 bits", f.getvalue())

        f2 = io.StringIO()
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(target="0x0")), \
             patch("script.miner_gpu.cl", mock_cl, create=True), \
             redirect_stdout(f2):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(self._make_args(difficulty_bits=20))
            self.assertIn("Target Difficulty: 20.00 bits", f2.getvalue())

    def test_prechecks_block_paused_lag_counted_and_cap6(self):
        mock_cl = MagicMock()
        for kwargs, expected_msg in [
            ({"miner_paused": 1}, "Miner contract"),
            ({"vault_paused": 1}, "StakeVault contract"),
            ({"onchain_epoch": 12, "last_synced": 10}, "lastSynced()=10 != epoch()=12"),
            ({"counted": 6}, "CAP (6)"),
            ({"used_mask": 0x3F}, "exhausted all 6 shares"),
        ]:
            use_k = None if "used_mask" in kwargs else 0
            with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
                 patch.object(miner_gpu, "HAS_NUMPY", True), \
                 patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
                 patch("script.miner_gpu.verify_chain_id", return_value=97), \
                 patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(**kwargs)), \
                 patch("script.miner_gpu.cl", mock_cl, create=True):
                with self.assertRaises(RuntimeError) as ctx:
                    miner_gpu.mine(self._make_args(k=use_k))
                self.assertIn(expected_msg, str(ctx.exception))
                mock_cl.Context.assert_not_called()

    def test_out_existing_file_refuses_overwrite_and_private_key_flag_removed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "existing_gpu.json")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write('{"keep_gpu": true}')
            mock_cl = MagicMock()
            with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
                 patch.object(miner_gpu, "HAS_NUMPY", True), \
                 patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
                 patch("script.miner_gpu.verify_chain_id", return_value=97), \
                 patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call()), \
                 patch("script.miner_gpu.cl", mock_cl, create=True):
                with self.assertRaises(FileExistsError):
                    miner_gpu.mine(self._make_args(out=out_path))
                mock_cl.Context.assert_not_called()
            with open(out_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"keep_gpu": true}')

        with patch.object(sys, "argv", ["miner_gpu.py", "--miner", self.MINER_ADDR, "--private-key", "0x" + "11" * 32]):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as sysex:
                    miner_gpu.main()
                self.assertEqual(sysex.exception.code, 2)

    def test_target_zero_explicit_and_rpc_rejected(self):
        mock_cl = MagicMock()
        # Explicit target=0
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call()), \
             patch("script.miner_gpu.cl", mock_cl, create=True):
            with self.assertRaises(ValueError) as ctx:
                miner_gpu.mine(self._make_args(target="0"))
            self.assertIn("Explicit --target must be > 0", str(ctx.exception))
            mock_cl.Context.assert_not_called()

        # Target 2^224 displays approx 2^32.00
        mock_cl.Context.side_effect = MiningStoppedAtGpuBoundary("GPU boundary")
        f = io.StringIO()
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(target=hex(1 << 224))), \
             patch("script.miner_gpu.cl", mock_cl, create=True), \
             redirect_stdout(f):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(self._make_args())
            out = f.getvalue()
            self.assertIn("Target Difficulty: 32.00 bits", out)
            self.assertIn("Expected Hashes:  2^32.00 ≈ 4,294,967,296", out)

        # Non power of 2 target: target = 3 * (1 << 223)
        # Expected trials = 2^256 / (3 * 2^223) = 2^33 / 3 ≈ 2,863,311,531
        non_pow2_target = 3 * (1 << 223)
        f_non_pow2 = io.StringIO()
        with patch.object(miner_gpu, "HAS_PYOPENCL", True), \
             patch.object(miner_gpu, "HAS_NUMPY", True), \
             patch("script.miner_gpu.detect_devices", return_value=[{"device_obj": MagicMock(), "name": "MockGPU", "backend": "Mock"}]), \
             patch("script.miner_gpu.verify_chain_id", return_value=97), \
             patch("script.miner_gpu.eth_call", side_effect=self._make_eth_call(target=hex(non_pow2_target))), \
             patch("script.miner_gpu.cl", mock_cl, create=True), \
             redirect_stdout(f_non_pow2):
            with self.assertRaises(MiningStoppedAtGpuBoundary):
                miner_gpu.mine(self._make_args())
            out_np = f_non_pow2.getvalue()
            self.assertIn("2,863,311,531", out_np)
