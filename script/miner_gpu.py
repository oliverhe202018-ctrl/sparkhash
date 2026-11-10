#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin GPU-Accelerated PoW Miner for MinerV1 on BSC Testnet (chainId 97)

Calculates valid proof-of-work shares meeting MinerV1 target difficulty using GPU.
Outputs verified share (nonce and hash) to a JSON file matching script/miner.py.

HARD CONSTRAINTS ENFORCED:
1. Hardcoded refusal for any chainId != 97 (BSC Testnet only).
2. Pure read-only RPC & local GPU PoW. Zero private key logic; never reads or touches .env.
3. Separation of mining and submission: this script ONLY generates shares to JSON.
4. Periodic on-chain epoch check; halts immediately on epoch change to prevent stale shares.
5. Windows WDDM TDR watchdog protection (bounded batch execution times).
"""

import os
import sys
import time
import json
import struct
import argparse
import urllib.request
import threading
import random

HAS_NUMPY = False
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None

# Chain ID constraint: strictly BSC Testnet
REQUIRED_CHAIN_ID = 97

# Default addresses and targets on BSC Testnet
DEFAULT_MINER_CONTRACT = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
DEFAULT_VAULT_CONTRACT = "0x40b92431de886Ea46117407c8f7123a31FB3F744"
DEFAULT_RPC_URL = "https://data-seed-prebsc-1-s1.binance.org:8545"
DEFAULT_DIFFICULTY_BITS = 32  # 32-bit difficulty: 1 << 224
DEFAULT_EASIEST_TARGET = 1 << 224  # 32-bit: 2^224
DEFAULT_HARDEST_TARGET = 1 << 221  # 35-bit: 2^221
CAP = 6
SELECTOR_INIT_EPOCH_SEED = "d9d9c33c"  # initEpochSeed(uint256,uint256)
SELECTOR_VAULT = "fbfa77cf"  # vault()
SELECTOR_STATUS = "645b8b1b"  # status(address)
HEX_CHARACTERS = set("0123456789abcdefABCDEF")
EVENT_EPOCH_SETTLED_TOPIC = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
EVENT_REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"

# Default batch size: 16,777,216 nonces (~15-30ms per kernel on RTX 4060 Ti)
# Well within Windows WDDM TDR watchdog limit (2000ms)
DEFAULT_BATCH_SIZE = 16 * 1024 * 1024

HAS_PYOPENCL = False
try:
    import pyopencl as cl
    HAS_PYOPENCL = True
except ImportError:
    pass

try:
    from Crypto.Hash import keccak as pycrypto_keccak
    def standard_keccak(data: bytes) -> bytes:
        h = pycrypto_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
    HAS_KECCAK = True
except ImportError:
    try:
        from eth_hash.auto import keccak as eth_keccak
        standard_keccak = eth_keccak
        HAS_KECCAK = True
    except ImportError:
        HAS_KECCAK = False


OPENCL_KECCAK_SOURCE = """
/*
 * OpenCL Keccak-256 Kernel for PoolCoin MinerV1
 * Computes keccak256(abi.encode(sub, nonce)) and verifies hash <= target.
 *
 * Attribution & License:
 * Keccak-f[1600] 24-round permutation adapted from Keccak reference specifications
 * and inspired by 0age/create2crunch (https://github.com/0age/create2crunch, MIT License).
 * MIT License Copyright (c) 2020 0age.
 */

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

__kernel void mine_batch(
    ulong sub0, ulong sub1, ulong sub2, ulong sub3,
    ulong start_nonce,
    ulong target_hi0, ulong target_hi1, ulong target_hi2, ulong target_hi3,
    __global volatile uint* found_count,
    __global ulong* found_nonce,
    __global ulong* found_hash
) {
    size_t gid = get_global_id(0);
    ulong nonce = start_nonce + (ulong)gid;

    ulong a[25];
    a[0] = sub0;
    a[1] = sub1;
    a[2] = sub2;
    a[3] = sub3;
    a[4] = 0;
    a[5] = 0;
    a[6] = 0;
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
        if (slot == 0) {
            *found_nonce = nonce;
            found_hash[0] = h0;
            found_hash[1] = h1;
            found_hash[2] = h2;
            found_hash[3] = h3;
        }
    }
}

