#!/usr/bin/env python3
"""
PoolCoin Keeper Bot for MinerV1 on BSC Testnet (chainId 97)
Monitors MinerV1 state:
- Automatically calls initEpochSeed sequentially from min uninitialized epoch to cur_epoch
- Preflights calls with eth_call and decodes revert error names before sending
- Automatically calls sync(steps) when lastSynced < cur_epoch
Includes gas price floor (>=0.1 gwei), multiplier, and replacement (speed-up) retry with identical nonce.
"""

import os, sys, time, json, argparse, urllib.request, urllib.error, re, random, subprocess, shutil
from decimal import Decimal
from urllib.parse import urlsplit
from enum import Enum
from typing import Optional, Dict, Any

try:
    from script.keeper_state import (
        KeeperStateManager,
        StateDomain,
        DomainValidationError,
        DomainLockedError,
        CorruptStateError,
        UnknownStateVersionError,
        InvalidStateError,
        StateWriteError,
    )
except ImportError:
    from keeper_state import (
        KeeperStateManager,
        StateDomain,
        DomainValidationError,
        DomainLockedError,
        CorruptStateError,
        UnknownStateVersionError,
        InvalidStateError,
        StateWriteError,
    )

try:
    from script.cast_signer import (
        CastSignerError,
        resolve_eip1559_fees,
        validate_cli_fee_and_gas_args,
        validate_eip1559_fees,
        validate_gas_limit,
    )
except ImportError:
    from cast_signer import (
        CastSignerError,
        resolve_eip1559_fees,
        validate_cli_fee_and_gas_args,
        validate_eip1559_fees,
        validate_gas_limit,
    )


class ReplacementError(Exception):
    """Base exception for EIP-1559 replacement errors."""
    pass

class ReplacementCeilingNotConfiguredError(ReplacementError):
    pass

class ReplacementCeilingExceededError(ReplacementError):
    pass

class ReplacementBudgetExceededError(ReplacementError):
    pass

class ReplacementConflictError(ReplacementError):
    pass

UINT256_MAX = (1 << 256) - 1

def bump_fee_exact(old_fee_wei: int, bump_percent: int = 15) -> int:
    """
    Computes exact integer ceil bump for EIP-1559 fees.
    Ensures strictly bumped > old_fee_wei, covering old_fee_wei in (0, 1).
    Does NOT use float.
    """
    if old_fee_wei < 0:
        raise ValueError(f"Fee cannot be negative: {old_fee_wei}")
    if bump_percent <= 0:
        raise ValueError(f"Bump percent must be a positive integer: {bump_percent}")
    if old_fee_wei == 0:
        return 1
    bumped = (old_fee_wei * (100 + bump_percent) + 99) // 100
    if bumped <= old_fee_wei:
        bumped = old_fee_wei + 1
    return bumped

def validate_replacement_config(
    max_fee_per_gas_gwei: Optional[Any],
    priority_fee_per_gas_gwei: Optional[Any],
    replacement_max_fee_gwei: Optional[Any],
    replacement_max_priority_fee_gwei: Optional[Any],
    replacement_bump_percent: Any = 15,
    replacement_max_total_budget_wei: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Validates replacement ceilings and bump parameters at startup.
    Distinguishes initial fee, user explicit hard ceilings, and replacement ceilings.
    """
    cfg: Dict[str, Any] = {
        "replacement_enabled": False,
        "replacement_max_fee_wei": None,
        "replacement_max_priority_fee_wei": None,
        "replacement_bump_percent": 15,
        "replacement_max_total_budget_wei": None,
    }

    if replacement_bump_percent is not None:
        try:
            pct = int(replacement_bump_percent)
            if pct <= 0:
                raise ValueError("Must be positive")
            cfg["replacement_bump_percent"] = pct
        except Exception as e:
            raise ValueError(f"Invalid replacement fee bump percent: {format_exception_safe(e)}")

    if replacement_max_total_budget_wei is not None and str(replacement_max_total_budget_wei).strip() != "":
        try:
            budget = int(replacement_max_total_budget_wei)
            if budget <= 0:
                raise ValueError("Must be positive")
            if budget > UINT256_MAX:
                raise ValueError("Exceeds uint256 max")
            cfg["replacement_max_total_budget_wei"] = budget
        except Exception as e:
            raise ValueError(f"Invalid max replacement total budget: {format_exception_safe(e)}")

    if replacement_max_fee_gwei is not None and str(replacement_max_fee_gwei).strip() != "":
        try:
            d_max = Decimal(str(replacement_max_fee_gwei).strip())
            if d_max <= 0:
                raise ValueError("Must be positive")
            max_wei = int(d_max * Decimal(10**9))
            if max_wei > UINT256_MAX:
                raise ValueError("Exceeds uint256 max")
            cfg["replacement_max_fee_wei"] = max_wei
            cfg["replacement_enabled"] = True
        except Exception as e:
            raise ValueError(f"Invalid replacement max fee per gas: {format_exception_safe(e)}")

        if max_fee_per_gas_gwei is not None and str(max_fee_per_gas_gwei).strip() != "":
            try:
                init_max_wei = int(Decimal(str(max_fee_per_gas_gwei).strip()) * Decimal(10**9))
                if cfg["replacement_max_fee_wei"] < init_max_wei:
                    raise ValueError(
                        f"Replacement max fee ceiling ({replacement_max_fee_gwei} gwei) cannot be lower than "
                        f"initial max fee ({max_fee_per_gas_gwei} gwei)"
                    )
            except Exception as e:
                raise ValueError(format_exception_safe(e))

    if replacement_max_priority_fee_gwei is not None and str(replacement_max_priority_fee_gwei).strip() != "":
        try:
            d_prio = Decimal(str(replacement_max_priority_fee_gwei).strip())
            if d_prio <= 0:
                raise ValueError("Must be positive")
            prio_wei = int(d_prio * Decimal(10**9))
            if prio_wei > UINT256_MAX:
                raise ValueError("Exceeds uint256 max")
            cfg["replacement_max_priority_fee_wei"] = prio_wei
        except Exception as e:
            raise ValueError(f"Invalid replacement max priority fee: {format_exception_safe(e)}")

        if cfg["replacement_max_fee_wei"] is not None and cfg["replacement_max_priority_fee_wei"] > cfg["replacement_max_fee_wei"]:
            raise ValueError(
                f"Replacement max priority fee ceiling ({replacement_max_priority_fee_gwei} gwei) cannot exceed "
                f"replacement max fee ceiling ({replacement_max_fee_gwei} gwei)"
            )
    elif cfg["replacement_max_fee_wei"] is not None:
        cfg["replacement_max_priority_fee_wei"] = cfg["replacement_max_fee_wei"]

    return cfg

# Contract function selectors (verified against MinerV1 compiled ABI)
SELECTOR_EPOCH = "900cf0cf"          # epoch() -> uint256
SELECTOR_LAST_SYNCED = "93afb4c5"    # lastSynced() -> uint256
SELECTOR_EPOCH_SEED = "4abccd28"     # epochSeed(uint256) -> uint256
SELECTOR_INIT_EPOCH_SEED = "d9d9c33c"  # initEpochSeed(uint256,uint256)
SELECTOR_INIT_EPOCH_SEED_SINGLE = "0e2a5ea2"  # initEpochSeed(uint256) (legacy)
EVENT_EPOCH_SETTLED_TOPIC = "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5"
EVENT_REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"
SELECTOR_SYNC = "b1357bf9"           # sync(uint256) -> uint256
SELECTOR_REMAINING_EMISSION = "b547df86" # remainingEmission() -> uint256
SELECTOR_SETTLED_EPOCH_QUOTA = "67ef6de5" # settledEpochQuota(uint256) -> uint256

DEFAULT_MIN_GAS_PRICE_GWEI = 0.1
DEFAULT_GAS_MULTIPLIER = 1.5
DEFAULT_TX_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 3
MAX_SEED_STEPS = 24

# Wallet-related environment variables isolated from cast subprocesses to prevent ambient overrides
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
    "KEEPER_PRIVATE_KEY",
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


class TxExecutionStatus(str, Enum):
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_REVERT = "CONFIRMED_REVERT"
    PENDING_UNCONFIRMED = "PENDING_UNCONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    PREFLIGHT_FAILURE = "PREFLIGHT_FAILURE"
    DRY_RUN = "DRY_RUN"


class TxResult(dict):
    """
    Structured outcome of transaction execution.
    Subclasses dict so existing callers and assertions (e.g. res.get('status'),
    res.get('dry_run'), receipt.get('logs')) remain backward-compatible,
    while explicitly exposing status, tx_hash, nonce, error, and helper properties.
    """
    def __init__(
        self,
        status: TxExecutionStatus,
        tx_hash: Optional[str] = None,
        receipt: Optional[Dict[str, Any]] = None,
        nonce: Optional[int] = None,
        error: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        dry_run: bool = False
    ):
        super().__init__()
        self.status = status
        self.tx_hash = tx_hash
        self.receipt = receipt
        self.nonce = nonce
        self.error = error
        self.context = context or {}
        self.dry_run = dry_run or (status == TxExecutionStatus.DRY_RUN)

        if receipt and isinstance(receipt, dict):
            self.update(receipt)
        if self.dry_run:
            self["dry_run"] = True
            self["status"] = 1
        elif receipt:
            pass
        else:
            self["status"] = None

    @property
    def is_confirmed_success(self) -> bool:
        return self.status == TxExecutionStatus.CONFIRMED_SUCCESS

    @property
    def is_confirmed_revert(self) -> bool:
        return self.status == TxExecutionStatus.CONFIRMED_REVERT

    @property
    def is_pending_unconfirmed(self) -> bool:
        return self.status == TxExecutionStatus.PENDING_UNCONFIRMED

    @property
    def is_uncertain(self) -> bool:
        return self.status == TxExecutionStatus.UNCERTAIN

    @property
    def is_preflight_failure(self) -> bool:
        return self.status == TxExecutionStatus.PREFLIGHT_FAILURE

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run or (self.status == TxExecutionStatus.DRY_RUN)

    def __bool__(self) -> bool:
        return self.status == TxExecutionStatus.CONFIRMED_SUCCESS

    def __repr__(self) -> str:
        return f"<TxResult status={self.status.value} tx_hash={self.tx_hash} nonce={self.nonce}>"


def get_isolated_cast_env(base_env=None):
    """
    Builds a clean environment mapping for cast subprocess execution:
    - Strips all wallet identity, keystore, private key, and signer selection variables.
    - Preserves system execution paths, proxies, and general runtime configuration.
    """
    source = dict(base_env if base_env is not None else os.environ)
    isolate_set = {v.upper() for v in WALLET_ENV_VARS_TO_ISOLATE}
    return {k: v for k, v in source.items() if k.upper() not in isolate_set}

# Custom error selectors (from MinerV1 compiled ABI)
ERROR_SELECTORS = {
    "4105720d": "PrevSeedNotInitialized",
    "bcae7b3d": "SeedAlreadyInitialized",
    "2d1adcaf": "FutureEpoch",
    "92555c0e": "BlockhashUnavailable",
    "6bf299d6": "SeedNotInitialized",
    "b9b4ff3d": "SyncingRequired",
    "302bec96": "SubmissionsPausedError",
    "a2a6e22d": "BadBatchLength",
    "47556579": "OnlyAdmin",
    "5a9464dd": "OnlyAdminOrGuardian",
    "d92e233d": "ZeroAddress",
    "beff4a6f": "MismatchStart",
    "2b4b8c76": "MismatchEpochLen",
    "fb348942": "NoActiveStake",
    "ff59fb44": "AddressCapExceeded",
    "0a2e50aa": "InvalidShareIndex",
    "fa5605ef": "DuplicateShareIndex",
    "09bde339": "InvalidProof",
    "756b7857": "EpochNotEnded",
    "646cf558": "AlreadyClaimed",
    "19a2a9bd": "ZeroWeight",
    "11688601": "ParameterOutOfBounds",
    "f92ee8a9": "InvalidInitialization",
    "d7e6bcf8": "NotInitializing",
    "3ee5aeb5": "ReentrancyGuardReentrantCall",
}


def sanitize_url(url: str) -> str:
    """
    Sanitizes RPC URL for display and logging:
    - Conservatively hides userinfo, path, query, and fragment.
    - Preserves scheme and host:port if present.
    - If URL parsing fails or URL is malformed/empty, returns fixed placeholder '<redacted-url>'.
    - Never falls back to printing the original raw URL string.
    """
    if not url or not isinstance(url, str):
        return "<redacted-url>"
    try:
        parts = urlsplit(url)
        scheme = parts.scheme
        hostname = parts.hostname
        if not scheme or not hostname:
            return "<redacted-url>"
        netloc = hostname
        if parts.port:
            netloc = f"{hostname}:{parts.port}"
        has_sensitive_parts = bool(
            parts.username
            or parts.password
            or (parts.path and parts.path not in ("", "/"))
            or parts.query
            or parts.fragment
        )
        if has_sensitive_parts:
            return f"{scheme}://{netloc}/***"
        return f"{scheme}://{netloc}"
    except Exception:
        return "<redacted-url>"

_CONFIGURED_SECRETS = set()

def register_secret(secret: str):
    """
    Registers a known secret/private key/sensitive path for sanitization in logs and outputs.
    """
    if secret and isinstance(secret, str):
        s = secret.strip()
        if len(s) >= 4:
            _CONFIGURED_SECRETS.add(s)
            if s.startswith(("0x", "0X")):
                if len(s[2:]) >= 4:
                    _CONFIGURED_SECRETS.add(s[2:])
            else:
                _CONFIGURED_SECRETS.add("0x" + s)
            if "/" in s or "\\" in s:
                _CONFIGURED_SECRETS.add(s.replace("\\", "/"))
                _CONFIGURED_SECRETS.add(s.replace("/", "\\"))

def clear_configured_secrets():
    """Clears registered secrets (primarily for test resets)."""
    _CONFIGURED_SECRETS.clear()


def parse_numeric(val, val_type, name, allow_zero=True):
    """
    Parses numeric configuration values safely:
    - Enforces type conversion (float or int).
    - Enforces non-negative values.
    - Rejects invalid inputs with a clean error that never leaks the raw value.
    """
    if val is None:
        return None
    try:
        parsed = val_type(val)
        if allow_zero:
            if parsed < 0:
                raise ValueError("negative")
        else:
            if parsed <= 0:
                raise ValueError("non-positive")
        return parsed
    except Exception:
        sys.stderr.write(
            f"[ERROR] Invalid value for {name}: must be a non-negative {'number' if val_type is float else 'integer'}.\n"
        )
        sys.exit(1)

def sanitize_text(text: str, sensitive_values=None, known_secrets=None) -> str:
    """
    Sanitizes arbitrary text by:
    - Masking embedded full URLs and URL credentials using sanitize_url.
    - Masking sensitive key-value pairs (e.g. private_key=..., password=...), including
      quoted values password="alpha beta", JSON {"password": "alpha beta"}, and single quotes.
    - Masking known configured secret keys registered by live trading configuration.
    Does NOT blindly mask arbitrary 64-hex strings: transaction hashes,
    block hashes, and seeds remain visible.
    Does NOT actively scan os.environ for keys.
    """
    if not text or not isinstance(text, str):
        return ""

    def _replace_url(match):
        raw_url = match.group(0)
        trail = ""
        punctuation = ('.', ',', ';', ':', '!', '?', ')', "'", '"')
        while raw_url and raw_url[-1] in punctuation:
            trail = raw_url[-1] + trail
            raw_url = raw_url[:-1]
        return sanitize_url(raw_url) + trail

    sanitized = re.sub(r"""(?:https?|wss?)://[^\s'"<>]+""", _replace_url, text)

    secrets_to_mask = set(_CONFIGURED_SECRETS)
    if sensitive_values:
        if isinstance(sensitive_values, (list, tuple, set)):
            for s in sensitive_values:
                if isinstance(s, str) and len(s.strip()) >= 4:
                    clean_s = s.strip()
                    secrets_to_mask.add(clean_s)
                    if "/" in clean_s or "\\" in clean_s:
                        secrets_to_mask.add(clean_s.replace("\\", "/"))
                        secrets_to_mask.add(clean_s.replace("/", "\\"))
        elif isinstance(sensitive_values, str) and len(sensitive_values.strip()) >= 4:
            clean_s = sensitive_values.strip()
            secrets_to_mask.add(clean_s)
            if "/" in clean_s or "\\" in clean_s:
                secrets_to_mask.add(clean_s.replace("\\", "/"))
                secrets_to_mask.add(clean_s.replace("/", "\\"))
    if known_secrets:
        if isinstance(known_secrets, (list, tuple, set)):
            secrets_to_mask.update(s.strip() for s in known_secrets if isinstance(s, str) and len(s.strip()) >= 4)
        elif isinstance(known_secrets, str) and len(known_secrets.strip()) >= 4:
            secrets_to_mask.add(known_secrets.strip())

    for sec in sorted(secrets_to_mask, key=len, reverse=True):
        if not sec or len(sec) < 4:
            continue
        placeholder = "0x[REDACTED_KEY]" if sec.startswith(("0x", "0X")) else "[REDACTED_KEY]"
        sanitized = re.sub(re.escape(sec), placeholder, sanitized, flags=re.IGNORECASE)

    def _replace_kv(m):
        prefix = m.group(1)
        quote = m.group(2) or ""
        key_lower = prefix.lower()
        placeholder = "[REDACTED_KEY]" if "key" in key_lower else "[REDACTED]"
        return f"{prefix}{quote}{placeholder}{quote}"

    kv_pattern = (
        r"""(?i)(["']?\b(?:keeper[_-]?)?(?:private[_-]?key|priv[_-]?key|privatekey|privkey|password|passwd|secret[_-]?key|secretkey|api[_-]?key|apikey|secret)\b["']?\s*[:=]\s*)"""
        r"""(?:(["'])((?:\\.|(?!\2).)*)\2|()([^'"\s,;}\]]+))"""
    )
    sanitized = re.sub(kv_pattern, _replace_kv, sanitized)
    sanitized = re.sub(r"""(?i)(--password-file\s+)(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)""", r"\1[REDACTED_PATH]", sanitized)
    sanitized = re.sub(r"""(?i)(--password\s+)(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)""", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"""(?i)(--private-key\s+)(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)""", r"\1[REDACTED_KEY]", sanitized)
    sanitized = re.sub(r"""(?i)(?:[a-zA-Z]:)?[\\/][^\s'"]*[\\/]\.foundry[\\/]keystores(?:[\\/][^\s'"]*)?""", "[REDACTED_KEYSTORE_PATH]", sanitized)

    return sanitized

def format_exception_safe(e: Exception) -> str:
    """
    Formats exceptions safely for logging:
    - Never prints raw str(e) from requests or urllib that could contain full URLs or credentials.
    - Preserves exception type, safe status/code, and sanitized message.
    """
    if e is None:
        return ""
    if not isinstance(e, Exception):
        return sanitize_text(str(e))
    exc_type = type(e).__name__
    if isinstance(e, (subprocess.SubprocessError, FileNotFoundError)):
        cmd_name = "cast"
        if hasattr(e, "cmd") and e.cmd:
            cmd_name = e.cmd[0] if isinstance(e.cmd, (list, tuple)) else str(e.cmd).split()[0]
        ret = getattr(e, "returncode", None)
        return f"{exc_type}({cmd_name}{', returncode=' + str(ret) if ret is not None else ''})"
    if hasattr(e, "method") and hasattr(e, "error_obj"):
        method = getattr(e, "method", "rpc")
        err_obj = getattr(e, "error_obj", None)
        if isinstance(err_obj, dict):
            code = err_obj.get("code")
            msg = err_obj.get("message", "")
            return f"RpcError({method}): code={code} message={sanitize_text(str(msg))}"
        return f"RpcError({method}): {sanitize_text(str(err_obj))}"
    if hasattr(e, "code") and hasattr(e, "reason"):
        return f"HTTPError({e.code}): {sanitize_text(str(e.reason))}"
    if hasattr(e, "reason"):
        reason = getattr(e, "reason")
        reason_str = type(reason).__name__ if isinstance(reason, Exception) else str(reason)
        return f"URLError: {sanitize_text(reason_str)}"
    is_requests_exc = any("requests" in getattr(cls, "__module__", "") for cls in type(e).__mro__) or hasattr(e, "request")
    if is_requests_exc:
        resp = getattr(e, "response", None)
        status_code = getattr(resp, "status_code", None) if resp is not None else None
        if status_code:
            return f"{exc_type}: HTTP status {status_code}"
        return f"{exc_type}: [network request failed]"
    msg = sanitize_text(str(e))
    return f"{exc_type}: {msg}"

class RpcError(RuntimeError):
    def __init__(self, method, error_obj):
        self.method = method
        self.error_obj = error_obj
        super().__init__("RPC Error (" + method + "): " + sanitize_text(str(error_obj)))


class NonceError(RuntimeError):
    """Base exception for live nonce resolution and consistency failures."""
    pass


class NonceQueryError(NonceError):
    """Raised when querying or parsing on-chain pending nonce fails in live mode."""
    pass


class NonceDivergenceError(NonceError):
    """Raised when cached current_nonce exceeds on-chain pending nonce in live mode."""
    pass


def decode_revert_error(err):
    """
    Decodes revert error from RPC error dict, Exception, or hex string.
    Returns decoded error name (e.g. 'PrevSeedNotInitialized', 'Error(reason)', or 'Panic(code)').
    """
    raw_str = str(err)
    # Check known names in raw string
    for name in ERROR_SELECTORS.values():
        if name in raw_str:
            return name

    hex_data = None
    if isinstance(err, dict):
        data_field = err.get("data")
        if isinstance(data_field, str):
            hex_data = data_field
        elif isinstance(data_field, dict):
            for v in data_field.values():
                if isinstance(v, dict) and "return" in v:
                    hex_data = v["return"]
                    break
        if not hex_data and "message" in err:
            m = re.search(r"0x[0-9a-fA-F]+", str(err["message"]))
            if m:
                hex_data = m.group(0)
    elif hasattr(err, "error_obj") and isinstance(err.error_obj, dict):
        return decode_revert_error(err.error_obj)

    if not hex_data:
        m = re.search(r"0x([0-9a-fA-F]{8,})", raw_str)
        if m:
            hex_data = "0x" + m.group(1)

    if hex_data:
        clean = hex_data.replace("0x", "")
        selector = clean[:8].lower()
        if selector in ERROR_SELECTORS:
            return ERROR_SELECTORS[selector]
        if selector == "08c379a0":
            try:
                if len(clean) >= 8 + 64 + 64:
                    str_len = int(clean[8 + 64 : 8 + 128], 16)
                    str_hex = clean[8 + 128 : 8 + 128 + str_len * 2]
                    reason = bytes.fromhex(str_hex).decode("utf-8", errors="replace")
                    return f"Error({reason})"
            except Exception:
                pass
            return "Error(string)"
        if selector == "4e487b71":
            try:
                if len(clean) >= 8 + 64:
                    code = int(clean[8 : 8 + 64], 16)
                    return f"Panic({hex(code)})"
            except Exception:
                pass
            return "Panic(uint256)"
        return f"CustomError(0x{selector})"

    if isinstance(err, Exception):
        return format_exception_safe(err)
    return sanitize_text(raw_str)

def load_env(env_path=".env"):
    config = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip().strip('"\'')
    return config

def rpc_call(rpc_url, method, params):
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params}
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Keeper/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            if getattr(e, "fp", None) is not None:
                err_body = e.read().decode("utf-8", errors="replace")
                e.close()
            else:
                err_body = ""
            res = json.loads(err_body)
        except Exception:
            try:
                e.close()
            except Exception:
                pass
            raise RuntimeError(f"HTTP Error {e.code}: {sanitize_text(str(e.reason))}") from None
    except urllib.error.URLError as e:
        reason = type(e.reason).__name__ if isinstance(e.reason, Exception) else str(e.reason)
        raise RuntimeError(f"URLError: {sanitize_text(str(reason))}") from None
    except Exception as e:
        raise RuntimeError(format_exception_safe(e)) from None
    if "error" in res:
        raise RpcError(method, res.get("error"))
    return res.get("result")

