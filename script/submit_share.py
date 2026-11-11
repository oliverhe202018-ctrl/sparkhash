#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Safe Share Submission Script for MinerV1 on BSC Testnet (chainId 97)

Submits mined PoW shares to MinerV1.
DEFAULT MODE: Read-only simulation via eth_call / cast call.
SEND MODE (--send): Prompts for manual user confirmation, enforces EIP-1559
maxFeePerGas >= max(eth_gasPrice, maxPriorityFeePerGas), and signs via Foundry keystore
(--account / --password-file) using cast send (or legacy MINER_PRIVATE_KEY env fallback).

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
import urllib.error
import urllib.request
from eth_abi import encode

try:
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from safe_output import format_exception_safe, sanitize_text, sanitize_url
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

REQUIRED_CHAIN_ID = 97
DEFAULT_MINER_CONTRACT = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
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


def simulate_call(rpc_url, to_addr, from_addr, calldata_bytes, sensitive_values=None):
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
        sensitive = _collect_sensitive_values(extra_secrets=sensitive_values)
        safe_exc_msg = format_exception_safe(e, sensitive_values=sensitive)
        err_msg = decode_revert_reason(safe_exc_msg)
        err_msg = sanitize_text(err_msg, sensitive_values=sensitive)
        return False, None, err_msg


