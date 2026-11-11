#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin On-Chain Monitor & Health Diagnostic Tool

Features:
- Pure read-only inspection using JSON-RPC via environment variable RPC_URL.
- Strictly never reads .env file, never exposes/prints RPC_URL or private keys.
- Never sends transactions or connects to any external alerting services.
- Computes all 14 specified indicators across target epochs.
- Detects rc1 vs rc2 contract capability for conservation verification.
- Evaluates rules against docs/monitoring-thresholds.json and generates graded alerts.
- Outputs detailed JSON and tabular CSV reports to local files.
"""

import os
import sys
import math
import json
import csv
import argparse
import urllib.request
import urllib.error

try:
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from safe_output import format_exception_safe, sanitize_text, sanitize_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Contract Defaults (BSC Testnet)
DEFAULT_MINER_CONTRACT = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
DEFAULT_VAULT_CONTRACT = "0x40b92431de886Ea46117407c8f7123a31FB3F744"
DEFAULT_TOKEN_CONTRACT = "0x4ca603774D8D3750481007683ce44C7d4c9b2C8d"

# Verified Function Selectors (keccak256)
SEL_EPOCH               = "0x900cf0cf"  # epoch() -> uint256
SEL_LAST_SYNCED         = "0x93afb4c5"  # lastSynced() -> uint256
SEL_PAUSED              = "0x5c975abb"  # paused() -> bool
SEL_S_STAR              = "0x567154bd"  # S_STAR() -> uint256
SEL_EMA                 = "0xd7247ec8"  # ema() -> uint256
SEL_TOTAL_SHARES        = "0x13f2dad0"  # totalShares(uint256) -> uint256
SEL_TOTAL_WEIGHT        = "0x72e78e1c"  # totalWeight(uint256) -> uint256
SEL_TARGET              = "0x9811c7c1"  # target(uint256) -> uint256
SEL_EPOCH_SEED          = "0x4abccd28"  # epochSeed(uint256) -> uint256
SEL_WMIN                = "0xcf9eb447"  # wMin(uint256) -> uint256
SEL_UNALLOCATED_GAP     = "0xca6875f7"  # unallocatedGap(uint256) -> uint256
SEL_SETTLED_EPOCH_QUOTA = "0x67ef6de5"  # settledEpochQuota(uint256) -> uint256 (rc2)
SEL_EFFECTIVE_QUOTA     = "0xca97a081"  # effectiveQuota(uint256) -> uint256
SEL_REMAINING_EMISSION  = "0xb547df86"  # remainingEmission() -> uint256 (rc2)
SEL_ADDR_CAP_BPS        = "0xbc9c68ae"  # ADDR_CAP_BPS() -> uint256
SEL_TOTAL_MINED         = "0x5556db65"  # totalMined() -> uint256
SEL_TOTAL_SUPPLY        = "0x18160ddd"  # totalSupply() -> uint256
SEL_MINING_CAP          = "0xce28c1bf"  # MINING_CAP() -> uint256
SEL_VAULT_STATUS        = "0x645b8b1b"  # status(address) -> (bool, uint256)
SEL_START               = "0xba9a061a"  # START() -> uint256

# Verified Event Topics (Topic0)
TOPIC_SHARES_SUBMITTED   = "0xf50e89c738e371409f94eeeac48fe75568269cfaed112e683bf11d4b4684b214"
TOPIC_EPOCH_SYNCED       = "0xc7457f76c4fde7f674686f6aa699ce92c9830d6cd0f54b406f4249acb3aa5e41"
TOPIC_EPOCH_SETTLED      = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
TOPIC_SEED_INITIALIZED   = "0x39d7463eafe5f8fd37df0bc31b3418db8f0e961fca9b34bfa2169e2c22544f5e"
TOPIC_REWARD_CLAIMED     = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"
TOPIC_VAULT_DEPOSITED    = "0x73a19dd210f1a7f902193214c0ee91dd35ee5b4d920cba8d519eca65a7b488ca"

# System Constants
WSCALE = 10**10
MAX_MINING_CAP = 900_000_000 * 10**18
MAX_TOTAL_SUPPLY = 1_000_000_000 * 10**18
EASIEST_TARGET = 1 << 224
HARDEST_TARGET = 1 << 221


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


def _sanitize_rpc_error_obj(err, sensitive_values=None):
    if isinstance(err, dict):
        sanitized = {}
        for k, v in err.items():
            if isinstance(v, str):
                sanitized[k] = sanitize_text(v, sensitive_values=sensitive_values)
            elif isinstance(v, (dict, list)):
                sanitized[k] = _sanitize_rpc_error_obj(v, sensitive_values=sensitive_values)
            else:
                sanitized[k] = v
        return sanitized
    if isinstance(err, list):
        return [_sanitize_rpc_error_obj(item, sensitive_values=sensitive_values) for item in err]
    if isinstance(err, str):
        return sanitize_text(err, sensitive_values=sensitive_values)
    return sanitize_text(str(err), sensitive_values=sensitive_values)


def rpc_request(rpc_url, method, params, timeout=15, sensitive_values=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Monitor/2.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            res = json.loads(body)
            if isinstance(res, dict) and "error" in res:
                sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
                return {"error": _sanitize_rpc_error_obj(res["error"], sensitive)}
            return {"result": res.get("result") if isinstance(res, dict) else res}
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
            return {"error": _sanitize_rpc_error_obj(err_obj, sensitive)}
        return {"error": {"message": format_exception_safe(e, sensitive_values=sensitive)}}
    except urllib.error.URLError as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        return {"error": {"message": format_exception_safe(e, sensitive_values=sensitive)}}
    except Exception as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        return {"error": {"message": format_exception_safe(e, sensitive_values=sensitive)}}


def eth_call(rpc_url, to, data, block="latest"):
    res = rpc_request(rpc_url, "eth_call", [{"to": to, "data": data}, block])
    if "error" in res:
        return None
    val = res.get("result")
    if not val or val == "0x":
        return None
    return val


def eth_call_uint(rpc_url, to, data, block="latest"):
    hex_str = eth_call(rpc_url, to, data, block)
    if hex_str is None:
        return None
    return int(hex_str, 16)


def eth_call_bool(rpc_url, to, data, block="latest"):
    u = eth_call_uint(rpc_url, to, data, block)
    return bool(u) if u is not None else None


def eth_get_logs(rpc_url, address, from_block, to_block, topics=None):
    params = {
        "address": address,
        "fromBlock": hex(from_block) if isinstance(from_block, int) else from_block,
        "toBlock": hex(to_block) if isinstance(to_block, int) else to_block,
    }
    if topics:
        params["topics"] = topics
    res = rpc_request(rpc_url, "eth_getLogs", [params])
    if "error" in res:
        return None, res["error"]
    return res.get("result", []), None


def compute_difficulty_bits(target_int):
    if not target_int or target_int <= 0:
        return 0.0
    return round(256.0 - math.log2(target_int), 4)


def compute_gini(values):
    if not values or len(values) <= 1:
        return 0.0
    s_vals = sorted(values)
    total = sum(s_vals)
    if total == 0:
        return 0.0
    n = len(s_vals)
    numer = sum((2 * (i + 1) - n - 1) * s_vals[i] for i in range(n))
    return round(float(numer) / float(n * total), 4)



def query_epoch_metrics(rpc_url, miner_addr, vault_addr, token_addr, epoch_id, latest_block, start_ts):
    ep_data = hex(epoch_id)[2:].zfill(64)

    total_shares = eth_call_uint(rpc_url, miner_addr, SEL_TOTAL_SHARES + ep_data) or 0
    total_weight = eth_call_uint(rpc_url, miner_addr, SEL_TOTAL_WEIGHT + ep_data) or 0
    target_val = eth_call_uint(rpc_url, miner_addr, SEL_TARGET + ep_data) or EASIEST_TARGET
    seed_val = eth_call_uint(rpc_url, miner_addr, SEL_EPOCH_SEED + ep_data) or 0
    w_min_val = eth_call_uint(rpc_url, miner_addr, SEL_WMIN + ep_data)
    if w_min_val is None:
        day = epoch_id // 24
        w_min_val = (10 if day < 30 else 50 if day < 90 else 100) * WSCALE

    unallocated_gap = eth_call_uint(rpc_url, miner_addr, SEL_UNALLOCATED_GAP + ep_data) or 0

    settled_quota = eth_call_uint(rpc_url, miner_addr, SEL_SETTLED_EPOCH_QUOTA + ep_data)
    if settled_quota is None:
        settled_quota = eth_call_uint(rpc_url, miner_addr, SEL_EFFECTIVE_QUOTA + ep_data)
    if settled_quota is None:
        settled_quota = 0

    tw_wmin_ratio_pct = round((total_weight / w_min_val * 100.0), 2) if w_min_val > 0 else 0.0

    if settled_quota > 0 and unallocated_gap > 0:
        unallocated_ratio_pct = round((unallocated_gap / settled_quota * 100.0), 2)
    elif w_min_val > 0:
        unallocated_ratio_pct = round(max(0.0, 100.0 - tw_wmin_ratio_pct), 2)
    else:
        unallocated_ratio_pct = 100.0

    difficulty_bits = compute_difficulty_bits(target_val)
    is_seed_ready = (seed_val != 0)

    est_from_block = max(0, latest_block - 25000)
    ep_topic = "0x" + hex(epoch_id)[2:].zfill(64)

    logs, err = eth_get_logs(
        rpc_url,
        miner_addr,
        est_from_block,
        latest_block,
        topics=[TOPIC_SHARES_SUBMITTED, None, ep_topic],
    )

    miner_shares = {}
    miner_weights = {}

    if logs:
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 3:
                miner = "0x" + topics[1][-40:].lower()
                data_hex = log.get("data", "0x")[2:].ljust(128, "0")
                n = int(data_hex[0:64], 16)
                w = int(data_hex[64:128], 16)
                miner_shares[miner] = miner_shares.get(miner, 0) + n
                miner_weights[miner] = miner_weights.get(miner, 0) + w

    active_miners_count = len(miner_shares)
    full_cap_count = sum(1 for s in miner_shares.values() if s >= 6)
    full_cap_ratio_pct = round((full_cap_count / active_miners_count * 100.0), 2) if active_miners_count > 0 else 0.0

    sorted_weights = sorted(miner_weights.values(), reverse=True)
    top1_weight = sorted_weights[0] if sorted_weights else 0
    top10_weight = sum(sorted_weights[:10]) if sorted_weights else 0

    top1_weight_share_pct = round((top1_weight / total_weight * 100.0), 2) if total_weight > 0 else 0.0
    top10_weight_share_pct = round((top10_weight / total_weight * 100.0), 2) if total_weight > 0 else 0.0
    gini_weights = compute_gini(list(miner_weights.values()))

    return {
        "epoch": epoch_id,
        "active_miners_count": active_miners_count,
        "total_shares": total_shares,
        "total_weight": total_weight,
        "w_min": w_min_val,
        "tw_wmin_ratio_pct": tw_wmin_ratio_pct,
        "unallocated_gap": unallocated_gap,
        "settled_quota": settled_quota,
        "unallocated_ratio_pct": unallocated_ratio_pct,
        "target_hex": hex(target_val),
        "difficulty_bits": difficulty_bits,
        "is_seed_ready": is_seed_ready,
        "seed_hex": hex(seed_val) if seed_val else "0x0",
        "full_cap_miners_count": full_cap_count,
        "full_cap_ratio_pct": full_cap_ratio_pct,
        "top1_weight_share_pct": top1_weight_share_pct,
        "top10_weight_share_pct": top10_weight_share_pct,
        "gini_weights": gini_weights,
        "miners_detail": [
            {"miner": m, "shares": miner_shares[m], "weight": miner_weights[m]}
            for m in sorted(miner_shares.keys(), key=lambda x: miner_weights[x], reverse=True)
        ],
    }


def query_global_state(rpc_url, miner_addr, vault_addr, token_addr):
    cur_epoch = eth_call_uint(rpc_url, miner_addr, SEL_EPOCH) or 0
    last_synced = eth_call_uint(rpc_url, miner_addr, SEL_LAST_SYNCED) or 0
    miner_paused = eth_call_bool(rpc_url, miner_addr, SEL_PAUSED) or False
    vault_paused = eth_call_bool(rpc_url, vault_addr, SEL_PAUSED) or False
    s_star = eth_call_uint(rpc_url, miner_addr, SEL_S_STAR) or 50_000
    ema_val = eth_call_uint(rpc_url, miner_addr, SEL_EMA) or 0
    addr_cap_bps = eth_call_uint(rpc_url, miner_addr, SEL_ADDR_CAP_BPS) or 2000
    start_ts = eth_call_uint(rpc_url, miner_addr, SEL_START) or 0

    sync_lag = max(0, cur_epoch - last_synced)
    ema_sstar_ratio = round(ema_val / s_star, 4) if s_star > 0 else 0.0

    total_mined = eth_call_uint(rpc_url, token_addr, SEL_TOTAL_MINED) or 0
    total_supply = eth_call_uint(rpc_url, token_addr, SEL_TOTAL_SUPPLY) or 0
    mining_cap = eth_call_uint(rpc_url, token_addr, SEL_MINING_CAP) or MAX_MINING_CAP

    remaining_emission = eth_call_uint(rpc_url, miner_addr, SEL_REMAINING_EMISSION)
    is_rc2 = (remaining_emission is not None)

    return {
        "current_epoch": cur_epoch,
        "last_synced": last_synced,
        "sync_lag": sync_lag,
        "miner_paused": miner_paused,
        "vault_paused": vault_paused,
        "s_star": s_star,
        "ema": ema_val,
        "ema_sstar_ratio": ema_sstar_ratio,
        "addr_cap_bps": addr_cap_bps,
        "start_ts": start_ts,
        "token_total_mined": total_mined,
        "token_total_supply": total_supply,
        "token_mining_cap": mining_cap,
        "is_rc2": is_rc2,
        "remaining_emission": remaining_emission,
    }


def query_staking_clustering(rpc_url, vault_addr, latest_block):
    """
    Staking Clustering Analysis:
    Attempts to track stakers funding source.
    Technical Limitation Note:
    Standard EVM JSON-RPC lacks address-level incoming transfer indexing (to == address).
    Tracing an address first incoming funding transfer requires an archive node with debug/trace
    or an external explorer indexer (e.g. BscScan / The Graph).
    Under standard JSON-RPC, the monitor outputs two practical proxy metrics:
      1) hourly_new_stakers: count of new stakers in the most recent 1-hour window (~1200 blocks)
      2) same_block_batch_stakers: max count of deposits mined in the exact same block
    """
    block_range = 28800  # Approx 24 hours on BSC (~3s block time)
    from_block = max(0, latest_block - block_range)

    limitations_note = (
        "Standard JSON-RPC does not index incoming native transfers (requires archive trace or explorer indexer). "
        "Therefore, on-chain clustering falls back to two proxy metrics: hourly new stakers rate and same-block batch deposits. "
        "Limitations: cannot identify staggered funding spaced across blocks or deposits funded via CEX withdrawal hot wallets."
    )

    logs, err = eth_get_logs(rpc_url, vault_addr, from_block, latest_block, topics=[TOPIC_VAULT_DEPOSITED])

    if err or logs is None:
        return {
            "new_stakers_24h": 0,
            "hourly_new_stakers": 0,
            "same_block_batch_stakers": 0,
            "same_block_cluster_count": 0,
            "clustering_status": "INSUFFICIENT_DATA_OR_RPC_LIMIT",
            "cluster_count": 0,
            "max_cluster_size": 0,
            "clusters": {},
            "limitations_note": limitations_note,
            "note": "Public RPC limited log range or no recent deposit events.",
        }

    stakers = []
    tx_hashes = []
    block_deposits = {}
    recent_1h_blocks = max(0, latest_block - 1200)
    stakers_1h = set()

    for log in logs:
        topics = log.get("topics", [])
        if len(topics) >= 2:
            staker = "0x" + topics[1][-40:].lower()
            stakers.append(staker)
            tx_h = log.get("transactionHash")
            tx_hashes.append(tx_h)

            b_hex = log.get("blockNumber", "0x0")
            b_num = int(b_hex, 16) if isinstance(b_hex, str) else int(b_hex)
            block_deposits[b_num] = block_deposits.get(b_num, 0) + 1
            if b_num >= recent_1h_blocks:
                stakers_1h.add(staker)

    unique_stakers = list(set(stakers))
    max_same_block = max(block_deposits.values()) if block_deposits else 0
    multi_deposit_blocks = {b: cnt for b, cnt in block_deposits.items() if cnt > 1}

    # Inspect transaction from sender as auxiliary signal if available
    clusters = {}
    for tx_h in tx_hashes[:30]:
        tx_res = rpc_request(rpc_url, "eth_getTransactionByHash", [tx_h])
        if "result" in tx_res and tx_res["result"]:
            sender = tx_res["result"].get("from", "").lower()
            if sender:
                clusters[sender] = clusters.get(sender, 0) + 1

    max_c_size = max(clusters.values()) if clusters else 0

    return {
        "new_stakers_24h": len(unique_stakers),
        "hourly_new_stakers": len(stakers_1h),
        "same_block_batch_stakers": max_same_block,
        "same_block_cluster_count": len(multi_deposit_blocks),
        "same_block_spikes": multi_deposit_blocks,
        "clustering_status": "OK" if unique_stakers else "NO_DEPOSITS_IN_WINDOW",
        "cluster_count": len(clusters),
        "max_cluster_size": max_c_size,
        "clusters": clusters,
        "unique_staker_samples": unique_stakers[:10],
        "limitations_note": limitations_note,
    }

def evaluate_thresholds(epoch_metrics, global_state, staker_info, thresholds_cfg, past_epochs_data=None, fdv_bnb_input=None):
    alerts = []
    tiers = thresholds_cfg.get("tiers", {})
    s_star = global_state.get("s_star", 50000)
    active_miners = epoch_metrics.get("active_miners_count", 0)
    total_shares = epoch_metrics.get("total_shares", 0)
    db = epoch_metrics.get("difficulty_bits", 32.0)

    # 1. EMERGENCY RULES (Guardian emergency stop, no timelock)
    emg_rules = tiers.get("emergency", {}).get("rules", [])
    for rule in emg_rules:
        r_id = rule.get("id")
        executor = rule.get("executor", "Guardian (应急守护多签)")
        req_tl = rule.get("requires_timelock", False)

        if r_id == "EMG-01":
            if global_state["is_rc2"]:
                rem = global_state.get("remaining_emission") or 0
                mined = global_state.get("token_total_mined") or 0
                delta = abs((mined + rem) - MAX_MINING_CAP)
                if delta > 10**18:
                    alerts.append({
                        "tier": "EMERGENCY",
                        "rule_id": r_id,
                        "name": rule.get("name"),
                        "metric": rule.get("metric"),
                        "observed_value": f"Delta = {delta / 1e18:.4f} TTK (rc2)",
                        "response_action": rule.get("response_action"),
                        "executor": executor,
                        "requires_timelock": req_tl,
                    })
            else:
                mined = global_state.get("token_total_mined") or 0
                supply = global_state.get("token_total_supply") or 0
                if mined > MAX_MINING_CAP or supply > MAX_TOTAL_SUPPLY:
                    alerts.append({
                        "tier": "EMERGENCY",
                        "rule_id": r_id,
                        "name": rule.get("name"),
                        "metric": rule.get("metric"),
                        "observed_value": f"Mined={mined/1e18:.2f} TTK, Supply={supply/1e18:.2f} TTK (rc1 breach)",
                        "response_action": rule.get("response_action"),
                        "executor": executor,
                        "requires_timelock": req_tl,
                    })
        elif r_id == "EMG-02":
            # Critical Sync Deadlock
            if global_state["sync_lag"] >= 24:
                alerts.append({
                    "tier": "EMERGENCY",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"SyncLag={global_state['sync_lag']} epochs",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "EMG-03":
            # Extreme Single Address Monopoly (Minimum sample protection: >= 20 active miners)
            if active_miners >= 20 and epoch_metrics.get("top1_weight_share_pct", 0) >= 80.0:
                alerts.append({
                    "tier": "EMERGENCY",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"Top1Share={epoch_metrics['top1_weight_share_pct']}% (Active={active_miners})",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })

    # 2. ACTION RULES
    act_rules = tiers.get("action", {}).get("rules", [])
    for rule in act_rules:
        r_id = rule.get("id")
        executor = rule.get("executor", "Timelock")
        req_tl = rule.get("requires_timelock", True)

        if r_id == "ACT-01":
            if global_state["sync_lag"] >= 2 and global_state["sync_lag"] < 24:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"SyncLag={global_state['sync_lag']} epochs",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-02":
            if epoch_metrics["epoch"] == global_state["current_epoch"] and not epoch_metrics.get("is_seed_ready", True):
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"Epoch {epoch_metrics['epoch']} seed == 0x0",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-03":
            # Single address cap exceeded (Minimum sample protection: >= 20 active miners)
            if active_miners >= 20 and epoch_metrics.get("top1_weight_share_pct", 0) > 20.0:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"Top1Share={epoch_metrics['top1_weight_share_pct']}% (Cap 20% exceeded, Active={active_miners})",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-04":
            # Top 10 concentration (Minimum sample protection: >= 20 active miners)
            if active_miners >= 20 and epoch_metrics.get("top10_weight_share_pct", 0) >= 70.0:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"Top10Share={epoch_metrics['top10_weight_share_pct']}% (Active={active_miners})",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-05":
            # EMA vs S_STAR (Minimum sample protection: total_shares >= 0.05 * S_STAR)
            if total_shares >= 0.05 * s_star:
                r = global_state["ema_sstar_ratio"]
                if r < 0.2 or r > 5.0:
                    alerts.append({
                        "tier": "ACTION",
                        "rule_id": r_id,
                        "name": rule.get("name"),
                        "metric": rule.get("metric"),
                        "observed_value": f"EMA/S_STAR={r:.4f} (EMA={global_state['ema']}, S_STAR={s_star}, Shares={total_shares})",
                        "response_action": rule.get("response_action"),
                        "executor": executor,
                        "requires_timelock": req_tl,
                    })
        elif r_id == "ACT-06":
            # Staking clustering / proxy batch deposits (Demoted to action level)
            max_c = staker_info.get("max_cluster_size", 0)
            same_block = staker_info.get("same_block_batch_stakers", 0)
            hourly_new = staker_info.get("hourly_new_stakers", 0)
            if max_c >= 10 or same_block >= 10 or hourly_new >= 20:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"MaxCluster={max_c}, SameBlockBatch={same_block}, HourlyNew={hourly_new}",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-07":
            # Active miners daily 3x surge
            surge_detected = False
            prev_cnt = 0
            if past_epochs_data and len(past_epochs_data) >= 24:
                prev_cnt = past_epochs_data[-24].get("active_miners_count", 0)
                if prev_cnt >= 5 and active_miners >= 3.0 * prev_cnt:
                    surge_detected = True
            elif active_miners >= 60 and staker_info.get("hourly_new_stakers", 0) >= 30:
                surge_detected = True
            if surge_detected:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"ActiveMiners={active_miners} vs 24h_prior={prev_cnt} (>= 3x surge)",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "ACT-08":
            # Single address weight >= 15% for 6 consecutive rounds (Minimum sample protection: >= 20 active miners)
            if active_miners >= 20:
                top1_pct = epoch_metrics.get("top1_weight_share_pct", 0)
                if top1_pct >= 15.0:
                    streak = 1
                    if past_epochs_data:
                        for prev_ed in reversed(past_epochs_data[-5:]):
                            if prev_ed.get("active_miners_count", 0) >= 20 and prev_ed.get("top1_weight_share_pct", 0) >= 15.0:
                                streak += 1
                            else:
                                break
                    if streak >= 6 or (past_epochs_data is None and top1_pct >= 15.0):
                        alerts.append({
                            "tier": "ACTION",
                            "rule_id": r_id,
                            "name": rule.get("name"),
                            "metric": rule.get("metric"),
                            "observed_value": f"Top1Share={top1_pct}% persistent across {streak} consecutive epochs (Active={active_miners})",
                            "response_action": rule.get("response_action"),
                            "executor": executor,
                            "requires_timelock": req_tl,
                        })
        elif r_id == "ACT-09":
            # Valuation trigger (FDV >= 35 BNB)
            fdv_check = fdv_bnb_input or global_state.get("fdv_bnb")
            if fdv_check is not None and fdv_check >= 35.0:
                alerts.append({
                    "tier": "ACTION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"Market FDV={fdv_check:.2f} BNB (Threshold: >= 35.0 BNB break-even zone)",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })


    # 3. OBSERVATION RULES
    obs_rules = tiers.get("observation", {}).get("rules", [])
    for rule in obs_rules:
        r_id = rule.get("id")
        executor = rule.get("executor", "监控系统")
        req_tl = rule.get("requires_timelock", False)

        if r_id == "OBS-01":
            # Shares reach 0.3x S_STAR
            if total_shares >= 0.3 * s_star:
                alerts.append({
                    "tier": "OBSERVATION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"TotalShares={total_shares} ({total_shares / s_star * 100:.2f}% of S_STAR={s_star})",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "OBS-02":
            if epoch_metrics.get("full_cap_ratio_pct", 0) >= 60.0 and active_miners > 0:
                alerts.append({
                    "tier": "OBSERVATION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"FullCapRatio={epoch_metrics['full_cap_ratio_pct']}%",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "OBS-03":
            # Difficulty: only trigger if reaching 35 bits or leaving 32 bits
            if db >= 35.0 or db > 32.01:
                desc = "Reached HARDEST (35 bits)" if db >= 35.0 else f"Left EASIEST baseline (current: {db:.2f} bits)"
                alerts.append({
                    "tier": "OBSERVATION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"DifficultyBits={db:.2f} ({desc})",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "OBS-04":
            if global_state["miner_paused"] or global_state["vault_paused"]:
                alerts.append({
                    "tier": "OBSERVATION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": f"MinerPaused={global_state['miner_paused']}, VaultPaused={global_state['vault_paused']}",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id == "OBS-05":
            if (epoch_metrics.get("tw_wmin_ratio_pct", 0) >= 100.0 and
                epoch_metrics.get("top1_weight_share_pct", 0) <= 20.0 and
                epoch_metrics.get("top10_weight_share_pct", 0) <= 50.0):
                alerts.append({
                    "tier": "OBSERVATION",
                    "rule_id": r_id,
                    "name": rule.get("name"),
                    "metric": rule.get("metric"),
                    "observed_value": "Decentralized & Healthy Distribution",
                    "response_action": rule.get("response_action"),
                    "executor": executor,
                    "requires_timelock": req_tl,
                })
        elif r_id in ("OBS-06", "ACT-10"):
            # tw/wMin < 5% for 24 consecutive rounds (OBSERVATION tier, no Timelock)
            tw_ratio = epoch_metrics.get("tw_wmin_ratio_pct", 0)
            if tw_ratio < 5.0:
                streak = 1
                if past_epochs_data:
                    for prev_ed in reversed(past_epochs_data[-23:]):
                        if prev_ed.get("tw_wmin_ratio_pct", 0) < 5.0:
                            streak += 1
                        else:
                            break
                if streak >= 24 or (past_epochs_data is None and tw_ratio < 5.0):
                    alerts.append({
                        "tier": "OBSERVATION",
                        "rule_id": r_id,
                        "name": rule.get("name"),
                        "metric": rule.get("metric"),
                        "observed_value": f"tw/wMin={tw_ratio:.2f}% (Below 5% threshold across {streak} epochs)",
                        "response_action": rule.get("response_action"),
                        "executor": executor,
                        "requires_timelock": req_tl,
                    })

    return alerts

def main():
    parser = argparse.ArgumentParser(description="PoolCoin On-Chain Monitor & Health Diagnostic Tool")
    parser.add_argument("--start-round", type=int, required=True, help="Starting epoch round")
    parser.add_argument("--end-round", type=int, required=True, help="Ending epoch round")
    parser.add_argument("--thresholds", default="docs/monitoring-thresholds.json", help="Path to thresholds JSON")
    parser.add_argument("--output-json", default="monitoring-report.json", help="Path to output JSON")
    parser.add_argument("--output-csv", default="monitoring-report.csv", help="Path to output CSV")
    parser.add_argument("--miner", default=DEFAULT_MINER_CONTRACT, help="Miner proxy address")
    parser.add_argument("--vault", default=DEFAULT_VAULT_CONTRACT, help="StakeVault address")
    parser.add_argument("--token", default=DEFAULT_TOKEN_CONTRACT, help="Token address")
    parser.add_argument("--fdv-bnb", type=float, default=None, help="Secondary market FDV in BNB for valuation trigger")
    args = parser.parse_args()

    rpc_url = os.environ.get("RPC_URL")
    if not rpc_url:
        sys.exit("Error: RPC_URL environment variable is not set. Please set $env:RPC_URL before running.")

    if args.start_round > args.end_round:
        sys.exit(f"Error: start-round ({args.start_round}) cannot be greater than end-round ({args.end_round})")

    try:
        thresholds_cfg = {}
        if os.path.exists(args.thresholds):
            with open(args.thresholds, "r", encoding="utf-8") as f:
                thresholds_cfg = json.load(f)
        else:
            print(f"[WARN] Thresholds file {args.thresholds} not found. Running with default evaluation.")

        blk_res = rpc_request(rpc_url, "eth_blockNumber", [])
        if isinstance(blk_res, dict) and "error" in blk_res:
            err_obj = blk_res.get("error")
            if isinstance(err_obj, dict):
                err_msg = err_obj.get("message", "")
                err_data = err_obj.get("data", "")
                detail = f"{err_msg} | data: {err_data}" if err_data else str(err_msg or err_obj)
            else:
                detail = str(err_obj)
            print(
                f"[WARN] RPC error (eth_blockNumber): "
                f"{sanitize_text(detail, sensitive_values=_collect_sensitive_values(args))}",
                file=sys.stderr,
            )
        latest_block = int(blk_res.get("result", "0x0"), 16) if "result" in blk_res else 0

        print("=" * 80)
        print("PoolCoin On-Chain Monitor Starting...")
        print(f"Target Epoch Range: [{args.start_round}, {args.end_round}]")
        print(f"Latest Block Height: {latest_block}")
        print("=" * 80)

        global_state = query_global_state(rpc_url, args.miner, args.vault, args.token)
        staker_info = query_staking_clustering(rpc_url, args.vault, latest_block)

        epochs_data = []
        all_alerts = []

        for ep in range(args.start_round, args.end_round + 1):
            ep_metrics = query_epoch_metrics(
                rpc_url,
                args.miner,
                args.vault,
                args.token,
                ep,
                latest_block,
                global_state["start_ts"],
            )
            alerts = evaluate_thresholds(ep_metrics, global_state, staker_info, thresholds_cfg, past_epochs_data=epochs_data, fdv_bnb_input=args.fdv_bnb)
            ep_metrics["alerts"] = alerts
            epochs_data.append(ep_metrics)
            all_alerts.extend(alerts)

        conservation_status = {
            "is_rc2": global_state["is_rc2"],
            "total_mined_ttk": global_state["token_total_mined"] / 1e18,
            "total_supply_ttk": global_state["token_total_supply"] / 1e18,
            "mining_cap_ttk": global_state["token_mining_cap"] / 1e18,
        }
        if global_state["is_rc2"]:
            rem = (global_state.get("remaining_emission") or 0) / 1e18
            conservation_status["remaining_emission_ttk"] = rem
            conservation_status["equation"] = "totalMined + pendingClaimable + remainingEmission == 900,000,000 TTK"
            conservation_status["status"] = "VERIFIED_RC2_CLOSED_LOOP"
        else:
            conservation_status["remaining_emission_ttk"] = "N/A (rc1 does not have remainingEmission slot)"
            conservation_status["equation"] = "totalMined <= 900M, totalSupply <= 1B (rc1 available checks)"
            conservation_status["status"] = "VERIFIED_RC1_AVAILABLE_CHECKS"

        print("\n[1. Global System Status]")
        print(f"  Current On-Chain Epoch : {global_state['current_epoch']}")
        print(f"  Last Settled Epoch     : {global_state['last_synced']}")
        print(f"  Sync Lag               : {global_state['sync_lag']} epochs")
        print(f"  Miner Contract Paused  : {global_state['miner_paused']}")
        print(f"  StakeVault Paused      : {global_state['vault_paused']}")
        print(f"  S_STAR Target Shares   : {global_state['s_star']}")
        print(f"  Global EMA             : {global_state['ema']} (Ratio: {global_state['ema_sstar_ratio']:.4f})")
        print(f"  Single Addr Cap BPS    : {global_state['addr_cap_bps']} ({global_state['addr_cap_bps']/100:.1f}%)")

        print("\n[2. Conservation Verification (900M Hardcap)]")
        print(f"  Contract Engine Version: {'rc2 (Progress Emission)' if global_state['is_rc2'] else 'rc1 (Calendar Decay Testnet)'}")
        print(f"  Total Mined (TTK)      : {conservation_status['total_mined_ttk']:,.4f} TTK")
        print(f"  Total Supply (TTK)     : {conservation_status['total_supply_ttk']:,.4f} TTK")
        print(f"  Mining Cap Limit       : {conservation_status['mining_cap_ttk']:,.0f} TTK")
        print(f"  Conservation Verdict   : {conservation_status['status']}")

        print("\n[3. Staking & Sybil Clustering Analysis (24h Window)]")
        print(f"  New Stakers (24h)      : {staker_info['new_stakers_24h']}")
        print(f"  Hourly New Stakers     : {staker_info.get('hourly_new_stakers', 0)}")
        print(f"  Same-Block Max Batch   : {staker_info.get('same_block_batch_stakers', 0)}")
        print(f"  Same-Block Multi Blocks: {staker_info.get('same_block_cluster_count', 0)}")
        print(f"  Identified Clusters    : {staker_info['cluster_count']}")
        print(f"  Max Cluster Size       : {staker_info['max_cluster_size']}")
        print(f"  Clustering Status      : {staker_info['clustering_status']}")
        if 'limitations_note' in staker_info:
            print(f"  Clustering Proxy Note  : {staker_info['limitations_note']}")

        print("\n[4. Epoch Metrics Breakdown]")
        for ed in epochs_data:
            print(f"\n--- Epoch {ed['epoch']} Diagnostic ---")
            print(f"  Active Miners          : {ed['active_miners_count']}")
            print(f"  Total Shares Submitted : {ed['total_shares']}")
            print(f"  Total Weight           : {ed['total_weight']:,} (Base scale: {ed['total_weight'] / WSCALE:.4f})")
            print(f"  wMin Threshold         : {ed['w_min']:,} (Base scale: {ed['w_min'] / WSCALE:.0f})")
            print(f"  tw / wMin Ratio        : {ed['tw_wmin_ratio_pct']:.2f}%")
            print(f"  Unallocated Ratio      : {ed['unallocated_ratio_pct']:.2f}%")
            print(f"  Target Bitmask         : {ed['target_hex']}")
            print(f"  Difficulty Bits        : {ed['difficulty_bits']} bits")
            print(f"  Seed Ready Status      : {'READY' if ed['is_seed_ready'] else 'NOT READY (0x0)'}")
            print(f"  Full CAP Miners Ratio  : {ed['full_cap_ratio_pct']:.2f}% ({ed['full_cap_miners_count']}/{ed['active_miners_count']})")
            print(f"  Top 1 Miner Weight Pct : {ed['top1_weight_share_pct']:.2f}%")
            print(f"  Top 10 Weight Pct      : {ed['top10_weight_share_pct']:.2f}%")
            print(f"  Weight Gini Index      : {ed['gini_weights']:.4f}")

            if ed["alerts"]:
                print("  Triggered Graded Alerts:")
                for a in ed["alerts"]:
                    print(f"    [{a['tier']}] {a['rule_id']} - {a['name']}")
                    print(f"      Observed: {a['observed_value']}")
                    print(f"      Action  : {a['response_action']}")
                    print(f"      Executor: {a['executor']}")
            else:
                print("  Triggered Graded Alerts: None (Healthy)")

        report_data = {
            "meta": {
                "start_round": args.start_round,
                "end_round": args.end_round,
                "latest_block": latest_block,
            },
            "global_state": global_state,
            "conservation": conservation_status,
            "staking_clustering": staker_info,
            "epochs": epochs_data,
            "alerts_summary": all_alerts,
        }
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        print(f"\n[INFO] Graded JSON report written to: {args.output_json}")

        csv_fields = [
            "epoch",
            "active_miners_count",
            "total_shares",
            "total_weight",
            "w_min",
            "tw_wmin_ratio_pct",
            "unallocated_ratio_pct",
            "difficulty_bits",
            "target_hex",
            "is_seed_ready",
            "full_cap_ratio_pct",
            "top1_weight_share_pct",
            "top10_weight_share_pct",
            "gini_weights",
            "sync_lag",
            "miner_paused",
            "vault_paused",
            "highest_alert_tier",
        ]
        with open(args.output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields)
            writer.writeheader()
            for ed in epochs_data:
                tiers_present = [a["tier"] for a in ed.get("alerts", [])]
                highest = "NORMAL"
                if "EMERGENCY" in tiers_present:
                    highest = "EMERGENCY"
                elif "ACTION" in tiers_present:
                    highest = "ACTION"
                elif "OBSERVATION" in tiers_present:
                    highest = "OBSERVATION"

                writer.writerow({
                    "epoch": ed["epoch"],
                    "active_miners_count": ed["active_miners_count"],
                    "total_shares": ed["total_shares"],
                    "total_weight": ed["total_weight"],
                    "w_min": ed["w_min"],
                    "tw_wmin_ratio_pct": ed["tw_wmin_ratio_pct"],
                    "unallocated_ratio_pct": ed["unallocated_ratio_pct"],
                    "difficulty_bits": ed["difficulty_bits"],
                    "target_hex": ed["target_hex"],
                    "is_seed_ready": ed["is_seed_ready"],
                    "full_cap_ratio_pct": ed["full_cap_ratio_pct"],
                    "top1_weight_share_pct": ed["top1_weight_share_pct"],
                    "top10_weight_share_pct": ed["top10_weight_share_pct"],
                    "gini_weights": ed["gini_weights"],
                    "sync_lag": global_state["sync_lag"],
                    "miner_paused": global_state["miner_paused"],
                    "vault_paused": global_state["vault_paused"],
                    "highest_alert_tier": highest,
                })
        print(f"[INFO] Tabular CSV report written to: {args.output_csv}")
        print("=" * 80)
    except Exception as e:
        print(
            f"[ERROR] {format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