def eth_call(rpc_url, to, data):
    data_hex = data if data.startswith("0x") else ("0x" + data)
    tx = {"to": to, "data": data_hex}
    res = rpc_call(rpc_url, "eth_call", [tx, "latest"])
    if res and res.startswith("0x"):
        return res[2:]
    return res or ""

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

def encode_uint256(val):
    return hex(val)[2:].zfill(64)

def to_checksum_address(address):
    """Converts address to EIP-55 checksum format."""
    if not address or address == "0x0000000000000000000000000000000000000000":
        return address
    try:
        from web3 import Web3
        return Web3.to_checksum_address(address)
    except Exception:
        pass
    try:
        from eth_hash.auto import keccak
        clean = address.lower().replace("0x", "")
        h = keccak(clean.encode("utf-8")).hex()
        return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))
    except Exception:
        return address

def calculate_gas_price(network_gas_price_wei, min_gas_price_gwei=DEFAULT_MIN_GAS_PRICE_GWEI, multiplier=DEFAULT_GAS_MULTIPLIER, bump_ratio=1.0):
    """
    Calculates effective gas price in wei:
    - Sets floor to at least min_gas_price_gwei (default 0.1 gwei = 100,000,000 wei)
    - Applies multiplier (e.g. 1.5x)
    - Applies bump_ratio for speed-up replacements
    """
    floor_wei = int(min_gas_price_gwei * 1e9)
    net_price = int(network_gas_price_wei) if (network_gas_price_wei is not None and int(network_gas_price_wei) > 0) else floor_wei
    base_price = max(net_price, floor_wei)
    calculated = int(base_price * multiplier * bump_ratio)
    return max(calculated, floor_wei)

def get_bumped_gas_price(prev_gas_price, min_bump_ratio=1.15, network_gas_price_wei=None, min_gas_price_gwei=DEFAULT_MIN_GAS_PRICE_GWEI, multiplier=DEFAULT_GAS_MULTIPLIER):
    """
    Computes bumped gas price for EVM replacement tx with identical nonce.
    Requires >= 10% over previous gas price (we use 15% + 1 wei minimum).
    """
    floor_wei = int(min_gas_price_gwei * 1e9)
    min_replacement_price = int(prev_gas_price * min_bump_ratio) + 1
    current_market_price = calculate_gas_price(network_gas_price_wei, min_gas_price_gwei, multiplier)
    return max(min_replacement_price, current_market_price, floor_wei)

