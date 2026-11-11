"""
Unit tests for script/safe_output.py and parity/divergence checks against script/keeper.py.
"""

import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from script import keeper
from script import safe_output


SAMPLE_ADDRESS = "0x1234567890AbcdEF1234567890aBcdef12345678"
SAMPLE_TX_HASH = "0x" + "9f8e7d6c" * 8
SAMPLE_BLOCK_HASH = "0x" + "1a2b3c4d" * 8
SAMPLE_SEED_HEX = "0x" + "5e6f7a8b" * 8
SAMPLE_PK_RAW = "a1b2c3d4" * 8
SAMPLE_PK_0X = "0x" + SAMPLE_PK_RAW


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeRequestsException(Exception):
    __module__ = "requests.exceptions"

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.request = object()
        self.response = _FakeResponse(status_code) if status_code is not None else None


class TestSafeOutputIndependence(unittest.TestCase):
    def test_does_not_import_keeper(self):
        module_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "script", "safe_output.py")
        )
        with open(module_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=module_path)

        imported_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.append(node.module)
                for alias in node.names:
                    imported_names.append(alias.name)

        for name in imported_names:
            self.assertNotIn("keeper", name.lower())


class TestSanitizeUrl(unittest.TestCase):
    def test_preserves_scheme_host_and_port(self):
        self.assertEqual(
            safe_output.sanitize_url("https://bsc-testnet.example.com"),
            "https://bsc-testnet.example.com",
        )
        self.assertEqual(
            safe_output.sanitize_url("https://bsc-testnet.example.com/"),
            "https://bsc-testnet.example.com",
        )
        self.assertEqual(
            safe_output.sanitize_url("http://127.0.0.1:8545"),
            "http://127.0.0.1:8545",
        )

    def test_strips_credentials_path_query_and_fragment(self):
        self.assertEqual(
            safe_output.sanitize_url(
                "https://alice:s3cr3t@rpc.example.com:8545/v1/mykey?token=abc&api_key=xyz#sec"
            ),
            "https://rpc.example.com:8545/***",
        )
        self.assertEqual(
            safe_output.sanitize_url("wss://user:pass@ws.example.com/ws?key=secret"),
            "wss://ws.example.com/***",
        )
        self.assertEqual(
            safe_output.sanitize_url("https://user@rpc.example.com"),
            "https://rpc.example.com/***",
        )

    def test_invalid_or_empty_urls_return_redacted_placeholder(self):
        for bad_url in ("", None, "not-a-url", "http://:8545", "https://host:99999/v1"):
            self.assertEqual(safe_output.sanitize_url(bad_url), "<redacted-url>")


