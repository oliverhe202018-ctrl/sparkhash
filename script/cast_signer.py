#!/usr/bin/env python3
"""Standalone Cast Signer module for PoolCoin scripts.

Provides isolated environment and command construction for Foundry cast keystore signing:
- Resolves keystore account address via cast wallet address
- Broadcasts EIP-1559 (type 2) transactions via cast send with --async, --account,
  --password-file, --nonce, --gas-price (maxFeePerGas), and --priority-gas-price
  (maxPriorityFeePerGas)
- Strips ETH_*, CAST_*, FOUNDRY_*, and *_PRIVATE_KEY environment variables for process isolation
- Verifies and sanitizes all output and error messages via safe_output
"""

import math
import os
import re
import shutil
import subprocess
from decimal import Decimal, InvalidOperation

from eth_hash.auto import keccak

try:
    from script.safe_output import format_exception_safe, sanitize_text
except ImportError:
    from safe_output import format_exception_safe, sanitize_text

UNCERTAIN_TX_WARNING = "\u7ed3\u679c\u4e0d\u786e\u5b9a\uff0c\u8bf7\u5148\u5230\u533a\u5757\u6d4f\u89c8\u5668\u6838\u5bf9\u8be5\u5730\u5740\u7684 nonce \u548c\u4ea4\u6613\uff0c\u4e0d\u8981\u91cd\u590d\u53d1\u9001"