class KeeperBot:
    def __init__(self, rpc_url, miner_address, private_key=None, keeper_address=None,
                 account=None, password_file=None,
                 dry_run=True, poll_interval=15, min_gas_price_gwei=DEFAULT_MIN_GAS_PRICE_GWEI,
                 gas_multiplier=DEFAULT_GAS_MULTIPLIER, tx_timeout=DEFAULT_TX_TIMEOUT,
                 max_retries=DEFAULT_MAX_RETRIES, max_seed_steps=MAX_SEED_STEPS,
                 state_dir=None, max_fee_per_gas_gwei=None, priority_fee_per_gas_gwei=None,
                 replacement_max_fee_gwei=None, replacement_max_priority_fee_gwei=None,
                 replacement_fee_bump_percent=None, replacement_max_total_budget_wei=None):
        self.rpc_url = rpc_url
        self.miner = miner_address.strip().lower() if miner_address and isinstance(miner_address, str) else (miner_address or "")
        self.dry_run = dry_run

        self.account = account.strip() if account and isinstance(account, str) else None
        if password_file is not None:
            if not isinstance(password_file, str) or not password_file.strip():
                self.alert("Startup check failed: Password file path cannot be empty.")
                sys.exit(1)
            if password_file != password_file.strip():
                self.alert("Startup check failed: Password file path cannot contain leading or trailing whitespace.")
                sys.exit(1)
            self.password_file = password_file
        else:
            self.password_file = None

        if self.account:
            self.validate_account_name(self.account)

        if self.password_file and not self.account:
            self.alert("Startup check failed: --password-file cannot be used without --account.")
            sys.exit(1)

        if self.account and private_key and str(private_key).strip():
            self.alert("Startup check failed: Conflicting signer configurations: both Foundry keystore --account and private key are configured. Specify only one.")
            sys.exit(1)

        if not self.dry_run and self.account and not self.password_file:
            self.alert("Startup check failed: In live mode, --account requires --password-file to avoid interactive prompt delays.")
            sys.exit(1)

        # In dry-run mode or keystore mode, keeper does not store or register raw private keys.
        self.private_key = None if (self.dry_run or self.account) else private_key
        self._known_secrets = set()
        if not self.dry_run and self.private_key and isinstance(self.private_key, str) and len(self.private_key.strip()) >= 4:
            clean_k = self.private_key.strip()
            self._known_secrets.add(clean_k)
            register_secret(clean_k)

        if self.password_file:
            register_secret(self.password_file)
            try:
                abs_pf = os.path.abspath(self.password_file)
                if len(abs_pf) >= 4:
                    register_secret(abs_pf)
            except Exception:
                pass
            clean_pf = self.password_file.strip("\'\"")
            if clean_pf and clean_pf != self.password_file:
                register_secret(clean_pf)
                try:
                    abs_clean = os.path.abspath(clean_pf)
                    if len(abs_clean) >= 4:
                        register_secret(abs_clean)
                except Exception:
                    pass

        # Validate numeric parameters without leaking raw configuration values
        try:
            self.min_gas_price_gwei = float(min_gas_price_gwei)
            if self.min_gas_price_gwei < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: min_gas_price_gwei must be a non-negative number.")
            sys.exit(1)

        try:
            parsed_fee_cfg = validate_cli_fee_and_gas_args(
                gas_limit=1,
                gas_multiplier=gas_multiplier,
                max_fee_per_gas_gwei=max_fee_per_gas_gwei,
                priority_fee_per_gas_gwei=priority_fee_per_gas_gwei,
            )
            self.gas_multiplier_input = gas_multiplier
            self.gas_multiplier = parsed_fee_cfg["effective_multiplier"]
            self.max_fee_per_gas_gwei = max_fee_per_gas_gwei
            self.priority_fee_per_gas_gwei = priority_fee_per_gas_gwei

            rep_cfg = validate_replacement_config(
                max_fee_per_gas_gwei=max_fee_per_gas_gwei,
                priority_fee_per_gas_gwei=priority_fee_per_gas_gwei,
                replacement_max_fee_gwei=replacement_max_fee_gwei,
                replacement_max_priority_fee_gwei=replacement_max_priority_fee_gwei,
                replacement_bump_percent=replacement_fee_bump_percent if replacement_fee_bump_percent is not None else 15,
                replacement_max_total_budget_wei=replacement_max_total_budget_wei,
            )
            self.replacement_enabled = rep_cfg["replacement_enabled"]
            self.replacement_max_fee_gwei = replacement_max_fee_gwei
            self.replacement_max_fee_wei = rep_cfg["replacement_max_fee_wei"]
            self.replacement_max_priority_fee_gwei = replacement_max_priority_fee_gwei
            self.replacement_max_priority_fee_wei = rep_cfg["replacement_max_priority_fee_wei"]
            self.replacement_bump_percent = rep_cfg["replacement_bump_percent"]
            self.replacement_max_total_budget_wei = rep_cfg["replacement_max_total_budget_wei"]
        except Exception as e:
            self.alert(f"Startup check failed: {format_exception_safe(e)}")
            sys.exit(1)

        try:
            self.tx_timeout = int(tx_timeout)
            if self.tx_timeout < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: tx_timeout must be a non-negative integer.")
            sys.exit(1)

        try:
            self.max_retries = int(max_retries)
            if self.max_retries < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: max_retries must be a non-negative integer.")
            sys.exit(1)

        try:
            self.poll_interval = int(poll_interval)
            if self.poll_interval < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: poll_interval must be a non-negative integer.")
            sys.exit(1)

        try:
            parsed_seed_steps = int(max_seed_steps)
            if parsed_seed_steps <= 0:
                raise ValueError("non-positive")
            if parsed_seed_steps > MAX_SEED_STEPS:
                self.log("WARN", f"max_seed_steps ({parsed_seed_steps}) exceeds contract MAX_SEED_STEPS ({MAX_SEED_STEPS}). Capping at {MAX_SEED_STEPS}.")
                parsed_seed_steps = MAX_SEED_STEPS
            self.max_seed_steps = parsed_seed_steps
        except Exception:
            self.alert("Startup check failed: max_seed_steps must be a positive integer.")
            sys.exit(1)
        self.current_nonce = None
        self._chain_id = None
        self.current_epoch = None
        self._derived_address = None

        self.locked_for_manual_review = False
        self.manual_review_context = None
        self.state_dir = state_dir or os.environ.get("KEEPER_STATE_DIR")
        self.state_manager = None
        self._domain_lock_acquired = False

        if self.rpc_url and isinstance(self.rpc_url, str):
            try:
                parsed_rpc = urlsplit(self.rpc_url)
                if parsed_rpc.username:
                    register_secret(parsed_rpc.username)
                    self._known_secrets.add(parsed_rpc.username)
                if parsed_rpc.password:
                    register_secret(parsed_rpc.password)
                    self._known_secrets.add(parsed_rpc.password)
                if parsed_rpc.query:
                    register_secret(parsed_rpc.query)
                    self._known_secrets.add(parsed_rpc.query)
            except Exception:
                pass

        # Validate miner contract address immediately at startup
        self.validate_miner()

        # Derive or assign keeper public address for nonce queries and validate identity
        self.validate_identity(keeper_address)

    def _ensure_state_manager(self) -> Optional[KeeperStateManager]:
        if self.state_manager is not None and self.state_manager.domain_valid:
            return self.state_manager

        chain_id = None
        try:
            chain_id = self.get_chain_id()
        except (Exception, SystemExit):
            chain_id = None

        miner_addr = None
        try:
            if self.miner:
                miner_addr = to_checksum_address(self.miner)
        except (Exception, SystemExit):
            miner_addr = None

        sender_addr = None
        try:
            if self.keeper_address:
                sender_addr = to_checksum_address(self.keeper_address)
        except (Exception, SystemExit):
            sender_addr = None

        domain = None
        if chain_id is not None and miner_addr is not None and sender_addr is not None:
            try:
                domain = StateDomain(chain_id=chain_id, miner_address=miner_addr, sender_address=sender_addr)
            except Exception:
                domain = None

        if self.state_manager is None:
            self.state_manager = KeeperStateManager(state_dir=self.state_dir, domain=domain)
        elif domain is not None and not self.state_manager.domain_valid:
            self.state_manager.set_domain(domain)

        return self.state_manager

    def close(self):
        if getattr(self, "_domain_lock_acquired", False) and getattr(self, "state_manager", None):
            self.state_manager.release_lock()
            self._domain_lock_acquired = False

    @staticmethod
    def _parse_rpc_nonce(val: Any) -> int:
        if val is None or isinstance(val, bool):
            raise ValueError(f"Unparseable nonce response of type {type(val).__name__}")
        if isinstance(val, int):
            if val < 0:
                raise ValueError("Negative integer nonce")
            return val
        if isinstance(val, str):
            cleaned = val.strip()
            if not cleaned:
                raise ValueError("Empty string nonce")
            if cleaned.startswith(("0x", "0X")):
                if not re.match(r"^0[xX][0-9a-fA-F]+$", cleaned):
                    raise ValueError("Malformed hexadecimal nonce")
                return int(cleaned, 16)
            if not re.match(r"^[0-9]+$", cleaned):
                raise ValueError("Malformed decimal nonce")
            return int(cleaned, 10)
        raise ValueError(f"Unsupported nonce response type {type(val).__name__}")

    def _collect_unlock_onchain_snapshot(self, state_data: Optional[Dict[str, Any]] = None) -> Any:
        sensitive = [self.password_file] if getattr(self, "password_file", None) else None
        known_secrets = getattr(self, "_known_secrets", None)
        try:
            res_l = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "latest"])
            latest_nonce = self._parse_rpc_nonce(res_l)
            res_p = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "pending"])
            pending_nonce = self._parse_rpc_nonce(res_p)

            receipts_status = {}
            if state_data:
                h_list = state_data.get("tx_hashes")
                if not isinstance(h_list, list):
                    h_single = state_data.get("tx_hash")
                    h_list = [h_single] if (h_single and h_single != "Unknown") else []
                for h in h_list:
                    if h and h != "Unknown":
                        try:
                            rec = rpc_call(self.rpc_url, "eth_getTransactionReceipt", [h])
                            if rec:
                                receipts_status[h] = {
                                    "status": rec.get("status"),
                                    "blockNumber": rec.get("blockNumber")
                                }
                            else:
                                receipts_status[h] = "not_found_or_pending"
                        except Exception:
                            receipts_status[h] = "receipt_query_failed"
            return {
                "latest_nonce": latest_nonce,
                "pending_nonce": pending_nonce,
                "tx_receipts": receipts_status
            }
        except Exception as snap_e:
            safe_err = sanitize_text(
                format_exception_safe(snap_e),
                sensitive_values=sensitive,
                known_secrets=known_secrets,
            )
            self.log("WARN", f"Failed to retrieve on-chain snapshot for manual unlock: {safe_err}")
            return "快照不可用"

    def manual_unlock(self, expected_nonce=None, operator_reason=None):
        sm = self._ensure_state_manager()
        if not sm or not sm.domain_valid:
            raise DomainValidationError("Cannot unlock without a valid state domain")

        result = sm.manual_unlock(
            expected_nonce=expected_nonce,
            operator_reason=operator_reason,
            snapshot_provider=self._collect_unlock_onchain_snapshot,
        )
        self.locked_for_manual_review = False
        self.manual_review_context = None
        return result

    def validate_account_name(self, name: str):
        if not name or not name.strip():
            self.alert("Startup check failed: Account name cannot be empty.")
            sys.exit(1)
        cleaned = name.strip()
        if (
            "/" in cleaned
            or "\\" in cleaned
            or ".." in cleaned
            or ":" in cleaned
            or cleaned in (".", "..")
            or os.path.basename(cleaned) != cleaned
            or os.path.splitdrive(cleaned)[0]
        ):
            self.alert("Startup check failed: Invalid account name. Directory paths and path traversal are not allowed.")
            sys.exit(1)

    def derive_keystore_address(self) -> str:
        """
        Derives signer address using:
        cast wallet address --account <name> --password-file <path>
        Strictly parses stdout into a single legal Ethereum address.
        Fails and aborts on empty output, multiple lines, format anomalies, or process errors.
        """
        cast_bin = shutil.which("cast") or "cast"
        cmd = [cast_bin, "wallet", "address", "--account", self.account, "--password-file", self.password_file]
        isolated_env = get_isolated_cast_env()

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=30,
                env=isolated_env
            )
        except Exception as e:
            safe_msg = sanitize_text(format_exception_safe(e), sensitive_values=[self.password_file])
            self.alert(f"Startup check failed: Failed to execute cast wallet address: {safe_msg}")
            sys.exit(1)

        if proc.returncode != 0:
            err_msg = sanitize_text((proc.stderr or proc.stdout or "unknown error").strip(), sensitive_values=[self.password_file])
            self.alert(f"Startup check failed: cast wallet address failed (code {proc.returncode}): {err_msg}")
            sys.exit(1)

        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if len(lines) != 1:
            self.alert("Startup check failed: cast wallet address returned empty or multiple lines.")
            sys.exit(1)

        candidate = lines[0]
        if not re.match(r"^0x[0-9a-fA-F]{40}$", candidate):
            self.alert("Startup check failed: cast wallet address output is not a valid Ethereum address format.")
            sys.exit(1)

        return to_checksum_address(candidate)

    def validate_identity(self, explicit_keeper_address=None):
        """
        Validates signer identity:
        - In dry-run mode: uses explicit keeper_address if provided (for simulated nonce queries),
          does not derive or require private key or password file.
        - In live keystore mode: calls `cast wallet address` to derive identity, strictly parsing output.
        - In live private key mode: requires private key, derives signing address.
        - In all live modes: if explicit keeper_address was provided and mismatches derived address,
          rejects cleanly before any nonce query or broadcast.
        """
        if self.dry_run:
            if explicit_keeper_address and str(explicit_keeper_address).strip():
                self.keeper_address = to_checksum_address(str(explicit_keeper_address).strip())
            else:
                self.keeper_address = None
            return

        if self.account:
            derived_address = getattr(self, "_derived_address", None)
            if not derived_address:
                derived_address = self.derive_keystore_address()
                self._derived_address = derived_address
        elif self.private_key:
            try:
                from eth_account import Account
                signer_account = Account.from_key(self.private_key)
                derived_address = signer_account.address
            except Exception as e:
                reason = f"Invalid private key provided for live mode: {type(e).__name__}"
                self.alert(f"Startup check failed: {reason}")
                sys.exit(1)
        else:
            reason = "Private key or keystore account is required for live execution mode (or run with --dry-run)."
            self.alert(f"Startup check failed: {reason}")
            sys.exit(1)

        if explicit_keeper_address and str(explicit_keeper_address).strip():
            clean_explicit = str(explicit_keeper_address).strip().lower()
            clean_derived = derived_address.strip().lower()
            if clean_explicit != clean_derived:
                reason = (
                    f"Explicit keeper address ({str(explicit_keeper_address).strip()}) does not match "
                    f"derived signer address ({derived_address})"
                )
                self.alert(f"Startup check failed: {reason}")
                sys.exit(1)

        self.keeper_address = to_checksum_address(derived_address)

    def validate_miner(self):
        """
        Validates miner target address at startup:
        1. Must be provided (not None, empty, or whitespace)
        2. Must not be zero address
        3. eth_getCode must return non-empty bytecode (must be a deployed contract)
        Exits immediately with error log if validation fails.
        """
        if not self.miner or not str(self.miner).strip():
            reason = "Miner contract address is not provided. Please set MINER_ADDRESS in .env or pass --miner-address."
            self.alert(f"Startup check failed: {reason}")
            sys.exit(1)

        clean_addr = str(self.miner).strip().lower()
        if clean_addr in ("0x0", "0x" + "0" * 40) or (clean_addr.startswith("0x") and set(clean_addr[2:]) == {"0"}):
            reason = f"Miner address cannot be the zero address ({self.miner}). Please provide a valid deployed MinerV1 contract address."
            self.alert(f"Startup check failed: {reason}")
            sys.exit(1)

        try:
            code = rpc_call(self.rpc_url, "eth_getCode", [self.miner, "latest"])
            if not code or code in ("0x", "0x0", "") or code.replace("0x", "") == "":
                reason = f"Target miner address {self.miner} has no contract bytecode deployed (eth_getCode returned empty). Expected a deployed MinerV1 contract."
                self.alert(f"Startup check failed: {reason}")
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            reason = f"Failed to verify contract code at miner address {self.miner}: {format_exception_safe(e)}"
            self.alert(f"Startup check failed: {reason}")
            sys.exit(1)

    def get_chain_id(self):
        if hasattr(self, "_chain_id") and self._chain_id is not None:
            return self._chain_id
        try:
            cid_res = rpc_call(self.rpc_url, "eth_chainId", [])
            self._chain_id = int(cid_res, 16) if isinstance(cid_res, str) and cid_res.startswith("0x") else int(cid_res)
        except Exception:
            self._chain_id = 97
        return self._chain_id

    def log(self, level, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        safe_msg = sanitize_text(msg, known_secrets=getattr(self, "_known_secrets", None))
        print("[" + ts + "] [" + level + "] " + safe_msg, flush=True)

    def alert(self, msg):
        self.log("ALERT", "CRITICAL: " + msg)

    def get_epoch_seed(self, epoch_num):
        seed_data = SELECTOR_EPOCH_SEED + encode_uint256(epoch_num)
        res_seed = eth_call(self.rpc_url, self.miner, seed_data)
        return int(res_seed, 16) if res_seed else 0

    def find_earliest_uninitialized_epoch(self, target_epoch):
        if target_epoch < 0:
            return 0
        if self.get_epoch_seed(0) == 0:
            return 0
        if self.get_epoch_seed(target_epoch) != 0:
            return target_epoch + 1
        low = 0
        high = target_epoch
        while low < high:
            mid = (low + high) // 2
            if self.get_epoch_seed(mid) != 0:
                low = mid + 1
            else:
                high = mid
        return low

    def get_epoch_state(self):
        res_epoch = eth_call(self.rpc_url, self.miner, SELECTOR_EPOCH)
        cur_epoch = int(res_epoch, 16) if res_epoch else 0
        self.current_epoch = cur_epoch
        res_synced = eth_call(self.rpc_url, self.miner, SELECTOR_LAST_SYNCED)
        last_synced = int(res_synced, 16) if res_synced else 0
        seed = self.get_epoch_seed(cur_epoch)
        return cur_epoch, last_synced, seed

    def get_network_gas_price(self):
        """Fetches raw eth_gasPrice from RPC or returns floor on error."""
        try:
            res = rpc_call(self.rpc_url, "eth_gasPrice", [])
            if res:
                return int(res, 16) if isinstance(res, str) and res.startswith("0x") else int(res)
        except Exception as e:
            self.log("WARN", f"Failed to fetch eth_gasPrice from RPC ({format_exception_safe(e)}); using floor {self.min_gas_price_gwei} gwei")
        return int(self.min_gas_price_gwei * 1e9)

    def get_gas_price(self, bump_ratio=1.0):
        """Computes gas price applying floor, multiplier, and optional bump_ratio."""
        net_price = self.get_network_gas_price()
        return calculate_gas_price(
            net_price,
            min_gas_price_gwei=self.min_gas_price_gwei,
            multiplier=self.gas_multiplier,
            bump_ratio=bump_ratio
        )

    def resolve_tx_fees(self) -> Dict[str, Any]:
        """Resolves EIP-1559 dual fees using shared cast_signer validation and RPC rules."""
        mult_arg = self.gas_multiplier_input if getattr(self, "gas_multiplier_input", None) is not None else self.gas_multiplier
        return resolve_eip1559_fees(
            self.rpc_url,
            rpc_call,
            gas_multiplier=mult_arg,
            max_fee_per_gas_gwei=getattr(self, "max_fee_per_gas_gwei", None),
            priority_fee_per_gas_gwei=getattr(self, "priority_fee_per_gas_gwei", None),
        )

    def _log_eip1559_fee_summary(self, level: str, fee_info: Dict[str, Any], gas_limit: int):
        max_fee_wei = fee_info["max_fee_per_gas_wei"]
        priority_fee_wei = fee_info["priority_fee_per_gas_wei"]
        max_total_fee_wei = max_fee_wei * gas_limit
        for warning in fee_info.get("warnings", []):
            if isinstance(warning, tuple) and len(warning) == 3 and warning[0] == "priority_floor":
                _, old_max, prio = warning
                self.log(
                    "WARN",
                    f"Computed maxFeePerGas ({old_max} wei) was below maxPriorityFeePerGas ({prio} wei); "
                    f"raised automatic maxFeePerGas to {prio} wei."
                )
        net_gas = fee_info.get("network_gas_price_wei")
        net_prio = fee_info.get("network_priority_fee_wei")
        net_gas_disp = f"{net_gas} wei" if net_gas is not None else "not queried (explicit override)"
        net_prio_disp = f"{net_prio} wei" if net_prio is not None else "not queried (explicit override)"
        max_src = "explicit" if fee_info.get("explicit_max_fee") else f"network*{fee_info.get('effective_multiplier')}x"
        prio_src = "explicit" if fee_info.get("explicit_priority_fee") else "network"
        max_gwei = Decimal(max_fee_wei) / Decimal(1_000_000_000)
        prio_gwei = Decimal(priority_fee_wei) / Decimal(1_000_000_000)
        self.log(level, f"  Tx Type:         EIP-1559 (type 2)")
        self.log(level, f"  Max Fee / Gas:   {max_gwei} gwei ({max_fee_wei} wei) [{max_src}]")
        self.log(level, f"  Priority Fee:    {prio_gwei} gwei ({priority_fee_wei} wei) [{prio_src}]")
        self.log(level, f"  Network Fees:    eth_gasPrice={net_gas_disp}, eth_maxPriorityFeePerGas={net_prio_disp}")
        self.log(level, f"  Gas Limit:       {gas_limit}")
        self.log(level, f"  Max Total Fee:   {max_total_fee_wei} wei")

    def get_current_fee_estimates(self) -> Tuple[Optional[int], Optional[int]]:
        """Fetches latest network fee estimates from RPC (base fee / priority fee)."""
        market_base = None
        market_prio = None
        try:
            res_p = rpc_call(self.rpc_url, "eth_maxPriorityFeePerGas")
            if res_p is not None:
                market_prio = int(res_p, 16) if isinstance(res_p, str) and res_p.startswith("0x") else int(res_p)
        except Exception:
            pass
        try:
            res_b = rpc_call(self.rpc_url, "eth_gasPrice")
            if res_b is not None:
                market_base = int(res_b, 16) if isinstance(res_b, str) and res_b.startswith("0x") else int(res_b)
        except Exception:
            pass
        return (market_base, market_prio)

    def calculate_replacement_fees(
        self,
        old_max_fee_wei: int,
        old_priority_fee_wei: int,
        gas_limit: int,
    ) -> Tuple[int, int]:
        """
        Computes bumped EIP-1559 fees for replacement with strict integer ceil bump.
        Enforces:
        - strictly bumped > old for both fees (covers 0 and 1 wei)
        - maxFee >= priorityFee
        - pure integer arithmetic, no float
        - bounds within explicit replacement ceilings and budget
        """
        if not getattr(self, "replacement_enabled", False) or self.replacement_max_fee_wei is None:
            raise ReplacementCeilingNotConfiguredError(
                "Automatic replacement disabled: No explicit replacement max fee ceiling configured"
            )

        pct = getattr(self, "replacement_bump_percent", 15)
        new_max_fee = bump_fee_exact(old_max_fee_wei, pct)
        new_priority_fee = bump_fee_exact(old_priority_fee_wei, pct)

        market_base, market_prio = self.get_current_fee_estimates()
        if market_prio is not None and market_prio > new_priority_fee:
            new_priority_fee = market_prio
        if market_base is not None:
            market_max = market_base * 2 + new_priority_fee
            if market_max > new_max_fee:
                new_max_fee = market_max

        if new_max_fee < new_priority_fee:
            new_max_fee = new_priority_fee

        if new_max_fee <= old_max_fee_wei:
            new_max_fee = old_max_fee_wei + 1
            if new_max_fee < new_priority_fee:
                new_max_fee = new_priority_fee

        if new_priority_fee <= old_priority_fee_wei:
            new_priority_fee = old_priority_fee_wei + 1
            if new_max_fee < new_priority_fee:
                new_max_fee = new_priority_fee

        if new_max_fee > UINT256_MAX or new_priority_fee > UINT256_MAX:
            raise ValueError(f"Computed replacement fees exceed uint256 max: max={new_max_fee}, prio={new_priority_fee}")

        if self.replacement_max_priority_fee_wei is not None and new_priority_fee > self.replacement_max_priority_fee_wei:
            raise ReplacementCeilingExceededError(
                f"Computed replacement priority fee ({new_priority_fee} wei) exceeds explicit ceiling ({self.replacement_max_priority_fee_wei} wei)"
            )

        if new_max_fee > self.replacement_max_fee_wei:
            raise ReplacementCeilingExceededError(
                f"Computed replacement max fee ({new_max_fee} wei) exceeds explicit ceiling ({self.replacement_max_fee_wei} wei)"
            )

        if self.replacement_max_total_budget_wei is not None and gas_limit is not None:
            total_fee = new_max_fee * gas_limit
            if total_fee > self.replacement_max_total_budget_wei:
                raise ReplacementBudgetExceededError(
                    f"Computed replacement total fee ({total_fee} wei) exceeds total budget ({self.replacement_max_total_budget_wei} wei)"
                )

        return (new_max_fee, new_priority_fee)

    def reconcile_pre_replacement(
        self,
        func_name: str,
        target_addr: str,
        calldata: str,
        nonce: int,
        gas_limit: int,
        current_max_fee: int,
        current_priority_fee: int,
        active_tx_hash: Optional[str],
        broadcasted_hashes: list,
    ) -> Dict[str, Any]:
        """
        Reconciles known broadcasted transactions before replacement:
        - checks receipts of all broadcasted tx_hashes
        - queries RPC for in-flight transaction fields and verifies identity
        - verifies state completeness if RPC is missing
        - checks on-chain latest/pending nonces
        - calculates strictly bumped fees bounded by ceilings
        """
        # 1. Check receipts
        for h in broadcasted_hashes:
            try:
                rec = rpc_call(self.rpc_url, "eth_getTransactionReceipt", [h])
            except Exception as e:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "RECONCILIATION_RPC_FAILURE",
                    "reason": f"RPC failure querying receipt for {h}: {format_exception_safe(e)}"
                }
            if rec and isinstance(rec, dict) and rec.get("blockNumber"):
                blk = int(rec.get("blockNumber", "0x0"), 16) if isinstance(rec.get("blockNumber"), str) else rec.get("blockNumber")
                used = int(rec.get("gasUsed", "0x0"), 16) if isinstance(rec.get("gasUsed"), str) else rec.get("gasUsed")
                st = rec.get("status")
                is_success = st in ("0x1", 1, True)
                return {
                    "action": "CONFIRMED_RECEIPT",
                    "tx_hash": h,
                    "receipt": rec,
                    "block_number": blk,
                    "gas_used": used,
                    "is_success": is_success,
                }

        # 2. Inspect on-chain tx
        last_hash = active_tx_hash or (broadcasted_hashes[-1] if broadcasted_hashes else None)
        tx_obj = None
        if last_hash and last_hash != "Unknown":
            try:
                tx_obj = rpc_call(self.rpc_url, "eth_getTransactionByHash", [last_hash])
            except Exception as e:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "RECONCILIATION_RPC_FAILURE",
                    "reason": f"RPC failure querying transaction {last_hash} before replacement: {format_exception_safe(e)}"
                }

        base_max_fee = current_max_fee
        base_priority_fee = current_priority_fee

        if tx_obj and isinstance(tx_obj, dict):
            tx_type = tx_obj.get("type")
            if tx_type is None:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": "On-chain transaction missing type field"
                }
            type_val = int(tx_type, 16) if isinstance(tx_type, str) else tx_type
            if type_val not in (2, "0x2", "2"):
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": f"On-chain transaction type {type_val} is not EIP-1559 type-2"
                }

            raw_from = tx_obj.get("from")
            if not raw_from or str(raw_from).strip() == "":
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": "On-chain transaction missing from field"
                }
            tx_from = str(raw_from).lower()
            if tx_from != self.keeper_address.lower():
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": f"On-chain sender {tx_from} does not match keeper {self.keeper_address}"
                }

            raw_to = tx_obj.get("to")
            if not raw_to or str(raw_to).strip() == "":
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": "On-chain transaction missing to field"
                }
            tx_to = str(raw_to).lower()
            if tx_to != target_addr.lower():
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": f"On-chain target {tx_to} does not match expected {target_addr}"
                }

            raw_nonce = tx_obj.get("nonce")
            if raw_nonce is None:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": "On-chain transaction missing nonce field"
                }
            tx_nonce = int(raw_nonce, 16) if isinstance(raw_nonce, str) else raw_nonce
            if tx_nonce != nonce:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": f"On-chain nonce {tx_nonce} does not match attempted nonce {nonce}"
                }

            raw_input = tx_obj.get("input") if tx_obj.get("input") is not None else tx_obj.get("data")
            if raw_input is None:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": "On-chain transaction missing input/data field"
                }
            tx_input = str(raw_input).lower()
            if tx_input != calldata.lower():
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                    "reason": f"On-chain calldata does not match {func_name} calldata"
                }

            raw_value = tx_obj.get("value")
            if raw_value is not None:
                tx_value = int(raw_value, 16) if isinstance(raw_value, str) else raw_value
                if tx_value != 0:
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                        "reason": f"On-chain transaction value {tx_value} is not 0"
                    }

            raw_cid = tx_obj.get("chainId")
            if raw_cid is not None:
                tx_cid = int(raw_cid, 16) if isinstance(raw_cid, str) else raw_cid
                expected_cid = self.get_chain_id()
                if tx_cid != expected_cid:
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                        "reason": f"On-chain chainId {tx_cid} does not match expected {expected_cid}"
                    }

            onchain_max = tx_obj.get("maxFeePerGas")
            onchain_prio = tx_obj.get("maxPriorityFeePerGas")
            if onchain_max is not None:
                try:
                    val = int(onchain_max, 16) if isinstance(onchain_max, str) else int(onchain_max)
                    base_max_fee = max(base_max_fee, val)
                except Exception:
                    pass
            if onchain_prio is not None:
                try:
                    pval = int(onchain_prio, 16) if isinstance(onchain_prio, str) else int(onchain_prio)
                    base_priority_fee = max(base_priority_fee, pval)
                except Exception:
                    pass
        else:
            sm = getattr(self, "state_manager", None) or self._ensure_state_manager()
            st_data = sm.load_state() if sm else None
            if st_data:
                has_to = st_data.get("to")
                has_data = st_data.get("calldata")
                has_fee = st_data.get("max_fee_per_gas_wei") is not None
                if not (has_to and has_data and has_fee):
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "STATE_RECONSTRUCTION_INCOMPLETE",
                        "reason": "Previous transaction fields missing from local state and unrecoverable from RPC; halting replacement for safety."
                    }
                st_to = str(has_to).lower()
                if st_to != target_addr.lower():
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                        "reason": f"State target {st_to} does not match expected {target_addr}"
                    }
                st_calldata = str(has_data).lower()
                if st_calldata != calldata.lower():
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "REPLACEMENT_IDENTITY_CONFLICT",
                        "reason": f"State calldata does not match {func_name} calldata"
                    }
                base_max_fee = max(base_max_fee, int(st_data.get("max_fee_per_gas_wei", 0)))
                base_priority_fee = max(base_priority_fee, int(st_data.get("max_priority_fee_per_gas_wei", 0)))
            else:
                return {
                    "action": "STOP_LOCK",
                    "reason_code": "STATE_RECONSTRUCTION_INCOMPLETE",
                    "reason": "Previous transaction unrecoverable from RPC and local state unavailable; halting replacement for safety."
                }

        # 3. Check latest/pending nonce
        try:
            res_l = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "latest"])
            latest_nonce = int(res_l, 16) if isinstance(res_l, str) and res_l.startswith("0x") else int(res_l)
            res_p = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "pending"])
            pending_nonce = int(res_p, 16) if isinstance(res_p, str) and res_p.startswith("0x") else int(res_p)
        except Exception as e:
            return {
                "action": "STOP_LOCK",
                "reason_code": "RECONCILIATION_RPC_FAILURE",
                "reason": f"RPC failure querying transaction count: {format_exception_safe(e)}"
            }

        if latest_nonce < 0 or pending_nonce < 0:
            return {
                "action": "STOP_LOCK",
                "reason_code": "NONCE_READING_ANOMALY",
                "reason": f"Negative nonce reading: latest={latest_nonce}, pending={pending_nonce}"
            }

        if latest_nonce > pending_nonce:
            return {
                "action": "STOP_LOCK",
                "reason_code": "NONCE_READING_ANOMALY",
                "reason": f"Contradictory nonce readings: latest ({latest_nonce}) > pending ({pending_nonce})"
            }

        if latest_nonce > nonce:
            for h in broadcasted_hashes:
                try:
                    rec = rpc_call(self.rpc_url, "eth_getTransactionReceipt", [h])
                except Exception as e:
                    return {
                        "action": "STOP_LOCK",
                        "reason_code": "RECONCILIATION_RPC_FAILURE",
                        "reason": f"RPC failure querying receipt for {h}: {format_exception_safe(e)}"
                    }
                if rec and isinstance(rec, dict) and rec.get("blockNumber"):
                    blk = int(rec.get("blockNumber", "0x0"), 16) if isinstance(rec.get("blockNumber"), str) else rec.get("blockNumber")
                    used = int(rec.get("gasUsed", "0x0"), 16) if isinstance(rec.get("gasUsed"), str) else rec.get("gasUsed")
                    st = rec.get("status")
                    is_success = st in ("0x1", 1, True)
                    return {
                        "action": "CONFIRMED_RECEIPT",
                        "tx_hash": h,
                        "receipt": rec,
                        "block_number": blk,
                        "gas_used": used,
                        "is_success": is_success,
                    }
            return {
                "action": "STOP_LOCK",
                "reason_code": "NONCE_CONSUMED_UNATTRIBUTED",
                "reason": f"Nonce {nonce} already consumed on-chain (latest={latest_nonce}) but none of known hashes {broadcasted_hashes} confirmed."
            }

        # 4. Calculate replacement fees
        try:
            new_max, new_prio = self.calculate_replacement_fees(
                old_max_fee_wei=base_max_fee,
                old_priority_fee_wei=base_priority_fee,
                gas_limit=gas_limit
            )
            return {
                "action": "PROCEED",
                "max_fee_wei": new_max,
                "priority_fee_wei": new_prio
            }
        except ReplacementCeilingNotConfiguredError as e:
            return {"action": "STOP_LOCK", "reason_code": "TYPE2_REPLACEMENT_DISABLED", "reason": str(e)}
        except ReplacementCeilingExceededError as e:
            return {"action": "STOP_LOCK", "reason_code": "REPLACEMENT_CEILING_EXCEEDED", "reason": str(e)}
        except ReplacementBudgetExceededError as e:
            return {"action": "STOP_LOCK", "reason_code": "REPLACEMENT_BUDGET_EXCEEDED", "reason": str(e)}
        except Exception as e:
            return {"action": "STOP_LOCK", "reason_code": "REPLACEMENT_FEE_CALCULATION_ERROR", "reason": str(e)}


    def get_nonce(self):
        """Fetches pending nonce for keeper account; enforces strict checks in live mode."""
        if self.dry_run:
            remote_nonce = 0
            if self.keeper_address:
                try:
                    res = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "pending"])
                    if res:
                        remote_nonce = int(res, 16) if isinstance(res, str) and res.startswith("0x") else int(res)
                except Exception as e:
                    self.log("WARN", f"Failed to query eth_getTransactionCount ({format_exception_safe(e)}); defaulting to 0")
            if self.current_nonce is not None:
                nonce = max(self.current_nonce, remote_nonce)
            else:
                nonce = remote_nonce
            self.current_nonce = nonce
            return nonce

        self.validate_identity(self.keeper_address)
        sensitive = [self.password_file] if getattr(self, "password_file", None) else None
        known_secrets = getattr(self, "_known_secrets", None)

        if not self.keeper_address:
            msg = sanitize_text(
                "Failed to resolve keeper address for live pending nonce query; "
                "aborting current round without fallback to 0 or cached nonce.",
                sensitive_values=sensitive,
                known_secrets=known_secrets,
            )
            self.alert(msg)
            raise NonceQueryError(msg)

        try:
            res = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "pending"])
            pending_nonce = self._parse_rpc_nonce(res)
        except Exception as e:
            err_detail = sanitize_text(
                format_exception_safe(e),
                sensitive_values=sensitive,
                known_secrets=known_secrets,
            )
            msg = sanitize_text(
                f"Failed to query or parse on-chain pending nonce for {self.keeper_address} ({err_detail}); "
                "aborting current round without fallback to 0 or cached nonce.",
                sensitive_values=sensitive,
                known_secrets=known_secrets,
            )
            self.alert(msg)
            raise NonceQueryError(msg) from e

        if self.current_nonce is not None and self.current_nonce > pending_nonce:
            latest_display = "unavailable"
            try:
                res_latest = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "latest"])
                latest_display = self._parse_rpc_nonce(res_latest)
            except Exception as latest_e:
                latest_err = sanitize_text(
                    format_exception_safe(latest_e),
                    sensitive_values=sensitive,
                    known_secrets=known_secrets,
                )
                self.log("WARN", f"Failed to query diagnostic latest nonce for {self.keeper_address}: {latest_err}")

            msg = sanitize_text(
                f"Nonce divergence between RPC view and cached process state for {self.keeper_address}: "
                f"latest={latest_display}, pending={pending_nonce}, cached={self.current_nonce}. "
                "Aborting current round without using cached nonce or downgrading to on-chain nonce; "
                "will re-evaluate next round.",
                sensitive_values=sensitive,
                known_secrets=known_secrets,
            )
            self.alert(msg)
            raise NonceDivergenceError(msg)

        self.current_nonce = pending_nonce
        return pending_nonce

    def estimate_gas_limit(self, calldata, default_gas=300000):
        """Estimates gas for transaction with a safety buffer."""
        sender = self.keeper_address or "0x0000000000000000000000000000000000000000"
        try:
            tx_obj = {"from": sender, "to": self.miner, "data": calldata}
            res = rpc_call(self.rpc_url, "eth_estimateGas", [tx_obj])
            est = int(res, 16) if isinstance(res, str) and res.startswith("0x") else int(res)
            return max(int(est * 1.3), 80000)
        except Exception:
            return default_gas

    def get_transaction_receipt(self, tx_hash: str):
        try:
            res = rpc_call(self.rpc_url, "eth_getTransactionReceipt", [tx_hash])
            if res and isinstance(res, dict) and res.get("blockNumber"):
                return res
            return None
        except Exception:
            return None

    def wait_for_receipt(self, tx_hash, timeout_secs):
        """Polls for transaction receipt up to timeout_secs."""
        start = time.time()
        while time.time() - start < timeout_secs:
            try:
                receipt = rpc_call(self.rpc_url, "eth_getTransactionReceipt", [tx_hash])
                if receipt and receipt.get("blockNumber"):
                    return receipt
            except Exception:
                pass
            time.sleep(3)
        return None

    def preflight_check(self, calldata, func_desc=""):
        """
        Executes eth_call before sending transaction to simulate execution.
        If the call reverts, decodes and prints the error name, and returns (False, err_name).
        Returns (True, None) if preflight succeeded.
        """
        data_hex = calldata if calldata.startswith("0x") else ("0x" + calldata)
        tx = {"to": self.miner, "data": data_hex}
        if self.keeper_address:
            tx["from"] = self.keeper_address

        try:
            res = rpc_call(self.rpc_url, "eth_call", [tx, "latest"])
            if isinstance(res, str) and res.startswith("0x"):
                clean = res[2:].lower()
                if len(clean) >= 8 and clean[:8] in ERROR_SELECTORS:
                    err_name = ERROR_SELECTORS[clean[:8]]
                    self.alert(f"Preflight check failed for {func_desc}: {err_name}")
                    return False, err_name
            return True, None
        except Exception as e:
            err_name = decode_revert_error(e)
            self.alert(f"Preflight check failed for {func_desc}: {err_name}")
            return False, err_name

    def lock_for_manual_review(self, reason: str, nonce: Optional[int], func_name: str, tx_hash: Optional[str] = None, reason_code: Optional[str] = None, tx_hashes: Optional[list] = None):
        self.locked_for_manual_review = True
        if nonce is None and getattr(self, "keeper_address", None):
            try:
                nonce = self.get_nonce()
            except (Exception, SystemExit):
                nonce = None

        sensitive = [self.password_file] if getattr(self, "password_file", None) else None

        chain_id = "Unknown"
        try:
            cid = self.get_chain_id()
            chain_id = cid if cid is not None else "Unknown"
        except (Exception, SystemExit) as e:
            err_msg = sanitize_text(format_exception_safe(e), sensitive_values=sensitive)
            self.log("WARN", f"Failed to retrieve chain_id for manual review: {err_msg}")
            chain_id = "Unknown"

        signer = "Unknown"
        try:
            signer = self.keeper_address or "Unknown"
        except (Exception, SystemExit):
            signer = "Unknown"

        target = "Unknown"
        try:
            target = to_checksum_address(self.miner) if self.miner else "Unknown"
        except (Exception, SystemExit):
            target = "Unknown"

        clean_hash = None
        try:
            if tx_hash and isinstance(tx_hash, str) and re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash):
                clean_hash = tx_hash
        except (Exception, SystemExit):
            clean_hash = None

        hashes = list(tx_hashes) if isinstance(tx_hashes, list) else []
        if clean_hash and clean_hash not in hashes:
            hashes.append(clean_hash)

        try:
            clean_reason = sanitize_text(reason, sensitive_values=sensitive)
        except (Exception, SystemExit):
            clean_reason = "Transaction outcome uncertain"

        self.manual_review_context = {
            "chain_id": chain_id,
            "signer_address": signer,
            "target_contract": target,
            "nonce": nonce,
            "function_name": func_name,
            "tx_hash": clean_hash
        }
        if reason_code:
            self.manual_review_context["reason_code"] = reason_code
        if hashes:
            self.manual_review_context["tx_hashes"] = hashes

        try:
            sm = self._ensure_state_manager()
            if sm and sm.domain_valid:
                sm.record_manual_review_lock(
                    reason=clean_reason,
                    nonce=nonce,
                    operation=func_name,
                    tx_hash=clean_hash,
                    reason_code=reason_code or "MANUAL_LOCK",
                    details=clean_reason,
                    tx_hashes=hashes
                )
        except (Exception, SystemExit) as persist_err:
            safe_err = sanitize_text(format_exception_safe(persist_err), sensitive_values=sensitive)
            self.log("WARN", f"Failed to persist manual review lock to disk: {safe_err}")


        try:
            self.alert("=================================================================")
            self.alert("CRITICAL: TRANSACTION OUTCOME UNCERTAIN - KEEPER AUTOMATION HALTED")
            self.alert(f"Reason: {clean_reason}")
            self.alert("Manual verification is REQUIRED before resuming keeper operations.")
            self.alert(f"  Chain ID:        {chain_id}")
            self.alert(f"  Signer Address:  {signer}")
            self.alert(f"  Target Contract: {target}")
            self.alert(f"  Nonce:           {nonce}")
            self.alert(f"  Function:        {func_name}")
            self.alert(f"  Tx Hash:         {clean_hash or 'None (Broadcast unconfirmed / No valid hash returned)'}")
            self.alert("=================================================================")
        except (Exception, SystemExit):
            pass

    def is_receipt_successful(self, receipt):
        if not receipt:
            return False
        if isinstance(receipt, TxResult):
            return receipt.status == TxExecutionStatus.CONFIRMED_SUCCESS
        if isinstance(receipt, dict):
            status = receipt.get("status")
            return status in (1, "0x1", "1", True)
        return False

    def _is_tx_success(self, receipt, allow_dry_run=False):
        if isinstance(receipt, TxResult):
            if receipt.status == TxExecutionStatus.CONFIRMED_SUCCESS:
                return True
            if allow_dry_run and receipt.status == TxExecutionStatus.DRY_RUN:
                return True
            return False
        if isinstance(receipt, dict):
            if allow_dry_run and receipt.get("dry_run") is True:
                status = receipt.get("status", 1)
                return status in (1, "0x1", "1") or status is True
            return self.is_receipt_successful(receipt)
        return False


    def send_transaction(self, calldata, desc, func_name=None, preflight=True):
        if func_name is None:
            func_name = desc

        nonce = getattr(self, "current_nonce", None)

        if getattr(self, "locked_for_manual_review", False):
            self.alert(f"Transaction broadcast BLOCKED: Keeper is locked pending manual review. Broadcast refused for {func_name}.")
            return TxResult(
                status=TxExecutionStatus.UNCERTAIN,
                nonce=getattr(self, "current_nonce", None),
                error="Keeper locked pending manual review",
                context=getattr(self, "manual_review_context", None)
            )

        if self.dry_run:
            if preflight:
                ok, err_name = self.preflight_check(calldata, func_name)
                if not ok:
                    self.log("ACTION", f"Skipping {func_name}: preflight simulation failed ({err_name})")
                    return TxResult(
                        status=TxExecutionStatus.PREFLIGHT_FAILURE,
                        error=f"preflight simulation failed: {err_name}",
                        context={
                            "chain_id": self.get_chain_id(),
                            "target_contract": to_checksum_address(self.miner),
                            "function_name": func_name,
                            "calldata": calldata,
                            "revert_error": err_name
                        }
                    )
            try:
                fee_info = self.resolve_tx_fees()
                validate_eip1559_fees(fee_info["max_fee_per_gas_wei"], fee_info["priority_fee_per_gas_wei"])
                gas_limit = validate_gas_limit(self.estimate_gas_limit(calldata, default_gas=300000))
            except Exception as e:
                err_str = sanitize_text(
                    format_exception_safe(e),
                    sensitive_values=[self.password_file] if self.password_file else None,
                )
                self.alert(f"Dry-run EIP-1559 fee resolution failed for {func_name}: {err_str}")
                return TxResult(
                    status=TxExecutionStatus.PREFLIGHT_FAILURE,
                    error=f"fee resolution failed: {err_str}",
                    context={"function_name": func_name}
                )
            nonce = self.get_nonce()
            chain_id = self.get_chain_id()
            cur_epoch = getattr(self, "current_epoch", None)
            if cur_epoch is None:
                res_ep = eth_call(self.rpc_url, self.miner, SELECTOR_EPOCH)
                cur_epoch = int(res_ep, 16) if res_ep else 0
                self.current_epoch = cur_epoch

            self.log("DRY-RUN", f"[Simulated] Would call {func_name} on Miner {self.miner} (simulation only, does not guarantee on-chain confirmation)")
            self.log("DRY-RUN", f"  Target Contract: {self.miner}")
            self.log("DRY-RUN", f"  On-Chain Epoch:  {cur_epoch}")
            self.log("DRY-RUN", f"  Chain ID:        {chain_id}")
            self.log("DRY-RUN", f"  Function:        {func_name}")
            self._log_eip1559_fee_summary("DRY-RUN", fee_info, gas_limit)
            self.log("DRY-RUN", f"  Nonce:           {nonce}{' (simulated)' if not self.keeper_address else ''}")
            if self.account:
                if self.keeper_address:
                    self.log("DRY-RUN", f"  Signer Mode:     Foundry Keystore (account: {self.account}) [simulated address: {self.keeper_address}]")
                else:
                    self.log("DRY-RUN", f"  Signer Mode:     Foundry Keystore (account: {self.account}) [unverified in dry-run; no simulated address provided]")
            self.log("DRY-RUN", f"  Calldata:        {calldata}")
            self.current_nonce = nonce + 1
            return TxResult(
                status=TxExecutionStatus.DRY_RUN,
                receipt={"status": 1, "dry_run": True},
                nonce=nonce,
                dry_run=True
            )

        self.validate_identity(self.keeper_address)

        sm = self._ensure_state_manager()
        if not sm or not sm.domain_valid:
            err_msg = "Cannot safely determine state domain (chainId, miner, or sender invalid). Broadcast refused."
            self.alert(f"Transaction broadcast BLOCKED: {err_msg}")
            self.lock_for_manual_review(reason=err_msg, nonce=nonce, func_name=func_name, reason_code="INVALID_DOMAIN")
            return TxResult(
                status=TxExecutionStatus.UNCERTAIN,
                nonce=nonce,
                error=err_msg,
                context=self.manual_review_context
            )

        # 1. Acquire mutex lock FIRST (sender-level mutual exclusion across processes)
        if not getattr(self, "_domain_lock_acquired", False):
            if not sm.acquire_lock():
                err_msg = f"Sender {self.keeper_address} on chain {self.get_chain_id()} is locked by another active Keeper process"
                self.alert(f"Transaction broadcast BLOCKED: {err_msg}")
                return TxResult(
                    status=TxExecutionStatus.UNCERTAIN,
                    nonce=nonce,
                    error=err_msg,
                    context=getattr(self, "manual_review_context", None)
                )
            self._domain_lock_acquired = True

        try:
            # 2. Re-read persistent state INSIDE the lock (avoids TOCTOU race)
            can_proceed, block_reason, state_data = sm.check_startup_protection()
            if not can_proceed:
                if state_data and state_data.get("status") == "PREPARING_TO_SEND":
                    updated_state = sm.recover_crash_to_manual_review(state_data)
                    self.locked_for_manual_review = True
                    self.manual_review_context = sm.build_context_from_state(updated_state)
                    err_msg = "crash before confirmation detected from previous run (status: PREPARING_TO_SEND). Broadcast refused and locked for manual review."
                    self.alert(f"Transaction broadcast BLOCKED: {err_msg}")
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        nonce=updated_state.get("nonce", nonce),
                        error=err_msg,
                        context=self.manual_review_context
                    )
                else:
                    self.locked_for_manual_review = True
                    if state_data:
                        self.manual_review_context = sm.build_context_from_state(state_data)
                    reported_nonce = state_data.get("nonce") if (state_data and state_data.get("nonce") != "Unknown") else nonce
                    self.alert(f"Transaction broadcast BLOCKED: {block_reason}")
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        nonce=reported_nonce,
                        error=block_reason,
                        context=self.manual_review_context
                    )

            # 3. Perform preflight simulation under the lock
            if preflight:
                ok, err_name = self.preflight_check(calldata, func_name)
                if not ok:
                    self.log("ACTION", f"Skipping {func_name}: preflight simulation failed ({err_name})")
                    return TxResult(
                        status=TxExecutionStatus.PREFLIGHT_FAILURE,
                        error=f"preflight simulation failed: {err_name}",
                        context={
                            "chain_id": self.get_chain_id(),
                            "target_contract": to_checksum_address(self.miner),
                            "function_name": func_name,
                            "calldata": calldata,
                            "revert_error": err_name
                        }
                    )

            # 4. Re-fetch fresh gas price and fresh nonce INSIDE the lock
            try:
                fee_info = self.resolve_tx_fees()
                max_fee_wei = fee_info["max_fee_per_gas_wei"]
                priority_fee_wei = fee_info["priority_fee_per_gas_wei"]
                validate_eip1559_fees(max_fee_wei, priority_fee_wei)
                gas_limit = validate_gas_limit(self.estimate_gas_limit(calldata, default_gas=300000))
            except Exception as e:
                err_str = sanitize_text(
                    format_exception_safe(e),
                    sensitive_values=[self.password_file] if self.password_file else None,
                )
                self.alert(f"Transaction broadcast ABORTED for {func_name}: EIP-1559 fee resolution failed ({err_str})")
                return TxResult(
                    status=TxExecutionStatus.PREFLIGHT_FAILURE,
                    error=f"fee resolution failed: {err_str}",
                    context={"function_name": func_name}
                )
            nonce = self.get_nonce()
            target_addr = to_checksum_address(self.miner)
            self.log("INFO", f"Prepared type-2 {func_name} (nonce {nonce}):")
            self._log_eip1559_fee_summary("INFO", fee_info, gas_limit)

            if self.account:
                cast_bin = shutil.which("cast") or "cast"
                isolated_env = get_isolated_cast_env()

                def _cast_broadcast(attempt, cur_max_fee, cur_prio_fee):
                    cmd = [
                        cast_bin, "send", target_addr,
                        "--data", calldata,
                        "--account", self.account,
                        "--password-file", self.password_file,
                        "--rpc-url", self.rpc_url,
                        "--gas-price", str(cur_max_fee),
                        "--priority-gas-price", str(cur_prio_fee),
                        "--gas-limit", str(gas_limit),
                        "--nonce", str(nonce),
                        "--async"
                    ]
                    try:
                        proc = subprocess.run(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            shell=False,
                            timeout=self.tx_timeout,
                            env=isolated_env
                        )
                    except subprocess.TimeoutExpired:
                        return (None, f"cast send subprocess timed out after {self.tx_timeout}s", True)
                    except Exception as e:
                        return (None, format_exception_safe(e), False)

                    out = proc.stdout or ""
                    err = proc.stderr or ""
                    if proc.returncode != 0:
                        return (None, f"{err}\n{out}".strip(), False)
                    m = re.search(r"0x[0-9a-fA-F]{64}", out)
                    if not m:
                        return (None, f"cast send succeeded (code 0) but no valid txHash found in output: {out}", False)
                    return (m.group(0), None, False)

                return self._execute_type2_broadcast_with_replacement(
                    func_name=func_name,
                    target_addr=target_addr,
                    calldata=calldata,
                    nonce=nonce,
                    gas_limit=gas_limit,
                    initial_max_fee_wei=max_fee_wei,
                    initial_priority_fee_wei=priority_fee_wei,
                    broadcast_fn=_cast_broadcast,
                    sensitive_password_file=self.password_file,
                    sm=sm
                )

            if not self.private_key:
                self.alert("Cannot broadcast " + desc + ": No private key provided in environment!")
                return TxResult(
                    status=TxExecutionStatus.PREFLIGHT_FAILURE,
                    error="No private key configured",
                    context={"function_name": func_name}
                )

            try:
                from eth_account import Account
            except ImportError:
                self.alert("Cannot broadcast " + desc + ": eth_account package not installed!")
                return TxResult(
                    status=TxExecutionStatus.PREFLIGHT_FAILURE,
                    error="eth_account not installed",
                    context={"function_name": func_name}
                )

            chain_id = self.get_chain_id()

            def _raw_broadcast(attempt, cur_max_fee, cur_prio_fee):
                tx_dict = {
                    "type": 2,
                    "to": target_addr,
                    "value": 0,
                    "gas": gas_limit,
                    "maxFeePerGas": cur_max_fee,
                    "maxPriorityFeePerGas": cur_prio_fee,
                    "nonce": nonce,
                    "chainId": chain_id,
                    "data": calldata
                }
                try:
                    signed = Account.sign_transaction(tx_dict, self.private_key)
                    raw_bytes = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
                    raw_hex = "0x" + raw_bytes.hex()
                except Exception as e:
                    return (None, f"Failed to sign transaction: {format_exception_safe(e)}", False)
                try:
                    h = rpc_call(self.rpc_url, "eth_sendRawTransaction", [raw_hex])
                    if not h or not isinstance(h, str) or not re.match(r"^0x[0-9a-fA-F]{64}$", h):
                        return (None, f"Malformed or missing txHash from eth_sendRawTransaction: {h}", False)
                    return (h, None, False)
                except Exception as e:
                    return (None, format_exception_safe(e), False)

            return self._execute_type2_broadcast_with_replacement(
                func_name=func_name,
                target_addr=target_addr,
                calldata=calldata,
                nonce=nonce,
                gas_limit=gas_limit,
                initial_max_fee_wei=max_fee_wei,
                initial_priority_fee_wei=priority_fee_wei,
                broadcast_fn=_raw_broadcast,
                sensitive_password_file=None,
                sm=sm
            )

        finally:
            if not self.dry_run and sm and getattr(self, "_domain_lock_acquired", False):
                sm.release_lock()
                self._domain_lock_acquired = False




    def _execute_type2_broadcast_with_replacement(
        self,
        func_name: str,
        target_addr: str,
        calldata: str,
        nonce: int,
        gas_limit: int,
        initial_max_fee_wei: int,
        initial_priority_fee_wei: int,
        broadcast_fn,
        sensitive_password_file: Optional[str] = None,
        sm: Optional[Any] = None,
    ) -> TxResult:
        if sm is None:
            sm = getattr(self, "state_manager", None) or self._ensure_state_manager()
        active_tx_hash = None
        broadcasted_hashes = []
        max_fee_wei = initial_max_fee_wei
        priority_fee_wei = initial_priority_fee_wei

        def _check_all_known_receipts():
            for h in broadcasted_hashes:
                self.log("INFO", f"Checking receipt for broadcasted txHash {h}...")
                receipt = self.wait_for_receipt(h, min(self.tx_timeout, 15))
                if receipt:
                    st_r = receipt.get("status")
                    blk = int(receipt.get("blockNumber", "0x0"), 16) if isinstance(receipt.get("blockNumber"), str) else receipt.get("blockNumber")
                    used = int(receipt.get("gasUsed", "0x0"), 16) if isinstance(receipt.get("gasUsed"), str) else receipt.get("gasUsed")
                    if st_r in ("0x1", 1, True):
                        self.log("SUCCESS", f"Transaction {h} CONFIRMED in block {blk}! Gas used: {used}")
                        self.current_nonce = nonce + 1
                        if sm:
                            try:
                                sm.record_confirmed_success(tx_hash=h, block_number=blk, gas_used=used)
                            except Exception as sm_e:
                                err_str = format_exception_safe(sm_e)
                                self.alert(f"Transaction confirmed on-chain but failed to save CONFIRMED_SUCCESS state: {err_str}")
                                self.lock_for_manual_review(
                                    reason=f"Tx confirmed on-chain ({h}) but failed to write state ({err_str})",
                                    nonce=nonce,
                                    func_name=func_name,
                                    tx_hash=h,
                                    reason_code="STATE_WRITE_FAILURE",
                                    tx_hashes=broadcasted_hashes
                                )
                                return TxResult(
                                    status=TxExecutionStatus.UNCERTAIN,
                                    tx_hash=h,
                                    nonce=nonce,
                                    error=f"Tx confirmed on-chain but failed to write state: {err_str}",
                                    context=self.manual_review_context
                                )
                        return TxResult(
                            status=TxExecutionStatus.CONFIRMED_SUCCESS,
                            tx_hash=h,
                            receipt=receipt,
                            nonce=nonce
                        )
                    else:
                        self.alert(f"Transaction {h} REVERTED in block {blk}!")
                        self.current_nonce = nonce + 1
                        if sm:
                            try:
                                sm.record_confirmed_revert(tx_hash=h, block_number=blk, gas_used=used, revert_reason="revert")
                            except Exception as sm_e:
                                err_str = format_exception_safe(sm_e)
                                self.alert(f"Transaction reverted on-chain but failed to save CONFIRMED_REVERT state: {err_str}")
                                self.lock_for_manual_review(
                                    reason=f"Tx reverted on-chain ({h}) but failed to write state ({err_str})",
                                    nonce=nonce,
                                    func_name=func_name,
                                    tx_hash=h,
                                    reason_code="STATE_WRITE_FAILURE",
                                    tx_hashes=broadcasted_hashes
                                )
                                return TxResult(
                                    status=TxExecutionStatus.UNCERTAIN,
                                    tx_hash=h,
                                    nonce=nonce,
                                    error=f"Tx reverted on-chain but failed to write state: {err_str}",
                                    context=self.manual_review_context
                                )
                        return TxResult(
                            status=TxExecutionStatus.CONFIRMED_REVERT,
                            tx_hash=h,
                            receipt=receipt,
                            nonce=nonce
                        )
            return None

        total_attempts = 1 + max(0, self.max_retries)
        for attempt in range(1, total_attempts + 1):
            if not self.dry_run and sm:
                try:
                    sm.record_preparing_to_send(
                        operation=func_name,
                        epoch=getattr(self, "current_epoch", "Unknown"),
                        nonce=nonce,
                        details=f"Broadcasting type-2 {func_name} with nonce {nonce} (attempt {attempt})",
                        tx_type=2,
                        max_fee_per_gas_wei=max_fee_wei,
                        max_priority_fee_per_gas_wei=priority_fee_wei,
                        gas_limit=gas_limit,
                        to_address=target_addr,
                        value_wei=0,
                        calldata=calldata,
                        tx_hashes=broadcasted_hashes,
                        attempt=attempt,
                    )
                except Exception as e:
                    err_str = sanitize_text(format_exception_safe(e), sensitive_values=[sensitive_password_file] if sensitive_password_file else None)
                    self.alert(f"Transaction broadcast ABORTED: Failed to write in-flight state before sending: {err_str}")
                    self.lock_for_manual_review(
                        reason=f"Failed to persist in-flight state ({err_str})",
                        nonce=nonce,
                        func_name=func_name,
                        reason_code="STATE_WRITE_FAILURE",
                        tx_hashes=broadcasted_hashes
                    )
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        nonce=nonce,
                        error=f"Failed to persist in-flight state: {err_str}",
                        context=self.manual_review_context
                    )

            tx_hash, raw_err, is_timeout = broadcast_fn(attempt, max_fee_wei, priority_fee_wei)

            if raw_err:
                err_str = sanitize_text(raw_err, sensitive_values=[sensitive_password_file] if sensitive_password_file else None)
                if is_timeout:
                    self.alert(f"Broadcast timed out on attempt {attempt}: {err_str}")
                    if broadcasted_hashes:
                        rec_res = _check_all_known_receipts()
                        if rec_res:
                            return rec_res
                    self.lock_for_manual_review(
                        reason=f"Broadcast timed out during execution ({err_str})",
                        nonce=nonce,
                        func_name=func_name,
                        tx_hash=active_tx_hash,
                        reason_code="UNCERTAIN_BROADCAST_RESULT",
                        tx_hashes=broadcasted_hashes
                    )
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        tx_hash=active_tx_hash,
                        nonce=nonce,
                        error=err_str,
                        context=self.manual_review_context
                    )

                is_underpriced = "replacement underpriced" in err_str.lower() or "replacement transaction underpriced" in err_str.lower()
                is_nonce_err = "nonce too low" in err_str.lower() or "already known" in err_str.lower()

                if is_underpriced:
                    if broadcasted_hashes:
                        rec_res = _check_all_known_receipts()
                        if rec_res:
                            return rec_res
                    self.log("WARN", f"Broadcast rejected: replacement underpriced on attempt {attempt}: {err_str}")
                    if attempt >= total_attempts:
                        self.lock_for_manual_review(
                            reason=f"Replacement underpriced on attempt {attempt} and retries exhausted ({err_str})",
                            nonce=nonce,
                            func_name=func_name,
                            tx_hash=active_tx_hash,
                            reason_code="REPLACEMENT_UNDERPRICED_FAILED",
                            tx_hashes=broadcasted_hashes
                        )
                        return TxResult(
                            status=TxExecutionStatus.UNCERTAIN,
                            tx_hash=active_tx_hash,
                            nonce=nonce,
                            error=err_str,
                            context=self.manual_review_context
                        )
                    recon = self.reconcile_pre_replacement(
                        func_name=func_name,
                        target_addr=target_addr,
                        calldata=calldata,
                        nonce=nonce,
                        gas_limit=gas_limit,
                        current_max_fee=max_fee_wei,
                        current_priority_fee=priority_fee_wei,
                        active_tx_hash=active_tx_hash,
                        broadcasted_hashes=broadcasted_hashes
                    )
                    if recon["action"] == "CONFIRMED_RECEIPT":
                        h = recon["tx_hash"]
                        rec = recon["receipt"]
                        blk = recon["block_number"]
                        used = recon["gas_used"]
                        if recon["is_success"]:
                            self.log("SUCCESS", f"Transaction {h} CONFIRMED in block {blk}! Gas used: {used}")
                            self.current_nonce = nonce + 1
                            if sm:
                                sm.record_confirmed_success(tx_hash=h, block_number=blk, gas_used=used)
                            return TxResult(status=TxExecutionStatus.CONFIRMED_SUCCESS, tx_hash=h, receipt=rec, nonce=nonce)
                        else:
                            self.alert(f"Transaction {h} REVERTED in block {blk}!")
                            self.current_nonce = nonce + 1
                            if sm:
                                sm.record_confirmed_revert(tx_hash=h, block_number=blk, gas_used=used, revert_reason="revert")
                            return TxResult(status=TxExecutionStatus.CONFIRMED_REVERT, tx_hash=h, receipt=rec, nonce=nonce)
                    elif recon["action"] == "STOP_LOCK":
                        self.lock_for_manual_review(
                            reason=recon["reason"],
                            nonce=nonce,
                            func_name=func_name,
                            tx_hash=active_tx_hash,
                            reason_code=recon["reason_code"],
                            tx_hashes=broadcasted_hashes
                        )
                        return TxResult(
                            status=TxExecutionStatus.PENDING_UNCONFIRMED if ("CEILING" in recon["reason_code"] or "BUDGET" in recon["reason_code"] or "DISABLED" in recon["reason_code"]) else TxExecutionStatus.UNCERTAIN,
                            tx_hash=active_tx_hash,
                            nonce=nonce,
                            error=recon["reason"],
                            context=self.manual_review_context
                        )
                    elif recon["action"] == "PROCEED":
                        max_fee_wei = recon["max_fee_wei"]
                        priority_fee_wei = recon["priority_fee_wei"]
                        continue

                elif is_nonce_err:
                    if broadcasted_hashes:
                        rec_res = _check_all_known_receipts()
                        if rec_res:
                            return rec_res
                    diag_latest = None
                    try:
                        res_l = rpc_call(self.rpc_url, "eth_getTransactionCount", [self.keeper_address, "latest"])
                        diag_latest = int(res_l, 16) if isinstance(res_l, str) and res_l.startswith("0x") else int(res_l)
                    except Exception:
                        pass
                    if diag_latest is not None and diag_latest > nonce:
                        lock_code = "NONCE_CONSUMED_UNATTRIBUTED"
                        lock_reason = f"Nonce {nonce} consumed on-chain (latest={diag_latest}) but receipt unattributed after {err_str}"
                    else:
                        lock_code = "NONCE_TOO_LOW_UNRECONCILED"
                        lock_reason = f"RPC reported nonce too low on attempt {attempt}: {err_str}"
                    self.lock_for_manual_review(
                        reason=lock_reason,
                        nonce=nonce,
                        func_name=func_name,
                        tx_hash=active_tx_hash,
                        reason_code=lock_code,
                        tx_hashes=broadcasted_hashes
                    )
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        tx_hash=active_tx_hash,
                        nonce=nonce,
                        error=err_str,
                        context=self.manual_review_context
                    )

                else:
                    if broadcasted_hashes:
                        rec_res = _check_all_known_receipts()
                        if rec_res:
                            return rec_res
                    self.lock_for_manual_review(
                        reason=f"Broadcast failed on attempt {attempt}: {err_str}",
                        nonce=nonce,
                        func_name=func_name,
                        tx_hash=active_tx_hash,
                        reason_code=None,
                        tx_hashes=broadcasted_hashes
                    )
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        tx_hash=active_tx_hash,
                        nonce=nonce,
                        error=err_str,
                        context=self.manual_review_context
                    )

            # Broadcast succeeded
            active_tx_hash = tx_hash
            if tx_hash not in broadcasted_hashes:
                broadcasted_hashes.append(tx_hash)
            if sm:
                try:
                    sm.record_broadcasted(tx_hash)
                except Exception as sm_e:
                    err_str = format_exception_safe(sm_e)
                    self.alert(f"Failed to record broadcasted txHash {tx_hash} to state file: {err_str}")
                    self.lock_for_manual_review(
                        reason=f"Broadcasted txHash {tx_hash} could not be persisted to state ({err_str})",
                        nonce=nonce,
                        func_name=func_name,
                        tx_hash=tx_hash,
                        reason_code="STATE_WRITE_FAILURE",
                        tx_hashes=broadcasted_hashes
                    )
                    return TxResult(
                        status=TxExecutionStatus.UNCERTAIN,
                        tx_hash=tx_hash,
                        nonce=nonce,
                        error=err_str,
                        context=self.manual_review_context
                    )
            self.log("TX", f"Broadcasted type-2 {func_name} txHash={tx_hash} (nonce={nonce}, maxFee={max_fee_wei} wei, priorityFee={priority_fee_wei} wei, attempt={attempt})")

            receipt = self.wait_for_receipt(tx_hash, self.tx_timeout)
            if receipt:
                st_r = receipt.get("status")
                blk = int(receipt.get("blockNumber", "0x0"), 16) if isinstance(receipt.get("blockNumber"), str) else receipt.get("blockNumber")
                used = int(receipt.get("gasUsed", "0x0"), 16) if isinstance(receipt.get("gasUsed"), str) else receipt.get("gasUsed")
                if st_r in ("0x1", 1, True):
                    self.log("SUCCESS", f"Transaction {tx_hash} CONFIRMED in block {blk}! Gas used: {used}")
                    self.current_nonce = nonce + 1
                    if sm:
                        try:
                            sm.record_confirmed_success(tx_hash=tx_hash, block_number=blk, gas_used=used)
                        except Exception as sm_e:
                            err_str = format_exception_safe(sm_e)
                            self.alert(f"Transaction confirmed on-chain but failed to save CONFIRMED_SUCCESS state: {err_str}")
                            self.lock_for_manual_review(
                                reason=f"Tx confirmed on-chain ({tx_hash}) but failed to write state ({err_str})",
                                nonce=nonce,
                                func_name=func_name,
                                tx_hash=tx_hash,
                                reason_code="STATE_WRITE_FAILURE",
                                tx_hashes=broadcasted_hashes
                            )
                            return TxResult(
                                status=TxExecutionStatus.UNCERTAIN,
                                tx_hash=tx_hash,
                                nonce=nonce,
                                error=f"Tx confirmed on-chain but failed to write state: {err_str}",
                                context=self.manual_review_context
                            )
                    return TxResult(
                        status=TxExecutionStatus.CONFIRMED_SUCCESS,
                        tx_hash=tx_hash,
                        receipt=receipt,
                        nonce=nonce
                    )
                else:
                    self.alert(f"Transaction {tx_hash} REVERTED in block {blk}!")
                    self.current_nonce = nonce + 1
                    if sm:
                        try:
                            sm.record_confirmed_revert(tx_hash=tx_hash, block_number=blk, gas_used=used, revert_reason="revert")
                        except Exception as sm_e:
                            err_str = format_exception_safe(sm_e)
                            self.alert(f"Transaction reverted on-chain but failed to save CONFIRMED_REVERT state: {err_str}")
                            self.lock_for_manual_review(
                                reason=f"Tx reverted on-chain ({tx_hash}) but failed to write state ({err_str})",
                                nonce=nonce,
                                func_name=func_name,
                                tx_hash=tx_hash,
                                reason_code="STATE_WRITE_FAILURE",
                                tx_hashes=broadcasted_hashes
                            )
                            return TxResult(
                                status=TxExecutionStatus.UNCERTAIN,
                                tx_hash=tx_hash,
                                nonce=nonce,
                                error=f"Tx reverted on-chain but failed to write state: {err_str}",
                                context=self.manual_review_context
                            )
                    return TxResult(
                        status=TxExecutionStatus.CONFIRMED_REVERT,
                        tx_hash=tx_hash,
                        receipt=receipt,
                        nonce=nonce
                    )

            self.log("TIMEOUT", f"Transaction {tx_hash} not confirmed within {self.tx_timeout}s.")
            if attempt >= total_attempts:
                if broadcasted_hashes:
                    rec_res = _check_all_known_receipts()
                    if rec_res:
                        return rec_res
                lock_reason = (
                    f"Transaction unconfirmed after all {self.max_retries} speed-up attempts exhausted"
                    if self.max_retries > 0
                    else f"Transaction unconfirmed after {self.tx_timeout}s"
                )
                lock_code = "MAX_RETRIES_EXCEEDED" if self.max_retries > 0 else None
                self.lock_for_manual_review(
                    reason=lock_reason,
                    nonce=nonce,
                    func_name=func_name,
                    tx_hash=active_tx_hash,
                    reason_code=lock_code,
                    tx_hashes=broadcasted_hashes
                )
                return TxResult(
                    status=TxExecutionStatus.PENDING_UNCONFIRMED,
                    tx_hash=active_tx_hash,
                    nonce=nonce,
                    error=lock_reason,
                    context=self.manual_review_context
                )

            if not getattr(self, "replacement_enabled", False) or self.replacement_max_fee_wei is None:
                if broadcasted_hashes:
                    rec_res = _check_all_known_receipts()
                    if rec_res:
                        return rec_res
                lock_reason = (
                    f"Type-2 transaction unconfirmed after {self.tx_timeout}s; legacy single-gasPrice replacement disabled in K2 (manual review required)"
                )
                self.lock_for_manual_review(
                    reason=lock_reason,
                    nonce=nonce,
                    func_name=func_name,
                    tx_hash=active_tx_hash,
                    reason_code="TYPE2_REPLACEMENT_DISABLED",
                    tx_hashes=broadcasted_hashes
                )
                return TxResult(
                    status=TxExecutionStatus.PENDING_UNCONFIRMED,
                    tx_hash=active_tx_hash,
                    nonce=nonce,
                    error=lock_reason,
                    context=self.manual_review_context
                )

            recon = self.reconcile_pre_replacement(
                func_name=func_name,
                target_addr=target_addr,
                calldata=calldata,
                nonce=nonce,
                gas_limit=gas_limit,
                current_max_fee=max_fee_wei,
                current_priority_fee=priority_fee_wei,
                active_tx_hash=active_tx_hash,
                broadcasted_hashes=broadcasted_hashes
            )
            if recon["action"] == "CONFIRMED_RECEIPT":
                h = recon["tx_hash"]
                rec = recon["receipt"]
                blk = recon["block_number"]
                used = recon["gas_used"]
                if recon["is_success"]:
                    self.log("SUCCESS", f"Transaction {h} CONFIRMED in block {blk}! Gas used: {used}")
                    self.current_nonce = nonce + 1
                    if sm:
                        sm.record_confirmed_success(tx_hash=h, block_number=blk, gas_used=used)
                    return TxResult(status=TxExecutionStatus.CONFIRMED_SUCCESS, tx_hash=h, receipt=rec, nonce=nonce)
                else:
                    self.alert(f"Transaction {h} REVERTED in block {blk}!")
                    self.current_nonce = nonce + 1
                    if sm:
                        sm.record_confirmed_revert(tx_hash=h, block_number=blk, gas_used=used, revert_reason="revert")
                    return TxResult(status=TxExecutionStatus.CONFIRMED_REVERT, tx_hash=h, receipt=rec, nonce=nonce)
            elif recon["action"] == "STOP_LOCK":
                self.lock_for_manual_review(
                    reason=recon["reason"],
                    nonce=nonce,
                    func_name=func_name,
                    tx_hash=active_tx_hash,
                    reason_code=recon["reason_code"],
                    tx_hashes=broadcasted_hashes
                )
                return TxResult(
                    status=TxExecutionStatus.PENDING_UNCONFIRMED if ("CEILING" in recon["reason_code"] or "BUDGET" in recon["reason_code"] or "DISABLED" in recon["reason_code"]) else TxExecutionStatus.UNCERTAIN,
                    tx_hash=active_tx_hash,
                    nonce=nonce,
                    error=recon["reason"],
                    context=self.manual_review_context
                )
            elif recon["action"] == "PROCEED":
                max_fee_wei = recon["max_fee_wei"]
                priority_fee_wei = recon["priority_fee_wei"]
                self.log("RETRY", f"Speeding up type-2 {func_name} (attempt {attempt+1}/{total_attempts}) with nonce={nonce}, bumped maxFee={max_fee_wei} wei, priorityFee={priority_fee_wei} wei")
                continue

    def handle_epoch_seed(self, cur_epoch, seed):
        if seed != 0:
            self.log("OK", "Epoch " + str(cur_epoch) + " seed already initialized: " + hex(seed)[:10] + "...")
            return True

        if self.get_epoch_seed(cur_epoch) != 0:
            self.log("OK", "Epoch " + str(cur_epoch) + " seed already initialized on-chain. Skipping.")
            return True

        min_e = self.find_earliest_uninitialized_epoch(cur_epoch)
        if min_e > cur_epoch:
            self.log("OK", "Epoch " + str(cur_epoch) + " seed already initialized. Skipping.")
            return True

        n = cur_epoch - min_e + 1
        if n <= 0:
            return True

        max_steps = min(int(getattr(self, "max_seed_steps", MAX_SEED_STEPS)), MAX_SEED_STEPS)
        if max_steps <= 0:
            max_steps = MAX_SEED_STEPS

        if min_e == cur_epoch:
            self.log("ACTION", "Epoch " + str(cur_epoch) + " seed uninitialized (0x0). Needs initEpochSeed.")
        else:
            self.log("ACTION", f"Epoch seeds uninitialized from {min_e} to {cur_epoch} (total: {n} epochs). Initializing in batches of up to {max_steps}...")

        if self.dry_run:
            estimated_txs = (n + max_steps - 1) // max_steps
            self.log(
                "DRY-RUN",
                f"Estimated transactions to send: {estimated_txs} "
                f"(calculated via ceil({n} / {max_steps}) with max_seed_steps={max_steps}; "
                f"simulation estimate only, on-chain state not checked)"
            )
            for step_idx in range(estimated_txs):
                calldata = "0x" + SELECTOR_INIT_EPOCH_SEED + encode_uint256(cur_epoch) + encode_uint256(max_steps)
                func_desc = f"initEpochSeed({cur_epoch}, {max_steps})"
                receipt = self.send_transaction(calldata, func_desc, func_desc)
                if not self._is_tx_success(receipt, allow_dry_run=True):
                    self.alert(f"Failed to simulate {func_desc}.")
                    return False
            return True

        max_allowed_calls = (n + max_steps - 1) // max_steps + 1
        calls_sent = 0
        prev_min_e = min_e

        while calls_sent < max_allowed_calls:
            if self.get_epoch_seed(cur_epoch) != 0:
                self.log("OK", f"Epoch {cur_epoch} seed is already initialized on-chain. Skipping.")
                return True

            jitter = random.uniform(0.0, 3.0)
            self.log("ACTION", f"Applying {jitter:.2f}s random jitter before sending initEpochSeed...")
            time.sleep(jitter)

            calldata = "0x" + SELECTOR_INIT_EPOCH_SEED + encode_uint256(cur_epoch) + encode_uint256(max_steps)
            func_desc = f"initEpochSeed({cur_epoch}, {max_steps})"
            receipt = self.send_transaction(calldata, func_desc, func_desc)
            calls_sent += 1

            if not self._is_tx_success(receipt, allow_dry_run=False):
                self.alert(f"Failed to confirm {func_desc}. Stopping seed initialization sequence.")
                return False

            if isinstance(receipt, (dict, TxResult)):
                logs = receipt.get("receipt", {}).get("logs", []) if isinstance(receipt, TxResult) else receipt.get("logs", [])
                for log in logs:
                    topics = log.get("topics", [])
                    if topics and topics[0].lower() == EVENT_EPOCH_SETTLED_TOPIC.lower():
                        self.log("EVENT", f"Observed EpochSettled event in tx {func_desc}")

            new_cur_seed = self.get_epoch_seed(cur_epoch)
            if new_cur_seed != 0:
                self.log("OK", f"Epoch {cur_epoch} seed successfully initialized: {hex(new_cur_seed)[:10]}...")
                return True

            new_min_e = self.find_earliest_uninitialized_epoch(cur_epoch)
            if new_min_e <= prev_min_e:
                self.alert(f"On-chain state did not advance after confirmed {func_desc} (earliest uninitialized still {new_min_e}, cur_epoch={cur_epoch} seed=0x0). Halting.")
                return False

            self.log("ACTION", f"Epoch seed initialization progressed: earliest uninitialized epoch advanced from {prev_min_e} to {new_min_e}.")
            prev_min_e = new_min_e
        else:
            if self.get_epoch_seed(cur_epoch) == 0:
                self.alert(f"Reached maximum allowed seed initialization calls ({max_allowed_calls}) without fully initializing epoch {cur_epoch}. Halting to prevent infinite loop.")
                return False
            return True
        return True

    def handle_sync_lag(self, cur_epoch, last_synced):
        if last_synced < cur_epoch:
            lag = cur_epoch - last_synced
            self.alert("Sync lag detected: lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + " (Lag = " + str(lag) + " epochs)!")
            steps = min(lag, 48)
            calldata = "0x" + SELECTOR_SYNC + encode_uint256(steps)
            func_desc = f"sync({steps})"
            receipt = self.send_transaction(calldata, func_desc, func_desc)
            if not self._is_tx_success(receipt, allow_dry_run=self.dry_run):
                self.alert(f"Failed to confirm {func_desc}.")
                return False
            return True
        else:
            self.log("OK", "Sync state up-to-date (lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + ").")
            return True

    def step(self):
        if getattr(self, "locked_for_manual_review", False):
            self.alert("Keeper is locked pending manual review. Broadcast refused.")
            return 1

        if not self.dry_run:
            sm = self._ensure_state_manager()
            if sm and sm.domain_valid:
                can_proceed, block_reason, state_data = sm.check_startup_protection()
                if not can_proceed:
                    st = state_data.get("status") if state_data else None
                    if st == "PREPARING_TO_SEND":
                        if not sm.acquire_lock():
                            self.log("INFO", "Another active Keeper process is currently broadcasting (PREPARING_TO_SEND with lock held). Skipping step.")
                            return 1
                        try:
                            updated_state = sm.recover_crash_to_manual_review(state_data)
                            self.locked_for_manual_review = True
                            self.manual_review_context = sm.build_context_from_state(updated_state)
                            self.alert("Keeper is locked pending manual review: crash before confirmation detected from previous run. Broadcast refused.")
                            return 1
                        finally:
                            sm.release_lock()
                    else:
                        self.locked_for_manual_review = True
                        if state_data:
                            self.manual_review_context = sm.build_context_from_state(state_data)
                        self.alert(f"Keeper is locked pending manual review: {block_reason}. Broadcast refused.")
                        return 1
        try:
            chain_id = self.get_chain_id()
            cur_epoch, last_synced, seed = self.get_epoch_state()
            seed_status = "Initialized" if seed != 0 else "UNINITIALIZED"
            if self.dry_run:
                self.log("DRY-RUN", f"[Context] Target Contract: {self.miner} | On-Chain Epoch: {cur_epoch} | Chain ID: {chain_id}")
            self.log("STATUS", "Current Epoch: " + str(cur_epoch) + " | LastSynced: " + str(last_synced) + " | Seed: " + seed_status)
            seed_ok = self.handle_epoch_seed(cur_epoch, seed)
            if getattr(self, "locked_for_manual_review", False) or seed_ok is not True:
                return 1
            sync_ok = self.handle_sync_lag(cur_epoch, last_synced)
            if getattr(self, "locked_for_manual_review", False) or sync_ok is not True:
                return 1
            return 0
        except Exception as e:
            self.alert("Failed during keeper inspection: " + format_exception_safe(e))
            return 1

    def run_forever(self):
        mode = "DRY-RUN (Simulated actions only)" if self.dry_run else "LIVE (Broadcasting transactions)"
        self.log("START", "Starting PoolCoin Keeper Bot [" + mode + "] targeting Miner " + self.miner)
        self.log("CONFIG", f"RPC URL: {sanitize_url(self.rpc_url)} | Poll: {self.poll_interval}s | Floor: {self.min_gas_price_gwei} gwei | Mult: {self.gas_multiplier}x | Timeout: {self.tx_timeout}s")
        while True:
            if getattr(self, "locked_for_manual_review", False):
                self.alert("Keeper is locked pending manual review. Halting automatic execution.")
                return 1
            step_code = self.step()
            if getattr(self, "locked_for_manual_review", False):
                self.alert("Keeper entered locked state during step. Halting automatic execution.")
                if isinstance(step_code, int) and not isinstance(step_code, bool) and step_code != 0:
                    return step_code
                return 1
            if not (isinstance(step_code, int) and not isinstance(step_code, bool) and step_code == 0):
                if isinstance(step_code, int) and not isinstance(step_code, bool) and step_code != 0:
                    return step_code
                return 1
            time.sleep(self.poll_interval)

def main():
    parser = argparse.ArgumentParser(
        description="PoolCoin Keeper Bot for MinerV1 on BSC Testnet (chainId 97)\n"
                    "Note: --no-env disables reading from file-based .env configuration files\n"
                    "(neither default .env nor custom --env-file will be loaded). It does not clear\n"
                    "existing process environment variables (os.environ). When --no-env is specified,\n"
                    "an explicit --rpc-url is required."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print transactions without broadcasting")
    parser.add_argument("--once", action="store_true", help="Run a single inspection pass and exit")
    parser.add_argument("--interval", type=int, default=None, help="Polling interval in seconds (default 15)")
    parser.add_argument("--rpc-url", default=None, help="Explicit RPC URL endpoint (takes precedence over RPC_URL in .env or os.environ)")
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="Do not load any .env file (including default .env and --env-file). "
             "Note: --no-env only disables loading from file-based .env configurations; "
             "it does NOT clear existing process environment variables (os.environ). "
             "When --no-env is specified, --rpc-url must be explicitly provided."
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env configuration file")
    parser.add_argument("--miner-address", default=None, help="Miner proxy contract address (overrides .env)")
    parser.add_argument("--keeper-address", default=None, help="Keeper public address for nonce queries (dry-run)")
    parser.add_argument("--account", default=None, help="Foundry keystore account name in ~/.foundry/keystores")
    parser.add_argument("--password-file", default=None, help="Path to password file for keystore decryption")
    parser.add_argument("--min-gas-price-gwei", type=float, default=None, help="Gas price floor in gwei (default: 0.1)")
    parser.add_argument("--gas-multiplier", type=str, default=None, help="Gas price multiplier (default: 1.5)")
    parser.add_argument("--max-fee-per-gas-gwei", type=str, default=None, help="Explicit EIP-1559 maxFeePerGas in gwei")
    parser.add_argument("--priority-fee-per-gas-gwei", type=str, default=None, help="Explicit EIP-1559 maxPriorityFeePerGas in gwei")
    parser.add_argument("--replacement-max-fee-per-gas-gwei", type=str, default=None, help="Explicit maximum maxFeePerGas in gwei allowed during automatic EIP-1559 replacement (required to enable replacement)")
    parser.add_argument("--replacement-max-priority-fee-per-gas-gwei", type=str, default=None, help="Explicit maximum maxPriorityFeePerGas in gwei allowed during automatic EIP-1559 replacement")
    parser.add_argument("--replacement-fee-bump-percent", type=int, default=None, help="Percentage bump for EIP-1559 replacement fees (default: 15)")
    parser.add_argument("--max-replacement-total-budget-wei", type=str, default=None, help="Explicit maximum total fee budget in wei for replacement (gas_limit * maxFeePerGas)")
    parser.add_argument("--tx-timeout", type=int, default=None, help="Tx confirmation timeout in seconds before speedup (default: 60)")
    parser.add_argument("--max-retries", type=int, default=None, help="Max replacement retries with identical nonce (default: 3)")
    parser.add_argument("--max-seed-steps", type=int, default=None, help=f"Max uninitialized epochs to process per initEpochSeed transaction (default: {MAX_SEED_STEPS}, max: {MAX_SEED_STEPS})")
    parser.add_argument("--state-dir", default=None, help="Directory for keeper persistent state and lock files (default: ~/.poolcoin/keeper_state)")
    parser.add_argument("--check-lock", action="store_true", help="Inspect persistent lock status for the configured domain and exit")
    parser.add_argument("--unlock-manual-review", action="store_true", help="Perform manual unlock for the configured domain")
    parser.add_argument("--unlock-confirm", default=None, help="Must specify 'YES' to confirm manual unlock")
    parser.add_argument("--unlock-reason", default=None, help="Mandatory explanation for manual unlock audit log")
    parser.add_argument("--expected-nonce", type=int, default=None, help="Optional expected nonce validation for manual unlock")
    args = parser.parse_args()

    if args.no_env:
        # Strictly forbid opening, parsing, or loading any .env file
        env = {}
        if not args.rpc_url:
            sys.stderr.write(
                "[ERROR] --no-env is specified but no explicit --rpc-url was provided. "
                "Refusing to fall back to ambient or default RPC configurations.\n"
            )
            sys.exit(1)
        rpc_url = args.rpc_url
    else:
        env = load_env(args.env_file)
        rpc_url = (
            args.rpc_url
            or env.get("RPC_URL")
            or os.environ.get("RPC_URL")
            or "https://data-seed-prebsc-1-s1.binance.org:8545"
        )

    def get_cfg(key):
        return env.get(key) or os.environ.get(key)

    miner_addr = (
        args.miner_address
        or get_cfg("MINER_ADDRESS")
        or get_cfg("MINER_PROXY_ADDRESS")
    )

    account_arg = args.account.strip() if args.account and isinstance(args.account, str) else None
    if args.account is not None:
        if not account_arg:
            sys.stderr.write("[ERROR] Startup check failed: Account name cannot be empty.\n")
            sys.exit(1)
        if (
            "/" in account_arg
            or "\\" in account_arg
            or ".." in account_arg
            or ":" in account_arg
            or account_arg in (".", "..")
            or os.path.basename(account_arg) != account_arg
            or os.path.splitdrive(account_arg)[0]
        ):
            sys.stderr.write("[ERROR] Startup check failed: Invalid account name. Directory paths and path traversal are not allowed.\n")
            sys.exit(1)

    if args.password_file is not None:
        if not isinstance(args.password_file, str) or not args.password_file.strip():
            sys.stderr.write("[ERROR] Startup check failed: Password file path cannot be empty.\n")
            sys.exit(1)
        if args.password_file != args.password_file.strip():
            sys.stderr.write("[ERROR] Startup check failed: Password file path cannot contain leading or trailing whitespace.\n")
            sys.exit(1)
        password_file_arg = args.password_file
        register_secret(password_file_arg)
        try:
            abs_arg = os.path.abspath(password_file_arg)
            if len(abs_arg) >= 4:
                register_secret(abs_arg)
        except Exception:
            pass
    else:
        password_file_arg = None

    if password_file_arg and not account_arg:
        sys.stderr.write("[ERROR] Startup check failed: --password-file cannot be used without --account.\n")
        sys.exit(1)

    if account_arg:
        configured_priv_key = get_cfg("KEEPER_PRIVATE_KEY") or get_cfg("PRIVATE_KEY")
        if configured_priv_key and str(configured_priv_key).strip():
            sys.stderr.write(
                "[ERROR] Conflicting signer configurations: both Foundry keystore --account and "
                "private key (KEEPER_PRIVATE_KEY / PRIVATE_KEY) are configured. Specify only one.\n"
            )
            sys.exit(1)

        if not args.dry_run and not password_file_arg:
            sys.stderr.write("[ERROR] Startup check failed: In live mode, --account requires --password-file to avoid interactive prompt delays.\n")
            sys.exit(1)
        priv_key = None
        is_dry_run = args.dry_run
    else:
        priv_key = None
        if not args.dry_run:
            priv_key = get_cfg("KEEPER_PRIVATE_KEY") or get_cfg("PRIVATE_KEY")
            if priv_key:
                register_secret(priv_key)
        if not priv_key and not args.dry_run:
            print("[WARN] No private key detected in environment; defaulting to --dry-run for safety.")
        is_dry_run = args.dry_run or not priv_key

    keeper_addr = args.keeper_address or get_cfg("KEEPER_ADDRESS")

    raw_min_floor = args.min_gas_price_gwei if args.min_gas_price_gwei is not None else get_cfg("KEEPER_MIN_GAS_PRICE_GWEI")
    min_floor = parse_numeric(raw_min_floor, float, "--min-gas-price-gwei / KEEPER_MIN_GAS_PRICE_GWEI")
    if min_floor is None:
        min_floor = DEFAULT_MIN_GAS_PRICE_GWEI

    raw_multiplier = args.gas_multiplier if args.gas_multiplier is not None else get_cfg("KEEPER_GAS_PRICE_MULTIPLIER")
    if args.gas_multiplier is not None:
        multiplier = args.gas_multiplier
    elif raw_multiplier is not None and str(raw_multiplier).strip() != "":
        multiplier = raw_multiplier
    else:
        multiplier = DEFAULT_GAS_MULTIPLIER

    if args.max_fee_per_gas_gwei is not None:
        max_fee_gwei = args.max_fee_per_gas_gwei
    else:
        raw_max_fee = get_cfg("KEEPER_MAX_FEE_PER_GAS_GWEI")
        max_fee_gwei = raw_max_fee if (raw_max_fee is not None and str(raw_max_fee).strip() != "") else None

    if args.priority_fee_per_gas_gwei is not None:
        priority_fee_gwei = args.priority_fee_per_gas_gwei
    else:
        raw_priority_fee = get_cfg("KEEPER_PRIORITY_FEE_PER_GAS_GWEI")
        priority_fee_gwei = raw_priority_fee if (raw_priority_fee is not None and str(raw_priority_fee).strip() != "") else None

    if args.replacement_max_fee_per_gas_gwei is not None:
        replacement_max_fee_gwei = args.replacement_max_fee_per_gas_gwei
    else:
        raw_rep_max = get_cfg("KEEPER_REPLACEMENT_MAX_FEE_PER_GAS_GWEI")
        replacement_max_fee_gwei = raw_rep_max if (raw_rep_max is not None and str(raw_rep_max).strip() != "") else None

    if args.replacement_max_priority_fee_per_gas_gwei is not None:
        replacement_max_priority_fee_gwei = args.replacement_max_priority_fee_per_gas_gwei
    else:
        raw_rep_prio = get_cfg("KEEPER_REPLACEMENT_MAX_PRIORITY_FEE_PER_GAS_GWEI")
        replacement_max_priority_fee_gwei = raw_rep_prio if (raw_rep_prio is not None and str(raw_rep_prio).strip() != "") else None

    raw_bump_pct = args.replacement_fee_bump_percent if args.replacement_fee_bump_percent is not None else get_cfg("KEEPER_REPLACEMENT_FEE_BUMP_PERCENT")
    bump_percent = parse_numeric(raw_bump_pct, int, "--replacement-fee-bump-percent / KEEPER_REPLACEMENT_FEE_BUMP_PERCENT")
    if bump_percent is None:
        bump_percent = 15

    raw_total_budget = args.max_replacement_total_budget_wei if args.max_replacement_total_budget_wei is not None else get_cfg("KEEPER_MAX_REPLACEMENT_TOTAL_BUDGET_WEI")
    total_budget_wei = parse_numeric(raw_total_budget, int, "--max-replacement-total-budget-wei / KEEPER_MAX_REPLACEMENT_TOTAL_BUDGET_WEI")

    raw_timeout = args.tx_timeout if args.tx_timeout is not None else get_cfg("KEEPER_TX_TIMEOUT")
    timeout = parse_numeric(raw_timeout, int, "--tx-timeout / KEEPER_TX_TIMEOUT")
    if timeout is None:
        timeout = DEFAULT_TX_TIMEOUT

    raw_retries = args.max_retries if args.max_retries is not None else get_cfg("KEEPER_MAX_RETRIES")
    retries = parse_numeric(raw_retries, int, "--max-retries / KEEPER_MAX_RETRIES")
    if retries is None:
        retries = DEFAULT_MAX_RETRIES

    raw_max_seed_steps = args.max_seed_steps if args.max_seed_steps is not None else get_cfg("KEEPER_MAX_SEED_STEPS")
    seed_steps = parse_numeric(raw_max_seed_steps, int, "--max-seed-steps / KEEPER_MAX_SEED_STEPS")
    if seed_steps is None:
        seed_steps = MAX_SEED_STEPS

    raw_interval = args.interval if args.interval is not None else get_cfg("KEEPER_POLL_INTERVAL")
    interval = parse_numeric(raw_interval, int, "--interval / KEEPER_POLL_INTERVAL")
    if interval is None:
        interval = 15

    bot = KeeperBot(
        rpc_url=rpc_url,
        miner_address=miner_addr,
        private_key=priv_key,
        keeper_address=keeper_addr,
        account=account_arg,
        password_file=password_file_arg,
        dry_run=is_dry_run,
        poll_interval=interval,
        min_gas_price_gwei=min_floor,
        gas_multiplier=multiplier,
        tx_timeout=timeout,
        max_retries=retries,
        max_seed_steps=seed_steps,
        state_dir=args.state_dir,
        max_fee_per_gas_gwei=max_fee_gwei,
        priority_fee_per_gas_gwei=priority_fee_gwei,
        replacement_max_fee_gwei=replacement_max_fee_gwei,
        replacement_max_priority_fee_gwei=replacement_max_priority_fee_gwei,
        replacement_fee_bump_percent=bump_percent,
        replacement_max_total_budget_wei=total_budget_wei,
    )

    if args.check_lock:
        sm = bot._ensure_state_manager()
        if not sm or not sm.domain_valid:
            sys.stderr.write("[ERROR] Cannot inspect lock: Unable to securely determine state domain (chainId, miner, sender).\n")
            sys.exit(1)
        status_cat, details, state_data = sm.inspect_lock_status()
        print(f"Domain: chainId={sm.domain.chain_id}, miner={sm.domain.miner_address}, sender={sm.domain.sender_address}")
        print(f"State file: {sm.get_state_path()}")
        print(f"Lock status: {status_cat} ({details})")
        if state_data:
            print(f"Details: {json.dumps(state_data, indent=2, ensure_ascii=False)}")
        if status_cat == "无锁":
            sys.exit(0)
        else:
            sys.exit(2)

    if args.unlock_manual_review:
        if args.unlock_confirm != "YES":
            sys.stderr.write("[ERROR] Manual unlock requires explicit confirmation: specify --unlock-confirm YES\n")
            sys.exit(1)
        if not args.unlock_reason or not args.unlock_reason.strip():
            sys.stderr.write("[ERROR] Manual unlock requires a non-empty audit reason: specify --unlock-reason '<reason>'\n")
            sys.exit(1)
        try:
            bot.manual_unlock(expected_nonce=args.expected_nonce, operator_reason=args.unlock_reason.strip())
            print(f"[SUCCESS] Manual review lock successfully unlocked for domain {bot.miner}. Audit record logged.")
            sys.exit(0)
        except Exception as e:
            sys.stderr.write(f"[ERROR] Manual unlock failed: {format_exception_safe(e)}\n")
            sys.exit(1)

    if args.once:
        exec_code = bot.step()
    else:
        exec_code = bot.run_forever()
    if isinstance(exec_code, int) and not isinstance(exec_code, bool):
        if exec_code == 0:
            return 0
        if 1 <= exec_code <= 255:
            return exec_code
    return 1

if __name__ == "__main__":
    sys.exit(main())
