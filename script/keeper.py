#!/usr/bin/env python3
"""
PoolCoin Keeper Bot for MinerV1 on BSC Testnet (chainId 97)
Monitors MinerV1 state:
- Automatically calls initEpochSeed sequentially from min uninitialized epoch to cur_epoch
- Preflights calls with eth_call and decodes revert error names before sending
- Automatically calls sync(steps) when lastSynced < cur_epoch
Includes gas price floor (>=0.1 gwei), multiplier, and replacement (speed-up) retry with identical nonce.
"""

import os, sys, time, json, argparse, urllib.request, urllib.error, re, random
from urllib.parse import urlsplit

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
    Registers a known secret/private key for sanitization in logs and outputs.
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

    secrets_to_mask = set(_CONFIGURED_SECRETS)
    if sensitive_values:
        if isinstance(sensitive_values, (list, tuple, set)):
            secrets_to_mask.update(s.strip() for s in sensitive_values if isinstance(s, str) and len(s.strip()) >= 4)
        elif isinstance(sensitive_values, str) and len(sensitive_values.strip()) >= 4:
            secrets_to_mask.add(sensitive_values.strip())
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
            err_body = e.read().decode("utf-8")
            res = json.loads(err_body)
        except Exception:
            raise RuntimeError(f"HTTP Error {e.code}: {e.reason}") from None
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
                 dry_run=True, poll_interval=15, min_gas_price_gwei=DEFAULT_MIN_GAS_PRICE_GWEI,
                 gas_multiplier=DEFAULT_GAS_MULTIPLIER, tx_timeout=DEFAULT_TX_TIMEOUT,
                 max_retries=DEFAULT_MAX_RETRIES, max_seed_steps=24):
        self.rpc_url = rpc_url
        self.miner = miner_address.strip().lower() if miner_address and isinstance(miner_address, str) else (miner_address or "")
        self.dry_run = dry_run
        # In dry-run mode, keeper does not actively retrieve, register, or retain private keys or signing material.
        # (Keeper does not claim to wipe caller-owned strings in caller memory, but guarantees dry-run will not store or register it).
        self.private_key = None if self.dry_run else private_key
        self._known_secrets = set()
        if not self.dry_run and private_key and isinstance(private_key, str) and len(private_key.strip()) >= 4:
            clean_k = private_key.strip()
            self._known_secrets.add(clean_k)
            register_secret(clean_k)

        # Validate numeric parameters without leaking raw configuration values
        try:
            self.min_gas_price_gwei = float(min_gas_price_gwei)
            if self.min_gas_price_gwei < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: min_gas_price_gwei must be a non-negative number.")
            sys.exit(1)

        try:
            self.gas_multiplier = float(gas_multiplier)
            if self.gas_multiplier < 0:
                raise ValueError("negative")
        except Exception:
            self.alert("Startup check failed: gas_multiplier must be a non-negative number.")
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

        self.max_seed_steps = max_seed_steps
        self.current_nonce = None
        self._chain_id = None
        self.current_epoch = None

        # Validate miner contract address immediately at startup
        self.validate_miner()

        # Derive or assign keeper public address for nonce queries and validate identity
        self.validate_identity(keeper_address)

    def validate_identity(self, explicit_keeper_address=None):
        """
        Validates signer identity:
        - In dry-run mode: uses explicit keeper_address if provided (for simulated nonce queries),
          does not derive or require private key.
        - In live mode: requires a private key, derives signing address from private key.
          If private key is invalid, fails cleanly without leaking raw secret.
          If explicit keeper_address was provided and mismatches derived address, rejects cleanly
          before any nonce query or broadcast.
        """
        if self.dry_run:
            if explicit_keeper_address and str(explicit_keeper_address).strip():
                self.keeper_address = to_checksum_address(str(explicit_keeper_address).strip())
            else:
                self.keeper_address = None
            return

        if not self.private_key:
            reason = "Private key is required for live execution mode (or run with --dry-run)."
            self.alert(f"Startup check failed: {reason}")
            sys.exit(1)

        try:
            from eth_account import Account
            signer_account = Account.from_key(self.private_key)
            derived_address = signer_account.address
        except Exception as e:
            reason = f"Invalid private key provided for live mode: {type(e).__name__}"
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

    def get_nonce(self):
        """Fetches pending nonce for keeper account, or returns 0 if address unavailable."""
        remote_nonce = 0
        if not self.dry_run:
            self.validate_identity(self.keeper_address)
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

    def is_receipt_successful(self, receipt):
        if not receipt:
            return False
        if isinstance(receipt, dict):
            status = receipt.get("status")
            return status in (1, "0x1", "1", True)
        return bool(receipt)

    def send_transaction(self, calldata, desc, func_name=None, preflight=True):
        if func_name is None:
            func_name = desc

        if preflight:
            ok, err_name = self.preflight_check(calldata, func_name)
            if not ok:
                self.log("ACTION", f"Skipping {func_name}: preflight simulation failed ({err_name})")
                return None

        gas_price = self.get_gas_price()
        nonce = self.get_nonce()

        if self.dry_run:
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
            self.log("DRY-RUN", f"  Gas Price:       {gas_price / 1e9:.4f} gwei ({gas_price} wei) [Multiplier: {self.gas_multiplier}x, Floor: {self.min_gas_price_gwei} gwei]")
            self.log("DRY-RUN", f"  Nonce:           {nonce}{' (simulated)' if not self.keeper_address else ''}")
            self.log("DRY-RUN", f"  Calldata:        {calldata}")
            self.current_nonce = nonce + 1
            return {"status": 1, "dry_run": True}

        if not self.dry_run:
            self.validate_identity(self.keeper_address)

        if not self.private_key:
            self.alert("Cannot broadcast " + desc + ": No private key provided in environment!")
            return None

        try:
            from eth_account import Account
        except ImportError:
            self.alert("Cannot broadcast " + desc + ": eth_account package not installed!")
            return None

        # Fetch chainId
        try:
            cid_res = rpc_call(self.rpc_url, "eth_chainId", [])
            chain_id = int(cid_res, 16) if isinstance(cid_res, str) and cid_res.startswith("0x") else int(cid_res)
        except Exception:
            chain_id = 97

        gas_limit = self.estimate_gas_limit(calldata, default_gas=300000)
        target_addr = to_checksum_address(self.miner)

        current_gas_price = gas_price
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                net_price = self.get_network_gas_price()
                current_gas_price = get_bumped_gas_price(
                    current_gas_price,
                    min_bump_ratio=1.15,
                    network_gas_price_wei=net_price,
                    min_gas_price_gwei=self.min_gas_price_gwei,
                    multiplier=self.gas_multiplier
                )
                self.log("RETRY", f"Speed-up replacement attempt #{attempt} for {func_name} with IDENTICAL nonce {nonce}, bumped gasPrice: {current_gas_price / 1e9:.4f} gwei ({current_gas_price} wei)")

            tx_dict = {
                "to": target_addr,
                "value": 0,
                "gas": gas_limit,
                "gasPrice": current_gas_price,
                "nonce": nonce,
                "chainId": chain_id,
                "data": calldata
            }

            signed = Account.sign_transaction(tx_dict, self.private_key)
            raw_bytes = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            raw_hex = "0x" + raw_bytes.hex()

            try:
                tx_hash = rpc_call(self.rpc_url, "eth_sendRawTransaction", [raw_hex])
                self.log("TX", f"Broadcasted {func_name} txHash={tx_hash} (nonce={nonce}, gasPrice={current_gas_price / 1e9:.4f} gwei, attempt={attempt})")
            except Exception as e:
                err_str = format_exception_safe(e)
                if "nonce too low" in err_str.lower() or "already known" in err_str.lower():
                    self.log("WARN", f"RPC response: {err_str}; checking if already mined...")
                else:
                    self.alert(f"eth_sendRawTransaction failed on attempt {attempt}: {err_str}")

            # Wait for receipt
            receipt = self.wait_for_receipt(tx_hash, self.tx_timeout)
            if receipt:
                status = receipt.get("status")
                blk = int(receipt.get("blockNumber", "0x0"), 16)
                used = int(receipt.get("gasUsed", "0x0"), 16)
                if status == "0x1" or status == 1:
                    self.log("SUCCESS", f"Transaction {tx_hash} CONFIRMED in block {blk}! Gas used: {used}")
                    self.current_nonce = nonce + 1
                    return receipt
                else:
                    self.alert(f"Transaction {tx_hash} REVERTED in block {blk}!")
                    self.current_nonce = nonce + 1
                    return receipt

            self.log("TIMEOUT", f"Transaction {tx_hash} not confirmed within {self.tx_timeout}s. Retrying with identical nonce {nonce}...")

        self.alert(f"All {self.max_retries} speed-up attempts exhausted for {func_name} with nonce {nonce}!")
        self.current_nonce = None
        return None

    def handle_epoch_seed(self, cur_epoch, seed):
        if seed != 0:
            self.log("OK", "Epoch " + str(cur_epoch) + " seed already initialized: " + hex(seed)[:10] + "...")
            return

        # Find earliest uninitialized epoch e <= cur_epoch
        min_e = cur_epoch
        for e in range(cur_epoch - 1, -1, -1):
            s = self.get_epoch_seed(e)
            if s == 0:
                min_e = e
            else:
                break

        if min_e == cur_epoch:
            self.log("ACTION", "Epoch " + str(cur_epoch) + " seed uninitialized (0x0). Needs initEpochSeed.")
        else:
            self.log("ACTION", f"Epoch seeds uninitialized from {min_e} to {cur_epoch}. Initializing sequentially...")

        for e in range(min_e, cur_epoch + 1):
            if not self.dry_run:
                jitter = random.uniform(0.0, 3.0)
                self.log("ACTION", f"Applying {jitter:.2f}s random jitter before sending initEpochSeed...")
                time.sleep(jitter)
            calldata = "0x" + SELECTOR_INIT_EPOCH_SEED + encode_uint256(e) + encode_uint256(self.max_seed_steps)
            func_desc = f"initEpochSeed({e}, {self.max_seed_steps})"
            receipt = self.send_transaction(calldata, func_desc, func_desc)
            if not self.is_receipt_successful(receipt):
                self.alert(f"Failed to confirm {func_desc}. Stopping seed initialization sequence.")
                break
            if receipt and isinstance(receipt, dict):
                for log in receipt.get("logs", []):
                    topics = log.get("topics", [])
                    if topics and topics[0].lower() == EVENT_EPOCH_SETTLED_TOPIC.lower():
                        self.log("EVENT", f"Observed EpochSettled event in tx {func_desc}")

    def handle_sync_lag(self, cur_epoch, last_synced):
        if last_synced < cur_epoch:
            lag = cur_epoch - last_synced
            self.alert("Sync lag detected: lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + " (Lag = " + str(lag) + " epochs)!")
            steps = min(lag, 48)
            calldata = "0x" + SELECTOR_SYNC + encode_uint256(steps)
            func_desc = f"sync({steps})"
            self.send_transaction(calldata, func_desc, func_desc)
        else:
            self.log("OK", "Sync state up-to-date (lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + ").")

    def step(self):
        try:
            chain_id = self.get_chain_id()
            cur_epoch, last_synced, seed = self.get_epoch_state()
            seed_status = "Initialized" if seed != 0 else "UNINITIALIZED"
            if self.dry_run:
                self.log("DRY-RUN", f"[Context] Target Contract: {self.miner} | On-Chain Epoch: {cur_epoch} | Chain ID: {chain_id}")
            self.log("STATUS", "Current Epoch: " + str(cur_epoch) + " | LastSynced: " + str(last_synced) + " | Seed: " + seed_status)
            self.handle_epoch_seed(cur_epoch, seed)
            self.handle_sync_lag(cur_epoch, last_synced)
        except Exception as e:
            self.alert("Failed during keeper inspection: " + format_exception_safe(e))

    def run_forever(self):
        mode = "DRY-RUN (Simulated actions only)" if self.dry_run else "LIVE (Broadcasting transactions)"
        self.log("START", "Starting PoolCoin Keeper Bot [" + mode + "] targeting Miner " + self.miner)
        self.log("CONFIG", f"RPC URL: {sanitize_url(self.rpc_url)} | Poll: {self.poll_interval}s | Floor: {self.min_gas_price_gwei} gwei | Mult: {self.gas_multiplier}x | Timeout: {self.tx_timeout}s")
        while True:
            self.step()
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
    parser.add_argument("--min-gas-price-gwei", type=float, default=None, help="Gas price floor in gwei (default: 0.1)")
    parser.add_argument("--gas-multiplier", type=float, default=None, help="Gas price multiplier (default: 1.5)")
    parser.add_argument("--tx-timeout", type=int, default=None, help="Tx confirmation timeout in seconds before speedup (default: 60)")
    parser.add_argument("--max-retries", type=int, default=None, help="Max replacement retries with identical nonce (default: 3)")
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
    priv_key = None
    if not args.dry_run:
        priv_key = get_cfg("KEEPER_PRIVATE_KEY") or get_cfg("PRIVATE_KEY")
        if priv_key:
            register_secret(priv_key)

    is_dry_run = args.dry_run or not priv_key
    if not priv_key and not args.dry_run:
        print("[WARN] No private key detected in environment; defaulting to --dry-run for safety.")

    keeper_addr = args.keeper_address or get_cfg("KEEPER_ADDRESS")

    raw_min_floor = args.min_gas_price_gwei if args.min_gas_price_gwei is not None else get_cfg("KEEPER_MIN_GAS_PRICE_GWEI")
    min_floor = parse_numeric(raw_min_floor, float, "--min-gas-price-gwei / KEEPER_MIN_GAS_PRICE_GWEI")
    if min_floor is None:
        min_floor = DEFAULT_MIN_GAS_PRICE_GWEI

    raw_multiplier = args.gas_multiplier if args.gas_multiplier is not None else get_cfg("KEEPER_GAS_PRICE_MULTIPLIER")
    multiplier = parse_numeric(raw_multiplier, float, "--gas-multiplier / KEEPER_GAS_PRICE_MULTIPLIER")
    if multiplier is None:
        multiplier = DEFAULT_GAS_MULTIPLIER

    raw_timeout = args.tx_timeout if args.tx_timeout is not None else get_cfg("KEEPER_TX_TIMEOUT")
    timeout = parse_numeric(raw_timeout, int, "--tx-timeout / KEEPER_TX_TIMEOUT")
    if timeout is None:
        timeout = DEFAULT_TX_TIMEOUT

    raw_retries = args.max_retries if args.max_retries is not None else get_cfg("KEEPER_MAX_RETRIES")
    retries = parse_numeric(raw_retries, int, "--max-retries / KEEPER_MAX_RETRIES")
    if retries is None:
        retries = DEFAULT_MAX_RETRIES

    raw_interval = args.interval if args.interval is not None else get_cfg("KEEPER_POLL_INTERVAL")
    interval = parse_numeric(raw_interval, int, "--interval / KEEPER_POLL_INTERVAL")
    if interval is None:
        interval = 15

    bot = KeeperBot(
        rpc_url=rpc_url,
        miner_address=miner_addr,
        private_key=priv_key,
        keeper_address=keeper_addr,
        dry_run=is_dry_run,
        poll_interval=interval,
        min_gas_price_gwei=min_floor,
        gas_multiplier=multiplier,
        tx_timeout=timeout,
        max_retries=retries
    )

    if args.once:
        bot.step()
    else:
        bot.run_forever()

if __name__ == "__main__":
    main()