MAX_UINT64 = (1 << 64) - 1
MAX_UINT256 = (1 << 256) - 1
GWEI_TO_WEI = Decimal("1000000000")
GWEI_TO_WEI_INT = 1_000_000_000
MAX_GWEI_INT = MAX_UINT256 // GWEI_TO_WEI_INT
MAX_DECIMAL_INPUT_LEN = 128
MAX_GWEI_EXPONENT = 200
MAX_MULTIPLIER_SIG_DIGITS = 80
MIN_MULTIPLIER_EXP = -80
MAX_MULTIPLIER_EXP = 18
MAX_GAS_MULTIPLIER_INT = 10**18
_ASCII_DECIMAL_RE = re.compile(r"^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$", re.ASCII)

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
    - Explicitly rejects bool, float, unprefixed decimal strings, negative numbers,
      None, empty strings, out-of-range values (>= 2**64 - 1), and malformed values.
    - Accepts valid non-bool int with 0 <= nonce < 2**64 - 1.
    - Accepts valid 0x/0X-prefixed ASCII hex quantity strings (e.g. '0x0', '0x1a', '0X5')
      with 0 <= nonce < 2**64 - 1.
    Raises ValueError on invalid values.
    """
    if nonce_val is None:
        raise ValueError("Nonce cannot be None.")
    if isinstance(nonce_val, bool):
        raise ValueError("Nonce cannot be a boolean value.")
    if isinstance(nonce_val, int):
        if nonce_val < 0 or nonce_val >= MAX_UINT64:
            raise ValueError("Nonce out of valid uint64 tx range [0, 2**64 - 1).")
        return nonce_val
    if isinstance(nonce_val, str):
        if len(nonce_val) > 64:
            raise ValueError("Nonce string exceeds maximum allowed length.")
        clean = nonce_val.strip()
        if not clean:
            raise ValueError("Nonce string cannot be empty.")
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", clean):
            raise ValueError(f"Nonce string must be a valid 0x-prefixed ASCII hex quantity: {nonce_val}")
        val = int(clean, 16)
        if val < 0 or val >= MAX_UINT64:
            raise ValueError("Nonce out of valid uint64 tx range [0, 2**64 - 1).")
        return val
    raise ValueError(f"Unsupported nonce type: {type(nonce_val).__name__}")


def validate_calldata(calldata: str) -> str:
    """Validates hex calldata: even byte length, ASCII hex only, allows empty '0x', rejects internal whitespace."""
    if not isinstance(calldata, str):
        raise CastSignerError(f"Calldata must be a hex string, got {type(calldata).__name__}.")
    stripped = calldata.strip()
    if not stripped:
        raise CastSignerError("Calldata cannot be empty; use '0x' for empty calldata.")
    if any(ch.isspace() for ch in stripped):
        raise CastSignerError("Calldata cannot contain internal whitespace.")
    hex_body = stripped[2:] if stripped.startswith(("0x", "0X")) else stripped
    if not re.fullmatch(r"[0-9a-fA-F]*", hex_body):
        raise CastSignerError("Calldata contains invalid non-hex characters.")
    if len(hex_body) % 2 != 0:
        raise CastSignerError("Calldata hex length must be even (whole bytes).")
    return "0x" + hex_body


def validate_nonce(nonce: int) -> int:
    """Validates non-bool transaction nonce in [0, 2**64 - 1)."""
    if isinstance(nonce, bool) or not isinstance(nonce, int):
        raise CastSignerError(f"Nonce must be a non-bool integer, got {type(nonce).__name__}.")
    if nonce < 0 or nonce >= MAX_UINT64:
        raise CastSignerError("Nonce out of valid range [0, 2**64 - 1).")
    return nonce


def validate_gas_limit(gas_limit: int) -> int:
    """Validates non-bool positive uint64 gas limit in [1, 2**64 - 1]."""
    if isinstance(gas_limit, bool) or not isinstance(gas_limit, int):
        raise CastSignerError(f"Gas limit must be a non-bool integer, got {type(gas_limit).__name__}.")
    if gas_limit <= 0 or gas_limit > MAX_UINT64:
        raise CastSignerError("Gas limit must be between 1 and 2**64 - 1.")
    return gas_limit


def validate_fee_wei(fee_wei: int, field_name: str = "gas_price") -> int:
    """Validates non-bool integer gas fee in [0, 2**256 - 1]."""
    if isinstance(fee_wei, bool) or not isinstance(fee_wei, int):
        raise CastSignerError(f"{field_name} must be a non-bool integer, got {type(fee_wei).__name__}.")
    if fee_wei < 0 or fee_wei > MAX_UINT256:
        raise CastSignerError(f"{field_name} out of valid uint256 range [0, 2**256 - 1].")
    return fee_wei


def validate_eip1559_fees(gas_price: int, priority_gas_price: int) -> tuple:
    """Validates both EIP-1559 fee integers (maxFeePerGas and maxPriorityFeePerGas) and maxFee >= priorityFee."""
    max_fee = validate_fee_wei(gas_price, "gas_price (maxFeePerGas)")
    prio_fee = validate_fee_wei(priority_gas_price, "priority_gas_price (maxPriorityFeePerGas)")
    if max_fee < prio_fee:
        raise CastSignerError(
            f"gas_price / maxFeePerGas ({max_fee} wei) cannot be less than "
            f"priority_gas_price / maxPriorityFeePerGas ({prio_fee} wei)."
        )
    return max_fee, prio_fee


def _parse_bounded_decimal_str(raw_str: str, field_name: str, max_abs_exp: int) -> Decimal:
    """Parses a bounded ASCII decimal string (with optional '+' and scientific notation) safely."""
    if len(raw_str) > MAX_DECIMAL_INPUT_LEN:
        raise ValueError(f"Invalid {field_name}: input exceeds maximum length ({MAX_DECIMAL_INPUT_LEN}).")
    s = raw_str.strip()
    if not s or not _ASCII_DECIMAL_RE.fullmatch(s):
        raise ValueError(f"Invalid {field_name} format.")
    if "e" in s or "E" in s:
        _, _, exp_raw = s.lower().partition("e")
        exp_neg = exp_raw.startswith("-")
        exp_digits = exp_raw[1:] if exp_raw[0] in "+-" else exp_raw
        exp_digits = exp_digits.lstrip("0") or "0"
        if len(exp_digits) > 4:
            raise ValueError(f"{field_name} exponent out of supported range.")
        exp_val = -int(exp_digits) if exp_neg else int(exp_digits)
        if abs(exp_val) > max_abs_exp:
            raise ValueError(f"{field_name} exponent out of supported range.")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Invalid {field_name} format.") from None


def _extract_normalized_decimal_parts(
    d: Decimal, field_name: str, max_abs_raw_exp: int
) -> tuple:
    """
    Validates a Decimal is finite and non-negative (rejecting -0 as well),
    checks digit/exponent bounds, and returns (is_zero, sig_digits, eff_exp)
    independent of the active Decimal context.
    """
    if not d.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    if d.is_signed():
        raise ValueError(f"{field_name} must be non-negative.")
    _, digits, exp = d.as_tuple()
    if not isinstance(exp, int) or len(digits) > MAX_DECIMAL_INPUT_LEN or abs(exp) > max_abs_raw_exp:
        raise ValueError(f"{field_name} magnitude or exponent out of supported range.")
    n_digits = len(digits)
    first_nz = 0
    while first_nz < n_digits and digits[first_nz] == 0:
        first_nz += 1
    if first_nz == n_digits:
        return True, (0,), 0
    last_nz = n_digits - 1
    while last_nz > first_nz and digits[last_nz] == 0:
        last_nz -= 1
    sig_digits = digits[first_nz : last_nz + 1]
    eff_exp = exp + (n_digits - 1 - last_nz)
    return False, sig_digits, eff_exp


def _digits_to_int(sig_digits: tuple) -> int:
    mantissa = 0
    for digit in sig_digits:
        mantissa = mantissa * 10 + digit
    return mantissa


def _decimal_to_exact_ratio(d: Decimal) -> tuple:
    """Converts a pre-bounded non-negative Decimal into an exact integer ratio (num, den)."""
    is_zero, sig_digits, eff_exp = _extract_normalized_decimal_parts(
        d, "gas_multiplier", max_abs_raw_exp=MAX_DECIMAL_INPUT_LEN
    )
    if is_zero:
        return 0, 1
    mantissa = _digits_to_int(sig_digits)
    if eff_exp >= 0:
        return mantissa * (10 ** eff_exp), 1
    return mantissa, 10 ** (-eff_exp)


def gwei_to_wei(raw_value, field_name: str = "gas fee (gwei)") -> int:
    """
    Converts a gwei quantity to exact integer wei without depending on Decimal context.
    - Supports non-negative int, float, Decimal, and ASCII decimal/scientific strings
      (optional leading '+', normal scientific notation like '1e-9').
    - Rejects bool, None, negative values (including -0 / -0.0), NaN, sNaN, Infinity,
      oversized inputs/exponents, sub-wei fractional precision (without rounding), and > MAX_UINT256.
    """
    if raw_value is None or isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be a valid non-negative decimal number, got {type(raw_value).__name__}.")

    if isinstance(raw_value, int):
        if raw_value < 0:
            raise ValueError(f"{field_name} must be finite and non-negative.")
        if raw_value > MAX_GWEI_INT:
            raise ValueError(f"{field_name} converted wei out of uint256 range.")
        return raw_value * GWEI_TO_WEI_INT
    elif isinstance(raw_value, Decimal):
        d = raw_value
    elif isinstance(raw_value, float):
        if not math.isfinite(raw_value) or raw_value < 0 or math.copysign(1.0, raw_value) < 0:
            raise ValueError(f"{field_name} must be finite and non-negative.")
        if raw_value > 1e69:
            raise ValueError(f"{field_name} converted wei out of uint256 range.")
        d = Decimal(str(raw_value))
    elif isinstance(raw_value, str):
        d = _parse_bounded_decimal_str(raw_value, field_name, max_abs_exp=MAX_GWEI_EXPONENT)
    else:
        raise ValueError(f"Unsupported type for {field_name}: {type(raw_value).__name__}")

    is_zero, sig_digits, eff_exp = _extract_normalized_decimal_parts(
        d, field_name, max_abs_raw_exp=MAX_GWEI_EXPONENT
    )
    if is_zero:
        return 0

    wei_exp = eff_exp + 9
    if wei_exp < 0:
        raise ValueError(f"{field_name} has sub-wei precision; smallest unit is 1 wei (1e-9 gwei).")
    if wei_exp > 78 or len(sig_digits) > 78 or (len(sig_digits) + wei_exp) > 78:
        raise ValueError(f"{field_name} converted wei out of uint256 range.")

    wei = _digits_to_int(sig_digits) * (10 ** wei_exp)
    if wei < 0 or wei > MAX_UINT256:
        raise ValueError(f"{field_name} converted wei out of uint256 range.")
    return wei


def parse_rpc_fee_wei(raw_val, method_name: str) -> int:
    """Strictly parses eth_gasPrice / eth_maxPriorityFeePerGas RPC result into non-negative uint256 wei."""
    if raw_val is None or isinstance(raw_val, bool):
        raise ValueError(f"{method_name} returned invalid value ({type(raw_val).__name__}).")
    if isinstance(raw_val, int):
        if raw_val < 0 or raw_val > MAX_UINT256:
            raise ValueError(f"{method_name} returned out-of-range integer (expected 0..2**256 - 1).")
        return raw_val
    if isinstance(raw_val, str):
        if len(raw_val) > 130:
            raise ValueError(f"{method_name} returned oversized hex quantity.")
        clean = raw_val.strip()
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", clean):
            raise ValueError(f"{method_name} returned malformed hex quantity: {raw_val!r}")
        val = int(clean, 16)
        if val < 0 or val > MAX_UINT256:
            raise ValueError(f"{method_name} returned out-of-range hex quantity (expected 0..2**256 - 1).")
        return val
    raise ValueError(f"{method_name} returned unsupported type: {type(raw_val).__name__}")


def parse_gas_multiplier(raw_multiplier) -> Decimal:
    """
    Parses gas multiplier cleanly without depending on Decimal context and enforces >= 1.

    Bounds enforced before conversion:
    - String length <= 128 ASCII chars, exponent in [-128, 128]
    - Normalized significant digits <= 80, normalized exponent in [-80, 18]
    - Value strictly in (0, 10**18]; values in (0, 1) clamp to Decimal("1")
    """
    if raw_multiplier is None or isinstance(raw_multiplier, bool):
        raise ValueError("gas_multiplier must be a positive finite number.")
    if isinstance(raw_multiplier, int):
        if raw_multiplier <= 0:
            raise ValueError("gas_multiplier must be finite and > 0.")
        if raw_multiplier > MAX_GAS_MULTIPLIER_INT:
            raise ValueError("gas_multiplier exceeds maximum allowed ceiling (10**18).")
        return Decimal(raw_multiplier)
    elif isinstance(raw_multiplier, Decimal):
        d = raw_multiplier
    elif isinstance(raw_multiplier, float):
        if not math.isfinite(raw_multiplier) or raw_multiplier <= 0 or math.copysign(1.0, raw_multiplier) < 0:
            raise ValueError("gas_multiplier must be finite and > 0.")
        if raw_multiplier > float(MAX_GAS_MULTIPLIER_INT):
            raise ValueError("gas_multiplier exceeds maximum allowed ceiling (10**18).")
        d = Decimal(str(raw_multiplier))
    elif isinstance(raw_multiplier, str):
        d = _parse_bounded_decimal_str(raw_multiplier, "gas_multiplier", max_abs_exp=MAX_DECIMAL_INPUT_LEN)
    else:
        raise ValueError(f"Unsupported gas_multiplier type: {type(raw_multiplier).__name__}")

    is_zero, sig_digits, eff_exp = _extract_normalized_decimal_parts(
        d, "gas_multiplier", max_abs_raw_exp=MAX_DECIMAL_INPUT_LEN
    )
    if is_zero:
        raise ValueError("gas_multiplier must be finite and > 0.")
    if len(sig_digits) > MAX_MULTIPLIER_SIG_DIGITS:
        raise ValueError(f"gas_multiplier exceeds maximum significant digits ({MAX_MULTIPLIER_SIG_DIGITS}).")
    if eff_exp < MIN_MULTIPLIER_EXP or eff_exp > MAX_MULTIPLIER_EXP:
        raise ValueError("gas_multiplier exponent out of supported range.")
    order = len(sig_digits) + eff_exp
    if order > 19 or (order == 19 and not (sig_digits == (1,) and eff_exp == MAX_MULTIPLIER_EXP)):
        raise ValueError("gas_multiplier exceeds maximum allowed ceiling (10**18).")
    if order <= 0:
        return Decimal("1")
    return Decimal((0, sig_digits, eff_exp))


def validate_cli_fee_and_gas_args(
    gas_limit,
    gas_multiplier=1.15,
    max_fee_per_gas_gwei=None,
    priority_fee_per_gas_gwei=None,
    deprecated_gas_price_gwei=None,
) -> dict:
    """Pre-validates CLI gas limit, multiplier, and explicit gwei overrides before RPC or subprocess calls."""
    validated_limit = validate_gas_limit(gas_limit)
    mult_dec = parse_gas_multiplier(gas_multiplier)
    if max_fee_per_gas_gwei is not None and deprecated_gas_price_gwei is not None:
        raise ValueError("Cannot specify both --max-fee-per-gas-gwei and deprecated --gas-price-gwei.")
    raw_max_fee = max_fee_per_gas_gwei if max_fee_per_gas_gwei is not None else deprecated_gas_price_gwei
    explicit_max_fee_wei = gwei_to_wei(raw_max_fee, "maxFeePerGas (gwei)") if raw_max_fee is not None else None
    explicit_priority_fee_wei = (
        gwei_to_wei(priority_fee_per_gas_gwei, "maxPriorityFeePerGas (gwei)")
        if priority_fee_per_gas_gwei is not None
        else None
    )
    if (
        explicit_max_fee_wei is not None
        and explicit_priority_fee_wei is not None
        and explicit_max_fee_wei < explicit_priority_fee_wei
    ):
        raise ValueError(
            f"Explicit maxFeePerGas ({explicit_max_fee_wei} wei) cannot be less than "
            f"maxPriorityFeePerGas ({explicit_priority_fee_wei} wei)."
        )
    return {
        "gas_limit": validated_limit,
        "effective_multiplier": mult_dec,
        "explicit_max_fee_wei": explicit_max_fee_wei,
        "explicit_priority_fee_wei": explicit_priority_fee_wei,
    }


def resolve_eip1559_fees(
    rpc_url: str,
    rpc_call_fn,
    gas_multiplier=1.15,
    max_fee_per_gas_gwei=None,
    priority_fee_per_gas_gwei=None,
    deprecated_gas_price_gwei=None,
) -> dict:
    """
    Resolves EIP-1559 type-2 fees (maxFeePerGas and maxPriorityFeePerGas) using RPC calls
    and optional explicit CLI overrides.

    - Dual explicit overrides: queries neither fee RPC and uses exact user-specified values.
    - Explicit maxFee only: queries only eth_maxPriorityFeePerGas; never raises explicit maxFee
      and rejects if network priority fee exceeds explicit maxFee.
    - Explicit priorityFee only: queries only eth_gasPrice and computes floor(gasPrice * multiplier);
      raises computed maxFee to priorityFee with a 'priority_floor' warning if needed.
    - No overrides: queries both eth_gasPrice and eth_maxPriorityFeePerGas.
    - Rejects RPC failures, malformed responses, or uint256 overflow without clamping.
    """
    parsed = validate_cli_fee_and_gas_args(
        gas_limit=1,
        gas_multiplier=gas_multiplier,
        max_fee_per_gas_gwei=max_fee_per_gas_gwei,
        priority_fee_per_gas_gwei=priority_fee_per_gas_gwei,
        deprecated_gas_price_gwei=deprecated_gas_price_gwei,
    )
    mult_dec = parsed["effective_multiplier"]
    explicit_max_fee_wei = parsed["explicit_max_fee_wei"]
    explicit_priority_fee_wei = parsed["explicit_priority_fee_wei"]

    if explicit_max_fee_wei is not None:
        network_gas_price_wei = None
    else:
        net_gas_price_raw = rpc_call_fn(rpc_url, "eth_gasPrice", [])
        network_gas_price_wei = parse_rpc_fee_wei(net_gas_price_raw, "eth_gasPrice")

    if explicit_priority_fee_wei is not None:
        priority_fee_wei = explicit_priority_fee_wei
        network_priority_fee_wei = None
    else:
        net_prio_raw = rpc_call_fn(rpc_url, "eth_maxPriorityFeePerGas", [])
        network_priority_fee_wei = parse_rpc_fee_wei(net_prio_raw, "eth_maxPriorityFeePerGas")
        priority_fee_wei = network_priority_fee_wei

    warnings = []
    if explicit_max_fee_wei is not None:
        if explicit_max_fee_wei < priority_fee_wei:
            raise ValueError(
                f"Explicit maxFeePerGas ({explicit_max_fee_wei} wei) cannot be less than "
                f"maxPriorityFeePerGas ({priority_fee_wei} wei)."
            )
        max_fee_wei = explicit_max_fee_wei
    else:
        mult_num, mult_den = _decimal_to_exact_ratio(mult_dec)
        scaled_max_fee_wei = (network_gas_price_wei * mult_num) // mult_den
        if scaled_max_fee_wei > MAX_UINT256:
            raise ValueError("Computed maxFeePerGas exceeds uint256 range.")
        max_fee_wei = max(scaled_max_fee_wei, network_gas_price_wei)
        if max_fee_wei < priority_fee_wei:
            warnings.append(("priority_floor", max_fee_wei, priority_fee_wei))
            max_fee_wei = priority_fee_wei

    validate_eip1559_fees(max_fee_wei, priority_fee_wei)
    return {
        "network_gas_price_wei": network_gas_price_wei,
        "network_priority_fee_wei": network_priority_fee_wei,
        "max_fee_per_gas_wei": max_fee_wei,
        "priority_fee_per_gas_wei": priority_fee_wei,
        "effective_multiplier": mult_dec,
        "explicit_max_fee": explicit_max_fee_wei is not None,
        "explicit_priority_fee": explicit_priority_fee_wei is not None,
        "warnings": warnings,
    }


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
    priority_gas_price: int,
    timeout: float = 60.0,
) -> str:
    """
    Broadcasts a single EIP-1559 (type 2) transaction using:
    cast send <contract> --data <calldata> --account <account> --password-file <file>
              --rpc-url <rpc_url> --nonce <nonce> --gas-price <gas_price>
              --priority-gas-price <priority_gas_price> --gas-limit <gas_limit> --async

    EIP-1559 fee parameter semantics:
    - gas_price: retained interface name representing type-2 maxFeePerGas (in wei),
      passed to Foundry cast via --gas-price.
    - priority_gas_price: type-2 maxPriorityFeePerGas (in wei),
      passed to Foundry cast via --priority-gas-price.

    Shell is False, isolated env without ETH_* / PRIVATE_KEY.
    Single send, no auto-retries or price bumping.
    On timeout or missing txHash, raises CastSendUncertainError and displays warning:
    UNCOINCERTAIN_TX_WARNING
    """
    if not account or not isinstance(account, str) or not account.strip():
        raise CastSignerError("Keystore account name must be specified and non-empty.")
    clean_account = account.strip()

    if not rpc_url or not isinstance(rpc_url, str) or not rpc_url.strip():
        raise CastSignerError("RPC URL must be specified and non-empty.")
    clean_rpc_url = rpc_url.strip()

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise CastSignerError(f"Timeout must be a positive finite number, got {timeout!r}.")

    validate_password_file(password_file)
    clean_pw_file = password_file.strip()
    abs_pw_file = os.path.abspath(clean_pw_file)

    clean_contract = to_checksum_address(contract_address)
    clean_calldata = validate_calldata(calldata)
    validated_nonce = validate_nonce(nonce)
    validated_gas_limit = validate_gas_limit(gas_limit)
    validated_max_fee, validated_priority_fee = validate_eip1559_fees(gas_price, priority_gas_price)

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
        clean_rpc_url,
        "--nonce",
        str(validated_nonce),
        "--gas-price",
        str(validated_max_fee),
        "--priority-gas-price",
        str(validated_priority_fee),
        "--gas-limit",
        str(validated_gas_limit),
        "--async",
    ]
    isolated_env = get_isolated_cast_env()

    sensitive_secrets = [clean_pw_file, abs_pw_file, clean_rpc_url]

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