class TestSanitizeTextSensitivePatterns(unittest.TestCase):
    def test_embedded_urls_with_credentials_and_query_secrets(self):
        raw_text = (
            "Dial failed on https://bob:topsecret@node.example.com:8545/v2/proj?api_key=k123, "
            "fallback wss://u:p@ws.example.com/stream?token=t456."
        )
        cleaned = safe_output.sanitize_text(raw_text)
        self.assertEqual(
            cleaned,
            "Dial failed on https://node.example.com:8545/***, fallback wss://ws.example.com/***.",
        )
        self.assertNotIn("topsecret", cleaned)
        self.assertNotIn("k123", cleaned)
        self.assertNotIn("t456", cleaned)

    def test_partial_url_query_params_token_key_api_key(self):
        text_token = "GET /v1/rpc?token=tok_abc_123&epoch=7"
        text_key = "GET /v1/rpc?key=key_abc_123&epoch=7"
        text_api_key = "GET /v1/rpc?foo=1&api_key=ak_abc_123&epoch=7"

        self.assertEqual(
            safe_output.sanitize_text(text_token),
            "GET /v1/rpc?token=[REDACTED]&epoch=7",
        )
        self.assertEqual(
            safe_output.sanitize_text(text_key),
            "GET /v1/rpc?key=[REDACTED_KEY]&epoch=7",
        )
        self.assertEqual(
            safe_output.sanitize_text(text_api_key),
            "GET /v1/rpc?foo=1&api_key=[REDACTED_KEY]&epoch=7",
        )

    def test_64hex_private_key_forms(self):
        samples = [
            f"private_key={SAMPLE_PK_0X}",
            f"KEEPER_PRIVATE_KEY={SAMPLE_PK_0X}",
            f"MINER_PRIVATE_KEY={SAMPLE_PK_0X}",
            f"DEPLOYER_PRIVATE_KEY={SAMPLE_PK_RAW}",
            f"pk={SAMPLE_PK_0X}",
            f"key={SAMPLE_PK_0X}",
            f"cast send --private-key {SAMPLE_PK_0X}",
            f"cast send --private-key={SAMPLE_PK_0X}",
            f"Signer private key is {SAMPLE_PK_0X}",
            f"raw secret {SAMPLE_PK_RAW}",
            SAMPLE_PK_RAW,
        ]
        for sample in samples:
            cleaned = safe_output.sanitize_text(sample)
            self.assertNotIn(SAMPLE_PK_RAW, cleaned, msg=f"Leaked key in: {sample}")
            self.assertNotIn(SAMPLE_PK_0X, cleaned, msg=f"Leaked 0x key in: {sample}")
            self.assertIn("[REDACTED_KEY]", cleaned, msg=f"Missing placeholder for: {sample}")

    def test_caller_sensitive_values_including_password_file_paths(self):
        win_path = r"C:\Users\operator\.secrets\keeper+pw[1].txt"
        posix_path = "/home/operator/.secrets/keeper_pw.txt"
        text_msg = (
            "Failed reading C:/Users/operator/.secrets/keeper+pw[1].txt "
            f"and {posix_path} with key {SAMPLE_PK_0X}"
        )
        cleaned = safe_output.sanitize_text(
            text_msg,
            sensitive_values=[win_path, posix_path, SAMPLE_PK_0X, None, "", "ab"],
        )
        self.assertNotIn("operator", cleaned)
        self.assertNotIn("keeper+pw", cleaned)
        self.assertNotIn(SAMPLE_PK_RAW, cleaned)
        self.assertIn("[REDACTED_KEY]", cleaned)

        single_cleaned = safe_output.sanitize_text(
            f"Cannot open {posix_path}",
            sensitive_values=posix_path,
        )
        self.assertEqual(single_cleaned, "Cannot open [REDACTED_KEY]")

        path_obj_cleaned = safe_output.sanitize_text(
            f"Cannot open {posix_path}",
            sensitive_values=[Path(posix_path)],
        )
        self.assertEqual(path_obj_cleaned, "Cannot open [REDACTED_KEY]")

    def test_cli_flags_and_foundry_keystore_paths(self):
        raw_cmd = (
            "cast wallet address --password-file /etc/pool/pw.txt "
            "--password-file=/etc/pool/pw2.txt --password mysecret "
            r"keystore=C:\Users\dev\.foundry\keystores\deployer"
        )
        cleaned = safe_output.sanitize_text(raw_cmd)
        self.assertIn("--password-file [REDACTED_PATH]", cleaned)
        self.assertIn("--password-file=[REDACTED_PATH]", cleaned)
        self.assertIn("--password [REDACTED]", cleaned)
        self.assertIn("[REDACTED_KEYSTORE_PATH]", cleaned)
        self.assertNotIn("pw.txt", cleaned)
        self.assertNotIn("pw2.txt", cleaned)
        self.assertNotIn("mysecret", cleaned)
        self.assertNotIn("deployer", cleaned)


class TestSanitizeTextNoFalsePositives(unittest.TestCase):
    def test_preserves_contract_address(self):
        text_msg = f"MinerV1 contract address={SAMPLE_ADDRESS} verified on chainId=97"
        self.assertEqual(safe_output.sanitize_text(text_msg), text_msg)
        key_addr_msg = f"key={SAMPLE_ADDRESS} token={SAMPLE_ADDRESS}"
        self.assertEqual(safe_output.sanitize_text(key_addr_msg), key_addr_msg)

    def test_preserves_tx_hash_and_chain_hashes(self):
        samples = [
            f"txHash={SAMPLE_TX_HASH}",
            f"tx_hash: {SAMPLE_TX_HASH}",
            f"Confirmed tx {SAMPLE_TX_HASH} in block {SAMPLE_BLOCK_HASH}",
            f"epoch_seed={SAMPLE_SEED_HEX}",
            SAMPLE_TX_HASH,
            f"txHash={SAMPLE_PK_RAW}",
            f"sha256: {SAMPLE_PK_RAW}",
        ]
        for sample in samples:
            self.assertEqual(
                safe_output.sanitize_text(sample),
                sample,
                msg=f"False positive redaction on: {sample}",
            )

    def test_preserves_epoch_numbers_amounts_and_english_sentences(self):
        samples = [
            "epoch=42 cur_epoch=1280 lastSynced=1279",
            "amount=1000000000000000000 wei (1.0 POOL), gasPrice=0.1 gwei, fee=$0.04",
            "Keeper loop healthy; claimed 25.5 token reward for epoch 42 with key check passed.",
            "Please wait for confirmation before retrying the next batch.",
        ]
        for sample in samples:
            self.assertEqual(safe_output.sanitize_text(sample), sample)

    def test_preserves_combined_realistic_log_line(self):
        log_line = (
            f"Synced epoch=108 for miner={SAMPLE_ADDRESS} via txHash={SAMPLE_TX_HASH}; "
            "reward=1250000000000000000 wei (1.25 POOL), gasPrice=0.15 gwei."
        )
        self.assertEqual(safe_output.sanitize_text(log_line), log_line)


