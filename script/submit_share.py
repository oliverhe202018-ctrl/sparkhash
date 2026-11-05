#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Safe Share Submission Script for MinerV1 on BSC Testnet (chainId 97)

Submits mined PoW shares to MinerV1.
DEFAULT MODE: Read-only simulation via eth_call / cast call.
SEND MODE (--send): Prompts for manual user confirmation, enforces gasPrice >= eth_gasPrice,
and signs transaction using MINER_PRIVATE_KEY from environment.

HARD CONSTRAINTS ENFORCED:
1. Hardcoded refusal for any chainId != 97 (BSC Testnet only).
2. Never reads .env or prints private keys. Private key is strictly from MINER_PRIVATE_KEY env var.
3. Requires explicit manual confirmation ("yes") before broadcasting.
"""

import os
import sys
import time
import json
import argparse
import urllib.request
from eth_abi import encode
from eth_hash.auto import keccak

REQUIRED_CHAIN_ID = 97
DEFAULT_MINER_CONTRACT = "0x420382a0f874c1F120621f847d81581e8737504e"
DEFAULT_RPC_URL = "https://data-seed-prebsc-1-s1.binance.org:8545"

# Solidity custom errors in MinerV1 for clear error reporting
ERROR_SELECTORS = {
    keccak(b"OnlyAdmin()").hex()[:8]: "OnlyAdmin()",
    keccak(b"OnlyAdminOrGuardian()").hex()[:8]: "OnlyAdminOrGuardian()",
    keccak(b"ZeroAddress()").hex()[:8]: "ZeroAddress()",
    keccak(b"SubmissionsPausedError()").hex()[:8]: "SubmissionsPausedError()",
    keccak(b"BadBatchLength()").hex()[:8]: "BadBatchLength()",
    keccak(b"SyncingRequired()").hex()[:8]: "SyncingRequired()",
    keccak(b"SeedNotInitialized()").hex()[:8]: "SeedNotInitialized()",
    keccak(b"FutureEpoch()").hex()[:8]: "FutureEpoch()",
    keccak(b"NoActiveStake()").hex()[:8]: "NoActiveStake()",
    keccak(b"AddressCapExceeded()").hex()[:8]: "AddressCapExceeded()",
    keccak(b"InvalidShareIndex()").hex()[:8]: "InvalidShareIndex()",
    keccak(b"DuplicateShareIndex()").hex()[:8]: "DuplicateShareIndex()",
    keccak(b"InvalidProof()").hex()[:8]: "InvalidProof()",
    keccak(b"EpochNotEnded()").hex()[:8]: "EpochNotEnded()",
    keccak(b"AlreadyClaimed()").hex()[:8]: "AlreadyClaimed()",
}


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
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Submitter/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            err = res.get("error")
            err_data = err.get("data", "") if isinstance(err, dict) else ""
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"RPC Error ({method}): {err_msg} | data: {err_data}")
        return res.get("result")


def verify_chain_id(rpc_url):
    res = rpc_call(rpc_url, "eth_chainId", [])
    chain_id = int(res, 16) if isinstance(res, str) else int(res)
    if chain_id != REQUIRED_CHAIN_ID:
        raise ValueError(
            f"SECURITY REJECTION: Chain ID {chain_id} != {REQUIRED_CHAIN_ID} (BSC Testnet required)! Refusing to run."
        )
    return chain_id


def to_checksum_address(address):
    clean = address.lower().replace("0x", "")
    h = keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


def encode_submit_shares(ks, nonces):
    """
    Encodes submitShares(uint8[] calldata ks, uint256[] calldata nonces)
    Selector: 0x6efb5ee9
    """
    selector = bytes.fromhex("6efb5ee9")
    encoded_args = encode(["uint8[]", "uint256[]"], [ks, nonces])
    return selector + encoded_args


def decode_revert_reason(err_str):
    """Decodes custom error selector if present."""
    for sel, name in ERROR_SELECTORS.items():
        if sel in err_str:
            return f"{name} (selector 0x{sel})"
    return err_str


def simulate_call(rpc_url, to_addr, from_addr, calldata_bytes):
    """Runs eth_call simulation and returns (success, gas_estimate, error_msg)."""
    tx = {
        "to": to_addr,
        "from": from_addr,
        "data": "0x" + calldata_bytes.hex(),
    }
    try:
        res = rpc_call(rpc_url, "eth_call", [tx, "latest"])
        try:
            gas_est_hex = rpc_call(rpc_url, "eth_estimateGas", [tx])
            gas_est = int(gas_est_hex, 16)
        except Exception:
            gas_est = 250000
        return True, gas_est, None
    except Exception as e:
        err_msg = decode_revert_reason(str(e))
        return False, None, err_msg


def submit(args):
    # 1. Enforce BSC Testnet
    chain_id = verify_chain_id(args.rpc)

    # 2. Parse Share Data
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
        miner_addr = to_checksum_address(data["miner"])
        miner_contract = to_checksum_address(data.get("minerContract", DEFAULT_MINER_CONTRACT))
        k = int(data["k"])
        nonce = int(data["nonceDecimal"] if "nonceDecimal" in data else int(data["nonce"], 16))
        epoch = int(data.get("epoch", 0))
        share_hash = data.get("hash", "")
    else:
        if not args.miner or args.k is None or args.nonce is None:
            raise ValueError("Must provide either --file <share.json> OR (--miner, --k, --nonce)")
        miner_addr = to_checksum_address(args.miner)
        miner_contract = to_checksum_address(args.miner_contract)
        k = int(args.k)
        nonce = int(args.nonce if not str(args.nonce).startswith("0x") else int(args.nonce, 16))
        epoch = args.epoch
        share_hash = ""

    ks = [k]
    nonces = [nonce]
    calldata = encode_submit_shares(ks, nonces)

    print("=== PoolCoin Share Submitter ===")
    print(f"Network:          BSC Testnet (Chain ID {chain_id})")
    print(f"Miner Contract:   {miner_contract}")
    print(f"Miner Address:    {miner_addr}")
    if epoch is not None:
        print(f"Epoch:            {epoch}")
    print(f"Share Index k:    {k}")
    print(f"Nonce:            {nonce} (0x{nonce:x})")
    if share_hash:
        print(f"Share Hash:       {share_hash}")
    print(f"Calldata Size:    {len(calldata)} bytes")
    print("================================")

    # 3. Network Gas Price Check
    net_gas_price_hex = rpc_call(args.rpc, "eth_gasPrice", [])
    network_gas_price_wei = int(net_gas_price_hex, 16)
    network_gas_price_gwei = network_gas_price_wei / 1e9
    print(f"Network Gas Price: {network_gas_price_gwei:.3f} gwei ({network_gas_price_wei} wei)")

    # Enforce effective gas price >= network_gas_price
    multiplier = max(1.0, float(args.gas_multiplier))
    target_gas_price_wei = int(network_gas_price_wei * multiplier)

    if args.gas_price_gwei is not None:
        override_wei = int(float(args.gas_price_gwei) * 1e9)
        if override_wei < network_gas_price_wei:
            print(
                f"[WARNING] Requested gas price ({args.gas_price_gwei} gwei) is lower than network gasPrice ({network_gas_price_gwei:.3f} gwei). "
                f"Forcing gas price to network gasPrice floor."
            )
            target_gas_price_wei = network_gas_price_wei
        else:
            target_gas_price_wei = override_wei

    print(f"Selected Gas Price: {target_gas_price_wei / 1e9:.3f} gwei ({target_gas_price_wei} wei)")
    assert target_gas_price_wei >= network_gas_price_wei, "Enforcement check: tx gas price MUST be >= network gas price!"

    # 4. Mandatory Preflight eth_call Simulation
    print("\n[Step 1/2] Running eth_call preflight simulation...")
    success, gas_est, err = simulate_call(args.rpc, miner_contract, miner_addr, calldata)

    if not success:
        print(f"\n[REJECTED] Simulation failed! Contract will revert:")
        print(f"  Revert Reason: {err}")
        print("\nPossible causes:")
        print("  - InvalidProof: Nonce does not meet target difficulty, or wrong miner/epoch/k.")
        print("  - DuplicateShareIndex: Share index k has already been submitted in this epoch.")
        print("  - NoActiveStake: Miner address has not deposited in StakeVault or warmup not elapsed.")
        print("  - SyncingRequired / SeedNotInitialized: Contract epoch state needs Keeper sync.")
        sys.exit(1)

    print(f"[SUCCESS] Preflight simulation passed! Estimated gas: {gas_est:,}")

    # 5. Check --send Flag
    if not args.send:
        print("\n" + "=" * 50)
        print("SIMULATION COMPLETED (DRY-RUN)")
        print("No transaction was sent to the blockchain.")
        print("To broadcast this share on-chain, please execute:")
        print("  1. In your terminal, provide your private key via environment variable:")
        print("     PowerShell:  $env:MINER_PRIVATE_KEY=\"<your_private_key>\"")
        print("     Bash/Linux:  export MINER_PRIVATE_KEY=\"<your_private_key>\"")
        print("  2. Re-run this script with --send:")
        if args.file:
            print(f"     python script/submit_share.py --file {args.file} --send")
        else:
            print(f"     python script/submit_share.py --miner {miner_addr} --k {k} --nonce {nonce} --send")
        print("=" * 50)
        return

    # 6. Real Send Mode
    print("\n" + "=" * 50)
    print("WARNING: YOU ARE ABOUT TO BROADCAST A REAL TRANSACTION TO BSC TESTNET!")
    print("=" * 50)

    # Strictly read private key ONLY from environment variable (never from .env or file)
    private_key = os.environ.get("MINER_PRIVATE_KEY")
    if not private_key:
        print("[ERROR] MINER_PRIVATE_KEY environment variable is not set!")
        print("Please set it in your current terminal session:")
        print("  PowerShell:  $env:MINER_PRIVATE_KEY=\"<0x_hex_private_key>\"")
        print("  Bash/Linux:  export MINER_PRIVATE_KEY=\"<0x_hex_private_key>\"")
        print("Note: Do NOT write your private key into .env or any file.")
        sys.exit(1)

    # Verify that private key matches miner address
    try:
        from eth_account import Account
        account = Account.from_key(private_key)
        signer_addr = to_checksum_address(account.address)
        if signer_addr.lower() != miner_addr.lower():
            print(f"[SECURITY ERROR] MINER_PRIVATE_KEY corresponds to {signer_addr},")
            print(f"                 which does NOT match miner address {miner_addr}!")
            print("Submission rejected to prevent transaction failure.")
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to load private key from environment: {e}")
        sys.exit(1)

    # Print Full Transaction Summary for Manual Confirmation
    gas_limit = max(gas_est + 50000, int(args.gas_limit))
    print("\n================ TRANSACTION PREVIEW ================")
    print(f"Target Contract:  {miner_contract} (MinerV1 Proxy)")
    print(f"Sender / Signer:  {signer_addr}")
    print(f"Function:         submitShares(uint8[] ks, uint256[] nonces)")
    print(f"Share Index k:    [{k}]")
    print(f"Nonce:            [{nonce}]")
    print(f"Gas Limit:        {gas_limit:,}")
    print(f"Gas Price:        {target_gas_price_wei / 1e9:.3f} gwei ({target_gas_price_wei} wei)")
    print(f"Chain ID:         {chain_id} (BSC Testnet: VERIFIED)")
    print(f"Preflight Sim:    PASSED")
    print("=====================================================")

    # Prompt manual confirmation
    confirm = input("\nAre you sure you want to broadcast this transaction to BSC Testnet? (type 'yes' to broadcast): ")
    if confirm.strip().lower() != "yes":
        print("[ABORTED] Broadcast cancelled by user. No transaction was sent.")
        return

    # Fetch Nonce
    nonce_hex = rpc_call(args.rpc, "eth_getTransactionCount", [signer_addr, "pending"])
    account_nonce = int(nonce_hex, 16)

    # Build Transaction Object
    tx_dict = {
        "to": miner_contract,
        "from": signer_addr,
        "value": 0,
        "gas": gas_limit,
        "gasPrice": target_gas_price_wei,
        "nonce": account_nonce,
        "chainId": chain_id,
        "data": "0x" + calldata.hex(),
    }

    # Sign Transaction
    signed_tx = Account.sign_transaction(tx_dict, private_key)
    raw_tx_hex = "0x" + signed_tx.raw_transaction.hex()

    print("\nBroadcasting transaction to BSC Testnet...", flush=True)
    tx_hash = rpc_call(args.rpc, "eth_sendRawTransaction", [raw_tx_hex])
    print(f"\n[BROADCAST SUCCESS] Transaction Hash: {tx_hash}")
    print(f"View on BscScan Testnet: https://testnet.bscscan.com/tx/{tx_hash}")

    # Wait for receipt
    print("Waiting for transaction confirmation on-chain...", flush=True)
    timeout = 60
    poll_start = time.time()
    receipt = None
    while time.time() - poll_start < timeout:
        try:
            receipt = rpc_call(args.rpc, "eth_getTransactionReceipt", [tx_hash])
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
            print(f"[CONFIRMED] Transaction included in Block #{block_num}!")
            print(f"Gas Used: {gas_used:,}")
            print("Share successfully submitted and recorded in MinerV1!")
        else:
            print(f"[FAILED] Transaction reverted on-chain in Block #{block_num}!")
            print(f"Gas Used: {gas_used:,}")
    else:
        print("[INFO] Transaction submitted. Please check status on BscScan:")
        print(f"  https://testnet.bscscan.com/tx/{tx_hash}")


def main():
    parser = argparse.ArgumentParser(description="PoolCoin Safe Share Submitter (BSC Testnet)")
    parser.add_argument("--file", type=str, default=None, help="Path to JSON file generated by miner.py")
    parser.add_argument("--miner", type=str, default=None, help="Miner wallet address")
    parser.add_argument("--k", type=int, default=None, help="Share index (0..14)")
    parser.add_argument("--nonce", type=str, default=None, help="Mined nonce (decimal or 0x hex)")
    parser.add_argument("--epoch", type=int, default=None, help="Target epoch")
    parser.add_argument("--rpc", type=str, default=DEFAULT_RPC_URL, help="BSC Testnet RPC URL")
    parser.add_argument("--miner-contract", type=str, default=DEFAULT_MINER_CONTRACT, help="Miner proxy address")
    parser.add_argument("--send", action="store_true", help="Broadcast transaction (default: simulation only)")
    parser.add_argument("--gas-multiplier", type=float, default=1.15, help="Gas price multiplier over eth_gasPrice (default: 1.15)")
    parser.add_argument("--gas-price-gwei", type=float, default=None, help="Explicit gas price in gwei (must be >= eth_gasPrice)")
    parser.add_argument("--gas-limit", type=int, default=350000, help="Gas limit ceiling (default: 350000)")

    args = parser.parse_args()
    submit(args)


if __name__ == "__main__":
    main()
