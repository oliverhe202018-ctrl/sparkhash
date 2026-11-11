#!/usr/bin/env python3
"""Standalone safe output and log sanitization utilities for PoolCoin scripts."""

import os
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

_URL_IN_TEXT_RE = re.compile(r"""(?:https?|wss?)://[^\s'"<>]+""")

# Deliberate divergence from script/keeper.py:
# keeper.py relies solely on full-URL sanitization for ?token= and ?key=, and its kv_pattern
# misses ?token= / ?key= in partial URLs or bare query strings (and swallows &next=... on ?api_key=).
# Redact ?token=, ?key=, and ?api_key= query parameters cleanly up to `&` or delimiters.
_QUERY_SECRET_RE = re.compile(
    r"""(?i)([?&](?:api[_-]?key|token|key)=)"""
    r"""(?:(["'])((?:\\.|(?!\2).)*)\2|([^&\s'"<>)}\];,]+))"""
)

# Deliberate divergence from script/keeper.py:
# keeper.py only permits an optional `keeper[_-]?` prefix, which leaves env/config names like
# `MINER_PRIVATE_KEY=...`, `DEPLOYER_PRIVATE_KEY=...`, and `ETH_PRIVATE_KEY=...` unredacted.
# Allow arbitrary identifier prefixes ending in known secret field names (excluding `public_key`
# or generic `*_key` names unless matched by 64-hex rules below).
_KV_SECRET_RE = re.compile(
    r"""(?i)(["']?\b(?:[a-z0-9_-]*[_-])?(?:private[_-]?key|priv[_-]?key|privatekey|privkey|password|passwd|secret[_-]?key|secretkey|api[_-]?key|apikey|signing[_-]?key|secret)\b["']?\s*[:=]\s*)"""
    r"""(?:(["'])((?:\\.|(?!\2).)*)\2|([^'"\s,;}\]]+))"""
)

# Deliberate divergence from script/keeper.py:
# Redact `pk=...`, `*_pk=...`, and `key=...` strictly when assigned a 64-hex key value.
_KV_64HEX_KEY_RE = re.compile(
    r"""(?i)(["']?\b(?:(?:[a-z0-9_-]*[_-])?pk|key)\b["']?\s*[:=]\s*)"""
    r"""(?:(["'])(?:0x)?[0-9a-fA-F]{64}\2|(?:0x)?[0-9a-fA-F]{64}\b)"""
)

# Deliberate divergence from script/keeper.py:
# Support both `--flag value` and `--flag=value` syntax for CLI secret flags.
_CLI_PASSWORD_FILE_RE = re.compile(
    r"""(?i)(--password-file(?:\s+|=))(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)"""
)
_CLI_PASSWORD_RE = re.compile(
    r"""(?i)(--password(?:\s+|=))(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)"""
)
_CLI_PRIVATE_KEY_RE = re.compile(
    r"""(?i)(--private-key(?:\s+|=))(?:(["'])((?:\\.|(?!\2).)*)\2|[^\s]+)"""
)

_FOUNDRY_KEYSTORE_RE = re.compile(
    r"""(?i)(?:[a-zA-Z]:)?[\\/][^\s'"]*[\\/]\.foundry[\\/]keystores(?:[\\/][^\s'"]*)?"""
)

# Deliberate divergence from script/keeper.py:
# Catch space/phrase-delimited 64-hex private keys (e.g. "Signer private key is 0x...")
# even without `=`/`:` delimiters or prior secret registration.
_PHRASE_64HEX_PK_RE = re.compile(
    r"""(?i)(\b(?:[a-z0-9_-]*[_-])?(?:private[\s_-]+key|priv[\s_-]*key|privatekey|privkey|secret[\s_-]+key|signing[\s_-]+key)\b"""
    r"""(?:\s+(?:is|was|for|[:=]))?\s+)"""
    r"""(["']?)(?:0x)?[0-9a-fA-F]{64}\b\2"""
)

# Deliberate divergence from script/keeper.py:
# Catch bare unprefixed 64-hex strings (without `0x`), unless preceded by a non-secret
# hash/tx/seed/digest label. `0x`-prefixed 64-hex strings without private-key context remain
# unredacted so Ethereum txHash, blockHash, topic, and seed values are never corrupted.
_BARE_64HEX_RE = re.compile(r"(?<![0-9a-fA-FxX])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_NON_SECRET_HEX_LABEL_RE = re.compile(
    r"""(?i)(?:^|[^a-z0-9])(?:[a-z0-9_-]*[_-])?(?:tx[_-]?hash|tx|transaction(?:[_-]?hash)?|block[_-]?hash|seed|topic|digest|sha[_-]?256|keccak(?:256)?|merkle|root|checksum|hash)(?:\s*[:=]\s*|\s+(?:is\s+)?|[_-])?["']?$"""
)
_FULL_64HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$", re.IGNORECASE)