class TestFormatExceptionSafe(unittest.TestCase):
    def test_formats_standard_exception_with_sanitized_message(self):
        pw_path = "/var/secrets/keeper_pw.txt"
        exc = RuntimeError(
            f"Failed calling https://u:p@rpc.example.com/v1?api_key=abc with {pw_path} "
            f"and MINER_PRIVATE_KEY={SAMPLE_PK_0X}"
        )
        formatted = safe_output.format_exception_safe(exc, sensitive_values=[pw_path])
        self.assertTrue(formatted.startswith("RuntimeError: "))
        self.assertIn("https://rpc.example.com/***", formatted)
        self.assertNotIn("p@rpc", formatted)
        self.assertNotIn("keeper_pw.txt", formatted)
        self.assertNotIn(SAMPLE_PK_RAW, formatted)

    def test_formats_subprocess_and_file_not_found_without_leaking_args_or_paths(self):
        timeout_exc = subprocess.TimeoutExpired(
            cmd=["cast", "send", "--private-key", SAMPLE_PK_0X],
            timeout=30,
        )
        called_exc = subprocess.CalledProcessError(
            returncode=2,
            cmd=["cast", "wallet", "address", "--password-file", "/secret/pw.txt"],
        )
        fnf_exc = FileNotFoundError(2, "No such file", "/secret/pw.txt")
        empty_cmd_exc = subprocess.SubprocessError()
        empty_cmd_exc.cmd = "   "

        self.assertEqual(safe_output.format_exception_safe(timeout_exc), "TimeoutExpired(cast)")
        self.assertEqual(
            safe_output.format_exception_safe(called_exc),
            "CalledProcessError(cast, returncode=2)",
        )
        self.assertEqual(safe_output.format_exception_safe(fnf_exc), "FileNotFoundError(cast)")
        self.assertEqual(safe_output.format_exception_safe(empty_cmd_exc), "SubprocessError(cast)")

    def test_formats_http_url_rpc_and_requests_exceptions(self):
        http_exc = urllib.error.HTTPError(
            url="https://user:pass@rpc.example.com/v1?token=abc",
            code=401,
            msg="Unauthorized for https://user:pass@rpc.example.com/v1?token=abc",
            hdrs=None,
            fp=None,
        )
        url_exc = urllib.error.URLError("Failed https://user:pass@rpc.example.com/v1")
        rpc_exc = keeper.RpcError(
            "eth_call",
            {"code": -32000, "message": "upstream https://u:p@rpc.example.com/v1 failed"},
        )
        req_exc_status = _FakeRequestsException("https://u:p@rpc.example.com/v1", status_code=502)
        req_exc_conn = _FakeRequestsException("https://u:p@rpc.example.com/v1", status_code=None)

        self.assertEqual(
            safe_output.format_exception_safe(http_exc),
            "HTTPError(401): Unauthorized for https://rpc.example.com/***",
        )
        self.assertEqual(
            safe_output.format_exception_safe(url_exc),
            "URLError: Failed https://rpc.example.com/***",
        )
        self.assertEqual(
            safe_output.format_exception_safe(rpc_exc),
            "RpcError(eth_call): code=-32000 message=upstream https://rpc.example.com/*** failed",
        )
        self.assertEqual(
            safe_output.format_exception_safe(req_exc_status),
            "_FakeRequestsException: HTTP status 502",
        )
        self.assertEqual(
            safe_output.format_exception_safe(req_exc_conn),
            "_FakeRequestsException: [network request failed]",
        )
        self.assertEqual(safe_output.format_exception_safe(None), "")


