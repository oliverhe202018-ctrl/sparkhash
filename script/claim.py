#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Mining Reward Claim Script for MinerV1 on BSC Testnet (chainId 97)

Claims finalized epoch mining rewards from MinerV1.
DEFAULT MODE: Read-only simulation via eth_call (dry-run preflight).
SEND MODE (--send): Prompts for manual user secondary confirmation, enforces gasPrice >= eth_gasPrice,
and signs transaction using PRIVATE_KEY from environment variable.

HARD CONSTRAINTS ENFORCED:
1. Hardcoded refusal for any chainId != 97 (BSC Testnet only).
2. Never reads .env or prints private keys. Private key is strictly from PRIVATE_KEY env var.
3. Requires explicit manual confirmation ("yes") before broadcasting.
4. Gas price strictly >= network gasPrice floor.
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

ERROR_SELECTORS = {
    "756b7857": "EpochNotEnded() - 该轮次尚未结束，无法领取奖励",
    "646cf558": "AlreadyClaimed() - 该轮次奖励已被领取，请勿重复领取",
    "19a2a9bd": "ZeroWeight() - 矿工在该轮次中没有有效算力权重 (userWeight == 0)",
    "3ee5aeb5": "ReentrancyGuardReentrantCall() - 重入防护触发",
    "ca97a081": "EffectiveQuotaZero - 该轮次有效配额为 0",
}

SEL_CLAIM = "0x379607f5"           # claim(uint256)
SEL_EPOCH = "0x900cf0cf"           # epoch() -> uint256
SEL_TOKEN = "0xfc0c546a"           # token() -> address
SEL_ADDR_CAP_BPS = "0xbc9c68ae"    # ADDR_CAP_BPS() -> uint256


def to_checksum_address(address):
    clean = address.lower().replace("0x", "")
    h = keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


def rpc_call(rpc_url, method, params):
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            err = res.get("error")
            err_data = err.get("data", "") if isinstance(err, dict) else ""
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"RPC Error ({method}): {err_msg} | data: {err_data}")
        return res.get("result")


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
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Claimer/1.0"},
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
                headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Claimer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                cid = int(data["result"], 16)
                if cid == REQUIRED_CHAIN_ID:
                    return url
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
        err_msg = decode_revert_reason(str(e))
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
    ]
    batch = []
    for sig, in_types, in_vals, _ in calls_def:
        sel = "0x" + keccak(sig.encode()).hex()[:8]
        data = sel + (encode(in_types, in_vals).hex() if in_types else "")
        batch.append(("eth_call", [{"to": miner_contract, "data": data}, "latest"]))

    res_list = rpc_batch_call(rpc_url, batch)
    results = []
    for i, (_, _, _, out_types) in enumerate(calls_def):
        val = decode(out_types, bytes.fromhex(res_list[i][2:]))[0]
        results.append(val)

    effQ, tw, wm, uw, capBps, cl = results
    if capBps == 0:
        cap_res = rpc_call(rpc_url, "eth_call", [{"to": miner_contract, "data": SEL_ADDR_CAP_BPS}, "latest"])
        capBps = decode(["uint256"], bytes.fromhex(cap_res[2:]))[0]

    denom = max(tw, wm)
    amt = (effQ * uw) // denom if denom > 0 else 0
    maxAmt = (effQ * capBps) // 10000
    final_amt = min(amt, maxAmt)

    return {
        "effectiveQuota": effQ,
        "totalWeight": tw,
        "wMin": wm,
        "userWeight": uw,
        "capBps": capBps,
        "claimed": cl,
        "expectedReward": final_amt,
    }