def sanitize_url(url: str) -> str:
    """
    Sanitize an RPC/HTTP/WS URL for safe logging and display:
    - Removes username, password, path, query string, and fragment.
    - Preserves scheme and hostname (with port if present).
    - Returns "<redacted-url>" when parsing fails or URL is empty/invalid.
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


def _collect_secrets(sensitive_values: Any, known_secrets: Any) -> set:
    secrets_to_mask = set()
    for source in (sensitive_values, known_secrets):
        if not source:
            continue
        items = source if isinstance(source, (list, tuple, set, frozenset)) else (source,)
        for item in items:
            if isinstance(item, os.PathLike):
                item = os.fspath(item)
            if not isinstance(item, str):
                continue
            clean_secret = item.strip()
            if len(clean_secret) < 4:
                continue
            secrets_to_mask.add(clean_secret)
            if "/" in clean_secret or "\\" in clean_secret:
                secrets_to_mask.add(clean_secret.replace("\\", "/"))
                secrets_to_mask.add(clean_secret.replace("/", "\\"))
            # Deliberate divergence from script/keeper.py:
            # For explicit 64-hex private keys passed via sensitive_values, mask both
            # 0x-prefixed and unprefixed representations (keeper.py only did this in register_secret).
            if _FULL_64HEX_RE.fullmatch(clean_secret):
                if clean_secret.lower().startswith("0x"):
                    secrets_to_mask.add(clean_secret[2:])
                else:
                    secrets_to_mask.add("0x" + clean_secret)
    return secrets_to_mask


def sanitize_text(text: str, sensitive_values: Any = None, known_secrets: Any = None) -> str:
    """
    Sanitize text for safe console, file, and error output.

    Deliberate differences from script/keeper.py:
    - Stateless: does not rely on a module-global _CONFIGURED_SECRETS registry; pass
      caller secrets via sensitive_values (or known_secrets for signature parity).
    - Redacts ?token= / ?key= / ?api_key= query parameters even outside full URLs
      without swallowing subsequent `&param=...` fields.
    - Redacts arbitrary-prefix *_PRIVATE_KEY=... / *_SECRET_KEY=... env vars, 64-hex
      pk=/key= assignments, phrase-form private keys, and bare unprefixed 64-hex keys
      (while preserving 0x-prefixed txHash/blockHash/topic/seed values).
    - Supports `--flag=value` CLI flags and remains idempotent on already-redacted text.
    """
    if not text or not isinstance(text, str):
        return ""

    def _replace_url(match: re.Match) -> str:
        raw_url = match.group(0)
        trail = ""
        punctuation = (".", ",", ";", ":", "!", "?", ")", "'", "\"")
        while raw_url and raw_url[-1] in punctuation:
            trail = raw_url[-1] + trail
            raw_url = raw_url[:-1]
        return sanitize_url(raw_url) + trail

    sanitized = _URL_IN_TEXT_RE.sub(_replace_url, text)

    secrets_to_mask = _collect_secrets(sensitive_values, known_secrets)
    for secret in sorted(secrets_to_mask, key=len, reverse=True):
        if not secret or len(secret) < 4:
            continue
        placeholder = "0x[REDACTED_KEY]" if secret.startswith(("0x", "0X")) else "[REDACTED_KEY]"
        sanitized = re.sub(re.escape(secret), placeholder, sanitized, flags=re.IGNORECASE)

    def _replace_query_param(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2) or ""
        raw_val = match.group(3) if quote else (match.group(4) or "")
        if raw_val.startswith(("[REDACTED", "0x[REDACTED")):
            return match.group(0)
        placeholder = "[REDACTED_KEY]" if "key" in prefix.lower() else "[REDACTED]"
        return f"{prefix}{quote}{placeholder}{quote}"

    sanitized = _QUERY_SECRET_RE.sub(_replace_query_param, sanitized)

    def _replace_kv(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2) or ""
        raw_val = match.group(3) if quote else (match.group(4) or "")
        # Preserve already-redacted placeholders (fixes keeper.py double-redaction on re-runs).
        if raw_val.startswith(("[REDACTED", "0x[REDACTED")):
            return match.group(0)
        placeholder = "[REDACTED_KEY]" if "key" in prefix.lower() else "[REDACTED]"
        return f"{prefix}{quote}{placeholder}{quote}"

    sanitized = _KV_SECRET_RE.sub(_replace_kv, sanitized)

    def _replace_kv_64hex(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2) or ""
        return f"{prefix}{quote}[REDACTED_KEY]{quote}"

    sanitized = _KV_64HEX_KEY_RE.sub(_replace_kv_64hex, sanitized)

    sanitized = _CLI_PASSWORD_FILE_RE.sub(r"\1[REDACTED_PATH]", sanitized)
    sanitized = _CLI_PASSWORD_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _CLI_PRIVATE_KEY_RE.sub(r"\1[REDACTED_KEY]", sanitized)
    sanitized = _FOUNDRY_KEYSTORE_RE.sub("[REDACTED_KEYSTORE_PATH]", sanitized)

    def _replace_phrase_pk(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2) or ""
        return f"{prefix}{quote}[REDACTED_KEY]{quote}"

    sanitized = _PHRASE_64HEX_PK_RE.sub(_replace_phrase_pk, sanitized)

    def _replace_bare_64hex(match: re.Match) -> str:
        prefix_window = sanitized[max(0, match.start() - 40):match.start()]
        if _NON_SECRET_HEX_LABEL_RE.search(prefix_window):
            return match.group(0)
        return "[REDACTED_KEY]"

    sanitized = _BARE_64HEX_RE.sub(_replace_bare_64hex, sanitized)
    return sanitized


def format_exception_safe(exc: Any, sensitive_values: Any = None) -> str:
    """
    Format an exception as "<ExceptionType>: <sanitized message>" without leaking raw URLs,
    subprocess command arguments, credentials, or caller-provided sensitive values.

    Deliberate differences from script/keeper.py:
    - Accepts optional `sensitive_values` directly so callers do not need to wrap calls in
      a second `sanitize_text(...)`.
    - Guards empty whitespace `SubprocessError.cmd` and `None` `__module__` attributes.
    """
    if exc is None:
        return ""
    if not isinstance(exc, Exception):
        return sanitize_text(str(exc), sensitive_values=sensitive_values)
    exc_type = type(exc).__name__
    if isinstance(exc, (subprocess.SubprocessError, FileNotFoundError)):
        cmd_name = "cast"
        if hasattr(exc, "cmd") and exc.cmd:
            if isinstance(exc.cmd, (list, tuple)):
                cmd_name = str(exc.cmd[0]) if len(exc.cmd) > 0 else "cast"
            else:
                cmd_parts = str(exc.cmd).split()
                cmd_name = cmd_parts[0] if cmd_parts else "cast"
        cmd_name = sanitize_text(cmd_name, sensitive_values=sensitive_values) or "cast"
        returncode = getattr(exc, "returncode", None)
        suffix = f", returncode={returncode}" if returncode is not None else ""
        return f"{exc_type}({cmd_name}{suffix})"
    if hasattr(exc, "method") and hasattr(exc, "error_obj"):
        method = sanitize_text(str(getattr(exc, "method", "rpc")), sensitive_values=sensitive_values)
        err_obj = getattr(exc, "error_obj", None)
        if isinstance(err_obj, dict):
            code = err_obj.get("code")
            msg = err_obj.get("message", "")
            safe_msg = sanitize_text(str(msg), sensitive_values=sensitive_values)
            return f"RpcError({method}): code={code} message={safe_msg}"
        return f"RpcError({method}): {sanitize_text(str(err_obj), sensitive_values=sensitive_values)}"
    if hasattr(exc, "code") and hasattr(exc, "reason"):
        safe_reason = sanitize_text(str(exc.reason), sensitive_values=sensitive_values)
        return f"HTTPError({exc.code}): {safe_reason}"
    if hasattr(exc, "reason"):
        reason = getattr(exc, "reason")
        reason_str = type(reason).__name__ if isinstance(reason, Exception) else str(reason)
        return f"URLError: {sanitize_text(reason_str, sensitive_values=sensitive_values)}"
    is_requests_exc = any(
        "requests" in (getattr(cls, "__module__", "") or "") for cls in type(exc).__mro__
    ) or hasattr(exc, "request")
    if is_requests_exc:
        resp = getattr(exc, "response", None)
        status_code = getattr(resp, "status_code", None) if resp is not None else None
        if status_code:
            return f"{exc_type}: HTTP status {status_code}"
        return f"{exc_type}: [network request failed]"
    msg = sanitize_text(str(exc), sensitive_values=sensitive_values)
    return f"{exc_type}: {msg}"