class TestKeeperParityAndIntentionalDivergence(unittest.TestCase):
    def setUp(self):
        self._saved_keeper_secrets = set(keeper._CONFIGURED_SECRETS)
        keeper.clear_configured_secrets()

    def tearDown(self):
        keeper.clear_configured_secrets()
        keeper._CONFIGURED_SECRETS.update(self._saved_keeper_secrets)

    def test_parity_with_keeper_sanitize_url(self):
        urls = [
            "https://bsc-testnet.example.com",
            "https://bsc-testnet.example.com/",
            "https://bsc-testnet.example.com:8545",
            "https://user:pass@bsc-testnet.example.com:8545/v1/abc?api_key=xyz#frag",
            "wss://user:pass@ws.example.com/ws?token=abc",
            "https://user@host.example.com",
            "",
            None,
            "not-a-url",
            "http://:8545",
            "https://host.example.com:99999/v1",
        ]
        for url in urls:
            self.assertEqual(
                safe_output.sanitize_url(url),
                keeper.sanitize_url(url),
                msg=f"URL parity mismatch for: {url!r}",
            )

    def test_parity_with_keeper_sanitize_text(self):
        pw_path = r"C:\secrets\keeper_password.txt"
        cases = [
            ("", None),
            (None, None),
            (
                "RPC failed at https://u:p@rpc.example.com:8545/v1/key?token=abc.",
                None,
            ),
            (f"private_key={SAMPLE_PK_0X} txHash={SAMPLE_TX_HASH}", None),
            (f"KEEPER_PRIVATE_KEY={SAMPLE_PK_0X}", None),
            ('{"password": "alpha beta", "api_key": "k_123"}', None),
            (f"cast send --private-key {SAMPLE_PK_0X} --password secret --password-file /tmp/pw", None),
            (r"keystore C:\Users\alice\.foundry\keystores\main", None),
            (f"Cannot read {pw_path} for txHash={SAMPLE_TX_HASH}", [pw_path]),
            ("Cannot read C:/secrets/keeper_password.txt", [pw_path]),
            (
                f"Contract={SAMPLE_ADDRESS} txHash={SAMPLE_TX_HASH} blockHash={SAMPLE_BLOCK_HASH} "
                f"seed={SAMPLE_SEED_HEX} epoch=42 amount=1000000000000000000 wei",
                None,
            ),
            ("Keeper loop healthy; waiting for next epoch.", None),
        ]
        for text_val, sensitive in cases:
            self.assertEqual(
                safe_output.sanitize_text(text_val, sensitive_values=sensitive),
                keeper.sanitize_text(text_val, sensitive_values=sensitive),
                msg=f"sanitize_text parity mismatch for: {text_val!r}",
            )

    def test_parity_with_keeper_format_exception_safe(self):
        exceptions = [
            None,
            "plain string https://u:p@rpc.example.com/v1",
            ValueError("RPC https://u:p@rpc.example.com/v1?api_key=abc failed"),
            subprocess.TimeoutExpired(cmd=["cast", "send", "--private-key", SAMPLE_PK_0X], timeout=10),
            subprocess.CalledProcessError(returncode=1, cmd=["cast", "call"]),
            FileNotFoundError(2, "No such file", "/secret/pw.txt"),
            urllib.error.HTTPError(
                url="https://u:p@rpc.example.com/v1",
                code=503,
                msg="Service Unavailable at https://u:p@rpc.example.com/v1",
                hdrs=None,
                fp=None,
            ),
            urllib.error.URLError("Connection refused"),
            keeper.RpcError("eth_getTransactionCount", {"code": -32005, "message": "limit exceeded"}),
            _FakeRequestsException("https://u:p@rpc.example.com/v1", status_code=429),
            _FakeRequestsException("https://u:p@rpc.example.com/v1", status_code=None),
        ]
        for exc in exceptions:
            self.assertEqual(
                safe_output.format_exception_safe(exc),
                keeper.format_exception_safe(exc),
                msg=f"format_exception_safe parity mismatch for: {exc!r}",
            )

    def test_intentional_differences_documented_against_keeper(self):
        # 1. Partial URLs / bare query parameters: ?token= and ?key= are leaked by keeper.py,
        #    and ?api_key=...&epoch=5 has trailing params swallowed by keeper.py kv_pattern.
        token_query = "GET /v1/rpc?token=tok_secret&epoch=5"
        self.assertEqual(keeper.sanitize_text(token_query), token_query)
        self.assertEqual(
            safe_output.sanitize_text(token_query),
            "GET /v1/rpc?token=[REDACTED]&epoch=5",
        )

        key_query = "GET /v1/rpc?key=key_secret&epoch=5"
        self.assertEqual(keeper.sanitize_text(key_query), key_query)
        self.assertEqual(
            safe_output.sanitize_text(key_query),
            "GET /v1/rpc?key=[REDACTED_KEY]&epoch=5",
        )

        api_key_query = "GET /v1/rpc?api_key=ak_secret&epoch=5"
        self.assertEqual(
            keeper.sanitize_text(api_key_query),
            "GET /v1/rpc?api_key=[REDACTED_KEY]",
        )
        self.assertEqual(
            safe_output.sanitize_text(api_key_query),
            "GET /v1/rpc?api_key=[REDACTED_KEY]&epoch=5",
        )

        # 2. Other script env prefixes such as MINER_PRIVATE_KEY / DEPLOYER_PRIVATE_KEY:
        #    keeper.py only accepts an optional `keeper[_-]?` prefix and leaks them.
        miner_env = f"MINER_PRIVATE_KEY={SAMPLE_PK_0X}"
        self.assertEqual(keeper.sanitize_text(miner_env), miner_env)
        self.assertEqual(safe_output.sanitize_text(miner_env), "MINER_PRIVATE_KEY=[REDACTED_KEY]")

        # 3. Phrase-form 64-hex keys, pk=64-hex, and bare unprefixed 64-hex keys:
        #    keeper.py requires `=`/`:` on known keys or prior register_secret(); safe_output
        #    redacts them while still leaving 0x-prefixed txHash values untouched.
        phrase_pk = f"Signer private key is {SAMPLE_PK_0X}"
        self.assertEqual(keeper.sanitize_text(phrase_pk), phrase_pk)
        self.assertEqual(safe_output.sanitize_text(phrase_pk), "Signer private key is [REDACTED_KEY]")

        pk_kv = f"pk={SAMPLE_PK_0X}"
        self.assertEqual(keeper.sanitize_text(pk_kv), pk_kv)
        self.assertEqual(safe_output.sanitize_text(pk_kv), "pk=[REDACTED_KEY]")

        bare_pk = f"leaked {SAMPLE_PK_RAW}"
        self.assertEqual(keeper.sanitize_text(bare_pk), bare_pk)
        self.assertEqual(safe_output.sanitize_text(bare_pk), "leaked [REDACTED_KEY]")

        # 4. CLI `--flag=value` syntax for --password-file and idempotency on re-sanitization:
        #    keeper.py misses `--password-file=/path` and corrupts `[REDACTED_KEY]` to `[REDACTED_KEY]]`
        #    on a second sanitize_text pass.
        eq_flag = "cast wallet --password-file=/tmp/secret_pw.txt"
        self.assertEqual(keeper.sanitize_text(eq_flag), eq_flag)
        self.assertEqual(
            safe_output.sanitize_text(eq_flag),
            "cast wallet --password-file=[REDACTED_PATH]",
        )

        first_pass = "api_key=secret123"
        self.assertEqual(
            keeper.sanitize_text(keeper.sanitize_text(first_pass)),
            "api_key=[REDACTED_KEY]]",
        )
        self.assertEqual(
            safe_output.sanitize_text(safe_output.sanitize_text(first_pass)),
            "api_key=[REDACTED_KEY]",
        )

        # 5. sensitive_values with a 0x-prefixed 64-hex private key also redacts unprefixed form:
        #    keeper.py only derives both forms in register_secret(), not in sensitive_values.
        both_forms = f"raw={SAMPLE_PK_RAW} hex={SAMPLE_PK_0X}"
        self.assertEqual(
            keeper.sanitize_text(both_forms, sensitive_values=[SAMPLE_PK_0X]),
            f"raw={SAMPLE_PK_RAW} hex=0x[REDACTED_KEY]",
        )
        self.assertEqual(
            safe_output.sanitize_text(both_forms, sensitive_values=[SAMPLE_PK_0X]),
            "raw=[REDACTED_KEY] hex=0x[REDACTED_KEY]",
        )


if __name__ == "__main__":
    unittest.main()
