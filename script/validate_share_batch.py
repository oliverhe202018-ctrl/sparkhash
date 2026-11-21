#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoolCoin Offline Share Batch Validator and Calldata Generator
Validates exactly 6 PoW share JSON files for MinerV1 on BSC (chain IDs 56 and 97),
re-computes each proof against MinerV1 contract rules, and produces batch parameters
for submitShares(uint8[] ks, uint256[] nonces).

STRICT SAFETY CONSTRAINTS:
1. Purely offline: No RPC access, no private keys, no signing, no broadcast.
2. JSON inputs treated strictly as non-executable passive data; floats/scientific notation rejected.
3. Exclusive creation; refuses overwrite (open with 'x' mode to prevent overwriting existing files).
4. Comprehensive validation of chainId, proxy, miner, epoch, seed, target, and k coverage 0..5.

RC2 REFERENCE NOTICE:
  Actual rc2 Miner proxy contract address:
    0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf
  Do NOT use old proxy addresses as rc2 operational examples.
  All example values (epoch, seed, target, miner address) are purely illustrative schematics,
  not actual on-chain query results.
"""

import os
import sys
import json
import argparse
from decimal import Decimal
from typing import List, Dict, Any, Tuple

try:
    from script.miner_network import NETWORK_NAMES
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from miner_network import NETWORK_NAMES
    from safe_output import format_exception_safe, sanitize_text, sanitize_url

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

REQUIRED_CHAIN_ID = 97
BATCH_SIZE = 6
CAP = 6  # Single address share cap per epoch in MinerV1
SUBMIT_SHARES_SELECTOR = bytes.fromhex("6efb5ee9")  # submitShares(uint8[],uint256[])
RC2_MINER_PROXY = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"

HEX_DIGITS_SET = set("0123456789abcdefABCDEF")


# Keccak-256 implementation
try:
    from Crypto.Hash import keccak as pycrypto_keccak
    def keccak256(data: bytes) -> bytes:
        h = pycrypto_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
except ImportError:
    try:
        from eth_hash.auto import keccak as eth_keccak
        def keccak256(data: bytes) -> bytes:
            return eth_keccak(data)
    except ImportError:
        raise RuntimeError("No Keccak-256 library available (need pycryptodome or eth-hash)")


# ABI encoding implementation
try:
    from eth_abi import encode as abi_encode
    HAS_ETH_ABI = True
except ImportError:
    HAS_ETH_ABI = False


def encode_submit_shares(ks: List[int], nonces: List[int]) -> bytes:
    """
    ABI encodes: submitShares(uint8[] calldata ks, uint256[] calldata nonces)
    Selector: 0x6efb5ee9
    """
    if HAS_ETH_ABI:
        encoded_args = abi_encode(["uint8[]", "uint256[]"], [ks, nonces])
        return SUBMIT_SHARES_SELECTOR + encoded_args

    # Deterministic manual ABI encoder fallback
    offset_ks = 64
    offset_nonces = 64 + 32 + 32 * len(ks)

    head = offset_ks.to_bytes(32, "big") + offset_nonces.to_bytes(32, "big")

    # ks array: length + elements padded to 32 bytes
    ks_body = len(ks).to_bytes(32, "big")
    for k in ks:
        ks_body += int(k).to_bytes(32, "big")

    # nonces array: length + uint256 elements
    nonces_body = len(nonces).to_bytes(32, "big")
    for n in nonces:
        nonces_body += int(n).to_bytes(32, "big")

    return SUBMIT_SHARES_SELECTOR + head + ks_body + nonces_body


def normalize_address(addr: Any, field_name: str = "address") -> str:
    if isinstance(addr, bool) or not isinstance(addr, str):
        raise ValueError(f"{field_name} must be a string, got {type(addr).__name__}")
    s = addr.strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    if len(s) != 40:
        raise ValueError(f"{field_name} must be 20 bytes (40 hex characters), got {len(s)}: {addr!r}")
    for c in s:
        if c not in HEX_DIGITS_SET:
            raise ValueError(f"{field_name} contains invalid hex characters or internal spaces: {addr!r}")
    res = "0x" + s.lower()
    if len(res) != 42:
        raise ValueError(f"{field_name} normalized address must be 42 characters, got {len(res)}")
    return res


def normalize_bytes32(val: Any, field_name: str = "seed") -> bytes:
    if isinstance(val, bool):
        raise ValueError(f"{field_name} cannot be boolean")
    if isinstance(val, int):
        if not (0 <= val < (1 << 256)):
            raise ValueError(f"{field_name} integer out of uint256 bounds: {val}")
        res = val.to_bytes(32, "big")
        if len(res) != 32:
            raise ValueError(f"{field_name} conversion error: expected 32 bytes, got {len(res)}")
        return res
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a hex string or int, got {type(val).__name__}")
    s = val.strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    if len(s) == 0:
        raise ValueError(f"{field_name} cannot be empty")
    if len(s) > 64:
        raise ValueError(f"{field_name} exceeds 32 bytes (64 hex characters), got {len(s)}: {val!r}")
    for c in s:
        if c not in HEX_DIGITS_SET:
            raise ValueError(f"{field_name} contains invalid hex characters or internal spaces: {val!r}")
    s = s.zfill(64)
    res = bytes.fromhex(s)
    if len(res) != 32:
        raise ValueError(f"{field_name} normalized result must be exactly 32 bytes, got {len(res)}")
    return res


def normalize_uint256(val: Any, field_name: str = "target") -> int:
    if isinstance(val, bool):
        raise ValueError(f"{field_name} cannot be boolean")
    if isinstance(val, Decimal):
        raise ValueError(f"{field_name} must be exact integer, got float/scientific Decimal: {val}")
    if isinstance(val, int):
        val_int = val
    elif isinstance(val, str):
        s = val.strip()
        try:
            if s.startswith(("0x", "0X")):
                hex_part = s[2:]
                if not hex_part or any(c not in HEX_DIGITS_SET for c in hex_part):
                    raise ValueError(f"{field_name} invalid hex characters: {val!r}")
                val_int = int(hex_part, 16)
            else:
                if any(c not in "0123456789" for c in s):
                    raise ValueError(f"{field_name} invalid decimal characters: {val!r}")
                val_int = int(s, 10)
        except ValueError:
            raise ValueError(f"{field_name} invalid numeric format: {val!r}")
    else:
        raise ValueError(f"{field_name} must be int or string, got {type(val).__name__}")

    if not (0 <= val_int < (1 << 256)):
        raise ValueError(f"{field_name} must be within [0, 2^256 - 1], got {val_int}")
    return val_int


def compute_sub_hash(epoch_seed_bytes: bytes, miner_address: str, k: int, miner_contract: str, chain_id: int) -> bytes:
    """
    Computes inner sub-hash matching MinerV1.sol line 330:
    bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(this), block.chainid));
    """
    clean_user = miner_address.lower().replace("0x", "")
    user_bytes = bytes.fromhex(clean_user).rjust(32, b"\x00")

    k_bytes = int(k).to_bytes(32, "big")

    clean_contract = miner_contract.lower().replace("0x", "")
    contract_bytes = bytes.fromhex(clean_contract).rjust(32, b"\x00")

    chain_id_bytes = int(chain_id).to_bytes(32, "big")

    encoded_sub = epoch_seed_bytes + user_bytes + k_bytes + contract_bytes + chain_id_bytes
    return keccak256(encoded_sub)


def verify_share_pow(sub_bytes: bytes, nonce: int, target_int: int) -> Tuple[bool, bytes, int]:
    """
    Verifies outer share hash locally matching MinerV1.sol line 331:
    uint256(keccak256(abi.encode(sub, nonce))) <= target_
    """
    nonce_bytes = int(nonce).to_bytes(32, "big")
    encoded_outer = sub_bytes + nonce_bytes
    h_bytes = keccak256(encoded_outer)
    h_int = int.from_bytes(h_bytes, "big")
    return (h_int <= target_int, h_bytes, h_int)


def validate_single_share_file(
    file_path: str,
    expected_chain_id: int,
    expected_proxy_norm: str,
    expected_miner_norm: str,
    expected_epoch: int,
    expected_seed_bytes: bytes,
    expected_target_int: int
) -> Dict[str, Any]:
    """
    Reads and strictly validates one share JSON file against expected parameters and PoW rules.
    """
    if not os.path.isfile(file_path):
        raise ValueError(f"Share file not found or not a regular file: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
    except Exception as e:
        raise ValueError(
            f"Failed to read share file {file_path}: "
            f"{format_exception_safe(e, sensitive_values=_collect_sensitive_values())}"
        )

    try:
        data = json.loads(raw_text, parse_float=Decimal)
    except Exception as e:
        raise ValueError(
            f"Failed to parse JSON in {file_path}: "
            f"{format_exception_safe(e, sensitive_values=_collect_sensitive_values())}"
        )

    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON in {file_path} must be an object")

    # 1. Required fields presence check
    required_fields = [
        "miner", "minerContract", "chainId", "epoch", "k",
        "epochSeed", "target", "nonceDecimal", "hash"
    ]
    for rf in required_fields:
        if rf not in data:
            raise ValueError(f"{file_path}: Missing required field {rf!r}")

    # 2. chainId check
    c_id = data["chainId"]
    if isinstance(c_id, Decimal):
        raise ValueError(f"{file_path}: chainId cannot be float or scientific notation")
    if isinstance(c_id, bool) or not isinstance(c_id, int):
        raise ValueError(f"{file_path}: chainId must be integer, got {type(c_id).__name__}")
    if c_id != expected_chain_id:
        raise ValueError(f"{file_path}: chainId {c_id} != {expected_chain_id}")
    if c_id not in NETWORK_NAMES:
        raise ValueError(f"{file_path}: unsupported BSC chain ID {c_id}")

    # 3. Address value comparisons
    miner_norm = normalize_address(data["miner"], f"{file_path}: miner")
    if miner_norm != expected_miner_norm:
        raise ValueError(f"{file_path}: miner address value ({miner_norm}) != expected ({expected_miner_norm})")

    contract_norm = normalize_address(data["minerContract"], f"{file_path}: minerContract")
    if contract_norm != expected_proxy_norm:
        raise ValueError(f"{file_path}: proxy address value ({contract_norm}) != expected ({expected_proxy_norm})")

    # 4. Epoch, seed, target match
    ep = data["epoch"]
    if isinstance(ep, Decimal):
        raise ValueError(f"{file_path}: epoch cannot be float or scientific notation")
    if isinstance(ep, bool) or not isinstance(ep, int):
        raise ValueError(f"{file_path}: epoch must be integer, got {type(ep).__name__}")
    if ep != expected_epoch:
        raise ValueError(f"{file_path}: epoch {ep} != expected epoch {expected_epoch}")

    file_seed_bytes = normalize_bytes32(data["epochSeed"], f"{file_path}: epochSeed")
    if file_seed_bytes != expected_seed_bytes:
        raise ValueError(f"{file_path}: epochSeed (0x{file_seed_bytes.hex()}) != expected (0x{expected_seed_bytes.hex()})")

    file_target_int = normalize_uint256(data["target"], f"{file_path}: target")
    if file_target_int != expected_target_int:
        raise ValueError(f"{file_path}: target ({hex(file_target_int)}) != expected ({hex(expected_target_int)})")

    # 5. k validation (range 0..5)
    k_val = data["k"]
    if isinstance(k_val, Decimal):
        raise ValueError(f"{file_path}: k cannot be float or scientific notation")
    if isinstance(k_val, bool) or not isinstance(k_val, int):
        raise ValueError(f"{file_path}: k must be integer, got {type(k_val).__name__}")
    if not (0 <= k_val < CAP):
        raise ValueError(f"{file_path}: k must be in range 0..{CAP-1}, got {k_val}")

    # 6. nonceDecimal precision & uint256 check
    nd = data["nonceDecimal"]
    if isinstance(nd, Decimal):
        raise ValueError(f"{file_path}: nonceDecimal cannot be float or scientific notation: {nd}")
    if isinstance(nd, bool) or not isinstance(nd, int):
        raise ValueError(f"{file_path}: nonceDecimal must be exact integer, got {type(nd).__name__}")
    if not (0 <= nd < (1 << 256)):
        raise ValueError(f"{file_path}: nonceDecimal out of uint256 bounds: {nd}")

    # If nonce field exists, strictly check type, range, and exact equality with nonceDecimal
    if "nonce" in data:
        n_val = data["nonce"]
        if isinstance(n_val, bool):
            raise ValueError(f"{file_path}: nonce cannot be boolean, got {n_val!r}")
        if n_val is None:
            raise ValueError(f"{file_path}: nonce cannot be null")
        if isinstance(n_val, (float, Decimal)):
            raise ValueError(f"{file_path}: nonce cannot be float or Decimal, got {n_val!r}")
        if isinstance(n_val, (list, dict)):
            raise ValueError(f"{file_path}: nonce cannot be list or dict, got {type(n_val).__name__}")
        if isinstance(n_val, int):
            parsed_n = n_val
        elif isinstance(n_val, str):
            s = n_val.strip()
            if not s:
                raise ValueError(f"{file_path}: nonce cannot be empty string")
            if s.startswith(("0x", "0X")):
                hex_digits = s[2:]
                if not hex_digits or any(c not in HEX_DIGITS_SET for c in hex_digits):
                    raise ValueError(f"{file_path}: Invalid hex characters or internal spaces in nonce string: {n_val!r}")
                try:
                    parsed_n = int(hex_digits, 16)
                except ValueError:
                    raise ValueError(f"{file_path}: Invalid nonce hex string representation: {n_val!r}")
            else:
                if any(c not in "0123456789" for c in s):
                    raise ValueError(f"{file_path}: Invalid decimal characters, floats or spaces in nonce string: {n_val!r}")
                try:
                    parsed_n = int(s, 10)
                except ValueError:
                    raise ValueError(f"{file_path}: Invalid nonce decimal string representation: {n_val!r}")
        else:
            raise ValueError(f"{file_path}: nonce must be non-bool int or exact integer string, got {type(n_val).__name__}")

        if not (0 <= parsed_n < (1 << 256)):
            raise ValueError(f"{file_path}: nonce out of uint256 bounds: {parsed_n}")
        if parsed_n != nd:
            raise ValueError(f"{file_path}: nonce ({n_val}) does not match nonceDecimal ({nd})")

    # 7. Recompute PoW hash
    sub_bytes = compute_sub_hash(
        epoch_seed_bytes=expected_seed_bytes,
        miner_address=expected_miner_norm,
        k=k_val,
        miner_contract=expected_proxy_norm,
        chain_id=expected_chain_id
    )
    is_valid, computed_h_bytes, computed_h_int = verify_share_pow(sub_bytes, nd, expected_target_int)
    computed_h_hex = "0x" + computed_h_bytes.hex()

    # Recomputed hash must match file declared hash
    file_h = data["hash"]
    if not isinstance(file_h, str):
        raise ValueError(f"{file_path}: hash must be string, got {type(file_h).__name__}")
    clean_file_h = file_h.strip().lower()
    if clean_file_h != computed_h_hex.lower():
        raise ValueError(
            f"{file_path}: Hash mismatch! File declared {file_h}, but recomputed {computed_h_hex}"
        )

    # Recomputed hash must satisfy contract target
    if not is_valid:
        raise ValueError(
            f"{file_path}: PoW difficulty check failed! "
            f"uint256(hash) ({hex(computed_h_int)}) > target ({hex(expected_target_int)})"
        )

    return {
        "file_path": file_path,
        "k": k_val,
        "nonceDecimal": nd,
        "nonceHex": hex(nd),
        "hash": computed_h_hex,
        "subHash": "0x" + sub_bytes.hex(),
        "hashInt": computed_h_int,
    }


def validate_batch(
    files: List[str],
    chain_id: int,
    proxy: str,
    miner: str,
    epoch: int,
    seed: str,
    target: Any,
    out_path: str
) -> Dict[str, Any]:
    """
    Validates the entire batch of 6 files and generates batch submission parameters.
    """
    if chain_id not in NETWORK_NAMES:
        raise ValueError(f"Invalid chainId {chain_id}. BSC Mainnet (56) or Testnet (97) required.")

    # Reject duplicate input paths
    if len(files) != BATCH_SIZE:
        raise ValueError(f"Batch requires exactly {BATCH_SIZE} share files, got {len(files)}")

    norm_paths = [os.path.normcase(os.path.realpath(p)) for p in files]
    if len(set(norm_paths)) != len(files):
        raise ValueError("Duplicate input file paths detected in batch arguments")

    # Check output path existence early (pre-flight check)
    if os.path.exists(out_path):
        raise FileExistsError(f"Output file already exists (refusing to overwrite): {out_path}")

    # Normalize expected parameters
    norm_proxy = normalize_address(proxy, "proxy")
    norm_miner = normalize_address(miner, "miner")
    norm_epoch = normalize_uint256(epoch, "epoch")
    norm_seed_bytes = normalize_bytes32(seed, "seed")
    norm_target_int = normalize_uint256(target, "target")

    # Validate each share file
    verified_shares = []
    seen_ks = set()

    for fp in files:
        share_info = validate_single_share_file(
            file_path=fp,
            expected_chain_id=chain_id,
            expected_proxy_norm=norm_proxy,
            expected_miner_norm=norm_miner,
            expected_epoch=norm_epoch,
            expected_seed_bytes=norm_seed_bytes,
            expected_target_int=norm_target_int
        )
        k_val = share_info["k"]
        if k_val in seen_ks:
            raise ValueError(f"Duplicate share index k={k_val} found across input files")
        seen_ks.add(k_val)
        verified_shares.append(share_info)

    # Validate that k uniquely and completely covers 0..5
    expected_k_set = set(range(CAP))
    if seen_ks != expected_k_set:
        missing = sorted(list(expected_k_set - seen_ks))
        raise ValueError(f"Incomplete batch! k indices must exactly cover 0..{CAP-1}. Missing: {missing}")

    # Sort shares by k in ascending order
    verified_shares.sort(key=lambda s: s["k"])

    ks_sorted = [s["k"] for s in verified_shares]
    nonces_sorted = [s["nonceDecimal"] for s in verified_shares]

    calldata_bytes = encode_submit_shares(ks_sorted, nonces_sorted)
    calldata_hex = "0x" + calldata_bytes.hex()

    result = {
        "chainId": chain_id,
        "proxy": norm_proxy,
        "miner": norm_miner,
        "epoch": norm_epoch,
        "seed": "0x" + norm_seed_bytes.hex(),
        "target": hex(norm_target_int),
        "ks": ks_sorted,
        "nonces": nonces_sorted,
        "calldata": calldata_hex,
        "shares": [
            {
                "k": s["k"],
                "nonceDecimal": s["nonceDecimal"],
                "nonceHex": s["nonceHex"],
                "hash": s["hash"],
                "subHash": s["subHash"],
                "file": s["file_path"]
            }
            for s in verified_shares
        ]
    }

    # Exclusive creation; refuses overwrite (open with 'x' mode)
    try:
        with open(out_path, "x", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
    except FileExistsError:
        raise FileExistsError(f"Output file already exists (refusing to overwrite): {out_path}")
    except Exception as e:
        raise OSError(
            f"Failed to write output file {out_path}: "
            f"{format_exception_safe(e, sensitive_values=_collect_sensitive_values())}"
        ) from None

    return result


def main():
    parser = argparse.ArgumentParser(
        description="PoolCoin Offline Share Batch Validator and Calldata Generator (BSC Mainnet / Testnet)",
        epilog="""
