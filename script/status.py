#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Miner Status Inspector for MinerV1 on BSC Testnet (chainId 97)

Read-only inspection script.
Queries and displays:
1. Staking status and since (from StakeVault).
2. Current epoch and lastSynced (from MinerV1).
3. Warmup weight (ramp) for current and past epochs.
4. Per-epoch used bitmap (usedMask) and credited shares (counted).
5. Claimable rewards calculation & verification for ended epochs.
6. ERC-20 Token balance (TTK).

HARD CONSTRAINTS ENFORCED:
- Pure read-only inspection.
- Enforces chainId == 97 (BSC Testnet).
- Never reads or touches .env or any private keys.
- Never writes to any external services.
"""

import os
import sys
import time
import json
import argparse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from eth_abi import encode, decode
from eth_hash.auto import keccak

REQUIRED_CHAIN_ID = 97
DEFAULT_MINER_CONTRACT = "0x420382a0f874c1F120621f847d81581e8737504e"
DEFAULT_RPC_URLS = [
    "https://bsc-testnet-rpc.publicnode.com",
    "https://data-seed-prebsc-1-s1.binance.org:8545",
    "https://data-seed-prebsc-2-s1.binance.org:8545",
]

RAMP_EPOCHS = 24
RAMP_START_BPS = 2500
WSCALE = 10**10
CAP = 15
ADDR_CAP_BPS_DEFAULT = 2000

# Function selectors
SEL_EPOCH = "0x900cf0cf"          # epoch() -> uint256
SEL_LAST_SYNCED = "0x93afb4c5"    # lastSynced() -> uint256
SEL_TOKEN = "0xfc0c546a"          # token() -> address
SEL_VAULT = "0xfbfa77cf"          # vault() -> address
SEL_ADDR_CAP_BPS = "0xbc9c68ae"   # ADDR_CAP_BPS() -> uint256
SEL_VAULT_STATUS = "0x645b8b1b"   # status(address) -> (bool, uint256)
SEL_VAULT_STAKES = "0x16934fc4"   # stakes(address) -> (uint64, uint64, bool, uint128)
SEL_TOKEN_BALANCE = "0x70a08231"  # balanceOf(address) -> uint256
SEL_UNALLOCATED_GAP = "0xca6875f7"  # unallocatedGap(uint256) -> uint256
SELECTOR_INIT_EPOCH_SEED = "0xd9d9c33c"  # initEpochSeed(uint256,uint256)
EVENT_EPOCH_SETTLED_TOPIC = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
EVENT_REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"


def to_checksum_address(address):
    clean = address.lower().replace("0x", "")
    h = keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


def rpc_batch_call(rpc_url, calls, timeout=15):
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
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Status/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res_list = json.loads(resp.read().decode("utf-8"))
        results = {}
        for item in res_list:
            if "error" in item:
                results[item["id"]] = {"error": item["error"]}
            else:
                results[item["id"]] = item.get("result")
        return [results.get(i) for i in range(len(calls))]


def get_working_rpc(rpc_urls):
    for url in rpc_urls:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Status/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                cid = int(data["result"], 16)
                if cid == REQUIRED_CHAIN_ID:
                    return url
        except Exception:
            continue
    return rpc_urls[0]


def calculate_warmup_weight(epoch, since, active):
    """Calculates warmup ramp in basis points matching MinerV1._getRamp."""
    if not active:
        return 0, "未质押 (Not Staked)"
    if epoch < since + 1:
        return 0, f"Epoch {since} 质押生效中，Epoch {since + 1} 起开放挖矿 (质押当轮不可挖矿)"
    age = epoch - (since + 1)
    effective_age = min(age, RAMP_EPOCHS)
    ramp_bps = RAMP_START_BPS + (10000 - RAMP_START_BPS) * effective_age // RAMP_EPOCHS
    desc = f"{ramp_bps / 100:.2f}% ({ramp_bps} bps, 参与轮数={age}/{RAMP_EPOCHS})"
    return ramp_bps, desc


def format_used_mask(mask):
    """Formats a 15-bit uint16 mask into binary representation and list of active ks."""
    active_ks = [k for k in range(CAP) if (mask & (1 << k)) != 0]
    binary_str = f"{mask:015b}"
    k_str = ", ".join(str(k) for k in active_ks) if active_ks else "无"
    return f"0b{binary_str} (0x{mask:04x}) [已用 k 索引: {k_str}]"


def inspect_status(args):
    miner_addr = to_checksum_address(args.miner)
    miner_contract = to_checksum_address(args.miner_contract)
    
    rpc_url = args.rpc if args.rpc else get_working_rpc(DEFAULT_RPC_URLS)
    
    # 1. Verify chain ID
    cid_res = rpc_batch_call(rpc_url, [("eth_chainId", [])])[0]
    chain_id = int(cid_res, 16) if isinstance(cid_res, str) else int(cid_res)
    if chain_id != REQUIRED_CHAIN_ID:
        raise ValueError(f"安全拦截: Chain ID {chain_id} != {REQUIRED_CHAIN_ID} (必须为 BSC Testnet)!")

    # 2. Fetch protocol core addresses and states
    core_calls = [
        ("eth_call", [{"to": miner_contract, "data": SEL_EPOCH}, "latest"]),
        ("eth_call", [{"to": miner_contract, "data": SEL_LAST_SYNCED}, "latest"]),
        ("eth_call", [{"to": miner_contract, "data": SEL_TOKEN}, "latest"]),
        ("eth_call", [{"to": miner_contract, "data": SEL_VAULT}, "latest"]),
        ("eth_call", [{"to": miner_contract, "data": SEL_ADDR_CAP_BPS}, "latest"]),
    ]
    core_results = rpc_batch_call(rpc_url, core_calls)
    cur_epoch = int(core_results[0], 16)
    last_synced = int(core_results[1], 16)
    token_contract = to_checksum_address("0x" + core_results[2][-40:])
    vault_contract = to_checksum_address("0x" + core_results[3][-40:])
    default_addr_cap_bps = decode(["uint256"], bytes.fromhex(core_results[4][2:]))[0]

    # 3. Fetch Miner Staking & Token Balance
    user_calls = [
        ("eth_call", [{"to": vault_contract, "data": SEL_VAULT_STATUS + encode(["address"], [miner_addr]).hex()}, "latest"]),
        ("eth_call", [{"to": vault_contract, "data": SEL_VAULT_STAKES + encode(["address"], [miner_addr]).hex()}, "latest"]),
        ("eth_call", [{"to": token_contract, "data": SEL_TOKEN_BALANCE + encode(["address"], [miner_addr]).hex()}, "latest"]),
    ]
    user_results = rpc_batch_call(rpc_url, user_calls)
    active, since = decode(["bool", "uint256"], bytes.fromhex(user_results[0][2:]))
    deposit_epoch, withdraw_req_epoch, has_stake, deposit_amount = decode(
        ["uint64", "uint64", "bool", "uint128"], bytes.fromhex(user_results[1][2:])
    )
    token_balance_wei = decode(["uint256"], bytes.fromhex(user_results[2][2:]))[0]

    cur_ramp_bps, cur_ramp_desc = calculate_warmup_weight(cur_epoch, since, active)

    print("=" * 78)
    print("                  PoolCoin 矿工链上全景状态报告 (Miner Status Report)")
    print("=" * 78)
    print(f"网络:                BSC Testnet (Chain ID {chain_id})")
    print(f"RPC 节点:            {rpc_url}")
    print(f"矿工地址:            {miner_addr}")
    print(f"MinerV1 代理合约:    {miner_contract}")
    print(f"StakeVault 质押合约: {vault_contract}")
    print(f"Token (TTK) 合约:    {token_contract}")
    print("-" * 78)
    print("【1. 质押与基础轮次状态 (Staking & Protocol Status)】")
    print(f"  [*] 质押状态 (active):        {'Active (已激活生效)' if active else 'Inactive (未质押或已申请提款)'}")
    print(f"  [*] 质押生效轮 (since):       Epoch {since} (存入轮次: Epoch {deposit_epoch})")
    print(f"  [*] 质押金额 (deposit):       {deposit_amount / 1e18:.4f} BNB ({deposit_amount} wei)")
    if withdraw_req_epoch != 0:
        print(f"  [*] 提款请求状态:             已在 Epoch {withdraw_req_epoch} 申请提款 (处于 48 轮冷却期中)")
    else:
        print(f"  [*] 提款请求状态:             正常质押中 (未发起提款申请)")
    print(f"  [*] 当前协议轮次 (current):   Epoch {cur_epoch}")
    sync_status = "已完全同步" if last_synced >= cur_epoch else f"落后 {cur_epoch - last_synced} 轮 (需要 keeper 调用 sync 追赶)"
    print(f"  [*] 协议同步轮次 (lastSynced): Epoch {last_synced} ({sync_status})")
    print(f"  [*] 当前轮预热权重 (ramp):    {cur_ramp_desc}")
    print(f"  [*] Token 余额 (TTK):         {token_balance_wei / 1e18:.6f} TTK ({token_balance_wei} wei)")
    print("-" * 78)

    # 4. Fetch per-epoch participation
    scan_start = 0 if args.all_epochs else max(0, int(since))
    scan_end = cur_epoch

    epoch_queries = []
    epoch_meta = []
    for e in range(scan_start, scan_end + 1):
        epoch_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"usedMask(uint256,address)").hex()[:8] + encode(["uint256", "address"], [e, miner_addr]).hex()}, "latest"]))
        epoch_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"counted(uint256,address)").hex()[:8] + encode(["uint256", "address"], [e, miner_addr]).hex()}, "latest"]))
        epoch_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"userWeight(uint256,address)").hex()[:8] + encode(["uint256", "address"], [e, miner_addr]).hex()}, "latest"]))
        epoch_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"claimed(uint256,address)").hex()[:8] + encode(["uint256", "address"], [e, miner_addr]).hex()}, "latest"]))
        epoch_meta.append(e)

    epoch_results = rpc_batch_call(rpc_url, epoch_queries)

    epochs_with_data = []
    for idx, e in enumerate(epoch_meta):
        base_i = idx * 4
        mask = decode(["uint16"], bytes.fromhex(epoch_results[base_i][2:]))[0]
        c = decode(["uint8"], bytes.fromhex(epoch_results[base_i + 1][2:]))[0]
        uw = decode(["uint256"], bytes.fromhex(epoch_results[base_i + 2][2:]))[0]
        cl = decode(["bool"], bytes.fromhex(epoch_results[base_i + 3][2:]))[0]
        
        if c > 0 or mask > 0 or uw > 0 or e == cur_epoch or args.all_epochs:
            epochs_with_data.append({
                "epoch": e,
                "usedMask": mask,
                "counted": c,
                "userWeight": uw,
                "claimed": cl,
            })

    # For epochs with userWeight > 0 and ended (e < cur_epoch), fetch reward details
    reward_queries = []
    reward_meta = []
    for item in epochs_with_data:
        e = item["epoch"]
        if e < cur_epoch and item["userWeight"] > 0:
            reward_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"effectiveQuota(uint256)").hex()[:8] + encode(["uint256"], [e]).hex()}, "latest"]))
            reward_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"totalWeight(uint256)").hex()[:8] + encode(["uint256"], [e]).hex()}, "latest"]))
            reward_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"wMin(uint256)").hex()[:8] + encode(["uint256"], [e]).hex()}, "latest"]))
            reward_queries.append(("eth_call", [{"to": miner_contract, "data": "0x" + keccak(b"epochAddrCapBps(uint256)").hex()[:8] + encode(["uint256"], [e]).hex()}, "latest"]))
            reward_queries.append(("eth_call", [{"to": miner_contract, "data": SEL_UNALLOCATED_GAP + encode(["uint256"], [e]).hex()}, "latest"]))
            reward_meta.append(e)

    reward_results_map = {}
    if reward_queries:
        r_results = rpc_batch_call(rpc_url, reward_queries)
        for idx, e in enumerate(reward_meta):
            bi = idx * 5
            effQ = decode(["uint256"], bytes.fromhex(r_results[bi][2:]))[0]
            tw = decode(["uint256"], bytes.fromhex(r_results[bi + 1][2:]))[0]
            wm = decode(["uint256"], bytes.fromhex(r_results[bi + 2][2:]))[0]
            capBps = decode(["uint256"], bytes.fromhex(r_results[bi + 3][2:]))[0]
            unalloc_gap = decode(["uint256"], bytes.fromhex(r_results[bi + 4][2:]))[0] if len(r_results) > bi + 4 and r_results[bi + 4] else 0
            if capBps == 0:
                capBps = default_addr_cap_bps
            reward_results_map[e] = {
                "effectiveQuota": effQ,
                "totalWeight": tw,
                "wMin": wm,
                "capBps": capBps,
                "unallocatedGap": unalloc_gap,
            }

    print("【2. 轮次参与明细 (Per-Epoch Participation & Credited Shares)】")
    total_claimable_wei = 0
    claimable_epochs = []

    if not epochs_with_data:
        print("  [提示] 该矿工在所查询的轮次范围内暂无已计入份额。")
    else:
        for item in epochs_with_data:
            e = item["epoch"]
            mask = item["usedMask"]
            c = item["counted"]
            uw = item["userWeight"]
            cl = item["claimed"]
            ramp_bps, ramp_str = calculate_warmup_weight(e, since, active)

            epoch_tag = "当前进行中 (Current)" if e == cur_epoch else "已结束 (Ended)"
            print(f"\n  --- [Epoch {e:2d}] ({epoch_tag}) ---")
            print(f"    [-] 每轮已用位图 (usedMask):    {format_used_mask(mask)}")
            print(f"    [-] 已计入份额 (counted):       {c} / {CAP}")
            print(f"    [-] 该轮预热权重 (ramp):        {ramp_str}")
            print(f"    [-] 矿工获得权重 (userWeight):  {uw:,}")
            print(f"    [-] 奖励领取状态 (claimed):     {'已领取 (True)' if cl else '未领取 (False)'}")

            if e < cur_epoch and uw > 0:
                r_info = reward_results_map.get(e, {})
                effQ = r_info.get("effectiveQuota", 0)
                tw = r_info.get("totalWeight", 0)
                wm = r_info.get("wMin", 0)
                capBps = r_info.get("capBps", 2000)

                denom = max(tw, wm)
                amt = (effQ * uw) // denom if denom > 0 else 0
                maxAmt = (effQ * capBps) // 10000
                final_amt = min(amt, maxAmt)

                print(f"    [-] 轮次有效配额 (effQuota):    {effQ / 1e18:.4f} TTK")
                print(f"    [-] 权重分母 (denom):           {denom:,} (totalWeight={tw:,}, wMin={wm:,})")
                print(f"    [-] 单地址比例上限 (addrCap):   {capBps / 100:.2f}% (单地址最大奖励: {maxAmt / 1e18:.4f} TTK)")
                
                if cl:
                    print(f"    [+] 奖励结算状态:               [已在链上领取] Token 已铸造到矿工钱包")
                else:
                    print(f"    [+] 可领取奖励 (Claimable):     {final_amt / 1e18:.6f} TTK ({final_amt} wei)")
                    total_claimable_wei += final_amt
                    claimable_epochs.append((e, final_amt))
            elif e == cur_epoch:
                if uw > 0:
                    print(f"    [*] 奖励结算状态:               [本轮进行中] 矿工已提交 {c} 个份额，需等待本轮结束后调用 claim 领取")
                else:
                    print(f"    [*] 奖励结算状态:               [本轮进行中] 暂未提交有效份额")

    print("\n" + "-" * 78)
    print("【3. 可领取奖励总计 (Claimable Reward Summary)】")
    if claimable_epochs:
        epoch_list_str = ", ".join(f"Epoch {e} ({amt / 1e18:.4f} TTK)" for e, amt in claimable_epochs)
        print(f"  [+] 待领取轮次:             {epoch_list_str}")
        print(f"  [+] 待领取总奖励:           {total_claimable_wei / 1e18:.6f} TTK ({total_claimable_wei} wei)")
        print(f"  [+] 领取命令提示:           python script/claim.py --epoch {claimable_epochs[0][0]}")
    else:
        print("  [*] 待领取总奖励:           0 TTK (暂无待领取的已结束轮次)")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(description="PoolCoin 矿工链上全景状态查询工具 (BSC Testnet)")
    parser.add_argument("miner", nargs="?", default=None, help="矿工 EOA 钱包地址")
    parser.add_argument("--miner", dest="miner_flag", default=None, help="矿工 EOA 钱包地址 (--miner 方式)")
    parser.add_argument("--rpc", type=str, default=None, help="自定义 BSC Testnet RPC 节点地址")
    parser.add_argument("--miner-contract", type=str, default=DEFAULT_MINER_CONTRACT, help="MinerV1 代理合约地址")
    parser.add_argument("--all-epochs", action="store_true", help="全量扫描自 Epoch 0 起的所有轮次")
    args = parser.parse_args()

    miner_addr = args.miner or args.miner_flag
    if not miner_addr:
        print("[错误] 请提供矿工地址: python script/status.py <miner_address> 或 python script/status.py --miner <miner_address>")
        sys.exit(1)
    args.miner = miner_addr
    inspect_status(args)


if __name__ == "__main__":
    main()