def claim(args):
    rpc_url = args.rpc if args.rpc else get_working_rpc(DEFAULT_RPC_URLS)
    
    # 1. Enforce BSC Testnet
    res = rpc_call(rpc_url, "eth_chainId", [])
    chain_id = int(res, 16) if isinstance(res, str) else int(res)
    if chain_id != REQUIRED_CHAIN_ID:
        raise ValueError(
            f"安全拦截: Chain ID {chain_id} != {REQUIRED_CHAIN_ID} (必须为 BSC Testnet)! 拒绝执行。"
        )

    miner_contract = to_checksum_address(args.miner_contract)
    
    # 2. Determine Miner Address & Private Key
    private_key = None
    miner_addr = None

    if args.send:
        # Strictly read from environment variable
        private_key = os.environ.get("PRIVATE_KEY") or os.environ.get("MINER_PRIVATE_KEY")
        if not private_key:
            print("[错误] --send 模式需要提供签名私钥！")
            print("安全规则: 私钥只能从环境变量 PRIVATE_KEY (或 MINER_PRIVATE_KEY) 读取，禁止通过命令行或文件明文传递。")
            print("请在运行前设置环境变量:")
            print("  PowerShell: $env:PRIVATE_KEY=\"0x...\"")
            print("  Bash:       export PRIVATE_KEY=\"0x...\"")
            sys.exit(1)

        from eth_account import Account
        try:
            account = Account.from_key(private_key)
            miner_addr = to_checksum_address(account.address)
        except Exception as e:
            print(f"[错误] 解析环境变量私钥失败: {e}")
            sys.exit(1)

        if args.miner:
            input_miner = to_checksum_address(args.miner)
            if input_miner.lower() != miner_addr.lower():
                print(f"[安全拦截] 环境变量私钥对应地址 ({miner_addr}) 与参数指定的矿工地址 ({input_miner}) 不一致！")
                print("拒绝执行以避免资金或签名混乱。")
                sys.exit(1)
    else:
        # Dry-run mode: can use --miner, or derive from env PRIVATE_KEY if available
        if args.miner:
            miner_addr = to_checksum_address(args.miner)
        else:
            env_pk = os.environ.get("PRIVATE_KEY") or os.environ.get("MINER_PRIVATE_KEY")
            if env_pk:
                from eth_account import Account
                try:
                    account = Account.from_key(env_pk)
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
    print(f"网络:                BSC Testnet (Chain ID {chain_id})")
    print(f"RPC 节点:            {rpc_url}")
    print(f"MinerV1 代理合约:    {miner_contract}")
    print(f"矿工地址 (msg.sender): {miner_addr}")
    print(f"目标领取轮次:        Epoch {epoch_to_claim}")
    print(f"执行模式:            {'[广播发送模式 (--send)]' if args.send else '[只读预检模式 (Simulation Only)]'}")
    print("-" * 78)

    # 3. Calculate Reward & Fetch On-Chain State
    print("[步骤 1/3] 正在查询链上轮次结算状态与应得奖励...")
    r_details = calculate_reward_details(rpc_url, miner_contract, miner_addr, epoch_to_claim)
    print(f"  • 轮次有效配额:     {r_details['effectiveQuota'] / 1e18:.4f} TTK")
    print(f"  • 矿工有效权重:     {r_details['userWeight']:,}")
    print(f"  • 全网总权重分母:   {max(r_details['totalWeight'], r_details['wMin']):,} (totalWeight={r_details['totalWeight']:,}, wMin={r_details['wMin']:,})")
    print(f"  • 单地址比例上限:   {r_details['capBps'] / 100:.2f}%")
    print(f"  • 链上已领取标记:   {r_details['claimed']}")
    print(f"  • 预期可领取奖励:   {r_details['expectedReward'] / 1e18:.6f} TTK ({r_details['expectedReward']} wei)")

    if r_details["claimed"]:
        print(f"\n[提示] 该轮次 Epoch {epoch_to_claim} 的奖励此前已在链上领取完毕，无需重复操作。")
        return

    if r_details["userWeight"] == 0:
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

    # 5. Check Gas Price
    net_gas_price_hex = rpc_call(rpc_url, "eth_gasPrice", [])
    network_gas_price_wei = int(net_gas_price_hex, 16)
    network_gas_price_gwei = network_gas_price_wei / 1e9
    print(f"  [+] 当前网络 Gas 价格: {network_gas_price_gwei:.3f} gwei ({network_gas_price_wei} wei)")

    multiplier = max(1.0, float(args.gas_multiplier))
    target_gas_price_wei = int(network_gas_price_wei * multiplier)

    if args.gas_price_gwei is not None:
        override_wei = int(float(args.gas_price_gwei) * 1e9)
        if override_wei < network_gas_price_wei:
            print(f"  [警告] 指定 Gas Price ({args.gas_price_gwei} gwei) 低于网络价，强制提升至网络价下限。")
            target_gas_price_wei = network_gas_price_wei
        else:
            target_gas_price_wei = override_wei

    print(f"  [+] 最终交易 Gas 价格: {target_gas_price_wei / 1e9:.3f} gwei ({target_gas_price_wei} wei)")
    assert target_gas_price_wei >= network_gas_price_wei, "安全校验: 交易 gas price 必须 >= 网络当前 gas price！"

    # If NOT in --send mode, stop here and guide the user
    if not args.send:
        print("\n" + "=" * 78)
        print("                  预检成功 (DRY-RUN SIMULATION PASSED)")
        print("=" * 78)
        print(f"本操作为只读预检，尚未向网络广播任何交易。")
        print(f"若要向网络广播真实领取交易，请执行以下步骤:")
        print(f"  1. 确保已将矿工私钥注入环境变量: $env:PRIVATE_KEY=\"0x...\" (或 export PRIVATE_KEY=\"0x...\")")
        print(f"  2. 添加 --send 参数执行:")
        print(f"     python script/claim.py --epoch {epoch_to_claim} --send")
        print("=" * 78)
        return

    # 6. Broadcast Mode: Transaction Summary & Secondary Confirmation
    print("\n" + "=" * 78)
    print("                    交易就绪明细 (TRANSACTION PREVIEW)")
    print("=" * 78)
    print(f"合约目标:     {miner_contract} (MinerV1 Proxy)")
    print(f"签名调用方:   {miner_addr}")
    print(f"调用函数:     claim(uint256 e = {epoch_to_claim})")
    print(f"预期铸造量:   {r_details['expectedReward'] / 1e18:.6f} TTK")
    gas_limit = max(gas_est + 40000, int(args.gas_limit))
    print(f"Gas Limit:    {gas_limit:,}")
    print(f"Gas 价格:     {target_gas_price_wei / 1e9:.3f} gwei ({target_gas_price_wei} wei)")
    print(f"预计 Gas 费:  {(gas_limit * target_gas_price_wei) / 1e18:.6f} BNB")
    print("=" * 78)

    # Secondary confirmation prompt
    confirm = input("\n请确认是否向 BSC Testnet 广播领取交易？(输入 'yes' 确认发送): ")
    if confirm.strip().lower() != "yes":
        print("[已取消] 用户取消操作，未发送任何交易。")
        return

    # 7. Broadcast Transaction
    from eth_account import Account
    nonce_hex = rpc_call(rpc_url, "eth_getTransactionCount", [miner_addr, "pending"])
    account_nonce = int(nonce_hex, 16)

    tx_dict = {
        "to": miner_contract,
        "from": miner_addr,
        "value": 0,
        "gas": gas_limit,
        "gasPrice": target_gas_price_wei,
        "nonce": account_nonce,
        "chainId": chain_id,
        "data": calldata_hex,
    }

    signed_tx = Account.sign_transaction(tx_dict, private_key)
    raw_tx_hex = "0x" + signed_tx.raw_transaction.hex()

    print(f"\n[步骤 3/3] 正在向 BSC Testnet 广播交易 (Nonce: {account_nonce})...", flush=True)
    tx_hash = rpc_call(rpc_url, "eth_sendRawTransaction", [raw_tx_hex])
    print(f"\n[广播成功] 交易哈希: {tx_hash}")
    print(f"BscScan 浏览器查询: https://testnet.bscscan.com/tx/{tx_hash}")

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
            print(f"成功领取 Epoch {epoch_to_claim} 奖励 {r_details['expectedReward'] / 1e18:.6f} TTK！")
        else:
            print(f"[交易回滚] 交易在区块 #{block_num} 回滚，执行失败！")
            print(f"消耗 Gas: {gas_used:,}")
    else:
        print("[提示] 交易已提交，但超时未获取到回执。请在 BscScan 上查看最新状态:")
        print(f"  https://testnet.bscscan.com/tx/{tx_hash}")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin 挖矿奖励领取工具 (BSC Testnet)")
    parser.add_argument("--epoch", type=int, default=None, help="目标领取轮次 (epoch id)")
    parser.add_argument("--miner", type=str, default=None, help="矿工钱包地址 (只读预检时必填或从私钥推导)")
    parser.add_argument("--send", action="store_true", help="发送真实链上交易 (默认仅进行 eth_call 预检)")
    parser.add_argument("--rpc", type=str, default=None, help="自定义 BSC Testnet RPC 节点")
    parser.add_argument("--miner-contract", type=str, default=DEFAULT_MINER_CONTRACT, help="MinerV1 代理合约地址")
    parser.add_argument("--gas-multiplier", type=float, default=1.15, help="Gas price 浮动乘数 (默认: 1.15)")
    parser.add_argument("--gas-price-gwei", type=float, default=None, help="指定的 Gas 价格 (gwei, 不得低于网络价)")
    parser.add_argument("--gas-limit", type=int, default=250000, help="Gas Limit 上限 (默认: 250000)")

    args = parser.parse_args()
    claim(args)


if __name__ == "__main__":
    main()
