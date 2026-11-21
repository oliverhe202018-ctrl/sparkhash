#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Mining Reward Claim Script for MinerV1 on BSC (chain IDs 56 and 97)

Claims finalized epoch mining rewards from MinerV1.
DEFAULT MODE: Read-only simulation via eth_call (dry-run preflight).
SEND MODE (--send): Prompts for manual user secondary confirmation, enforces EIP-1559
maxFeePerGas >= max(eth_gasPrice, maxPriorityFeePerGas), and signs via Foundry keystore
(--account / --password-file) using cast send (or legacy PRIVATE_KEY env fallback).

HARD CONSTRAINTS ENFORCED:
1. The RPC chain ID must match the selected network (testnet by default).
2. Never reads .env or prints private keys. Private key is strictly from PRIVATE_KEY env var.
3. Requires explicit manual confirmation ("yes") before broadcasting.
4. EIP-1559 maxFeePerGas strictly >= network eth_gasPrice floor and >= maxPriorityFeePerGas.
"""

import os
import sys
import time
import json
import argparse
import urllib.error
import urllib.request

try:
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from safe_output import format_exception_safe, sanitize_text, sanitize_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from eth_abi import encode, decode
from eth_hash.auto import keccak

try:
    from script.cast_signer import (
        CastSendUncertainError,
        CastSignerError,
        UNCERTAIN_TX_WARNING,
        cast_send,
        get_keystore_address,
        parse_pending_nonce,
        resolve_eip1559_fees,
        validate_cli_fee_and_gas_args,
    )
except ImportError:
    from cast_signer import (
        CastSendUncertainError,
        CastSignerError,
        UNCERTAIN_TX_WARNING,
        cast_send,
        get_keystore_address,
        parse_pending_nonce,
        resolve_eip1559_fees,
        validate_cli_fee_and_gas_args,
    )

try:
    from script.miner_network import EXPLORERS, MAINNET_CHAIN_ID, NETWORK_NAMES, add_chain_argument, chain_settings, verify_rpc_chain
except ImportError:
    from miner_network import EXPLORERS, MAINNET_CHAIN_ID, NETWORK_NAMES, add_chain_argument, chain_settings, verify_rpc_chain

REQUIRED_CHAIN_ID = 97
DEFAULT_MINER_CONTRACT = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
DEFAULT_RPC_URLS = [
    "https://bsc-testnet-rpc.publicnode.com",
    "https://data-seed-prebsc-1-s1.binance.org:8545",
    "https://data-seed-prebsc-2-s1.binance.org:8545",
]

ERROR_SELECTORS = {
    "756b7857": "EpochNotEnded() - 该轮次尚未结束，无法领取奖励",
    "646cf558": "AlreadyClaimed() - 该轮次奖励已被领取，请勿重复领取",
    "19a2a9bd": "ZeroWeight() - 矿工在该轮次中没有有效算力权重 (userWeight == 0)",
    "3ee5aeb5": "ReentrancyGuardReentrantCall() - 重入防护触发",
    "ca97a081": "EffectiveQuotaZero - 该轮次有效配额为 0",
}

SEL_CLAIM = "0x379607f5"           # claim(uint256)
SELECTOR_INIT_EPOCH_SEED = "0xd9d9c33c"  # initEpochSeed(uint256,uint256)
EVENT_EPOCH_SETTLED_TOPIC = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
EVENT_REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"
SEL_EPOCH = "0x900cf0cf"           # epoch() -> uint256
SEL_TOKEN = "0xfc0c546a"           # token() -> address
SEL_ADDR_CAP_BPS = "0xbc9c68ae"    # ADDR_CAP_BPS() -> uint256
SEL_REMAINING_EMISSION = "0xb547df86"   # remainingEmission() -> uint256
SEL_SETTLED_EPOCH_QUOTA = "0x67ef6de5"  # settledEpochQuota(uint256) -> uint256


def parse_reward_claimed(log):
    """
    Parses on-chain RewardClaimed event log with 4 parameters:
    (miner, epoch, claimed_amount, capped_overflow)
    """
    topics = log.get("topics", [])
    if len(topics) < 3:
        return None
    miner = "0x" + topics[1][-40:].lower()
    epoch = int(topics[2], 16)
    data = log.get("data", "0x")
    if data.startswith("0x"):
        data = data[2:]
    data = data.ljust(128, "0")
    claimed_amount = int(data[0:64], 16)
    capped_overflow = int(data[64:128], 16)
    return miner, epoch, claimed_amount, capped_overflow

def to_checksum_address(address):
    clean = address.lower().replace("0x", "")
    h = keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


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
        for attr in ("private_key", "password_file", "keystore_password_file", "password", "keystore"):
            val = getattr(args, attr, None)
            if isinstance(val, (str, os.PathLike)):
                sval = os.fspath(val).strip()
                if len(sval) >= 4:
                    values.append(sval)
                    if "password_file" in attr or "keystore" in attr:
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
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params,
    }
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Claimer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
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
        finally:
            try:
                e.close()
            except Exception:
                pass
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


def rpc_batch_call(rpc_url, calls, timeout=15, sensitive_values=None):
    payload = []
    for idx, (method, params) in enumerate(calls):
        payload.append({
            "jsonrpc": "2.0",
            "id": idx,
            "method": method,
            "params": params,
        })
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Claimer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_list = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
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
                raise RuntimeError(f"RPC Batch Error: {safe_msg} | data: {safe_data}") from None
            raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None
        finally:
            try:
                e.close()
            except Exception:
                pass
    except urllib.error.URLError as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None
    except Exception as e:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        raise RuntimeError(format_exception_safe(e, sensitive_values=sensitive)) from None

    if isinstance(res_list, dict) and "error" in res_list:
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        safe_msg, safe_data = _format_rpc_error_pair(res_list.get("error"), sensitive)
        raise RuntimeError(f"RPC Batch Error: {safe_msg} | data: {safe_data}")
    if not isinstance(res_list, list):
        raise RuntimeError("RPC Batch Error: unexpected response format")

    results = {}
    for item in res_list:
        if not isinstance(item, dict):
            continue
        if "error" in item:
            sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
            idx = item.get("id")
            method = calls[idx][0] if isinstance(idx, int) and 0 <= idx < len(calls) else "batch"
            safe_msg, safe_data = _format_rpc_error_pair(item.get("error"), sensitive)
            raise RuntimeError(f"RPC Error ({method}): {safe_msg} | data: {safe_data}")
        results[item.get("id")] = item.get("result")
    return [results.get(i) for i in range(len(calls))]


def get_working_rpc(rpc_urls):
    for url in rpc_urls:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Claimer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                cid = int(data["result"], 16)
                if cid == REQUIRED_CHAIN_ID:
                    return url
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:
                pass
            continue
        except Exception:
            continue
    return rpc_urls[0]


def decode_revert_reason(err_str):
    for sel, desc in ERROR_SELECTORS.items():
        if sel.lower() in err_str.lower():
            return f"{desc} (错误选择器: 0x{sel})"
    return err_str


def simulate_claim(rpc_url, miner_contract, miner_addr, epoch):
    """Simulates claim(epoch) via eth_call and estimates gas."""
    calldata_hex = SEL_CLAIM + encode(["uint256"], [epoch]).hex()
    tx = {
        "to": miner_contract,
        "from": miner_addr,
        "data": calldata_hex,
    }
    try:
        res = rpc_call(rpc_url, "eth_call", [tx, "latest"])
        try:
            gas_est_hex = rpc_call(rpc_url, "eth_estimateGas", [tx])
            gas_est = int(gas_est_hex, 16)
        except Exception:
            gas_est = 180000
        return True, gas_est, None, calldata_hex
    except Exception as e:
        sensitive = _collect_sensitive_values()
        safe_exc_msg = format_exception_safe(e, sensitive_values=sensitive)
        err_msg = decode_revert_reason(safe_exc_msg)
        err_msg = sanitize_text(err_msg, sensitive_values=sensitive)
        return False, None, err_msg, calldata_hex


def calculate_reward_details(rpc_url, miner_contract, miner_addr, epoch):
    """Calculates expected reward amount for epoch using batch RPC."""
    calls_def = [
        ("effectiveQuota(uint256)", ["uint256"], [epoch], ["uint256"]),
        ("totalWeight(uint256)", ["uint256"], [epoch], ["uint256"]),
        ("wMin(uint256)", ["uint256"], [epoch], ["uint256"]),
        ("userWeight(uint256,address)", ["uint256", "address"], [epoch, miner_addr], ["uint256"]),
        ("epochAddrCapBps(uint256)", ["uint256"], [epoch], ["uint256"]),
        ("claimed(uint256,address)", ["uint256", "address"], [epoch, miner_addr], ["bool"]),
        ("epoch()", [], [], ["uint256"]),
        ("lastSynced()", [], [], ["uint256"]),
    ]
    batch = []
    for sig, in_types, in_vals, _ in calls_def:
        sel = "0x" + keccak(sig.encode()).hex()[:8]
        data = sel + (encode(in_types, in_vals).hex() if in_types else "")
        batch.append(("eth_call", [{"to": miner_contract, "data": data}, "latest"]))

    res_list = rpc_batch_call(rpc_url, batch)
    results = []
    for i, (_, _, _, out_types) in enumerate(calls_def[:6]):
        val = decode(out_types, bytes.fromhex(res_list[i][2:]))[0]
        results.append(val)

    effQ, tw, wm, uw, capBps, cl = results
    cur_epoch = None
    last_synced = None
    if len(res_list) >= 8 and res_list[6] and res_list[7]:
        cur_epoch = decode(["uint256"], bytes.fromhex(res_list[6][2:]))[0]
        last_synced = decode(["uint256"], bytes.fromhex(res_list[7][2:]))[0]

    if capBps == 0:
        cap_res = rpc_call(rpc_url, "eth_call", [{"to": miner_contract, "data": SEL_ADDR_CAP_BPS}, "latest"])
        capBps = decode(["uint256"], bytes.fromhex(cap_res[2:]))[0]

    denom = max(tw, wm)
    amt = (effQ * uw) // denom if denom > 0 else 0
    maxAmt = (effQ * capBps) // 10000
    final_amt = min(amt, maxAmt)

    epoch_ended = (epoch < cur_epoch) if cur_epoch is not None else None
    if epoch_ended is False:
        epoch_settled = False
    elif epoch_ended is True and last_synced is not None:
        epoch_settled = epoch < last_synced
    else:
        epoch_settled = None

    return {
        "effectiveQuota": effQ,
        "totalWeight": tw,
        "wMin": wm,
        "userWeight": uw,
        "capBps": capBps,
        "claimed": cl,
        "expectedReward": final_amt,
        "currentEpoch": cur_epoch,
        "lastSynced": last_synced,
        "epochEnded": epoch_ended,
        "epochSettled": epoch_settled,
    }


def claim(args):
    cli_fee_cfg = validate_cli_fee_and_gas_args(
        gas_limit=args.gas_limit,
        gas_multiplier=args.gas_multiplier,
        max_fee_per_gas_gwei=getattr(args, "max_fee_per_gas_gwei", None),
        priority_fee_per_gas_gwei=getattr(args, "priority_fee_per_gas_gwei", None),
        deprecated_gas_price_gwei=getattr(args, "gas_price_gwei", None),
    )

    chain_id, rpc_url, proxy = chain_settings(args, None, DEFAULT_MINER_CONTRACT)
    rpc_url = rpc_url or get_working_rpc(DEFAULT_RPC_URLS)
    verify_rpc_chain(rpc_call(rpc_url, "eth_chainId", []), chain_id)
    token_symbol = "SPKH" if chain_id == MAINNET_CHAIN_ID else "TTK"
    miner_contract = to_checksum_address(proxy)

    
    # 2. Determine Miner Address & Signing Mode
    has_account = bool(getattr(args, "account", None) and args.account.strip())
    has_keystore = bool(getattr(args, "keystore", None) and args.keystore.strip())
    has_pw = bool(getattr(args, "password_file", None) and args.password_file.strip())

    if has_account and has_keystore:
        print("[错误] 参数冲突: --account 与 --keystore 互斥，请只选择其中一种！")
        sys.exit(1)

    if (has_account and not has_pw) or (has_pw and not has_account and not has_keystore):
        print("[错误] --account 与 --password-file 必须同时提供！")
        sys.exit(1)

    has_signer_flags = has_account or has_keystore
    env_pk = os.environ.get("PRIVATE_KEY") or os.environ.get("MINER_PRIVATE_KEY")
    has_env_pk = bool(env_pk and env_pk.strip())

    if has_signer_flags and has_env_pk:
        print("[错误] 同时提供了 keystore 参数 (--account/--keystore) 与环境变量私钥。为避免签名方式歧义，请只选择其中一种方式。")
        sys.exit(1)

    private_key = None
    miner_addr = None

    if args.send:
        if has_signer_flags:
            try:
                miner_addr = get_keystore_address(
                    account=args.account,
                    password_file=args.password_file,
                    keystore=args.keystore,
                )
            except CastSignerError as e:
                safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
                print(f"[错误] Keystore 签名地址解析失败: {safe_err}")
                sys.exit(1)

            if args.miner:
                input_miner = to_checksum_address(args.miner)
                if input_miner.lower() != miner_addr.lower():
                    print(f"[安全拦截] Keystore 签名地址 ({miner_addr}) 与参数指定的矿工地址 ({input_miner}) 不一致！")
                    print("拒绝执行以避免资金或签名混乱。")
                    sys.exit(1)
        elif has_env_pk:
            print("[警告] 正在使用环境变量私钥签名方式（不推荐）。建议改用 Foundry keystore 方式 (--account <别名> --password-file <密码文件> 或 --keystore <路径>) 进行签名。")
            private_key = env_pk.strip()
            from eth_account import Account
            try:
                account = Account.from_key(private_key)
                miner_addr = to_checksum_address(account.address)
            except Exception as e:
                safe_err = format_exception_safe(
                    e,
                    sensitive_values=_collect_sensitive_values(args, extra_secrets=[private_key]),
                )
                print(f"[错误] 解析环境变量私钥失败: {safe_err}")
                sys.exit(1)

            if args.miner:
                input_miner = to_checksum_address(args.miner)
                if input_miner.lower() != miner_addr.lower():
                    print(f"[安全拦截] 环境变量私钥对应地址 ({miner_addr}) 与参数指定的矿工地址 ({input_miner}) 不一致！")
                    print("拒绝执行以避免资金或签名混乱。")
                    sys.exit(1)
        else:
            print("[错误] --send 模式需要提供签名方式！")
            print("安全规则: 推荐使用 Foundry keystore 参数: --keystore <路径> 或 --account <别名> --password-file <密码文件>")
            print("旧路径:   环境变量 PRIVATE_KEY (或 MINER_PRIVATE_KEY) 读取 (不推荐)")
            sys.exit(1)
    else:
        # Dry-run mode: can use --miner, or derive from keystore / env PRIVATE_KEY if available
        if has_signer_flags:
            try:
                derived_addr = get_keystore_address(
                    account=args.account,
                    password_file=args.password_file,
                    keystore=args.keystore,
                )
                if args.miner:
                    input_miner = to_checksum_address(args.miner)
                    if input_miner.lower() != derived_addr.lower():
                        print(f"[安全拦截] Keystore 签名地址 ({derived_addr}) 与参数指定的矿工地址 ({input_miner}) 不一致！")
                        sys.exit(1)
                miner_addr = derived_addr
            except CastSignerError as e:
                safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
                print(f"[错误] Keystore 签名地址解析失败: {safe_err}")
                sys.exit(1)
                safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
                print(f"[错误] Keystore 签名地址解析失败: {safe_err}")
                sys.exit(1)
        elif args.miner:
            miner_addr = to_checksum_address(args.miner)
        else:
            if has_env_pk:
                from eth_account import Account
                try:
                    account = Account.from_key(env_pk.strip())
                    miner_addr = to_checksum_address(account.address)
                except Exception:
                    pass
        if not miner_addr:
            print("[错误] 请提供矿工地址进行预检: python script/claim.py --epoch <epoch> --miner <address>")
            sys.exit(1)

    epoch_to_claim = args.epoch
    if epoch_to_claim is None:
        print("[错误] 请指定待领取的轮次: --epoch <epoch_id>")
        print("提示: 可先运行 python script/status.py 查看可领取轮次。")
        sys.exit(1)

    print("=" * 78)
    print("                 PoolCoin 挖矿奖励领取脚本 (Reward Claimer)")
    print("=" * 78)
    print(f"网络:                {NETWORK_NAMES[chain_id]} (Chain ID {chain_id})")
    print(f"RPC 节点:            {sanitize_url(rpc_url)}")
    print(f"MinerV1 代理合约:    {miner_contract}")
    print(f"矿工地址 (msg.sender): {miner_addr}")
    print(f"目标领取轮次:        Epoch {epoch_to_claim}")
    print(f"执行模式:            {'[广播发送模式 (--send)]' if args.send else '[只读预检模式 (Simulation Only)]'}")
    print("-" * 78)

    # 3. Calculate Reward & Fetch On-Chain State
    print("[步骤 1/3] 正在查询链上轮次结算状态与应得奖励...")
    r_details = calculate_reward_details(rpc_url, miner_contract, miner_addr, epoch_to_claim)
    epoch_ended = r_details.get("epochEnded")
    epoch_settled = r_details.get("epochSettled")
    cur_epoch = r_details.get("currentEpoch")
    last_synced = r_details.get("lastSynced")

    if r_details["claimed"]:
        settlement_status = "[已结算且已在链上领取]"
        reward_label = "已领取轮次应得奖励"
    elif epoch_ended is False:
        cur_info = f"当前进行中: Epoch {cur_epoch}" if cur_epoch is not None else "当前轮次进行中"
        settlement_status = f"[尚未结束 ({cur_info}) - 仅作动态估算，不可作为已确定可领取金额]"
        reward_label = "估算参考奖励 (未结束)"
    elif epoch_ended is True and epoch_settled is False:
        sync_info = f"lastSynced={last_synced}" if last_synced is not None else "尚未 sync 结算"
        settlement_status = (
            f"[已结束但尚未结算 ({sync_info}) - 仅作动态估算，实际结算配额将在 claim 交易内自动结算冻结（或由 Keeper sync 结算）]"
        )
        reward_label = "估算参考奖励 (待claim内结算或sync)"
    elif epoch_ended is True and epoch_settled is True:
        settlement_status = f"[已结束且已结算 (lastSynced={last_synced} > Epoch {epoch_to_claim})]"
        reward_label = "已结算可领取奖励"
    else:
        settlement_status = "[未获取轮次结束/结算高度 - 暂按估算口径展示，以链上结算为准]"
        reward_label = "估算参考奖励 (待确认)"

    print(f"  • 轮次结算状态:     {settlement_status}")
    print(f"  • 轮次有效配额:     {r_details['effectiveQuota'] / 1e18:.4f} {token_symbol}")
    print(f"  • 矿工有效权重:     {r_details['userWeight']:,}")
    print(f"  • 全网总权重分母:   {max(r_details['totalWeight'], r_details['wMin']):,} (totalWeight={r_details['totalWeight']:,}, wMin={r_details['wMin']:,})")
    print(f"  • 单地址比例上限:   {r_details['capBps'] / 100:.2f}%")
    print(f"  • 链上已领取标记:   {r_details['claimed']}")
    print(f"  • {reward_label}: {r_details['expectedReward'] / 1e18:.6f} {token_symbol} ({r_details['expectedReward']} wei)")

    if r_details["claimed"]:
        print(f"\n[提示] 该轮次 Epoch {epoch_to_claim} 的奖励此前已在链上领取完毕，无需重复操作。")
        return

    if r_details["userWeight"] == 0:
        if epoch_ended is False:
            print(f"\n[提示] Epoch {epoch_to_claim} 尚未结束 (当前 Epoch {cur_epoch})，且矿工当前暂无算力权重 (userWeight == 0)，无法领取。")
        else:
            print(f"\n[错误] 矿工在 Epoch {epoch_to_claim} 没有任何算力权重 (userWeight == 0)，无法领取。")
        return

    # 4. Mandatory Preflight eth_call Simulation
    print("\n[步骤 2/3] 执行 eth_call 链上模拟预检 (Preflight Dry-run)...")
    success, gas_est, err, calldata_hex = simulate_claim(rpc_url, miner_contract, miner_addr, epoch_to_claim)

    if not success:
        print(f"\n[预检失败] 合约执行 revert:")
        print(f"  回滚原因: {err}")
        sys.exit(1)

    print(f"  [+] eth_call 模拟调用通过！无 revert 错误。")
    print(f"  [+] 预估 Gas Limit: {gas_est:,}")

    # 5. Resolve EIP-1559 Fees (type 2: maxFeePerGas & maxPriorityFeePerGas)
    fee_info = resolve_eip1559_fees(
        rpc_url=rpc_url,
        rpc_call_fn=rpc_call,
        gas_multiplier=args.gas_multiplier,
        max_fee_per_gas_gwei=getattr(args, "max_fee_per_gas_gwei", None),
        priority_fee_per_gas_gwei=getattr(args, "priority_fee_per_gas_gwei", None),
        deprecated_gas_price_gwei=getattr(args, "gas_price_gwei", None),
    )
    network_gas_price_wei = fee_info["network_gas_price_wei"]
    network_priority_fee_wei = fee_info["network_priority_fee_wei"]
    max_fee_wei = fee_info["max_fee_per_gas_wei"]
    priority_fee_wei = fee_info["priority_fee_per_gas_wei"]

    if network_gas_price_wei is not None:
        print(f"  [+] 当前网络 Gas 价格 (eth_gasPrice): {network_gas_price_wei / 1e9:.3f} gwei ({network_gas_price_wei} wei)")
    else:
        print(f"  [+] 显式 Max Fee 覆盖 (maxFeePerGas): {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei) [未查询 eth_gasPrice，不适用]")
    if network_priority_fee_wei is not None:
        print(f"  [+] 当前网络建议优先费 (eth_maxPriorityFeePerGas): {network_priority_fee_wei / 1e9:.3f} gwei ({network_priority_fee_wei} wei)")
    else:
        print(f"  [+] 显式优先费覆盖 (maxPriorityFeePerGas): {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei) [未查询 eth_maxPriorityFeePerGas]")

    for warn_kind, orig_val, lifted_val in fee_info["warnings"]:
        if warn_kind == "priority_floor":
            print(f"  [警告] 计算所得 Max Fee ({orig_val / 1e9:.3f} gwei) 低于 Priority Fee，已提升至 {lifted_val / 1e9:.3f} gwei。")

    gas_limit = max(gas_est + 40000, cli_fee_cfg["gas_limit"])
    max_gas_cost_bnb = (gas_limit * max_fee_wei) / 1e18
    print(f"  [+] 最终 Max Fee (maxFeePerGas): {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei)")
    print(f"  [+] 最终 Priority Fee (maxPriorityFeePerGas): {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei)")
    print(f"  [+] Gas Limit 上限: {gas_limit:,}")
    print(f"  [+] 最大 Gas 费用上限: {max_gas_cost_bnb:.6f} BNB")
    if network_gas_price_wei is not None and not fee_info["explicit_max_fee"] and max_fee_wei < network_gas_price_wei:
        raise ValueError("安全校验失败: 自动计算的 maxFeePerGas 不得低于网络当前 gas price。")
    if max_fee_wei < priority_fee_wei:
        raise ValueError("安全校验失败: maxFeePerGas 不得低于 maxPriorityFeePerGas。")

    # If NOT in --send mode, stop here and guide the user
    if not args.send:
        print("\n" + "=" * 78)
        print("                  预检成功 (DRY-RUN SIMULATION PASSED)")
        print("=" * 78)
        print(f"本操作为只读预检，尚未向网络广播任何交易。")
        print(f"若要向网络广播真实领取交易，请使用 Foundry keystore 签名并执行:")
        print(f"  1. 准备 Foundry keystore 账户与密码文件 (--account 与 --password-file)")
        print(f"  2. 添加 --send 参数执行:")
        print(f"     python script/claim.py --chain-id {chain_id} --epoch {epoch_to_claim} --send --account <keystore_account> --password-file <password_file_path>")
        print("=" * 78)
        return

    # 6. Broadcast Mode: Transaction Summary & Secondary Confirmation
    print("\n" + "=" * 78)
    print("                    交易就绪明细 (TRANSACTION PREVIEW)")
    print("=" * 78)
    print(f"合约目标:     {miner_contract} (MinerV1 Proxy)")
    print(f"签名调用方:   {miner_addr}")
    print(f"调用函数:     claim(uint256 e = {epoch_to_claim})")
    if epoch_settled is True:
        print(f"已结算领取量: {r_details['expectedReward'] / 1e18:.6f} {token_symbol}")
    else:
        print(f"估算铸造量:   {r_details['expectedReward'] / 1e18:.6f} {token_symbol} (以链上结算为准)")
    print(f"Gas Limit:    {gas_limit:,}")
    print(f"Max Fee:      {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei)")
    print(f"Priority Fee: {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei)")
    print(f"最大 Gas 费上限: {max_gas_cost_bnb:.6f} BNB")
    print("=" * 78)

    # Secondary confirmation prompt
    confirm = input(f"\n请确认是否向 {NETWORK_NAMES[chain_id]} 广播领取交易？(输入 'yes' 确认发送): ")
    if confirm.strip().lower() != "yes":
        print("[已取消] 用户取消操作，未发送任何交易。")
        return

    # 7. Broadcast Transaction
    try:
        nonce_res = rpc_call(rpc_url, "eth_getTransactionCount", [miner_addr, "pending"])
        account_nonce = parse_pending_nonce(nonce_res)
    except Exception as e:
        safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
        print(f"[错误] 查询 pending nonce 失败: {safe_err}")
        print("查询失败直接中止，不得回退为 0。")
        sys.exit(1)

    print(f"\n[步骤 3/3] 正在向 {NETWORK_NAMES[chain_id]} 广播交易 (Nonce: {account_nonce})...", flush=True)

    if has_signer_flags:
        try:
            tx_hash = cast_send(
                contract_address=miner_contract,
                calldata=calldata_hex,
                account=args.account,
                password_file=args.password_file,
                rpc_url=rpc_url,
                nonce=account_nonce,
                gas_price=max_fee_wei,
                gas_limit=gas_limit,
                priority_gas_price=priority_fee_wei,
                keystore=args.keystore,
            )
        except CastSendUncertainError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[错误] {UNCERTAIN_TX_WARNING} ({safe_err})")
            sys.exit(1)
        except CastSignerError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[错误] Keystore 发送失败: {safe_err}")
            sys.exit(1)
    else:
        from eth_account import Account
        tx_dict = {
            "type": 2,
            "to": miner_contract,
            "from": miner_addr,
            "value": 0,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "nonce": account_nonce,
            "chainId": chain_id,
            "data": calldata_hex,
        }

        signed_tx = Account.sign_transaction(tx_dict, private_key)
        raw_tx_hex = "0x" + signed_tx.raw_transaction.hex()
        tx_hash = rpc_call(rpc_url, "eth_sendRawTransaction", [raw_tx_hex])
    print(f"\n[广播成功] 交易哈希: {tx_hash}")
    print(f"BscScan 浏览器查询: {EXPLORERS[chain_id]}/tx/{tx_hash}")

    # Wait for receipt
    print("正在等待区块链打包上链确认...", flush=True)
    timeout = 60
    poll_start = time.time()
    receipt = None
    while time.time() - poll_start < timeout:
        try:
            receipt = rpc_call(rpc_url, "eth_getTransactionReceipt", [tx_hash])
            if receipt:
                break
        except Exception:
            pass
        time.sleep(3)

    if receipt:
        status = int(receipt.get("status", "0x0"), 16)
        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        block_num = int(receipt.get("blockNumber", "0x0"), 16)
        if status == 1:
            print(f"[交易成功] 交易已在区块 #{block_num} 确认！")
            print(f"实际消耗 Gas: {gas_used:,}")
            if epoch_settled is True:
                print(f"成功领取 Epoch {epoch_to_claim} 奖励 {r_details['expectedReward'] / 1e18:.6f} {token_symbol}！")
            else:
                print(f"成功领取 Epoch {epoch_to_claim} 奖励（估算参考 {r_details['expectedReward'] / 1e18:.6f} {token_symbol}，实际以链上结算为准）！")
        else:
            print(f"[交易回滚] 交易在区块 #{block_num} 回滚，执行失败！")
            print(f"消耗 Gas: {gas_used:,}")
    else:
        print("[提示] 交易已提交，但超时未获取到回执。请在 BscScan 上查看最新状态:")
        print(f"  {EXPLORERS[chain_id]}/tx/{tx_hash}")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin 挖矿奖励领取工具 (BSC Mainnet / Testnet)")
    parser.add_argument("--epoch", type=int, default=None, help="目标领取轮次 (epoch id)")
    parser.add_argument("--miner", type=str, default=None, help="矿工钱包地址 (只读预检时必填或从私钥推导)")
    parser.add_argument("--account", type=str, default=None, help="Foundry keystore 钱包别名 (与 --password-file 搭配使用)")
    parser.add_argument("--password-file", type=str, default=None, help="Foundry keystore 密码文件路径 (与 --account 搭配使用)")
    parser.add_argument("--keystore", type=str, default=None, help="自定义加密 keystore 文件路径 (支持交互式原生 TTY 提示或配合 --password-file)")
    parser.add_argument("--send", action="store_true", help="发送真实链上交易 (默认仅进行 eth_call 预检)")
    parser.add_argument("--rpc", type=str, default=argparse.SUPPRESS, help="自定义 BSC RPC 节点")
    parser.add_argument("--miner-contract", type=str, default=None, help="MinerV1 代理合约地址")
    parser.add_argument("--gas-multiplier", type=str, default="1.15", help="Max fee 浮动乘数 (默认: 1.15)")
    parser.add_argument("--max-fee-per-gas-gwei", type=str, default=None, help="显式指定 EIP-1559 maxFeePerGas (gwei)")
    parser.add_argument("--priority-fee-per-gas-gwei", type=str, default=None, help="显式指定 EIP-1559 maxPriorityFeePerGas (gwei)")
    parser.add_argument("--gas-price-gwei", type=str, default=None, help="[兼容别名] 等同于 --max-fee-per-gas-gwei (gwei)")
    parser.add_argument("--gas-limit", type=int, default=250000, help="Gas Limit 上限 (默认: 250000)")

    add_chain_argument(parser)
    args = parser.parse_args()
    try:
        claim(args)
    except Exception as e:
        print(
            f"[错误] {format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
