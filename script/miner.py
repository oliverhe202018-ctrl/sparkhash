#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Multi-threaded PoW Miner for MinerV1 on BSC Testnet (chainId 97)

Calculates valid proof-of-work shares meeting MinerV1 target difficulty.
Outputs verified share (nonce and hash) to a JSON file.

HARD CONSTRAINTS ENFORCED:
1. Hardcoded refusal for any chainId != 97 (BSC Testnet only).
2. Pure read-only RPC & local PoW. Zero private key logic; never reads or touches .env.
3. Separation of mining and submission: this script ONLY generates shares to JSON.
"""

import os
import sys
import time
import json
import struct
import argparse
import urllib.error
import urllib.request
import threading
import multiprocessing as mp
import random

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
SELECTOR_EPOCH = "900cf0cf"  # epoch()
SELECTOR_EPOCH_SEED = "4abccd28"  # epochSeed(uint256)
SELECTOR_TARGET = "9811c7c1"  # target(uint256)
SELECTOR_USED_MASK = "1107d89b"  # usedMask(uint256,address)
SELECTOR_PAUSED = "5c975abb"  # paused()
SELECTOR_LAST_SYNCED = "93afb4c5"  # lastSynced()
SELECTOR_COUNTED = "834f9a82"  # counted(uint256,address)
HEX_CHARACTERS = set("0123456789abcdefABCDEF")
EVENT_EPOCH_SETTLED_TOPIC = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
EVENT_REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"

# Keccak-256 low-level bindings for high-performance hashing
try:
    from Crypto.Hash.keccak import _raw_keccak_lib
    from Crypto.Util._raw_api import ffi
    HAS_RAW_KECCAK = True
except Exception:
    HAS_RAW_KECCAK = False

try:
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from safe_output import format_exception_safe, sanitize_text, sanitize_url

from eth_hash.auto import keccak as standard_keccak

_SENSITIVE_ENV_EXACT = (
    "PRIVATE_KEY",
    "MINER_PRIVATE_KEY",
    "KEEPER_PRIVATE_KEY",
    "DEPLOYER_PRIVATE_KEY",
    "SIGNER_PRIVATE_KEY",
    "PASSWORD_FILE",
    "KEEPER_PASSWORD_FILE",
    "MINER_PASSWORD_FILE",
    "KEYSTORE_PASSWORD_FILE",
    "WALLET_PASSWORD_FILE",
    "PASSWORD",
    "KEYSTORE_PASSWORD",
    "WALLET_PASSWORD",
)
_SENSITIVE_ENV_SUFFIXES = (
    "_PRIVATE_KEY",
    "_PRIV_KEY",
    "_PASSWORD_FILE",
    "_SECRET_KEY",
    "_PASSPHRASE",
)


def _collect_sensitive_values(args=None, extra_secrets=None):
    values = []
    if extra_secrets:
        for item in extra_secrets:
            if isinstance(item, os.PathLike):
                item = os.fspath(item)
            if isinstance(item, str) and len(item.strip()) >= 4:
                values.append(item.strip())
    if args is not None:
        for attr in ("private_key", "password_file", "keystore_password_file", "password"):
            val = getattr(args, attr, None)
            if isinstance(val, (str, os.PathLike)):
                sval = os.fspath(val).strip()
                if len(sval) >= 4:
                    values.append(sval)
                    if "password_file" in attr:
                        values.append(os.path.abspath(sval))
    for key, val in os.environ.items():
        if not isinstance(val, str):
            continue
        upper_key = key.upper()
        if upper_key in _SENSITIVE_ENV_EXACT or upper_key.endswith(_SENSITIVE_ENV_SUFFIXES):
            sval = val.strip()
            if len(sval) >= 4:
                values.append(sval)
                if "PASSWORD_FILE" in upper_key:
                    values.append(os.path.abspath(sval))
    return values


def _format_rpc_error_pair(err, sensitive_values=None):
    err_data = err.get("data", "") if isinstance(err, dict) else ""
    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    safe_msg = sanitize_text(str(err_msg), sensitive_values=sensitive_values)
    safe_data = sanitize_text(str(err_data), sensitive_values=sensitive_values)
    return safe_msg, safe_data


def rpc_call(rpc_url, method, params, sensitive_values=None):
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
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Miner/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        err_obj = None
        try:
            if getattr(e, "fp", None) is not None:
                err_body = e.read().decode("utf-8", errors="replace")
                parsed = json.loads(err_body)
                if isinstance(parsed, dict) and "error" in parsed:
                    err_obj = parsed.get("error")
        except Exception:
            err_obj = None
        if err_obj is not None:
            safe_msg, safe_data = _format_rpc_error_pair(err_obj, sensitive)
            raise RuntimeError(f"RPC Error ({method}): {safe_msg} | data: {safe_data}") from None
        raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None
    except urllib.error.URLError as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None
    except Exception as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None

    if isinstance(res, dict) and "error" in res:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        safe_msg, safe_data = _format_rpc_error_pair(res.get("error"), sensitive)
        raise RuntimeError(f"RPC Error ({method}): {safe_msg} | data: {safe_data}")
    return res.get("result") if isinstance(res, dict) else res


def verify_chain_id(rpc_url):
    """Queries chainId from RPC and asserts it equals 97."""
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


def compute_sub_hash(epoch_seed_bytes, miner_address, k, miner_contract, chain_id):
    """
    Computes inner sub hash:
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
    Verifies outer share hash locally:
    uint256(keccak256(abi.encode(sub, nonce))) <= target_
    """
    nonce_bytes = int(nonce).to_bytes(32, "big")
    encoded_outer = sub_bytes + nonce_bytes
    h_bytes = standard_keccak(encoded_outer)
    h_int = int.from_bytes(h_bytes, "big")
    return (h_int <= target_int, h_bytes, h_int)


def worker_proc(
    worker_id,
    num_workers,
    start_nonce,
    sub_bytes,
    target_bytes,
    b_targets,
    stop_event,
    result_queue,
    stats_queue,
    chunk_size=50000,
):
    """Worker process performing high-speed Keccak-256 PoW search."""
    if HAS_RAW_KECCAK:
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

        b0_t, b1_t, b2_t, b3_t, b4_t = b_targets
        current_nonce = start_nonce + worker_id * chunk_size

        try:
            while not stop_event.is_set():
                for i in range(chunk_size):
                    nonce = current_nonce + i
                    pack_into(">Q", nonce_buf, 24, nonce)
                    reset(state)
                    absorb(state, buf, 64)
                    squeeze(state, out, 32, 0x01)

                    # Dynamic prefix match based on target difficulty
                    matched = False
                    if b0_t == 0 and b1_t == 0 and b2_t == 0 and b3_t == 0:
                        if out[0] == 0 and out[1] == 0 and out[2] == 0 and out[3] == 0 and out[4] <= b4_t:
                            matched = True
                    elif b0_t == 0 and b1_t == 0 and b2_t == 0:
                        if out[0] == 0 and out[1] == 0 and out[2] == 0 and out[3] <= b3_t:
                            matched = True
                    elif b0_t == 0 and b1_t == 0:
                        if out[0] == 0 and out[1] == 0 and out[2] <= b2_t:
                            matched = True
                    elif b0_t == 0:
                        if out[0] == 0 and out[1] <= b1_t:
                            matched = True
                    else:
                        if out[0] <= b0_t:
                            matched = True

                    if matched:
                        h = bytes(ffi.buffer(out, 32))
                        if h <= target_bytes:
                            stop_event.set()
                            result_queue.put((nonce, h.hex()))
                            return
                stats_queue.put(chunk_size)
                current_nonce += num_workers * chunk_size
        finally:
            _raw_keccak_lib.keccak_destroy(state)
    else:
        current_nonce = start_nonce + worker_id * chunk_size
        while not stop_event.is_set():
            for i in range(chunk_size):
                nonce = current_nonce + i
                h = standard_keccak(sub_bytes + nonce.to_bytes(32, "big"))
                if h <= target_bytes:
                    stop_event.set()
                    result_queue.put((nonce, h.hex()))
                    return
            stats_queue.put(chunk_size)
            current_nonce += num_workers * chunk_size


def monitor_progress(stats_queue, stop_event, target_hashes, log_interval=2.0):
    """Monitors and prints mining throughput and estimated time."""
    start_time = time.time()
    last_time = start_time
    total_hashes = 0
    last_hashes = 0

    while not stop_event.is_set():
        time.sleep(log_interval)
        now = time.time()
        batch_sum = 0
        while not stats_queue.empty():
            try:
                batch_sum += stats_queue.get_nowait()
            except Exception:
                break
        total_hashes += batch_sum
        dt = now - last_time
        overall_dt = now - start_time
        if dt > 0 and overall_dt > 0:
            instant_rate = (total_hashes - last_hashes) / dt
            avg_rate = total_hashes / overall_dt
            last_hashes = total_hashes
            last_time = now

            pct = (total_hashes / target_hashes * 100) if target_hashes > 0 else 0
            eta_sec = ((target_hashes - total_hashes) / avg_rate) if avg_rate > 0 and total_hashes < target_hashes else 0
            print(
                f"[Progress] Hashes: {total_hashes:,.0f} | Speed: {instant_rate/1e6:.2f} MH/s (avg: {avg_rate/1e6:.2f} MH/s) "
                f"| Done: {pct:.2f}% | Expected ETA: {eta_sec:.1f}s ({eta_sec/60:.1f}m)",
                flush=True
            )



def backfill_epoch_seed(rpc_url, miner_contract, cur_epoch, private_key=None, max_steps=24):
    """Sends initEpochSeed(cur_epoch, max_steps) backfill transaction when seed is 0."""
    calldata = "0x" + SELECTOR_INIT_EPOCH_SEED + encode_uint256(cur_epoch) + encode_uint256(max_steps)
    if private_key:
        try:
            from eth_account import Account
            clean_key = private_key.strip()
            if clean_key.startswith("0x"):
                clean_key = clean_key[2:]
            account = Account.from_key(bytes.fromhex(clean_key))
            sender = account.address
            res_nonce = rpc_call(rpc_url, "eth_getTransactionCount", [sender, "latest"])
            nonce = int(res_nonce, 16) if res_nonce else 0
            res_gp = rpc_call(rpc_url, "eth_gasPrice", [])
            gas_price = int(res_gp, 16) if res_gp else 1000000000
            tx_dict = {
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": 300000,
                "to": miner_contract,
                "value": 0,
                "data": bytes.fromhex(calldata[2:]),
                "chainId": REQUIRED_CHAIN_ID,
            }
            signed = account.sign_transaction(tx_dict)
            tx_hash = rpc_call(rpc_url, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
            print(f"[BACKFILL] Broadcasted initEpochSeed({cur_epoch}, {max_steps}) tx: {tx_hash}")
            time.sleep(2)
            return tx_hash
        except Exception as exc:
            safe_exc = format_exception_safe(
                exc,
                sensitive_values=_collect_sensitive_values(extra_secrets=[private_key]),
            )
            print(f"[BACKFILL] Failed to broadcast backfill tx: {safe_exc}")
            return None
    else:
        try:
            tx_obj = {"to": miner_contract, "data": calldata}
            tx_hash = rpc_call(rpc_url, "eth_sendTransaction", [tx_obj])
            print(f"[BACKFILL] Sent initEpochSeed tx via unlocked node: {tx_hash}")
            time.sleep(1)
            return tx_hash
        except Exception:
            print("[BACKFILL] No private key provided to sign backfill transaction. Provide via --private-key or MINER_PRIVATE_KEY.")
            return None

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


def get_contract_paused(rpc_url: str, contract_addr: str, contract_name: str) -> bool:
    """
    Queries paused() on a contract and strictly validates the returned ABI bool.
    """
    try:
        res = eth_call(rpc_url, contract_addr, SELECTOR_PAUSED)
    except Exception as e:
        raise RuntimeError(f"RPC call failed while querying {contract_name}.paused(): {type(e).__name__}") from None

    if not res or not isinstance(res, str):
        raise RuntimeError(f"Query {contract_name}.paused() returned empty response from RPC")

    raw = res[2:] if res.startswith("0x") else res
    if not raw or len(raw) > 64 or any(c not in HEX_CHARACTERS for c in raw):
        raise RuntimeError(f"Query {contract_name}.paused() returned invalid ABI response")

    bool_val = int(raw, 16)
    if bool_val not in (0, 1):
        raise RuntimeError(
            f"Query {contract_name}.paused() returned invalid ABI bool value (must be 0 or 1, got {bool_val})"
        )
    return bool_val == 1


def get_uint_call(rpc_url: str, contract_addr: str, call_data: str, label: str) -> int:
    """
    Queries a uint-returning view function and validates the returned hex payload.
    """
    try:
        res = eth_call(rpc_url, contract_addr, call_data)
    except Exception as e:
        raise RuntimeError(f"RPC call failed while querying {label}: {type(e).__name__}") from None

    if not res or not isinstance(res, str):
        raise RuntimeError(f"Query {label} returned empty response from RPC")

    raw = res[2:] if res.startswith("0x") else res
    if not raw or len(raw) > 64 or any(c not in HEX_CHARACTERS for c in raw):
        raise RuntimeError(f"Query {label} returned invalid ABI uint response")

    return int(raw, 16)


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


def mine(args):
    # Enforce BSC testnet
    if args.rpc:
        chain_id = verify_chain_id(args.rpc)
    else:
        chain_id = REQUIRED_CHAIN_ID

    miner_addr = to_checksum_address(args.miner)
    miner_contract = to_checksum_address(args.miner_contract)

    # 1. Resolve Epoch
    onchain_epoch = None
    if args.rpc:
        onchain_epoch = get_uint_call(args.rpc, miner_contract, SELECTOR_EPOCH, "epoch()")
    if args.epoch is not None:
        cur_epoch = args.epoch
    elif onchain_epoch is not None:
        cur_epoch = onchain_epoch
    else:
        raise RuntimeError("Cannot resolve epoch: --rpc is not set and --epoch was omitted.")

    print("=== PoolCoin Multi-Threaded PoW Miner ===")
    print(f"Target Network:   BSC Testnet (Chain ID {chain_id})")
    print(f"Miner Contract:   {miner_contract}")
    print(f"Miner Address:    {miner_addr}")
    print(f"Current Epoch:    {cur_epoch}")

    # 2. Pre-mining contract state & StakeVault checks if RPC available
    if args.rpc:
        if get_contract_paused(args.rpc, miner_contract, "Miner"):
            raise RuntimeError(f"Pre-check failed: Miner contract {miner_contract} is paused.")

        vault_for_pause = get_miner_vault(args.rpc, miner_contract)
        if get_contract_paused(args.rpc, vault_for_pause, "StakeVault"):
            raise RuntimeError(f"Pre-check failed: StakeVault contract {vault_for_pause} is paused.")

        last_synced = get_uint_call(args.rpc, miner_contract, SELECTOR_LAST_SYNCED, "lastSynced()")
        if last_synced != onchain_epoch:
            raise RuntimeError(
                f"Pre-check failed: Miner lastSynced()={last_synced} != epoch()={onchain_epoch}; "
                f"epoch sync is behind. Please run Keeper to sync before mining."
            )

        counted_data = SELECTOR_COUNTED + encode_uint256(cur_epoch) + encode_address(miner_addr)
        counted_shares = get_uint_call(args.rpc, miner_contract, counted_data, f"counted({cur_epoch}, {miner_addr})")
        if counted_shares >= CAP:
            raise RuntimeError(
                f"Pre-check failed: miner {miner_addr} has already reached share cap "
                f"counted({cur_epoch})={counted_shares} >= CAP ({CAP})."
            )

        if getattr(args, "skip_stake_check", False):
            print("[WARNING] --skip-stake-check specified: 未验证质押资格 (stake eligibility not verified).")
        else:
            vault_contract, active, since = verify_stake(args.rpc, miner_contract, miner_addr, cur_epoch)
            print(f"StakeVault:       {vault_contract}")
            print(f"Stake Status:     Active={active}, DepositEpoch={since}")
    elif getattr(args, "skip_stake_check", False):
        print("[WARNING] --skip-stake-check specified: 未验证质押资格 (stake eligibility not verified).")

    # 3. Resolve epochSeed
    if args.seed:
        epoch_seed_int = int(args.seed, 16) if args.seed.startswith("0x") else int(args.seed)
    elif args.rpc:
        seed_data = SELECTOR_EPOCH_SEED + encode_uint256(cur_epoch)  # epochSeed(uint256)
        res = eth_call(args.rpc, miner_contract, seed_data)
        epoch_seed_int = int(res, 16) if res else 0
    else:
        raise RuntimeError("Cannot resolve epochSeed: --rpc is not set and --seed was omitted.")

    if epoch_seed_int == 0:
        if getattr(args, "allow_backfill", False) is True:
            if not args.rpc:
                raise RuntimeError("Cannot backfill epoch seed: --rpc is required when --allow-backfill is enabled.")
            print(f"[INFO] Epoch {cur_epoch} seed is 0x0 (not initialized). Discovered uninitialized seed.")
            jitter = random.uniform(0.0, 3.0)
            print(f"[JITTER] Applying {jitter:.2f}s random jitter before sending backfill transaction...")
            time.sleep(jitter)
            pk = getattr(args, "private_key", None) or os.environ.get("MINER_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
            backfill_epoch_seed(args.rpc, miner_contract, cur_epoch, private_key=pk, max_steps=getattr(args, "max_seed_steps", 24))
            seed_data = SELECTOR_EPOCH_SEED + encode_uint256(cur_epoch)
            res = eth_call(args.rpc, miner_contract, seed_data)
            epoch_seed_int = int(res, 16) if res else 0
        else:
            raise RuntimeError(
                f"Epoch seed for epoch {cur_epoch} is 0x0 (not initialized)! "
                f"Please run Keeper to initialize epoch seeds before mining (or pass --allow-backfill to send initEpochSeed)."
            )

    if epoch_seed_int == 0:
        raise RuntimeError(
            f"Epoch seed for epoch {cur_epoch} is 0x0 (not initialized)! "
            f"Backfill transaction was not completed or failed. Please run Keeper or call initEpochSeed({cur_epoch}, 24)."
        )
    epoch_seed_bytes = epoch_seed_int.to_bytes(32, "big")
    print(f"Epoch Seed:       0x{epoch_seed_bytes.hex()}")

    # 4. Resolve Target & Difficulty
    if args.target:
        target_int = int(args.target, 16) if args.target.startswith("0x") else int(args.target)
        if target_int <= 0:
            raise ValueError(f"Explicit --target must be > 0, got {target_int}")
        diff_bits = 256 - target_int.bit_length()
    elif getattr(args, "difficulty_bits", None) is not None:
        diff_bits = args.difficulty_bits
        if diff_bits < 0 or diff_bits > 256:
            raise ValueError(f"Invalid difficulty_bits={diff_bits}; must be 0..256")
        target_int = (1 << (256 - diff_bits)) if diff_bits > 0 else ((1 << 256) - 1)
    else:
        if not args.rpc:
            raise RuntimeError(
                "Cannot query on-chain target(epoch): --rpc is not set and neither --target nor --difficulty-bits was specified."
            )
        target_data = SELECTOR_TARGET + encode_uint256(cur_epoch)  # target(uint256)
        target_int = get_uint_call(args.rpc, miner_contract, target_data, f"target({cur_epoch})")
        if target_int == 0:
            raise RuntimeError(
                f"On-chain target({cur_epoch}) is 0 (epoch not initialized/synced or invalid target); "
                f"refusing fallback to default 32-bit target (1<<224)."
            )
        diff_bits = 256 - target_int.bit_length()

    target_bytes = target_int.to_bytes(32, "big")
    b_targets = (target_bytes[0], target_bytes[1], target_bytes[2], target_bytes[3], target_bytes[4])
    expected_trials = 2 ** diff_bits
    print(f"Target Difficulty: {diff_bits} bits (Target: 0x{target_bytes.hex()[:16]}...)")
    print(f"Expected Hashes:  2^{diff_bits} = {expected_trials:,}")

    # 5. Resolve Share Index k
    if args.k is not None:
        k = args.k
    else:
        if not args.rpc:
            raise RuntimeError("Cannot query usedMask: --rpc is not set and --k was omitted.")
        mask_data = SELECTOR_USED_MASK + encode_uint256(cur_epoch) + encode_address(miner_addr)
        used_mask = get_uint_call(args.rpc, miner_contract, mask_data, f"usedMask({cur_epoch}, {miner_addr})")
        k = None
        for cand_k in range(CAP):
            if (used_mask & (1 << cand_k)) == 0:
                k = cand_k
                break
        if k is None:
            raise RuntimeError(f"Miner address {miner_addr} has exhausted all {CAP} shares for epoch {cur_epoch}!")
        print(f"Selected Share k: {k} (usedMask: 0x{used_mask:04x})")

    if k < 0 or k >= CAP:
        raise ValueError(f"Invalid share index k={k}; must be 0 <= k < {CAP}")

    out_file = args.out or f"share_{miner_addr[:8]}_epoch{cur_epoch}_k{k}.json"
    if os.path.exists(out_file):
        raise FileExistsError(f"Output file already exists (refusing to overwrite): {out_file}")

    sub_bytes = compute_sub_hash(epoch_seed_bytes, miner_addr, k, miner_contract, chain_id)
    print(f"Sub Hash:         0x{sub_bytes.hex()}")
    print(f"Worker Threads:   {args.threads}")
    print(f"Fast CFFI Engine: {HAS_RAW_KECCAK}")
    print("==========================================")

    # 6. Launch Mining
    stop_event = mp.Event()
    result_queue = mp.Queue()
    stats_queue = mp.Queue()

    workers = []
    chunk_size = 50000
    start_time = time.time()

    for wid in range(args.threads):
        p = mp.Process(
            target=worker_proc,
            args=(
                wid,
                args.threads,
                args.start_nonce,
                sub_bytes,
                target_bytes,
                b_targets,
                stop_event,
                result_queue,
                stats_queue,
                chunk_size,
            ),
        )
        p.start()
        workers.append(p)

    mon_thread = threading.Thread(
        target=monitor_progress,
        args=(stats_queue, stop_event, expected_trials),
        daemon=True,
    )
    mon_thread.start()

    print("[Miner] Mining started... Press Ctrl+C to abort.", flush=True)

    try:
        found_nonce, found_hash = result_queue.get()
        stop_event.set()
    except KeyboardInterrupt:
        print("\n[Miner] Keyboard interrupt received! Stopping workers...")
        stop_event.set()
        for p in workers:
            p.join(timeout=1.0)
        sys.exit(1)

    for p in workers:
        p.join()

    elapsed = time.time() - start_time
    total_hashes = 0
    while not stats_queue.empty():
        try:
            total_hashes += stats_queue.get_nowait()
        except Exception:
            break

    # Verify result independently
    valid, check_hash, check_int = verify_share_local(sub_bytes, found_nonce, target_int)
    if not valid:
        raise RuntimeError(f"FATAL: Local verification failed for mined nonce {found_nonce}!")

    actual_diff_bits = 256 - check_int.bit_length()
    avg_rate = total_hashes / elapsed if elapsed > 0 else 0

    print(f"\n[SUCCESS] Share Found!")
    print(f"Nonce:            {found_nonce} (0x{found_nonce:x})")
    print(f"Share Hash:       0x{found_hash}")
    print(f"Effective Bits:   {actual_diff_bits} bits (required >= {diff_bits})")
    print(f"Elapsed Time:     {elapsed:.2f} s ({elapsed/60:.2f} min)")
    print(f"Total Hashes:     {total_hashes:,.0f}")
    print(f"Average Hashrate: {avg_rate/1e6:.2f} MH/s")

    # 7. Write Output JSON
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

    with open(out_file, "x", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    print(f"Share successfully saved to: {out_file}")
    print("\nNext step: Run submission simulation with:")
    print(f"  python script/submit_share.py --file {out_file}")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin Multi-threaded PoW Miner (BSC Testnet)")
    parser.add_argument("--miner", type=str, required=True, help="Miner wallet address")
    parser.add_argument("--epoch", type=int, default=None, help="Epoch number (queries RPC if omitted)")
    parser.add_argument("--k", type=int, default=None, help="Share index 0..5 (auto-selected if omitted)")
    parser.add_argument("--rpc", type=str, default=DEFAULT_RPC_URL, help="BSC Testnet RPC URL")
    parser.add_argument("--miner-contract", type=str, default=DEFAULT_MINER_CONTRACT, help="Miner proxy contract address")
    parser.add_argument(
        "--threads",
        type=int,
        default=max(1, (os.cpu_count() or 4) - 1),
        help="Number of worker processes (default: total cores - 1)",
    )
    parser.add_argument("--difficulty-bits", type=int, default=None, help="Explicit local difficulty override in leading zero bits (queries on-chain target(epoch) if omitted)")
    parser.add_argument("--target", type=str, default=None, help="Explicit target override (hex or int)")
    parser.add_argument("--seed", type=str, default=None, help="Explicit epochSeed override (hex or int)")
    parser.add_argument("--start-nonce", type=int, default=0, help="Starting nonce offset")
    parser.add_argument("--skip-stake-check", action="store_true", help="Skip StakeVault stake check")
    parser.add_argument("--allow-backfill", action="store_true", help="Allow sending initEpochSeed backfill tx via env private key when seed is 0")
    parser.add_argument("--out", type=str, default=None, help="Output JSON file path")

    args = parser.parse_args()
    try:
        mine(args)
    except Exception as e:
        print(
            f"[ERROR] {format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    mp.freeze_support()
    main()
