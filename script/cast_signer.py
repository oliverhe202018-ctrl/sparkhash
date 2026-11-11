#!/usr/bin/env python3
"""Standalone Cast Signer module for PoolCoin scripts.

Provides isolated environment and command construction for Foundry cast keystore signing:
- Resolves keystore account address via cast wallet address
- Broadcasts transactions via cast send with --async, --account, --password-file, and --nonce
- Strips ETH_*, CAST_*, FOUNDRY_*, and *_PRIVATE_KEY environment variables for process isolation
- Verifies and sanitizes all output and error messages via safe_output
"""

import os
import re
import shutil
import subprocess
import sys

from eth_hash.auto import keccak

try:
    from script.safe_output import format_exception_safe, sanitize_text, sanitize_url
except ImportError:
    from safe_output import format_exception_safe, sanitize_text, sanitize_url

UNCERTAIN_TX_WARNING = "\u7ed3\u679c\u4e0d\u786e\u5b9a\uff0c\u8bf7\u5148\u5230\u533a\u5757\u6d4f\u89c8\u5668\u6838\u5bf9\u8be5\u5730\u5740\u7684 nonce \u548c\u4ea4\u6613\uff0c\u4e0d\u8981\u91cd\u590d\u53d1\u9001"

WALLET_ENV_VARS_TO_ISOLATE = (
    # Raw & Keystore Foundry/Cast variables
    "ETH_FROM",
    "ETH_KEYSTORE",
    "ETH_KEYSTORE_ACCOUNT",
    "ETH_PASSWORD",
    "CAST_ASYNC",
    "CAST_ACCOUNT",
    "CAST_PASSWORD",
    "CAST_PASSWORD_FILE",
    "CAST_PRIVATE_KEY",
    "CAST_MNEMONIC",
    "FOUNDRY_KEYSTORE",
    "FOUNDRY_ACCOUNT",
    "FOUNDRY_PASSWORD",
    "FOUNDRY_PASSWORD_FILE",
    "FOUNDRY_PRIVATE_KEY",
    "FOUNDRY_MNEMONIC",
    "FOUNDRY_ETH_FROM",
    # Private keys & mnemonics
    "PRIVATE_KEY",
    "MINER_PRIVATE_KEY",
    "KEEPER_PRIVATE_KEY",
    "DEPLOYER_PRIVATE_KEY",
    "SIGNER_PRIVATE_KEY",
    "ETH_PRIVATE_KEY",
    "MNEMONIC",
    "ETH_MNEMONIC",
    # Remote signers & hardware wallets
    "AWS_KMS_KEY_ID",
    "GCP_PROJECT_ID",
    "GCP_LOCATION",
    "GCP_KEY_RING",
    "GCP_KEY_NAME",
    "GCP_KEY_VERSION",
    "TURNKEY_API_PRIVATE_KEY",
    "TURNKEY_ORGANIZATION_ID",
    "TURNKEY_ADDRESS",
    "TEMPO_ACCESS_KEY",
    "TEMPO_ROOT_ACCOUNT",
    "TEMPO_SPONSOR_URL",
    "TREZOR_DERIVATION_PATH",
    "LEDGER_DERIVATION_PATH",
)


class CastSignerError(Exception):
    """Base exception for cast signer operations."""
    pass


class CastSendUncertainError(CastSignerError):
    """Raised when cast send times out or produces no valid txHash, leaving tx state uncertain."""
    pass


def to_checksum_address(address: str) -> str:
    """Convert an Ethereum address to checksum format.
    Strictly verifies that address is a string starting with 0x followed by exactly 40 hex digits.
    Raises CastSignerError on invalid input.
    """
    if not isinstance(address, str) or not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        safe_addr = sanitize_text(str(address)) if address is not None else "None"
        raise CastSignerError(f"Invalid Ethereum address: {safe_addr}")
    clean = address[2:].lower()
    h = keccak(clean.encode("utf-8")).hex()
    return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))