RC2 REFERENCE NOTICE:
  Actual rc2 Miner proxy contract address:
    0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf
  Do NOT use old proxy addresses as rc2 operational examples.

ILLUSTRATIVE PARAMETERS SCHEMATIC NOTICE:
  In example CLI invocations or documentation, all parameter values
  (such as epoch=42, seed=0x1234..., target=0x0000..., miner=0x1111...)
  are strictly illustrative schematics and must NOT pose as actual on-chain query results.
  Actual values must be explicitly queried from the live blockchain via read-only calls
  before validating batches.
"""
    )
    parser.add_argument(
        "--files",
        nargs=6,
        required=True,
        metavar="FILE",
        help="Exactly 6 share JSON file paths"
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        required=True,
        help="Expected chain ID (56 for BSC Mainnet or 97 for BSC Testnet)"
    )
    parser.add_argument(
        "--proxy",
        "--miner-contract",
        dest="proxy",
        required=True,
        help="Expected Miner proxy contract address (e.g. 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf)"
    )
    parser.add_argument(
        "--miner",
        required=True,
        help="Expected miner wallet address"
    )
    parser.add_argument(
        "--epoch",
        type=int,
        required=True,
        help="Expected epoch number"
    )
    parser.add_argument(
        "--seed",
        required=True,
        help="Expected epochSeed (hex string or bytes32)"
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Expected target difficulty (hex string or uint256 integer)"
    )
    parser.add_argument(
        "--out",
        "--output",
        dest="out",
        required=True,
        help="Output JSON file path (must not exist)"
    )

    args = parser.parse_args()

    try:
        res = validate_batch(
            files=args.files,
            chain_id=args.chain_id,
            proxy=args.proxy,
            miner=args.miner,
            epoch=args.epoch,
            seed=args.seed,
            target=args.target,
            out_path=args.out
        )
    except (ValueError, FileExistsError) as e:
        # Validation ValueError messages carry diagnostic unlabelled 64-hex values
        # (epochSeed, hash, target) that must remain intact without redaction.
        print(f"[VALIDATION FAILED] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(
            f"[VALIDATION FAILED] {format_exception_safe(e, sensitive_values=_collect_sensitive_values(args))}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=================================================================")
    print("             POOLCOIN SHARE BATCH OFFLINE VALIDATION             ")
    print("=================================================================")
    print(f"Validation Result:    SUCCESS (6/6 shares verified)")
    print(f"Chain ID:             {res['chainId']} ({NETWORK_NAMES[res['chainId']]})")
    print(f"Miner Proxy:          {res['proxy']}")
    print(f"Miner Address:        {res['miner']}")
    print(f"Epoch:                {res['epoch']}")
    print(f"Epoch Seed:           {res['seed']}")
    print(f"Target Difficulty:    {res['target']}")
    print("-----------------------------------------------------------------")
    print(f"Share Indices (ks):   {res['ks']}")
    print(f"Nonces (uint256):     {res['nonces']}")
    print(f"Calldata:             {res['calldata']}")
    print(f"Saved Output JSON:    {args.out}")
    print("-----------------------------------------------------------------")
    print("Function Arguments for submitShares(uint8[],uint256[]):")
    ks_cast = '[' + ','.join(str(k) for k in res['ks']) + ']'
    nonces_cast = '[' + ','.join(str(n) for n in res['nonces']) + ']'
    print(f'  ks (uint8[]):       "{ks_cast}"')
    print(f'  nonces (uint256[]): "{nonces_cast}"')
    print("=================================================================")
    print("【重要安全警示 (MANDATORY SECURITY NOTICE)】")
    print("本离线校验仅证明份额对显式给定的静态参数在数学上完全有效。")
    print("在向区块链广播交易前，必须另行在链上核对以下状态：")
    print("  1. 当前链上 epoch 是否依然与此批次一致（防止 epoch 切换导致失效）；")
    print("  2. 当前链上 epochSeed[e] 与 target[e] 是否与显式预期一致；")
    print("  3. 在当前目标 epoch，该矿工 counted 必须严格为 0，相关 k (0..5) 的 usedMask 位必须均未占用（即 usedMask & 0x3F == 0）；")
    print("     【注意】：整批包含 6 份份额，若 counted > 0 提交必超出单地址限额 CAP=6 导致交易 Revert；绝非仅检查 counted < CAP 即足够！")
    print("  4. Miner 合约当前是否处于 paused 暂停状态；")
    print("  5. 矿工在 StakeVault 是否拥有有效质押且预热期 (warmup) 已满足；")
    print("  6. 广播前必须先执行整批 eth_call (模拟调用) 验证不会发生 Revert。")
    print("严禁在未做链上核对的情况下宣称‘离线通过即可发送’！")
    print("=================================================================")


if __name__ == "__main__":
    main()