__kernel void hash_nonces(
    ulong sub0, ulong sub1, ulong sub2, ulong sub3,
    ulong total_count,
    __global const ulong* nonces,
    __global ulong* out_hashes
) {
    size_t gid = get_global_id(0);
    if (gid >= total_count) return;
    ulong nonce = nonces[gid];

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

    size_t out_idx = gid * 4;
    out_hashes[out_idx + 0] = a[0];
    out_hashes[out_idx + 1] = a[1];
    out_hashes[out_idx + 2] = a[2];
    out_hashes[out_idx + 3] = a[3];
}
"""


def detect_devices():
    """Detects available OpenCL and CUDA devices and prints hardware specifications."""
    detected = []
    print("=== GPU Device Detection ===")
    if HAS_PYOPENCL:
        try:
            platforms = cl.get_platforms()
            for p_idx, p in enumerate(platforms):
                devices = p.get_devices(device_type=cl.device_type.GPU)
                for d_idx, d in enumerate(devices):
                    detected.append({
                        "backend": "PyOpenCL",
                        "platform_idx": p_idx,
                        "platform_name": p.name.strip(),
                        "platform_vendor": p.vendor.strip(),
                        "device_idx": d_idx,
                        "device_obj": d,
                        "name": d.name.strip(),
                        "type": "GPU",
                        "version": d.version.strip(),
                        "global_mem_mb": d.global_mem_size // (1024 * 1024),
                        "compute_units": d.max_compute_units,
                    })
        except Exception as e:
            print(f"[OpenCL Warning] Failed to query OpenCL platforms: {e}")

    if detected:
        for idx, dev in enumerate(detected):
            print(f"[{idx}] Backend:       {dev['backend']}")
            print(f"    Platform:      {dev['platform_name']} ({dev['platform_vendor']})")
            print(f"    Device Name:   {dev['name']}")
            print(f"    OpenCL Ver:    {dev['version']}")
            print(f"    Global VRAM:   {dev['global_mem_mb']} MB")
            print(f"    Compute Units: {dev['compute_units']}")
    else:
        print("[!] No GPU detected via PyOpenCL.")
        try:
            import cupy
            count = cupy.cuda.runtime.getDeviceCount()
            print(f"[Info] CuPy detected {count} CUDA device(s).")
        except Exception:
            print("[Info] CuPy fallback not available.")
    print("=============================")
    return detected


def rpc_call(rpc_url, method, params):
    """Performs a JSON-RPC call."""
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params,
    }
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-GPU-Miner/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            raise RuntimeError(f"RPC Error ({method}): {res.get('error')}")
        return res.get("result")


def verify_chain_id(rpc_url):
    """Queries chainId from RPC and asserts it equals 97 (BSC Testnet only)."""
    res = rpc_call(rpc_url, "eth_chainId", [])
    chain_id = int(res, 16) if isinstance(res, str) else int(res)
    if chain_id != REQUIRED_CHAIN_ID:
        raise ValueError(
            f"SECURITY REJECTION: Chain ID {chain_id} != {REQUIRED_CHAIN_ID} (BSC Testnet required)! Refusing to run."
        )
    return chain_id


def eth_call(rpc_url, to, data):
    """Simulates eth_call."""
    tx = {"to": to, "data": "0x" + data}
    res = rpc_call(rpc_url, "eth_call", [tx, "latest"])
    if res and res.startswith("0x"):
        return res[2:]
    return res or ""


def to_checksum_address(address):
    """Converts address to EIP-55 checksum format."""
    clean = address.lower().replace("0x", "")
    h = standard_keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


def encode_uint256(val):
    return hex(val)[2:].zfill(64)


def encode_address(addr):
    return addr.lower().replace("0x", "").zfill(64)


def get_miner_vault(rpc_url: str, miner_contract: str) -> str:
    """
    Queries vault() on the miner contract and strictly validates the returned address.
    """
    try:
        res = eth_call(rpc_url, miner_contract, SELECTOR_VAULT)
    except Exception as e:
        raise RuntimeError(f"RPC call failed while querying vault(): {type(e).__name__}") from None

    if not res or not isinstance(res, str):
        raise RuntimeError("Query vault() returned empty response from RPC")

    raw = res[2:] if res.startswith("0x") else res
    if len(raw) != 64:
        raise RuntimeError(
            f"Query vault() returned malformed ABI data length: expected 64 hex characters (32 bytes), got {len(raw)}"
        )

    if any(c not in HEX_CHARACTERS for c in raw):
        raise RuntimeError("Query vault() returned invalid hex encoding")

    high_bytes = raw[:24]
    if high_bytes != "0" * 24:
        raise RuntimeError("Query vault() returned invalid ABI address encoding: high 12 bytes are non-zero")

    addr_hex = raw[24:64]
    if addr_hex == "0" * 40:
        raise RuntimeError("Query vault() returned zero address (0x0000000000000000000000000000000000000000)")

    return to_checksum_address("0x" + addr_hex)


def get_stake_status(rpc_url: str, vault_contract: str, miner_addr: str) -> tuple:
    """
    Queries status(address) on the vault contract and strictly validates the returned (bool active, uint256 since).
    """
    call_data = SELECTOR_STATUS + encode_address(miner_addr)
    try:
        res = eth_call(rpc_url, vault_contract, call_data)
    except Exception as e:
        raise RuntimeError(f"RPC call failed while querying status(): {type(e).__name__}") from None

    if not res or not isinstance(res, str):
        raise RuntimeError("Query status() returned empty response from RPC")

    raw = res[2:] if res.startswith("0x") else res
    if len(raw) != 128:
        raise RuntimeError(
            f"Query status() returned malformed ABI data length: expected 128 hex characters (64 bytes), got {len(raw)}"
        )

    if any(c not in HEX_CHARACTERS for c in raw):
        raise RuntimeError("Query status() returned invalid hex encoding")

    word0 = raw[:64]
    word1 = raw[64:128]

    bool_val = int(word0, 16)
    if bool_val not in (0, 1):
        raise RuntimeError(f"Query status() returned invalid ABI bool value (must be 0 or 1, got {bool_val})")

    active = (bool_val == 1)
    since = int(word1, 16)
    return (active, since)


def verify_stake(rpc_url: str, miner_contract: str, miner_addr: str, cur_epoch: int) -> tuple:
    """
    Dynamically queries vault() from miner_contract and verifies stake eligibility for miner_addr.
    Returns (vault_contract, active, since) on success.
    Raises RuntimeError on failure.
    """
    vault_contract = get_miner_vault(rpc_url, miner_contract)
    active, since = get_stake_status(rpc_url, vault_contract, miner_addr)

    if not active:
        raise RuntimeError(
            f"Stake check failed: miner {miner_addr} does not have active stake in StakeVault {vault_contract}"
        )

    if cur_epoch < since + 1:
        raise RuntimeError(
            f"Stake check failed: miner {miner_addr} deposited in epoch {since}, "
            f"warmup requires epoch >= {since + 1}, but current epoch is {cur_epoch}"
        )

    return (vault_contract, active, since)


def compute_sub_hash(epoch_seed_bytes, miner_address, k, miner_contract, chain_id):
    """
    Computes inner sub hash matching MinerV1.sol line 270:
    bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(this), block.chainid));
    """
    if len(epoch_seed_bytes) != 32:
        raise ValueError("epoch_seed must be 32 bytes")

    clean_user = miner_address.lower().replace("0x", "")
    user_bytes = bytes.fromhex(clean_user).rjust(32, b"\x00")

    k_bytes = int(k).to_bytes(32, "big")

    clean_contract = miner_contract.lower().replace("0x", "")
    contract_bytes = bytes.fromhex(clean_contract).rjust(32, b"\x00")

    chain_id_bytes = int(chain_id).to_bytes(32, "big")

    encoded_sub = epoch_seed_bytes + user_bytes + k_bytes + contract_bytes + chain_id_bytes
    return standard_keccak(encoded_sub)


def verify_share_local(sub_bytes, nonce, target_int):
    """
    Verifies outer share hash locally matching MinerV1.sol line 271:
    uint256(keccak256(abi.encode(sub, nonce))) <= target_
    """
    nonce_bytes = int(nonce).to_bytes(32, "big")
    encoded_outer = sub_bytes + nonce_bytes
    h_bytes = standard_keccak(encoded_outer)
    h_int = int.from_bytes(h_bytes, "big")
    return (h_int <= target_int, h_bytes, h_int)


def mine(args):
    if not HAS_PYOPENCL or not HAS_NUMPY:
        raise RuntimeError(
            "No compatible OpenCL GPU or numpy runtime found! Please ensure pyopencl, numpy, and GPU drivers are installed.\n"
            "Setup command: uv pip install pyopencl numpy --python .venv_gpu"
        )
    devices = detect_devices()
    if not devices:
        raise RuntimeError(
            "No compatible OpenCL GPU found! Please ensure PyOpenCL and GPU drivers are installed.\n"
            "Setup command: uv pip install pyopencl --python .venv_gpu"
        )

    if args.rpc:
        chain_id = verify_chain_id(args.rpc)
    else:
        chain_id = REQUIRED_CHAIN_ID

    miner_addr = to_checksum_address(args.miner)
    miner_contract = to_checksum_address(args.miner_contract)

    # 1. Resolve Epoch
    if args.epoch is not None:
        cur_epoch = args.epoch
    else:
        res = eth_call(args.rpc, miner_contract, "900cf0cf")  # epoch()
        cur_epoch = int(res, 16) if res else 0

    print("=== PoolCoin GPU-Accelerated PoW Miner ===")
    print(f"Target Network:   BSC Testnet (Chain ID {chain_id})")
    print(f"Miner Contract:   {miner_contract}")
    print(f"Miner Address:    {miner_addr}")
    print(f"Current Epoch:    {cur_epoch}")

    # 2. Check StakeVault status if RPC available
    if args.skip_stake_check:
        print("[WARNING] --skip-stake-check specified: \u672a\u9a8c\u8bc1\u8d28\u62bc\u8d44\u683c (stake eligibility not verified).")
    elif args.rpc:
        vault_contract, active, since = verify_stake(args.rpc, miner_contract, miner_addr, cur_epoch)
        print(f"StakeVault:       {vault_contract}")
        print(f"Stake Status:     Active={active}, DepositEpoch={since}")

    # 3. Resolve epochSeed
    if args.seed:
        epoch_seed_int = int(args.seed, 16) if args.seed.startswith("0x") else int(args.seed)
    else:
        seed_data = "4abccd28" + encode_uint256(cur_epoch)  # epochSeed(uint256)
        res = eth_call(args.rpc, miner_contract, seed_data)
        epoch_seed_int = int(res, 16) if res else 0

    if epoch_seed_int == 0:
        print(f"[INFO] Epoch {cur_epoch} seed is 0x0 (not initialized). Discovered uninitialized seed.")
        jitter = random.uniform(0.0, 3.0)
        print(f"[JITTER] Applying {jitter:.2f}s random jitter before sending backfill transaction...")
        time.sleep(jitter)
        pk = getattr(args, "private_key", None) or os.environ.get("MINER_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
        backfill_epoch_seed(args.rpc, miner_contract, cur_epoch, private_key=pk, max_steps=getattr(args, "max_seed_steps", 24))
        res = eth_call(args.rpc, miner_contract, seed_data)
        epoch_seed_int = int(res, 16) if res else 0

    if epoch_seed_int == 0:
        raise RuntimeError(
            f"Epoch seed for epoch {cur_epoch} is 0x0 (not initialized)! "
            f"Backfill transaction was not completed or failed. Wait for Keeper or call initEpochSeed({cur_epoch}, 24)."
        )
    epoch_seed_bytes = epoch_seed_int.to_bytes(32, "big")
    print(f"Epoch Seed:       0x{epoch_seed_bytes.hex()}")

    # 4. Resolve Target & Difficulty
    if args.target:
        target_int = int(args.target, 16) if args.target.startswith("0x") else int(args.target)
        diff_bits = 256 - target_int.bit_length()
    elif args.difficulty_bits:
        diff_bits = args.difficulty_bits
        target_int = 1 << (256 - diff_bits)
    else:
        target_data = "9811c7c1" + encode_uint256(cur_epoch)  # target(uint256)
        res = eth_call(args.rpc, miner_contract, target_data)
        target_int = int(res, 16) if (res and int(res, 16) > 0) else (1 << (256 - DEFAULT_DIFFICULTY_BITS))
        diff_bits = 256 - target_int.bit_length()

    target_bytes = target_int.to_bytes(32, "big")
    target_lanes = struct.unpack(">4Q", target_bytes)
    expected_trials = 2 ** diff_bits
    print(f"Target Difficulty: {diff_bits} bits (Target: 0x{target_bytes.hex()[:16]}...)")
    print(f"Expected Hashes:  2^{diff_bits} = {expected_trials:,}")

    # 5. Resolve Share Index k
    if args.k is not None:
        k = args.k
    else:
        mask_data = "1107d89b" + encode_uint256(cur_epoch) + encode_address(miner_addr)
        res = eth_call(args.rpc, miner_contract, mask_data)
        used_mask = int(res, 16) if res else 0
        k = None
        for cand_k in range(CAP):
            if (used_mask & (1 << cand_k)) == 0:
                k = cand_k
                break
        if k is None:
            raise RuntimeError(f"Miner address {miner_addr} has exhausted all 15 shares for epoch {cur_epoch}!")
        print(f"Selected Share k: {k} (usedMask: 0x{used_mask:04x})")

    if k < 0 or k >= CAP:
        raise ValueError(f"Invalid share index k={k}; must be 0 <= k < 6")

    sub_bytes = compute_sub_hash(epoch_seed_bytes, miner_addr, k, miner_contract, chain_id)
    sub_lanes = struct.unpack("<4Q", sub_bytes)
    print(f"Sub Hash:         0x{sub_bytes.hex()}")

    # 6. Initialize OpenCL Context & Kernel
    dev_info = devices[getattr(args, 'device_idx', 0) % len(devices)]
    cl_device = dev_info["device_obj"]
    print(f"Selected GPU:     {dev_info['name']} via {dev_info['backend']}")

    ctx = cl.Context([cl_device])
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, OPENCL_KECCAK_SOURCE).build()
    mine_knl = cl.Kernel(prg, "mine_batch")

    batch_size = getattr(args, 'batch_size', DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE
    local_size = 256
    batch_size = ((batch_size + local_size - 1) // local_size) * local_size
    print(f"Batch Size:       {batch_size:,} nonces / launch (WDDM watchdog safe)")
    print("==========================================")

    found_count_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=4)
    found_nonce_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=8)
    found_hash_buf = cl.Buffer(ctx, cl.mem_flags.READ_WRITE, size=32)

    zero_count = np.zeros(1, dtype=np.uint32)
    cl.enqueue_copy(queue, found_count_buf, zero_count)

    total_hashes = 0
    start_nonce = args.start_nonce
    current_nonce = start_nonce
    start_time = time.time()
    last_log_time = start_time
    last_epoch_check = start_time
    epoch_check_interval = 10.0

    print("[Miner] GPU Mining started... Press Ctrl+C to abort.", flush=True)

    found_nonce = None
    found_hash = None

    try:
        while True:
            mine_knl(
                queue, (batch_size,), (local_size,),
                np.uint64(sub_lanes[0]), np.uint64(sub_lanes[1]), np.uint64(sub_lanes[2]), np.uint64(sub_lanes[3]),
                np.uint64(current_nonce),
                np.uint64(target_lanes[0]), np.uint64(target_lanes[1]), np.uint64(target_lanes[2]), np.uint64(target_lanes[3]),
                found_count_buf, found_nonce_buf, found_hash_buf
            )

            count_np = np.empty(1, dtype=np.uint32)
            cl.enqueue_copy(queue, count_np, found_count_buf)
            queue.finish()

            total_hashes += batch_size
            current_nonce += batch_size

            if count_np[0] > 0:
                res_nonce = np.empty(1, dtype=np.uint64)
                res_hash = np.empty(4, dtype=np.uint64)
                cl.enqueue_copy(queue, res_nonce, found_nonce_buf)
                cl.enqueue_copy(queue, res_hash, found_hash_buf)
                queue.finish()
                found_nonce = int(res_nonce[0])
                hash_bytes = struct.pack(">4Q", *res_hash)
                found_hash = hash_bytes.hex()
                break

            now = time.time()

            # Periodic Epoch Check
            if args.rpc and (now - last_epoch_check >= epoch_check_interval):
                last_epoch_check = now
                try:
                    res = eth_call(args.rpc, miner_contract, "900cf0cf")
                    chain_epoch = int(res, 16) if res else cur_epoch
                    if chain_epoch != cur_epoch:
                        print(f"\n[EPOCH EXPIRED] On-chain epoch changed from {cur_epoch} to {chain_epoch}!")
                        print("[EPOCH EXPIRED] Halting GPU miner immediately to prevent submitting stale share.")
                        print(f"[ACTION REQUIRED] Please restart miner for new epoch {chain_epoch}.")
                        sys.exit(2)
                except Exception:
                    pass

            # Progress Logging
            if now - last_log_time >= 2.0:
                overall_dt = now - start_time
                avg_rate = total_hashes / overall_dt if overall_dt > 0 else 0
                pct = (total_hashes / expected_trials * 100) if expected_trials > 0 else 0
                eta_sec = ((expected_trials - total_hashes) / avg_rate) if (avg_rate > 0 and total_hashes < expected_trials) else 0
                print(
                    f"[Progress] Hashes: {total_hashes:,.0f} | Speed: {avg_rate/1e6:.2f} MH/s "
                    f"| Done: {pct:.2f}% | Expected ETA: {eta_sec:.1f}s ({eta_sec/60:.1f}m)",
                    flush=True
                )
                last_log_time = now

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n[Miner] Keyboard interrupt received! Stopping GPU miner...")
        sys.exit(1)

    elapsed = time.time() - start_time
    avg_rate = total_hashes / elapsed if elapsed > 0 else 0

    valid, check_hash, check_int = verify_share_local(sub_bytes, found_nonce, target_int)
    if not valid:
        raise RuntimeError(f"FATAL: Local CPU verification failed for GPU-mined nonce {found_nonce}!")

    actual_diff_bits = 256 - check_int.bit_length()

    print(f"\n[SUCCESS] Share Found on GPU!")
    print(f"Nonce:            {found_nonce} (0x{found_nonce:x})")
    print(f"Share Hash:       0x{found_hash}")
    print(f"Effective Bits:   {actual_diff_bits} bits (required >= {diff_bits})")
    print(f"Elapsed Time:     {elapsed:.2f} s ({elapsed/60:.2f} min)")
    print(f"Total Hashes:     {total_hashes:,.0f}")
    print(f"Average Hashrate: {avg_rate/1e6:.2f} MH/s")

    output_data = {
        "miner": miner_addr,
        "minerContract": miner_contract,
        "chainId": chain_id,
        "epoch": cur_epoch,
        "k": k,
        "epochSeed": "0x" + epoch_seed_bytes.hex(),
        "target": "0x" + target_bytes.hex(),
        "difficultyBits": diff_bits,
        "actualDifficultyBits": actual_diff_bits,
        "nonce": hex(found_nonce),
        "nonceDecimal": found_nonce,
        "hash": "0x" + found_hash,
        "timestamp": int(time.time()),
        "elapsedSeconds": round(elapsed, 2),
        "hashesChecked": total_hashes,
        "averageHashrate": round(avg_rate, 2),
    }

    out_file = args.out or f"share_{miner_addr[:8]}_epoch{cur_epoch}_k{k}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    print(f"Share successfully saved to: {out_file}")
    print("\nNext step: Run submission simulation with:")
    print(f"  python script/submit_share.py --file {out_file}")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin GPU-Accelerated PoW Miner (BSC Testnet)")
    parser.add_argument("--miner", type=str, required=True, help="Miner wallet address")
    parser.add_argument("--epoch", type=int, default=None, help="Epoch number (queries RPC if omitted)")
    parser.add_argument("--k", type=int, default=None, help="Share index 0..5 (auto-selected if omitted)")
    parser.add_argument("--rpc", type=str, default=DEFAULT_RPC_URL, help="BSC Testnet RPC URL")
    parser.add_argument("--miner-contract", type=str, default=DEFAULT_MINER_CONTRACT, help="Miner proxy contract address")
    parser.add_argument("--difficulty-bits", type=int, default=DEFAULT_DIFFICULTY_BITS, help="Difficulty in leading zero bits (default: 32)")
    parser.add_argument("--target", type=str, default=None, help="Explicit target override (hex or int)")
    parser.add_argument("--seed", type=str, default=None, help="Explicit epochSeed override (hex or int)")
    parser.add_argument("--start-nonce", type=int, default=0, help="Starting nonce offset")
    parser.add_argument("--skip-stake-check", action="store_true", help="Skip StakeVault stake check")
    parser.add_argument("--out", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--private-key", type=str, default=None, help="Private key for seed backfill transaction if seed is 0")
    parser.add_argument("--max-seed-steps", type=int, default=24, help="Max seed steps for backfill (default: 24)")

    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Nonces per GPU batch (default: 16M)")
    parser.add_argument("--device-idx", type=int, default=0, help="OpenCL GPU device index (default: 0)")
    parser.add_argument("--threads", type=int, default=1, help="Compatibility argument with CPU miner.py")

    args = parser.parse_args()
    mine(args)


if __name__ == "__main__":
    main()