def parse_pending_nonce(nonce_val) -> int:
    """
    Parses and strictly validates pending nonce from eth_getTransactionCount:
    - Explicitly rejects bool, negative numbers, None, empty strings, and malformed values.
    - Accepts valid non-negative int (including 0).
    - Accepts valid 0x hex quantity strings (e.g. '0x0', '0x1a', '0X5').
    Raises ValueError on invalid values.
    """
    if nonce_val is None:
        raise ValueError("Nonce cannot be None.")
    if isinstance(nonce_val, bool):
        raise ValueError("Nonce cannot be a boolean value.")
    if isinstance(nonce_val, int):
        if nonce_val < 0:
            raise ValueError(f"Nonce cannot be negative: {nonce_val}")
        return nonce_val
    if isinstance(nonce_val, str):
        clean = nonce_val.strip()
        if not clean:
            raise ValueError("Nonce string cannot be empty.")
        if clean.startswith("-"):
            raise ValueError(f"Nonce cannot be negative: {nonce_val}")
        if clean.startswith(("0x", "0X")):
            hex_part = clean[2:]
            if not hex_part or not all(c in "0123456789abcdefABCDEF" for c in hex_part):
                raise ValueError(f"Malformed hex nonce: {nonce_val}")
            val = int(hex_part, 16)
            if val < 0:
                raise ValueError(f"Nonce cannot be negative: {nonce_val}")
            return val
        if clean.isdigit():
            val = int(clean, 10)
            if val < 0:
                raise ValueError(f"Nonce cannot be negative: {nonce_val}")
            return val
        raise ValueError(f"Malformed nonce string: {nonce_val}")
    raise ValueError(f"Unsupported nonce type: {type(nonce_val).__name__}")


def get_isolated_cast_env(base_env=None) -> dict:
    """
    Builds a clean environment mapping for cast subprocess execution:
    - Strips all wallet identity, keystore, private key, and signer selection variables.
    - Specifically removes all ETH_*, CAST_*, FOUNDRY_* signer/wallet variables and any *_PRIVATE_KEY.
    - Preserves system execution paths, proxies, and general runtime configuration.
    """
    source = dict(base_env if base_env is not None else os.environ)
    isolate_set = {v.upper() for v in WALLET_ENV_VARS_TO_ISOLATE}
    return {
        k: v
        for k, v in source.items()
        if (
            k.upper() not in isolate_set
            and not k.upper().startswith("ETH_")
            and not k.upper().startswith("CAST_")
            and not k.upper().startswith("FOUNDRY_")
            and not k.upper().endswith("PRIVATE_KEY")
            and not k.upper().endswith("_PRIV_KEY")
        )
    }


def validate_password_file(password_file: str) -> None:
    """
    Validates that password file exists and is non-empty without reading its contents.
    All error messages mask the path to prevent secret or path leakage.
    """
    if not password_file or not isinstance(password_file, str) or not password_file.strip():
        raise CastSignerError("Password file path must be specified and non-empty.")

    clean_path = password_file.strip()
    abs_path = os.path.abspath(clean_path)
    safe_path = sanitize_text(clean_path, sensitive_values=[clean_path, abs_path])
    if safe_path == clean_path:
        safe_path = "[REDACTED_PATH]"

    if not os.path.exists(clean_path):
        raise CastSignerError(f"Password file does not exist: {safe_path}")
    if os.path.isdir(clean_path):
        raise CastSignerError(f"Password file path is a directory: {safe_path}")

    try:
        size = os.path.getsize(clean_path)
    except Exception as e:
        safe_err = format_exception_safe(e, sensitive_values=[clean_path, abs_path])
        raise CastSignerError(f"Cannot inspect password file ({safe_err})") from None

    if size == 0:
        raise CastSignerError(f"Password file is empty: {safe_path}")