def submit(args):
    cli_fee_cfg = validate_cli_fee_and_gas_args(
        gas_limit=args.gas_limit,
        gas_multiplier=args.gas_multiplier,
        max_fee_per_gas_gwei=getattr(args, "max_fee_per_gas_gwei", None),
        priority_fee_per_gas_gwei=getattr(args, "priority_fee_per_gas_gwei", None),
        deprecated_gas_price_gwei=getattr(args, "gas_price_gwei", None),
    )

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

    if bool(args.account) != bool(args.password_file):
        print("[ERROR] --account and --password-file must be provided together!")
        sys.exit(1)

    has_keystore = bool(args.account and args.password_file)
    env_pk = os.environ.get("MINER_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    has_env_pk = bool(env_pk and env_pk.strip())

    if has_keystore and has_env_pk:
        print("[ERROR] Both keystore flags (--account/--password-file) and environment private key are provided. Please specify only one signing method.")
        sys.exit(1)

    if not args.send and has_keystore:
        try:
            derived_signer = get_keystore_address(args.account, args.password_file)
            if derived_signer.lower() != miner_addr.lower():
                print(f"[SECURITY ERROR] Keystore signer address corresponds to {derived_signer},")
                print(f"                 which does NOT match miner address {miner_addr}!")
                print("Submission rejected to prevent transaction failure.")
                sys.exit(1)
        except CastSignerError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[ERROR] Failed to resolve keystore address: {safe_err}")
            sys.exit(1)

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

    # 3. Resolve EIP-1559 Fees (type 2: maxFeePerGas & maxPriorityFeePerGas)
    fee_info = resolve_eip1559_fees(
        rpc_url=args.rpc,
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
        print(f"Network Gas Price (eth_gasPrice): {network_gas_price_wei / 1e9:.3f} gwei ({network_gas_price_wei} wei)")
    else:
        print(f"Explicit Max Fee (maxFeePerGas): {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei) [eth_gasPrice skipped / N/A]")
    if network_priority_fee_wei is not None:
        print(f"Network Priority Fee (eth_maxPriorityFeePerGas): {network_priority_fee_wei / 1e9:.3f} gwei ({network_priority_fee_wei} wei)")
    else:
        print(f"Explicit Priority Fee (maxPriorityFeePerGas): {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei) [eth_maxPriorityFeePerGas skipped]")

    for warn_kind, orig_val, lifted_val in fee_info["warnings"]:
        if warn_kind == "priority_floor":
            print(
                f"[WARNING] Computed maxFeePerGas ({orig_val / 1e9:.3f} gwei) is lower than maxPriorityFeePerGas ({lifted_val / 1e9:.3f} gwei). "
                f"Raising maxFeePerGas to match priority fee."
            )

    print(f"Selected Max Fee (maxFeePerGas):           {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei)")
    print(f"Selected Priority Fee (maxPriorityFee):    {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei)")
    if network_gas_price_wei is not None and not fee_info["explicit_max_fee"] and max_fee_wei < network_gas_price_wei:
        raise ValueError("Enforcement check failed: computed maxFeePerGas cannot be less than network gas price.")
    if max_fee_wei < priority_fee_wei:
        raise ValueError("Enforcement check failed: maxFeePerGas cannot be less than maxPriorityFeePerGas.")

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
    gas_limit = max(gas_est + 50000, cli_fee_cfg["gas_limit"])
    max_gas_cost_bnb = (gas_limit * max_fee_wei) / 1e18
    print(f"Gas Limit Ceiling:        {gas_limit:,}")
    print(f"Max Gas Fee Ceiling:      {max_gas_cost_bnb:.6f} BNB")

    # 5. Check --send Flag
    if not args.send:
        print("\n" + "=" * 50)
        print("SIMULATION COMPLETED (DRY-RUN)")
        print("No transaction was sent to the blockchain.")
        print("To broadcast this share on-chain, please use Foundry keystore signing:")
        print("  1. Prepare your Foundry keystore account name and password file (--account and --password-file).")
        print("  2. Re-run this script with --send, --account, and --password-file:")
        if args.file:
            print(f"     python script/submit_share.py --file {args.file} --send --account <keystore_account> --password-file <password_file_path>")
        else:
            print(f"     python script/submit_share.py --miner {miner_addr} --k {k} --nonce {nonce} --send --account <keystore_account> --password-file <password_file_path>")
        print("=" * 50)
        return

    # 6. Real Send Mode
    print("\n" + "=" * 50)
    print("WARNING: YOU ARE ABOUT TO BROADCAST A REAL TRANSACTION TO BSC TESTNET!")
    print("=" * 50)

    private_key = None
    if has_keystore:
        try:
            signer_addr = get_keystore_address(args.account, args.password_file)
        except CastSignerError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[ERROR] Failed to resolve keystore address: {safe_err}")
            sys.exit(1)

        if signer_addr.lower() != miner_addr.lower():
            print(f"[SECURITY ERROR] Keystore signer address corresponds to {signer_addr},")
            print(f"                 which does NOT match miner address {miner_addr}!")
            print("Submission rejected to prevent transaction failure.")
            sys.exit(1)
    elif has_env_pk:
        print("[WARNING] Using environment private key is not recommended (不推荐). Please consider using --account and --password-file instead.")
        private_key = env_pk.strip()
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
            safe_err = format_exception_safe(
                e,
                sensitive_values=_collect_sensitive_values(args, extra_secrets=[private_key]),
            )
            print(f"[ERROR] Failed to load private key from environment: {safe_err}")
            sys.exit(1)
    else:
        print("[ERROR] --send mode requires Foundry keystore parameters (--account and --password-file).")
        print("Recommended: python script/submit_share.py ... --send --account <keystore_account> --password-file <password_file_path>")
        print("Legacy fallback: MINER_PRIVATE_KEY environment variable (not recommended; never write keys into .env or files).")
        sys.exit(1)

    # Print Full Transaction Summary for Manual Confirmation
    print("\n================ TRANSACTION PREVIEW ================")
    print(f"Target Contract:  {miner_contract} (MinerV1 Proxy)")
    print(f"Sender / Signer:  {signer_addr}")
    print(f"Function:         submitShares(uint8[] ks, uint256[] nonces)")
    print(f"Share Index k:    [{k}]")
    print(f"Nonce:            [{nonce}]")
    print(f"Gas Limit:        {gas_limit:,}")
    print(f"Max Fee:          {max_fee_wei / 1e9:.3f} gwei ({max_fee_wei} wei)")
    print(f"Priority Fee:     {priority_fee_wei / 1e9:.3f} gwei ({priority_fee_wei} wei)")
    print(f"Max Gas Cost Cap: {max_gas_cost_bnb:.6f} BNB")
    print(f"Chain ID:         {chain_id} (BSC Testnet: VERIFIED)")
    print(f"Preflight Sim:    PASSED")
    print("=====================================================")

    # Prompt manual confirmation
    confirm = input("\nAre you sure you want to broadcast this transaction to BSC Testnet? (type 'yes' to broadcast): ")
    if confirm.strip().lower() != "yes":
        print("[ABORTED] Broadcast cancelled by user. No transaction was sent.")
        return

    # Fetch Nonce
    try:
        nonce_res = rpc_call(args.rpc, "eth_getTransactionCount", [signer_addr, "pending"])
        account_nonce = parse_pending_nonce(nonce_res)
    except Exception as e:
        safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
        print(f"[ERROR] Failed to query pending nonce: {safe_err}")
        print("Nonce query failed, aborting transaction broadcast (will not fallback to 0).")
        sys.exit(1)

    if has_keystore:
        print("\nBroadcasting transaction to BSC Testnet via cast send...", flush=True)
        try:
            tx_hash = cast_send(
                contract_address=miner_contract,
                calldata="0x" + calldata.hex(),
                account=args.account,
                password_file=args.password_file,
                rpc_url=args.rpc,
                nonce=account_nonce,
                gas_price=max_fee_wei,
                gas_limit=gas_limit,
                priority_gas_price=priority_fee_wei,
            )
        except CastSendUncertainError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[ERROR] {UNCERTAIN_TX_WARNING} ({safe_err})")
            sys.exit(1)
        except CastSignerError as e:
            safe_err = format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))
            print(f"[ERROR] Keystore operation failed: {safe_err}")
            sys.exit(1)
    else:
        # Build Transaction Object
        tx_dict = {
            "type": 2,
            "to": miner_contract,
            "from": signer_addr,
            "value": 0,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
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
    parser.add_argument("--account", type=str, default=None, help="Foundry keystore account name (must be used with --password-file)")
    parser.add_argument("--password-file", type=str, default=None, help="Path to password file for Foundry keystore (must be used with --account)")
    parser.add_argument("--send", action="store_true", help="Broadcast transaction (default: simulation only)")
    parser.add_argument("--gas-multiplier", type=str, default="1.15", help="Max fee multiplier over eth_gasPrice (default: 1.15)")
    parser.add_argument("--max-fee-per-gas-gwei", type=str, default=None, help="Explicit EIP-1559 maxFeePerGas in gwei")
    parser.add_argument("--priority-fee-per-gas-gwei", type=str, default=None, help="Explicit EIP-1559 maxPriorityFeePerGas in gwei")
    parser.add_argument("--gas-price-gwei", type=str, default=None, help="[Compatibility alias] Equivalent to --max-fee-per-gas-gwei (gwei)")
    parser.add_argument("--gas-limit", type=int, default=350000, help="Gas limit ceiling (default: 350000)")

    args = parser.parse_args()
    try:
        submit(args)
    except Exception as e:
        print(
            f"[ERROR] {format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