def get_keystore_address(account: str, password_file: str, timeout: float = 30.0) -> str:
    """
    Derives signer address using:
    cast wallet address --account <name> --password-file <path>
    Strictly parses stdout into a single legal Ethereum address.
    """
    if not account or not isinstance(account, str) or not account.strip():
        raise CastSignerError("Keystore account name must be specified and non-empty.")

    clean_account = account.strip()
    validate_password_file(password_file)
    clean_pw_file = password_file.strip()
    abs_pw_file = os.path.abspath(clean_pw_file)

    cast_bin = shutil.which("cast") or "cast"
    cmd = [cast_bin, "wallet", "address", "--account", clean_account, "--password-file", clean_pw_file]
    isolated_env = get_isolated_cast_env()

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=timeout,
            env=isolated_env,
        )
    except subprocess.TimeoutExpired:
        raise CastSignerError(f"cast wallet address timed out after {timeout}s") from None
    except Exception as e:
        safe_msg = format_exception_safe(e, sensitive_values=[clean_pw_file, abs_pw_file])
        raise CastSignerError(f"Failed to execute cast wallet address: {safe_msg}") from None

    if proc.returncode != 0:
        err_msg = sanitize_text(
            (proc.stderr or proc.stdout or "unknown error").strip(),
            sensitive_values=[clean_pw_file, abs_pw_file],
        )
        raise CastSignerError(f"cast wallet address failed (code {proc.returncode}): {err_msg}")

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(lines) != 1:
        raise CastSignerError("cast wallet address returned empty or multiple lines.")

    candidate = lines[0]
    if not re.match(r"^0x[0-9a-fA-F]{40}$", candidate):
        raise CastSignerError("cast wallet address output is not a valid Ethereum address format.")

    return to_checksum_address(candidate)


def cast_send(
    contract_address: str,
    calldata: str,
    account: str,
    password_file: str,
    rpc_url: str,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    timeout: float = 60.0,
) -> str:
    """
    Broadcasts a single transaction using:
    cast send <contract> --data <calldata> --account <account> --password-file <file>
              --rpc-url <rpc_url> --nonce <nonce> --gas-price <gas_price>
              --gas-limit <gas_limit> --async
    Shell is False, isolated env without ETH_* / PRIVATE_KEY.
    Single send, no auto-retries or price bumping.
    On timeout or missing txHash, raises CastSendUncertainError and displays warning:
    UNCOINCERTAIN_TX_WARNING
    """
    if not account or not isinstance(account, str) or not account.strip():
        raise CastSignerError("Keystore account name must be specified and non-empty.")
    clean_account = account.strip()

    validate_password_file(password_file)
    clean_pw_file = password_file.strip()
    abs_pw_file = os.path.abspath(clean_pw_file)

    clean_contract = to_checksum_address(contract_address)
    clean_calldata = calldata.strip()
    if not clean_calldata.startswith(("0x", "0X")):
        clean_calldata = "0x" + clean_calldata

    cast_bin = shutil.which("cast") or "cast"
    cmd = [
        cast_bin,
        "send",
        clean_contract,
        "--data",
        clean_calldata,
        "--account",
        clean_account,
        "--password-file",
        clean_pw_file,
        "--rpc-url",
        rpc_url,
        "--nonce",
        str(nonce),
        "--gas-price",
        str(gas_price),
        "--gas-limit",
        str(gas_limit),
        "--async",
    ]
    isolated_env = get_isolated_cast_env()

    sensitive_secrets = [clean_pw_file, abs_pw_file, rpc_url]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=timeout,
            env=isolated_env,
        )
    except subprocess.TimeoutExpired:
        safe_err = f"cast send subprocess timed out after {timeout}s"
        raise CastSendUncertainError(safe_err) from None
    except Exception as e:
        safe_err = format_exception_safe(e, sensitive_values=sensitive_secrets)
        raise CastSendUncertainError(safe_err) from None

    out = proc.stdout or ""
    err = proc.stderr or ""
    out_clean = out.strip()
    err_str = sanitize_text(f"{err}\n{out}".strip(), sensitive_values=sensitive_secrets)

    if proc.returncode != 0:
        raise CastSendUncertainError(
            f"code {proc.returncode}: {err_str}"
        )

    if not re.match(r"^0x[0-9a-fA-F]{64}$", out_clean):
        raise CastSendUncertainError(
            f"output is not an exact 64-hex transaction hash: {err_str}"
        )

    return out_clean
