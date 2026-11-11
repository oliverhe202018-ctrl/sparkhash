#!/usr/bin/env python3
"""Offline mock tests for cast_signer and keystore signing in claim.py and submit_share.py."""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from decimal import Decimal, getcontext, localcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from script import cast_signer, claim, submit_share
from script.cast_signer import (
    CastSendUncertainError,
    CastSignerError,
    UNCERTAIN_TX_WARNING,
    MAX_UINT64,
    MAX_UINT256,
    cast_send,
    get_isolated_cast_env,
    get_keystore_address,
    gwei_to_wei,
    parse_gas_multiplier,
    to_checksum_address,
    parse_pending_nonce,
    parse_rpc_fee_wei,
    resolve_eip1559_fees,
    validate_calldata,
    validate_eip1559_fees,
    validate_gas_limit,
    validate_nonce,
    validate_password_file,
)

TEST_MINER = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
OTHER_ADDR = "0x1111111111111111111111111111111111111111"
TEST_TX_HASH = "0x" + "1234567890abcdef" * 4
SECRET_PW_NAME = "top_secret_keystore_pw_2026.txt"
SECRET_URL = "https://user:pass1234@rpc.example.com/v1?token=tok_secret_9988"
DUMMY_PK = "0x" + "11" * 32


ISOLATED_HOST_ENV_KEYS = (
    "PRIVATE_KEY", "MINER_PRIVATE_KEY", "KEEPER_PRIVATE_KEY", "DEPLOYER_PRIVATE_KEY",
    "SIGNER_PRIVATE_KEY", "ETH_PRIVATE_KEY", "ETH_PASSWORD", "ETH_KEYSTORE",
    "ETH_KEYSTORE_ACCOUNT", "CAST_ACCOUNT", "CAST_PASSWORD_FILE", "FOUNDRY_PRIVATE_KEY",
    "FOUNDRY_ACCOUNT", "FOUNDRY_PASSWORD", "FOUNDRY_PASSWORD_FILE",
)

class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._u = patch("urllib.request.urlopen", side_effect=RuntimeError("Offline violation urlopen"))
        self._s = patch("subprocess.run", side_effect=RuntimeError("Offline violation subprocess"))
        self._u.start()
        self._s.start()
    def tearDown(self):
        self._s.stop()
        self._u.stop()
        super().tearDown()

def _run_cli(main_fn, argv, env_overrides=None):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    clean_env = {k: v for k, v in os.environ.items() if k.upper() not in ISOLATED_HOST_ENV_KEYS}
    if env_overrides: clean_env.update(env_overrides)
    exit_code = 0
    with patch.dict(os.environ, clean_env, clear=True):
        with patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try: main_fn()
                except SystemExit as exc: exit_code = exc.code if isinstance(exc.code, int) else 1
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()

DERIVED_PK_ADDR = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"


class TestCastSignerDirect(OfflineTestCase):
    def test_isolated_env_filters_all_wallet_and_private_key_vars(self):
        dirty_env = {
            "ETH_FROM": "0x1111111111111111111111111111111111111111",
            "ETH_KEYSTORE": "/path/to/keystore",
            "ETH_PASSWORD": "secret_passphrase",
            "ETH_CUSTOM_PARAM": "some_value",
            "CAST_ACCOUNT": "my_account",
            "CAST_PASSWORD_FILE": "/path/to/pass.txt",
            "CAST_ASYNC": "1",
            "CAST_UNKNOWN": "x",
            "FOUNDRY_KEYSTORE": "k",
            "FOUNDRY_ACCOUNT": "a",
            "FOUNDRY_PASSWORD": "p",
            "FOUNDRY_PASSWORD_FILE": "pf",
            "FOUNDRY_PRIVATE_KEY": "0xabc",
            "FOUNDRY_MNEMONIC": "mnemonic phrase",
            "FOUNDRY_ETH_FROM": "0x123",
            "PRIVATE_KEY": DUMMY_PK,
            "MINER_PRIVATE_KEY": DUMMY_PK,
            "KEEPER_PRIVATE_KEY": DUMMY_PK,
            "DEPLOYER_PRIVATE_KEY": DUMMY_PK,
            "SIGNER_PRIVATE_KEY": DUMMY_PK,
            "ETH_PRIVATE_KEY": DUMMY_PK,
            "MNEMONIC": "twelve words seed phrase",
            "ETH_MNEMONIC": "another twelve words seed phrase",
            "CUSTOM_PRIVATE_KEY": DUMMY_PK,
            "TEMP_PRIV_KEY": DUMMY_PK,
            "AWS_KMS_KEY_ID": "kms-key-id",
            "GCP_PROJECT_ID": "gcp-proj",
            "TURNKEY_API_PRIVATE_KEY": "tk-key",
            "TREZOR_DERIVATION_PATH": "m/44'/60'/0'/0/0",
            "LEDGER_DERIVATION_PATH": "m/44'/60'/0'/0/0",
            "PATH": "/usr/bin;C:\\Windows",
            "HOME": "/home/user",
            "USER": "miner_user",
            "SYSTEMROOT": "C:\\Windows",
            "HTTP_PROXY": "http://proxy.local:8080",
            "HTTPS_PROXY": "http://proxy.local:8080",
        }

        clean = get_isolated_cast_env(dirty_env)

        self.assertEqual(clean.get("PATH"), "/usr/bin;C:\\Windows")
        self.assertEqual(clean.get("HOME"), "/home/user")
        self.assertEqual(clean.get("USER"), "miner_user")
        self.assertEqual(clean.get("SYSTEMROOT"), "C:\\Windows")
        self.assertEqual(clean.get("HTTP_PROXY"), "http://proxy.local:8080")
        self.assertEqual(clean.get("HTTPS_PROXY"), "http://proxy.local:8080")

        for k in clean:
            upper_k = k.upper()
            self.assertFalse(upper_k.startswith("ETH_"), f"Variable {k} should have been stripped")
            self.assertFalse(upper_k.startswith("CAST_"), f"Variable {k} should have been stripped")
            self.assertFalse(upper_k.startswith("FOUNDRY_"), f"Variable {k} should have been stripped")
            self.assertFalse(upper_k.endswith("PRIVATE_KEY"), f"Variable {k} should have been stripped")
            self.assertFalse(upper_k.endswith("_PRIV_KEY"), f"Variable {k} should have been stripped")
        self.assertNotIn("AWS_KMS_KEY_ID", clean)
        self.assertNotIn("TURNKEY_API_PRIVATE_KEY", clean)
        self.assertNotIn("MNEMONIC", clean)
        self.assertNotIn("TREZOR_DERIVATION_PATH", clean)
        self.assertNotIn("LEDGER_DERIVATION_PATH", clean)

    def test_to_checksum_address_valid_vectors(self):
        vectors = [
            ("0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf", "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"),
            ("0x565d06e8d0313e2f8b5aa4b19de569ca2200c4cf", "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"),
            ("0x52908400098527886E0F7030069857D2E4169EE7", "0x52908400098527886E0F7030069857D2E4169EE7"),
            ("0xde709f2102306220921060314715629080e2fb77", "0xde709f2102306220921060314715629080e2fb77"),
            ("0x8617E34a93a37d788c39b3Fb671495a9743c39C1", "0x8617E34a93a37d788c39b3Fb671495a9743c39C1"),
        ]
        for inp, exp in vectors:
            with self.subTest(inp=inp):
                self.assertEqual(to_checksum_address(inp), exp)

    def test_to_checksum_address_invalid_inputs(self):
        invalid_inputs = [
            None, 12345, ["0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"],
            "565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf", "0x1234", "0x" + "1" * 41,
            "0x" + "g" * 40, "", "0x", " 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf ",
        ]
        for inv in invalid_inputs:
            with self.subTest(inv=inv):
                with self.assertRaises(CastSignerError):
                    to_checksum_address(inv)

    def test_parse_pending_nonce_valid_inputs(self):
        valid_cases = [
            (0, 0), (5, 5), (100, 100), ("0x0", 0), ("0x00", 0),
            ("0x1a", 26), ("0X5", 5), (" 0x10 ", 16),
            (MAX_UINT64 - 1, MAX_UINT64 - 1),
            (hex(MAX_UINT64 - 1), MAX_UINT64 - 1),
        ]
        for inp, exp in valid_cases:
            with self.subTest(inp=inp):
                self.assertEqual(parse_pending_nonce(inp), exp)

    def test_parse_pending_nonce_invalid_inputs(self):
        invalid_cases = [
            True, False, -1, -10, "-1", "-0x1", None, "", "   ",
            "0x", "0xgh", "abc", "12.34", [0], {"nonce": 0},
            "0", "42", "007", 1.0, float("nan"), float("inf"),
            "0x1 2", MAX_UINT64, MAX_UINT64 + 1, hex(MAX_UINT64),
        ]
        for inv in invalid_cases:
            with self.subTest(inv=inv):
                with self.assertRaises(ValueError):
                    parse_pending_nonce(inv)

    def test_cast_signer_has_no_unused_imports(self):
        src_path = REPO_ROOT / "script" / "cast_signer.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        imported = {}
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imported[name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imported[name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Name):
                used.add(node.id)
        unused = [k for k in imported if k not in used]
        self.assertEqual(unused, [], f"Unused imports found: {unused}")

    def test_gwei_to_wei_exact_decimal_and_rejections(self):
        self.assertEqual(gwei_to_wei("0"), 0)
        self.assertEqual(gwei_to_wei("+0"), 0)
        self.assertEqual(gwei_to_wei("1"), 1_000_000_000)
        self.assertEqual(gwei_to_wei("+1.5"), 1_500_000_000)
        self.assertEqual(gwei_to_wei("0.1"), 100_000_000)
        self.assertEqual(gwei_to_wei("1.15"), 1_150_000_000)
        self.assertEqual(gwei_to_wei("0.000000001"), 1)
        self.assertEqual(gwei_to_wei("1e-9"), 1)
        self.assertEqual(gwei_to_wei("1.000000000000"), 1_000_000_000)
        self.assertEqual(gwei_to_wei("10000000000000000000000000001"), 10**37 + 10**9)
        self.assertEqual(gwei_to_wei(Decimal("2.5")), 2_500_000_000)
        self.assertEqual(gwei_to_wei(3), 3_000_000_000)

        bad_vals = [
            None, True, False, -1, -0.1, "-1", "-0.1", "-0", "-0.0",
            -0.0, Decimal("-0"), Decimal("-0.0"), Decimal("sNaN"), Decimal("-sNaN"),
            "", "   ", "1_0", "1 2", "abc", "NaN", "sNaN", "Infinity",
            "-Infinity", float("nan"), float("inf"), float("-inf"),
            Decimal("NaN"), Decimal("Infinity"), Decimal("-1"),
            "0.0000000005", "1.0000000001", "1e-10",
            "10000000000000000000000000000.0000000005",
            "1e1000000", "1e10000", "1e-1000000", "1e-10000",
            "0e1000000", "1" * 5000, 10**5000, -(10**5000),
            Decimal("1e1000000"), Decimal("1e-1000000"), Decimal("1" * 500),
            1e100, (MAX_UINT256 // 10**9) + 1,
        ]
        for bad in bad_vals:
            with self.subTest(bad_type=type(bad).__name__):
                with self.assertRaises(ValueError) as ctx:
                    gwei_to_wei(bad)
                self.assertLess(len(str(ctx.exception)), 300)

        orig_prec = getcontext().prec
        for prec in (6, 28, 80):
            with localcontext() as ctx:
                ctx.prec = prec
                self.assertEqual(gwei_to_wei("10000000000000000000000000001"), 10**37 + 10**9)
                self.assertEqual(gwei_to_wei("1.15"), 1_150_000_000)
                with self.assertRaises(ValueError):
                    gwei_to_wei("10000000000000000000000000000.0000000005")
        self.assertEqual(getcontext().prec, orig_prec)

    def test_validate_calldata_nonce_gas_and_fees(self):
        self.assertEqual(validate_calldata("0x"), "0x")
        self.assertEqual(validate_calldata("0X"), "0x")
        self.assertEqual(validate_calldata("0x1234abcd"), "0x1234abcd")
        for bad_cd in ("", "   ", "0x123", "0x 1234", "0x12\t34", "0xzz", 123, None):
            with self.subTest(calldata=bad_cd):
                with self.assertRaises(CastSignerError):
                    validate_calldata(bad_cd)

        self.assertEqual(validate_nonce(0), 0)
        self.assertEqual(validate_nonce(MAX_UINT64 - 1), MAX_UINT64 - 1)
        for bad_nonce in (True, False, -1, MAX_UINT64, MAX_UINT64 + 1, 1.5, "0x1", None):
            with self.subTest(nonce=bad_nonce):
                with self.assertRaises(CastSignerError):
                    validate_nonce(bad_nonce)

        self.assertEqual(validate_gas_limit(1), 1)
        self.assertEqual(validate_gas_limit(MAX_UINT64), MAX_UINT64)
        for bad_gas in (True, False, 0, -1, MAX_UINT64 + 1, 1.5, "250000", None):
            with self.subTest(gas=bad_gas):
                with self.assertRaises(CastSignerError):
                    validate_gas_limit(bad_gas)

        self.assertEqual(validate_eip1559_fees(100, 100), (100, 100))
        with self.assertRaises(CastSignerError):
            validate_eip1559_fees(100, 101)
        for bad_fee in (True, False, -1, MAX_UINT256 + 1, "100", 1.5, None):
            with self.subTest(fee=bad_fee):
                with self.assertRaises(CastSignerError):
                    validate_eip1559_fees(bad_fee, 0)
                with self.assertRaises(CastSignerError):
                    validate_eip1559_fees(MAX_UINT256, bad_fee)

        self.assertEqual(parse_gas_multiplier("1.15"), Decimal("1.15"))
        self.assertEqual(parse_gas_multiplier("0.5"), Decimal("1"))
        self.assertEqual(parse_gas_multiplier("1e-9"), Decimal("1"))
        self.assertEqual(
            parse_gas_multiplier("1.0000000000000000000000000001"),
            Decimal("1.0000000000000000000000000001"),
        )
        bad_mults = (
            True, False, None, 0, -1, -0.0, Decimal("-0"), Decimal("sNaN"),
            "0", "-0", "-1", "NaN", "sNaN", "Infinity", "1_0", "bad",
            "1e1000000", "1e10000", "1e-1000000", "1e-10000",
            "1" * 5000, 10**5000, -(10**5000), Decimal("1e1000000"),
        )
        for bad_mult in bad_mults:
            with self.subTest(mult_type=type(bad_mult).__name__):
                with self.assertRaises(ValueError) as ctx:
                    parse_gas_multiplier(bad_mult)
                self.assertLess(len(str(ctx.exception)), 300)

        for bad_rpc_fee in (10**5000, -(10**5000), "0x" + "f" * 5000, "0x", True, "100", "-0x1"):
            with self.assertRaises(ValueError) as ctx:
                parse_rpc_fee_wei(bad_rpc_fee, "eth_gasPrice")
            self.assertLess(len(str(ctx.exception)), 300)

    def test_validate_password_file_missing(self):
        with self.assertRaises(CastSignerError) as ctx:
            validate_password_file("this_file_does_not_exist_987654.txt")
        self.assertIn("Password file does not exist", str(ctx.exception))
        self.assertNotIn("this_file_does_not_exist_987654.txt", str(ctx.exception))

    def test_validate_password_file_empty(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            temp_path = f.name
        try:
            with self.assertRaises(CastSignerError) as ctx:
                validate_password_file(temp_path)
            self.assertIn("Password file is empty", str(ctx.exception))
            self.assertNotIn(temp_path, str(ctx.exception))
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_validate_password_file_directory(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(CastSignerError) as ctx:
                validate_password_file(td)
            self.assertIn("Password file path is a directory", str(ctx.exception))
            self.assertNotIn(td, str(ctx.exception))

    def test_validate_password_file_invalid_inputs(self):
        for invalid in [None, "", "   ", "\t\n"]:
            with self.assertRaises(CastSignerError):
                validate_password_file(invalid)

    def test_validate_password_file_valid(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("mock_password_content_12345\n")
            temp_path = f.name
        try:
            validate_password_file(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_get_keystore_address_success(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("dummy_password_data\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 0
            fake_proc.stdout = f"{TEST_MINER.lower()}\n"
            fake_proc.stderr = ""

            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                with patch.dict(os.environ, {"ETH_PASSWORD": "secret", "PRIVATE_KEY": DUMMY_PK}, clear=False):
                    addr = get_keystore_address("my_test_account", pw_path)

            self.assertEqual(addr, TEST_MINER)
            self.assertEqual(mock_run.call_count, 1)
            call_args, call_kwargs = mock_run.call_args
            cmd = call_args[0]
            self.assertEqual(cmd[1:4], ["wallet", "address", "--account"])
            self.assertEqual(cmd[4], "my_test_account")
            self.assertEqual(cmd[5], "--password-file")
            self.assertEqual(cmd[6], pw_path)
            self.assertFalse(call_kwargs.get("shell", True))

            env = call_kwargs.get("env", {})
            self.assertNotIn("ETH_PASSWORD", env)
            self.assertNotIn("PRIVATE_KEY", env)
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_get_keystore_address_subprocess_failure(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("dummy_password_data\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 1
            fake_proc.stdout = ""
            fake_proc.stderr = "Error: decrypting keystore failed: invalid password"

            with patch("subprocess.run", return_value=fake_proc):
                with self.assertRaises(CastSignerError) as ctx:
                    get_keystore_address("my_test_account", pw_path)
            self.assertIn("cast wallet address failed", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_get_keystore_address_invalid_output(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("dummy_password_data\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 0
            fake_proc.stdout = "Not an address\n"
            fake_proc.stderr = ""

            with patch("subprocess.run", return_value=fake_proc):
                with self.assertRaises(CastSignerError) as ctx:
                    get_keystore_address("my_test_account", pw_path)
            self.assertIn("not a valid Ethereum address format", str(ctx.exception))

            fake_proc.stdout = f"{TEST_MINER}\n{OTHER_ADDR}\n"
            with patch("subprocess.run", return_value=fake_proc):
                with self.assertRaises(CastSignerError) as ctx:
                    get_keystore_address("my_test_account", pw_path)
            self.assertIn("empty or multiple lines", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_get_keystore_address_timeout(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("dummy_password_data\n")
            pw_path = f.name

        try:
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cast", timeout=5.0)):
                with self.assertRaises(CastSignerError) as ctx:
                    get_keystore_address("my_test_account", pw_path, timeout=5.0)
            self.assertIn("timed out after 5.0s", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_command_structure_and_isolation(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("mock_password_file_data\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 0
            fake_proc.stdout = f"{TEST_TX_HASH}\n"
            fake_proc.stderr = ""

            dirty_env = {
                "ETH_PASSWORD": "secret_passphrase",
                "PRIVATE_KEY": DUMMY_PK,
                "MINER_PRIVATE_KEY": DUMMY_PK,
                "PATH": os.environ.get("PATH", ""),
            }

            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                with patch.dict(os.environ, dirty_env, clear=False):
                    tx_hash = cast_send(
                        contract_address=TEST_MINER,
                        calldata="0x1234abcd",
                        account="test_account_alias",
                        password_file=pw_path,
                        rpc_url="http://127.0.0.1:8545",
                        nonce=7,
                        gas_price=2000000000,
                        gas_limit=150000,
                        priority_gas_price=100000000,
                    )

            self.assertEqual(tx_hash, TEST_TX_HASH)
            self.assertEqual(mock_run.call_count, 1)
            call_args, call_kwargs = mock_run.call_args
            cmd = call_args[0]

            self.assertEqual(cmd[1], "send")
            self.assertEqual(cmd[2], TEST_MINER)
            self.assertIn("--data", cmd)
            self.assertEqual(cmd[cmd.index("--data") + 1], "0x1234abcd")
            self.assertIn("--account", cmd)
            self.assertEqual(cmd[cmd.index("--account") + 1], "test_account_alias")
            self.assertIn("--password-file", cmd)
            self.assertEqual(cmd[cmd.index("--password-file") + 1], pw_path)
            self.assertIn("--rpc-url", cmd)
            self.assertEqual(cmd[cmd.index("--rpc-url") + 1], "http://127.0.0.1:8545")
            self.assertIn("--nonce", cmd)
            self.assertEqual(cmd[cmd.index("--nonce") + 1], "7")
            self.assertIn("--gas-price", cmd)
            self.assertEqual(cmd[cmd.index("--gas-price") + 1], "2000000000")
            self.assertIn("--priority-gas-price", cmd)
            self.assertEqual(cmd[cmd.index("--priority-gas-price") + 1], "100000000")
            self.assertIn("--gas-limit", cmd)
            self.assertEqual(cmd[cmd.index("--gas-limit") + 1], "150000")
            self.assertIn("--async", cmd)

            self.assertNotIn("--priority-fee", cmd)
            self.assertNotIn("--legacy", cmd)
            self.assertNotIn("--private-key", cmd)
            for item in cmd:
                self.assertNotIn("mock_password_file_data", item)

            self.assertFalse(call_kwargs.get("shell", True))
            env = call_kwargs.get("env", {})
            self.assertNotIn("ETH_PASSWORD", env)
            self.assertNotIn("PRIVATE_KEY", env)
            self.assertNotIn("MINER_PRIVATE_KEY", env)
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)


    def test_cast_send_exact_single_hash_success(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("mock_password\n")
            pw_path = f.name
        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 0
            fake_proc.stdout = f"  {TEST_TX_HASH}  \n"
            fake_proc.stderr = ""
            with patch("subprocess.run", return_value=fake_proc):
                res = cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x1234abcd",
                    account="test_account",
                    password_file=pw_path,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=2000000000,
                    gas_limit=100000,
                    priority_gas_price=100000000,
                )
            self.assertEqual(res, TEST_TX_HASH)
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_invalid_contract_address_fails_before_subprocess(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("mock_password\n")
            pw_path = f.name
        try:
            with patch("subprocess.run") as mock_run:
                with self.assertRaises(CastSignerError):
                    cast_send(
                        contract_address="0xInvalidContractAddress",
                        calldata="0x1234abcd",
                        account="test_account",
                        password_file=pw_path,
                        rpc_url="http://127.0.0.1:8545",
                        nonce=0,
                        gas_price=2000000000,
                        gas_limit=100000,
                        priority_gas_price=100000000,
                    )
                self.assertEqual(mock_run.call_count, 0)
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_mixed_or_malformed_outputs_raise_uncertain_error(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name

        cases = [
            ("", "empty output"),
            (f"Transaction submitted:\n{TEST_TX_HASH}\nWaiting for receipt...\n", "mixed text"),
            (f"{TEST_TX_HASH}\n{TEST_TX_HASH}\n", "multiple hashes"),
            ("0x" + "a" * 65, "overlong hex"),
            ("0x" + "a" * 63, "short hex"),
            ("0x" + "g" * 64, "invalid hex chars"),
            ("random string without 0x", "random string"),
        ]

        try:
            for stdout_val, label in cases:
                with self.subTest(case=label):
                    fake_proc = MagicMock()
                    fake_proc.returncode = 0
                    fake_proc.stdout = stdout_val
                    fake_proc.stderr = ""
                    with patch("subprocess.run", return_value=fake_proc):
                        with self.assertRaises(CastSendUncertainError):
                            cast_send(
                                contract_address=TEST_MINER,
                                calldata="0x1234abcd",
                                account="test_account",
                                password_file=pw_path,
                                rpc_url="http://127.0.0.1:8545",
                                nonce=1,
                                gas_price=2000000000,
                                gas_limit=100000,
                                priority_gas_price=100000000,
                            )
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_timeout_raises_uncertain_error(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name

        try:
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cast", timeout=60)):
                with self.assertRaises(CastSendUncertainError) as ctx:
                    cast_send(
                        contract_address=TEST_MINER,
                        calldata="0x1234abcd",
                        account="test_account",
                        password_file=pw_path,
                        rpc_url="http://127.0.0.1:8545",
                        nonce=1,
                        gas_price=2000000000,
                        gas_limit=100000,
                        priority_gas_price=100000000,
                    )
            self.assertIn("timed out", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_missing_txhash_raises_uncertain_error(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 0
            fake_proc.stdout = "Some output without any 64-char transaction hash\n"
            fake_proc.stderr = ""

            with patch("subprocess.run", return_value=fake_proc):
                with self.assertRaises(CastSendUncertainError) as ctx:
                    cast_send(
                        contract_address=TEST_MINER,
                        calldata="0x1234abcd",
                        account="test_account",
                        password_file=pw_path,
                        rpc_url="http://127.0.0.1:8545",
                        nonce=1,
                        gas_price=2000000000,
                        gas_limit=100000,
                        priority_gas_price=100000000,
                    )
            self.assertIn("output is not an exact 64-hex transaction hash", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_nonzero_exit_raises_uncertain_error(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name

        try:
            fake_proc = MagicMock()
            fake_proc.returncode = 1
            fake_proc.stdout = ""
            fake_proc.stderr = "Error: replacement transaction underpriced"

            with patch("subprocess.run", return_value=fake_proc):
                with self.assertRaises(CastSendUncertainError) as ctx:
                    cast_send(
                        contract_address=TEST_MINER,
                        calldata="0x1234abcd",
                        account="test_account",
                        password_file=pw_path,
                        rpc_url="http://127.0.0.1:8545",
                        nonce=1,
                        gas_price=2000000000,
                        gas_limit=100000,
                        priority_gas_price=100000000,
                    )
            self.assertIn("code 1:", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_allows_empty_0x_calldata(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name
        try:
            fake_proc = MagicMock(returncode=0, stdout=f"{TEST_TX_HASH}\n", stderr="")
            with patch("subprocess.run", return_value=fake_proc) as mock_run:
                tx_hash = cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x",
                    account="test_account",
                    password_file=pw_path,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=1000000000,
                    gas_limit=21000,
                    priority_gas_price=100000000,
                )
            self.assertEqual(tx_hash, TEST_TX_HASH)
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[cmd.index("--data") + 1], "0x")
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_cast_send_invalid_inputs_fail_before_subprocess(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("pw\n")
            pw_path = f.name
        try:
            with patch("subprocess.run") as mock_run:
                with self.assertRaises(TypeError):
                    cast_send(TEST_MINER, "0x1234", "acc", pw_path, "http://127.0.0.1:8545", 0, 1000, 21000)
                bad_cases = [
                    {"calldata": ""},
                    {"calldata": "0x123"},
                    {"calldata": "0x 1234"},
                    {"calldata": "0xzzzz"},
                    {"nonce": True},
                    {"nonce": -1},
                    {"nonce": MAX_UINT64},
                    {"nonce": 1.0},
                    {"gas_limit": False},
                    {"gas_limit": 0},
                    {"gas_limit": MAX_UINT64 + 1},
                    {"gas_price": True},
                    {"gas_price": -1},
                    {"priority_gas_price": False},
                    {"priority_gas_price": -1},
                    {"gas_price": 100, "priority_gas_price": 200},
                ]
                for overrides in bad_cases:
                    kwargs = {
                        "contract_address": TEST_MINER,
                        "calldata": "0x1234abcd",
                        "account": "acc",
                        "password_file": pw_path,
                        "rpc_url": "http://127.0.0.1:8545",
                        "nonce": 0,
                        "gas_price": 2000000000,
                        "gas_limit": 21000,
                        "priority_gas_price": 100000000,
                    }
                    kwargs.update(overrides)
                    with self.subTest(overrides=overrides):
                        with self.assertRaises(CastSignerError):
                            cast_send(**kwargs)
                self.assertEqual(mock_run.call_count, 0)
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)

    def test_resolve_eip1559_fees_defaults_overrides_and_failures(self):
        seen = []
        def ok_rpc(url, method, params=None):
            seen.append(method)
            if method == "eth_gasPrice":
                return hex(3_000_000_000)
            if method == "eth_maxPriorityFeePerGas":
                return hex(100_000_000)
            raise AssertionError(method)

        # 1. No overrides: queries both eth_gasPrice and eth_maxPriorityFeePerGas
        res = resolve_eip1559_fees("http://127.0.0.1:8545", ok_rpc, gas_multiplier="1.15")
        self.assertEqual(seen, ["eth_gasPrice", "eth_maxPriorityFeePerGas"])
        self.assertEqual(res["max_fee_per_gas_wei"], 3_450_000_000)
        self.assertEqual(res["priority_fee_per_gas_wei"], 100_000_000)

        # 2. Dual explicit overrides: ZERO fee RPC calls, exact values kept (3 gwei RPC cannot raise 1 gwei)
        seen.clear()
        res_ovr = resolve_eip1559_fees(
            "http://127.0.0.1:8545", ok_rpc,
            max_fee_per_gas_gwei="1.0", priority_fee_per_gas_gwei="0.2",
        )
        self.assertEqual(seen, [])
        self.assertEqual(res_ovr["max_fee_per_gas_wei"], 1_000_000_000)
        self.assertEqual(res_ovr["priority_fee_per_gas_wei"], 200_000_000)
        self.assertIsNone(res_ovr["network_gas_price_wei"])
        self.assertIsNone(res_ovr["network_priority_fee_wei"])
        self.assertEqual(res_ovr["warnings"], [])

        # 3. Explicit maxFee only: queries ONLY eth_maxPriorityFeePerGas; never raises explicit maxFee
        seen.clear()
        res_max_only = resolve_eip1559_fees(
            "http://127.0.0.1:8545", ok_rpc,
            max_fee_per_gas_gwei="1.0",
        )
        self.assertEqual(seen, ["eth_maxPriorityFeePerGas"])
        self.assertEqual(res_max_only["max_fee_per_gas_wei"], 1_000_000_000)
        self.assertEqual(res_max_only["priority_fee_per_gas_wei"], 100_000_000)
        self.assertIsNone(res_max_only["network_gas_price_wei"])
        self.assertEqual(res_max_only["network_priority_fee_wei"], 100_000_000)

        # Explicit maxFee only rejected when network priority fee > explicit maxFee
        with self.assertRaises(ValueError):
            resolve_eip1559_fees(
                "http://127.0.0.1:8545",
                lambda u, m, p=None: hex(500_000_000) if m == "eth_maxPriorityFeePerGas" else (_ for _ in ()).throw(AssertionError(m)),
                max_fee_per_gas_gwei="0.2",
            )

        # 4. Explicit priorityFee only: queries ONLY eth_gasPrice
        seen.clear()
        res_prio_only = resolve_eip1559_fees(
            "http://127.0.0.1:8545", ok_rpc,
            gas_multiplier="1.15", priority_fee_per_gas_gwei="0.2",
        )
        self.assertEqual(seen, ["eth_gasPrice"])
        self.assertEqual(res_prio_only["max_fee_per_gas_wei"], 3_450_000_000)
        self.assertEqual(res_prio_only["priority_fee_per_gas_wei"], 200_000_000)
        self.assertIsNone(res_prio_only["network_priority_fee_wei"])
        self.assertEqual(res_prio_only["network_gas_price_wei"], 3_000_000_000)

        # Precision D.5 + context invariance D.7
        orig_prec = getcontext().prec
        for prec in (6, 28, 80):
            with localcontext() as ctx:
                ctx.prec = prec
                res_exact = resolve_eip1559_fees(
                    "http://127.0.0.1:8545",
                    lambda u, m, p=None: hex(10**28 + 1) if m == "eth_gasPrice" else hex(1),
                    gas_multiplier="1.0000000000000000000000000001",
                )
                self.assertEqual(res_exact["max_fee_per_gas_wei"], 10**28 + 2)
        self.assertEqual(getcontext().prec, orig_prec)

        # Multiplier overflow > MAX_UINT256 rejected without clamping
        with self.assertRaises(ValueError):
            resolve_eip1559_fees(
                "http://127.0.0.1:8545",
                lambda u, m, p=None: hex(MAX_UINT256) if m == "eth_gasPrice" else hex(1),
                gas_multiplier="1.15",
            )

        with self.assertRaises(ValueError):
            resolve_eip1559_fees(
                "http://127.0.0.1:8545", lambda *a: (_ for _ in ()).throw(AssertionError("should not call rpc")),
                max_fee_per_gas_gwei="0.1", priority_fee_per_gas_gwei="0.2",
            )

        for bad_prio in (RuntimeError("rpc down"), None, "", "100", "-0x1", True):
            def bad_prio_rpc(url, method, params=None):
                if method == "eth_gasPrice":
                    return hex(2_000_000_000)
                if isinstance(bad_prio, Exception):
                    raise bad_prio
                return bad_prio
            with self.subTest(bad_prio=bad_prio):
                with self.assertRaises((ValueError, RuntimeError)):
                    resolve_eip1559_fees("http://127.0.0.1:8545", bad_prio_rpc)

        def low_gas_rpc(url, method, params=None):
            return hex(50_000_000) if method == "eth_gasPrice" else hex(100_000_000)
        lifted = resolve_eip1559_fees("http://127.0.0.1:8545", low_gas_rpc)
        self.assertEqual(lifted["max_fee_per_gas_wei"], 100_000_000)
        self.assertEqual(lifted["priority_fee_per_gas_wei"], 100_000_000)
        self.assertTrue(any(w[0] == "priority_floor" for w in lifted["warnings"]))
        with self.assertRaises(ValueError):
            parse_rpc_fee_wei("100", "eth_maxPriorityFeePerGas")


class TestClaimKeystoreIntegration(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp_pw = tempfile.NamedTemporaryFile("w", delete=False)
        self.tmp_pw.write("secret_password_content_123\n")
        self.tmp_pw.flush()
        self.pw_path = self.tmp_pw.name

    def tearDown(self):
        self.tmp_pw.close()
        if os.path.exists(self.pw_path):
            os.unlink(self.pw_path)
        super().tearDown()

    def test_claim_keystore_send_success(self):
        def fake_rpc_call(url, method, params=None):
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_gasPrice":
                return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas":
                return hex(100_000_000)
            if method == "eth_getTransactionCount":
                self.assertEqual(params[1], "pending")
                return "0x4"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(55000), "blockNumber": hex(12345)}
            raise AssertionError(f"Unexpected rpc method in claim: {method}")

        fake_reward_details = {
            "effectiveQuota": 100 * 10**18,
            "totalWeight": 3000,
            "wMin": 1000,
            "userWeight": 1500,
            "capBps": 2000,
            "claimed": False,
            "expectedReward": 20 * 10**18,
            "currentEpoch": 5,
            "lastSynced": 5,
            "epochEnded": True,
            "epochSettled": True,
        }

        argv = [
            "claim.py",
            "--epoch", "2",
            "--send",
            "--account", "keystore_miner",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]

        with patch("script.claim.get_keystore_address", return_value=TEST_MINER) as mock_get_addr:
            with patch("script.claim.calculate_reward_details", return_value=fake_reward_details):
                with patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")):
                    with patch("script.claim.rpc_call", side_effect=fake_rpc_call):
                        with patch("script.claim.cast_send", return_value=TEST_TX_HASH) as mock_cast_send:
                            with patch("builtins.input", return_value="yes"):
                                code, out, err = _run_cli(claim.main, argv)

        self.assertEqual(code, 0, f"claim failed with stderr: {err}")
        self.assertIn(f"交易哈希: {TEST_TX_HASH}", out)
        self.assertIn("[交易成功]", out)
        self.assertEqual(mock_get_addr.call_count, 1)
        self.assertEqual(mock_cast_send.call_count, 1)
        _, kwargs = mock_cast_send.call_args
        self.assertEqual(kwargs["account"], "keystore_miner")
        self.assertEqual(kwargs["password_file"], self.pw_path)
        self.assertEqual(kwargs["nonce"], 4)
        self.assertEqual(kwargs["gas_price"], 2_300_000_000)
        self.assertEqual(kwargs["priority_gas_price"], 100_000_000)
        self.assertEqual(kwargs["gas_limit"], 250000)
        self.assertEqual(kwargs["contract_address"], claim.DEFAULT_MINER_CONTRACT)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 0)

    def test_claim_address_mismatch_rejected(self):
        argv = [
            "claim.py",
            "--epoch", "2",
            "--send",
            "--miner", OTHER_ADDR,
            "--account", "keystore_miner",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]

        with patch("script.claim.rpc_call", return_value="0x61"):
            with patch("script.claim.get_keystore_address", return_value=TEST_MINER):
                with patch("script.claim.cast_send") as mock_cast_send:
                    code, out, err = _run_cli(claim.main, argv)

        self.assertNotEqual(code, 0)
        self.assertIn("[安全拦截]", out + err)
        self.assertIn("不一致", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

    def test_claim_missing_or_empty_password_file_rejected(self):
        argv_missing = [
            "claim.py",
            "--epoch", "2",
            "--send",
            "--account", "keystore_miner",
            "--password-file", "nonexistent_pw_file_998877.txt",
            "--rpc", "http://127.0.0.1:8545",
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            with patch("script.claim.cast_send") as mock_cast_send:
                code, out, err = _run_cli(claim.main, argv_missing)
        self.assertNotEqual(code, 0)
        self.assertIn("Keystore 签名地址解析失败", out + err)
        self.assertNotIn("nonexistent_pw_file_998877.txt", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

        with tempfile.NamedTemporaryFile("w", delete=False) as empty_f:
            empty_path = empty_f.name
        try:
            argv_empty = [
                "claim.py",
                "--epoch", "2",
                "--send",
                "--account", "keystore_miner",
                "--password-file", empty_path,
                "--rpc", "http://127.0.0.1:8545",
            ]
            with patch("script.claim.rpc_call", return_value="0x61"):
                with patch("script.claim.cast_send") as mock_cast_send:
                    code, out, err = _run_cli(claim.main, argv_empty)
            self.assertNotEqual(code, 0)
            self.assertIn("Keystore 签名地址解析失败", out + err)
            self.assertEqual(mock_cast_send.call_count, 0)
        finally:
            if os.path.exists(empty_path):
                os.unlink(empty_path)

    def test_claim_both_account_and_env_private_key_rejected(self):
        argv = [
            "claim.py",
            "--epoch", "2",
            "--send",
            "--account", "keystore_miner",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            with patch("script.claim.cast_send") as mock_cast_send:
                code, out, err = _run_cli(claim.main, argv, env_overrides={"PRIVATE_KEY": DUMMY_PK})

        self.assertNotEqual(code, 0)
        self.assertIn("同时提供了 keystore 参数", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

    def test_claim_only_account_or_only_password_file_rejected(self):
        argv_acc = ["claim.py", "--epoch", "2", "--send", "--account", "acc1"]
        with patch("script.claim.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(claim.main, argv_acc)
        self.assertNotEqual(code, 0)
        self.assertIn("--account 与 --password-file 必须同时提供", out + err)

        argv_pw = ["claim.py", "--epoch", "2", "--send", "--password-file", self.pw_path]
        with patch("script.claim.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(claim.main, argv_pw)
        self.assertNotEqual(code, 0)
        self.assertIn("--account 与 --password-file 必须同时提供", out + err)

    def test_claim_legacy_env_pk_prints_warning(self):
        fake_reward_details = {
            "effectiveQuota": 100 * 10**18,
            "totalWeight": 3000,
            "wMin": 1000,
            "userWeight": 1500,
            "capBps": 2000,
            "claimed": False,
            "expectedReward": 20 * 10**18,
            "currentEpoch": 5,
            "lastSynced": 5,
            "epochEnded": True,
            "epochSettled": True,
        }
        argv = ["claim.py", "--epoch", "2", "--send", "--rpc", "http://127.0.0.1:8545"]
        with patch("script.claim.rpc_call", side_effect=lambda url, m, p=None: "0x61" if m == "eth_chainId" else (hex(100_000_000) if m == "eth_maxPriorityFeePerGas" else hex(2_000_000_000))):
            with patch("script.claim.calculate_reward_details", return_value=fake_reward_details):
                with patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")):
                    with patch("builtins.input", return_value="no"):
                        code, out, err = _run_cli(claim.main, argv, env_overrides={"PRIVATE_KEY": DUMMY_PK})

        self.assertIn("不推荐", out + err)
        self.assertIn("Foundry keystore", out + err)

    def test_claim_nonce_query_failure_aborts(self):
        fake_reward_details = {
            "effectiveQuota": 100 * 10**18, "totalWeight": 3000, "wMin": 1000,
            "userWeight": 1500, "capBps": 2000, "claimed": False, "expectedReward": 20 * 10**18,
        }
        invalid_resps = [
            RuntimeError("RPC error"), True, False, -1, None, "", "0xgh",
        ]
        argv = [
            "claim.py", "--epoch", "2", "--send", "--account", "keystore_miner",
            "--password-file", self.pw_path, "--rpc", "http://127.0.0.1:8545",
        ]
        for resp in invalid_resps:
            with self.subTest(resp=resp):
                def fake_rpc(url, method, params=None):
                    if method == "eth_chainId": return "0x61"
                    if method == "eth_gasPrice": return hex(2_000_000_000)
                    if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
                    if method == "eth_getTransactionCount":
                        self.assertEqual(params[1], "pending")
                        if isinstance(resp, Exception): raise resp
                        return resp
                    raise AssertionError(f"Unexpected: {method}")

                with patch("script.claim.get_keystore_address", return_value=TEST_MINER):
                    with patch("script.claim.calculate_reward_details", return_value=fake_reward_details):
                        with patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")):
                            with patch("script.claim.rpc_call", side_effect=fake_rpc):
                                with patch("script.claim.cast_send") as mock_send:
                                    with patch("builtins.input", return_value="yes"):
                                        code, out, err = _run_cli(claim.main, argv)

                self.assertNotEqual(code, 0)
                self.assertIn("查询 pending nonce 失败", out + err)
                self.assertIn("不得回退为 0", out + err)
                self.assertEqual(mock_send.call_count, 0)

    def test_claim_cast_send_timeout_or_uncertain_prints_warning_once(self):
        def fake_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            if method == "eth_getTransactionCount":
                self.assertEqual(params[1], "pending")
                return "0x8"
            raise AssertionError(f"Unexpected: {method}")

        fake_reward_details = {
            "effectiveQuota": 100 * 10**18, "totalWeight": 3000, "wMin": 1000,
            "userWeight": 1500, "capBps": 2000, "claimed": False, "expectedReward": 20 * 10**18,
        }
        argv = [
            "claim.py", "--epoch", "2", "--send", "--account", "keystore_miner",
            "--password-file", self.pw_path, "--rpc", "http://127.0.0.1:8545",
        ]
        # Case 1: Timeout
        with patch("script.claim.get_keystore_address", return_value=TEST_MINER):
            with patch("script.claim.calculate_reward_details", return_value=fake_reward_details):
                with patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")):
                    with patch("script.claim.rpc_call", side_effect=fake_rpc):
                        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cast send", timeout=60)):
                            with patch("builtins.input", return_value="yes"):
                                code, out, err = _run_cli(claim.main, argv)
        self.assertNotEqual(code, 0)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 1)

        # Case 2: Output without valid txHash
        fake_bad = MagicMock(returncode=0, stdout="No valid hash", stderr="")
        with patch("script.claim.get_keystore_address", return_value=TEST_MINER):
            with patch("script.claim.calculate_reward_details", return_value=fake_reward_details):
                with patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")):
                    with patch("script.claim.rpc_call", side_effect=fake_rpc):
                        with patch("subprocess.run", return_value=fake_bad):
                            with patch("builtins.input", return_value="yes"):
                                code, out, err = _run_cli(claim.main, argv)
        self.assertNotEqual(code, 0)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 1)

    def test_claim_fee_overrides_priority_failure_and_dry_run_copy(self):
        seen_methods = []
        def fake_rpc_override(url, method, params=None):
            seen_methods.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_getTransactionCount": return "0x4"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(55000), "blockNumber": hex(12345)}
            raise AssertionError(f"Unexpected method: {method}")

        settled_details = {
            "effectiveQuota": 100 * 10**18, "totalWeight": 3000, "wMin": 1000,
            "userWeight": 1500, "capBps": 2000, "claimed": False, "expectedReward": 20 * 10**18,
            "currentEpoch": 5, "lastSynced": 5, "epochEnded": True, "epochSettled": True,
        }
        argv_ovr = [
            "claim.py", "--epoch", "2", "--send", "--account", "keystore_miner",
            "--password-file", self.pw_path, "--rpc", "http://127.0.0.1:8545",
            "--max-fee-per-gas-gwei", "1.0", "--priority-fee-per-gas-gwei", "0.2",
        ]
        with patch("script.claim.get_keystore_address", return_value=TEST_MINER), \
             patch("script.claim.calculate_reward_details", return_value=settled_details), \
             patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
             patch("script.claim.rpc_call", side_effect=fake_rpc_override), \
             patch("script.claim.cast_send", return_value=TEST_TX_HASH) as mock_cast, \
             patch("builtins.input", return_value="yes"):
            code, out, err = _run_cli(claim.main, argv_ovr)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_methods, ["eth_chainId", "eth_getTransactionCount", "eth_getTransactionReceipt"])
        self.assertEqual(mock_cast.call_args[1]["gas_price"], 1_000_000_000)
        self.assertEqual(mock_cast.call_args[1]["priority_gas_price"], 200_000_000)
        self.assertIn("未查询 eth_gasPrice", out)
        self.assertIn("未查询 eth_maxPriorityFeePerGas", out)

        # Explicit maxFee only: queries ONLY eth_maxPriorityFeePerGas
        seen_max_only = []
        def fake_rpc_max_only(url, method, params=None):
            seen_max_only.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            raise AssertionError(f"Unexpected method: {method}")
        with patch("script.claim.calculate_reward_details", return_value=settled_details), \
             patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
             patch("script.claim.rpc_call", side_effect=fake_rpc_max_only), \
             patch("script.claim.cast_send") as mock_cast:
            code, out, err = _run_cli(claim.main, [
                "claim.py", "--epoch", "2", "--miner", TEST_MINER, "--rpc", "http://127.0.0.1:8545",
                "--max-fee-per-gas-gwei", "1.0",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_max_only, ["eth_chainId", "eth_maxPriorityFeePerGas"])
        self.assertIn("1.000 gwei (1000000000 wei)", out)
        self.assertIn("未查询 eth_gasPrice", out)

        # Explicit priorityFee only: queries ONLY eth_gasPrice, lifts auto maxFee with clear warning
        seen_prio_only = []
        def fake_rpc_prio_only(url, method, params=None):
            seen_prio_only.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(50_000_000)
            raise AssertionError(f"Unexpected method: {method}")
        with patch("script.claim.calculate_reward_details", return_value=settled_details), \
             patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
             patch("script.claim.rpc_call", side_effect=fake_rpc_prio_only), \
             patch("script.claim.cast_send") as mock_cast:
            code, out, err = _run_cli(claim.main, [
                "claim.py", "--epoch", "2", "--miner", TEST_MINER, "--rpc", "http://127.0.0.1:8545",
                "--priority-fee-per-gas-gwei", "0.2",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_prio_only, ["eth_chainId", "eth_gasPrice"])
        self.assertIn("低于 Priority Fee，已提升至 0.200 gwei", out)
        self.assertIn("0.200 gwei (200000000 wei)", out)

        for bad_args in (
            ["--max-fee-per-gas-gwei", "0.1", "--priority-fee-per-gas-gwei", "0.2"],
            ["--max-fee-per-gas-gwei", "1.0000000001"],
            ["--gas-limit", "0"],
        ):
            with patch("script.claim.rpc_call", side_effect=AssertionError("RPC must not run")), \
                 patch("script.claim.cast_send") as mock_cast:
                code, out, err = _run_cli(claim.main, ["claim.py", "--epoch", "2", "--miner", TEST_MINER, *bad_args])
            self.assertNotEqual(code, 0)
            self.assertEqual(mock_cast.call_count, 0)

        def fail_prio_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": raise RuntimeError("priority fee unsupported")
            raise AssertionError(method)
        with patch("script.claim.calculate_reward_details", return_value=settled_details), \
             patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
             patch("script.claim.rpc_call", side_effect=fail_prio_rpc), \
             patch("script.claim.cast_send") as mock_cast:
            code, out, err = _run_cli(claim.main, ["claim.py", "--epoch", "2", "--miner", TEST_MINER, "--rpc", "http://127.0.0.1:8545"])
        self.assertNotEqual(code, 0)
        self.assertEqual(mock_cast.call_count, 0)

        cases = [
            ({"currentEpoch": 2, "lastSynced": 2, "epochEnded": False, "epochSettled": False}, "尚未结束"),
            ({"currentEpoch": 5, "lastSynced": 2, "epochEnded": True, "epochSettled": False}, "已结束但尚未结算"),
            ({"currentEpoch": 5, "lastSynced": 5, "epochEnded": True, "epochSettled": True}, "已结束且已结算"),
        ]
        for patch_fields, expected_txt in cases:
            details = {**settled_details, **patch_fields}
            with patch("script.claim.calculate_reward_details", return_value=details), \
                 patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
                 patch("script.claim.rpc_call", side_effect=lambda u, m, p=None: "0x61" if m == "eth_chainId" else (hex(100_000_000) if m == "eth_maxPriorityFeePerGas" else hex(2_000_000_000))), \
                 patch("script.claim.cast_send") as mock_cast:
                code, out, err = _run_cli(claim.main, ["claim.py", "--epoch", "2", "--miner", TEST_MINER, "--rpc", "http://127.0.0.1:8545"])
            self.assertEqual(code, 0, err)
            self.assertEqual(mock_cast.call_count, 0)
            self.assertIn(expected_txt, out)
            self.assertIn("--account", out)
            self.assertIn("--password-file", out)
            self.assertNotIn("PRIVATE_KEY", out)

    def test_claim_legacy_env_pk_builds_type2_tx(self):
        settled_details = {
            "effectiveQuota": 100 * 10**18, "totalWeight": 3000, "wMin": 1000,
            "userWeight": 1500, "capBps": 2000, "claimed": False, "expectedReward": 20 * 10**18,
            "currentEpoch": 5, "lastSynced": 5, "epochEnded": True, "epochSettled": True,
        }
        signed_mock = MagicMock(raw_transaction=bytes.fromhex("ab" * 16))
        def fake_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            if method == "eth_getTransactionCount": return "0x2"
            if method == "eth_sendRawTransaction": return TEST_TX_HASH
            if method == "eth_getTransactionReceipt": return {"status": "0x1", "gasUsed": hex(55000), "blockNumber": hex(123)}
            raise AssertionError(method)
        with patch("script.claim.calculate_reward_details", return_value=settled_details), \
             patch("script.claim.simulate_claim", return_value=(True, 120000, None, "0x1234")), \
             patch("script.claim.rpc_call", side_effect=fake_rpc), \
             patch("eth_account.Account.sign_transaction", return_value=signed_mock) as mock_sign, \
             patch("builtins.input", return_value="yes"):
            code, out, err = _run_cli(claim.main, ["claim.py", "--epoch", "2", "--send", "--rpc", "http://127.0.0.1:8545"], env_overrides={"PRIVATE_KEY": DUMMY_PK})
        self.assertEqual(code, 0, err)
        tx_dict = mock_sign.call_args[0][0]
        self.assertEqual(tx_dict["type"], 2)
        self.assertEqual(tx_dict["maxFeePerGas"], 2_300_000_000)
        self.assertEqual(tx_dict["maxPriorityFeePerGas"], 100_000_000)
        self.assertNotIn("gasPrice", tx_dict)


class TestSubmitShareKeystoreIntegration(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp_pw = tempfile.NamedTemporaryFile("w", delete=False)
        self.tmp_pw.write("secret_password_submit_123\n")
        self.tmp_pw.flush()
        self.pw_path = self.tmp_pw.name

    def tearDown(self):
        self.tmp_pw.close()
        if os.path.exists(self.pw_path):
            os.unlink(self.pw_path)
        super().tearDown()

    def test_submit_share_keystore_send_success(self):
        def fake_rpc_call(url, method, params=None):
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_gasPrice":
                return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas":
                return hex(100_000_000)
            if method == "eth_getTransactionCount":
                self.assertEqual(params[1], "pending")
                return "0x7"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(65000), "blockNumber": hex(223344)}
            raise AssertionError(f"Unexpected rpc method in submit_share: {method}")

        argv = [
            "submit_share.py",
            "--miner", TEST_MINER,
            "--k", "0",
            "--nonce", "12345",
            "--send",
            "--account", "keystore_submitter",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]

        with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER) as mock_get_addr:
            with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                with patch("script.submit_share.rpc_call", side_effect=fake_rpc_call):
                    with patch("script.submit_share.cast_send", return_value=TEST_TX_HASH) as mock_cast_send:
                        with patch("builtins.input", return_value="yes"):
                            code, out, err = _run_cli(submit_share.main, argv)

        self.assertEqual(code, 0, f"submit_share failed with stderr: {err}")
        self.assertIn(f"Transaction Hash: {TEST_TX_HASH}", out)
        self.assertIn("[CONFIRMED]", out)
        self.assertEqual(mock_get_addr.call_count, 1)
        self.assertEqual(mock_cast_send.call_count, 1)
        _, kwargs = mock_cast_send.call_args
        self.assertEqual(kwargs["account"], "keystore_submitter")
        self.assertEqual(kwargs["password_file"], self.pw_path)
        self.assertEqual(kwargs["nonce"], 7)
        self.assertEqual(kwargs["gas_price"], 2_300_000_000)
        self.assertEqual(kwargs["priority_gas_price"], 100_000_000)
        self.assertEqual(kwargs["gas_limit"], 350000)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 0)

    def test_submit_share_address_mismatch_rejected(self):
        argv = [
            "submit_share.py",
            "--miner", OTHER_ADDR,
            "--k", "0",
            "--nonce", "12345",
            "--send",
            "--account", "keystore_submitter",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]

        with patch("script.submit_share.rpc_call", side_effect=lambda url, m, p=None: "0x61" if m == "eth_chainId" else hex(2_000_000_000)):
            with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER):
                    with patch("script.submit_share.cast_send") as mock_cast_send:
                        code, out, err = _run_cli(submit_share.main, argv)

        self.assertNotEqual(code, 0)
        self.assertIn("[SECURITY ERROR]", out + err)
        self.assertIn("does NOT match", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

    def test_submit_share_missing_or_empty_password_file_rejected(self):
        argv_missing = [
            "submit_share.py",
            "--miner", TEST_MINER,
            "--k", "0",
            "--nonce", "12345",
            "--send",
            "--account", "keystore_submitter",
            "--password-file", "missing_file_001122.txt",
            "--rpc", "http://127.0.0.1:8545",
        ]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            with patch("script.submit_share.cast_send") as mock_cast_send:
                code, out, err = _run_cli(submit_share.main, argv_missing)
        self.assertNotEqual(code, 0)
        self.assertIn("Failed to resolve keystore address", out + err)
        self.assertNotIn("missing_file_001122.txt", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

        with tempfile.NamedTemporaryFile("w", delete=False) as empty_f:
            empty_path = empty_f.name
        try:
            argv_empty = [
                "submit_share.py",
                "--miner", TEST_MINER,
                "--k", "0",
                "--nonce", "12345",
                "--send",
                "--account", "keystore_submitter",
                "--password-file", empty_path,
                "--rpc", "http://127.0.0.1:8545",
            ]
            with patch("script.submit_share.rpc_call", return_value="0x61"):
                with patch("script.submit_share.cast_send") as mock_cast_send:
                    code, out, err = _run_cli(submit_share.main, argv_empty)
            self.assertNotEqual(code, 0)
            self.assertIn("Failed to resolve keystore address", out + err)
            self.assertEqual(mock_cast_send.call_count, 0)
        finally:
            if os.path.exists(empty_path):
                os.unlink(empty_path)

    def test_submit_share_both_account_and_env_private_key_rejected(self):
        argv = [
            "submit_share.py",
            "--miner", TEST_MINER,
            "--k", "0",
            "--nonce", "12345",
            "--send",
            "--account", "keystore_submitter",
            "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            with patch("script.submit_share.cast_send") as mock_cast_send:
                code, out, err = _run_cli(submit_share.main, argv, env_overrides={"MINER_PRIVATE_KEY": DUMMY_PK})

        self.assertNotEqual(code, 0)
        self.assertIn("Both keystore flags", out + err)
        self.assertEqual(mock_cast_send.call_count, 0)

    def test_submit_share_only_account_or_only_password_file_rejected(self):
        argv_acc = ["submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "1", "--account", "acc1"]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(submit_share.main, argv_acc)
        self.assertNotEqual(code, 0)
        self.assertIn("--account and --password-file must be provided together", out + err)

        argv_pw = ["submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "1", "--password-file", self.pw_path]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(submit_share.main, argv_pw)
        self.assertNotEqual(code, 0)
        self.assertIn("--account and --password-file must be provided together", out + err)

    def test_submit_share_legacy_env_pk_prints_warning(self):
        argv = [
            "submit_share.py",
            "--miner", DERIVED_PK_ADDR,
            "--k", "0",
            "--nonce", "12345",
            "--send",
            "--rpc", "http://127.0.0.1:8545",
        ]
        with patch("script.submit_share.rpc_call", side_effect=lambda url, m, p=None: "0x61" if m == "eth_chainId" else (hex(100_000_000) if m == "eth_maxPriorityFeePerGas" else hex(2_000_000_000))):
            with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                with patch("builtins.input", return_value="no"):
                    code, out, err = _run_cli(submit_share.main, argv, env_overrides={"MINER_PRIVATE_KEY": DUMMY_PK})

        self.assertIn("不推荐", out + err)
        self.assertIn("--account", out + err)

    def test_submit_share_nonce_query_failure_aborts(self):
        invalid_resps = [
            RuntimeError("RPC pending failed"), False, True, -5, None, "", "0xxyz",
        ]
        argv = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345",
            "--send", "--account", "keystore_submitter", "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]
        for resp in invalid_resps:
            with self.subTest(resp=resp):
                def fake_rpc(url, method, params=None):
                    if method == "eth_chainId": return "0x61"
                    if method == "eth_gasPrice": return hex(2_000_000_000)
                    if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
                    if method == "eth_getTransactionCount":
                        self.assertEqual(params[1], "pending")
                        if isinstance(resp, Exception): raise resp
                        return resp
                    raise AssertionError(f"Unexpected: {method}")

                with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER):
                    with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                        with patch("script.submit_share.rpc_call", side_effect=fake_rpc):
                            with patch("script.submit_share.cast_send") as mock_send:
                                with patch("builtins.input", return_value="yes"):
                                    code, out, err = _run_cli(submit_share.main, argv)

                self.assertNotEqual(code, 0)
                self.assertIn("Failed to query pending nonce", out + err)
                self.assertIn("will not fallback to 0", out + err)
                self.assertEqual(mock_send.call_count, 0)

    def test_submit_share_cast_send_timeout_or_uncertain_prints_warning_once(self):
        def fake_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            if method == "eth_getTransactionCount":
                self.assertEqual(params[1], "pending")
                return "0x3"
            raise AssertionError(f"Unexpected: {method}")

        argv = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345",
            "--send", "--account", "keystore_submitter", "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545",
        ]
        # Case 1: Timeout
        with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER):
            with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                with patch("script.submit_share.rpc_call", side_effect=fake_rpc):
                    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cast send", timeout=60)):
                        with patch("builtins.input", return_value="yes"):
                            code, out, err = _run_cli(submit_share.main, argv)
        self.assertNotEqual(code, 0)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 1)

        # Case 2: Output without valid txHash
        fake_bad = MagicMock(returncode=0, stdout="Unexpected output", stderr="")
        with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER):
            with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)):
                with patch("script.submit_share.rpc_call", side_effect=fake_rpc):
                    with patch("subprocess.run", return_value=fake_bad):
                        with patch("builtins.input", return_value="yes"):
                            code, out, err = _run_cli(submit_share.main, argv)
        self.assertNotEqual(code, 0)
        self.assertEqual((out + err).count(UNCERTAIN_TX_WARNING), 1)

    def test_submit_share_fee_overrides_priority_failure_and_dry_run(self):
        seen_methods = []
        def fake_rpc_override(url, method, params=None):
            seen_methods.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_getTransactionCount": return "0x7"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "gasUsed": hex(65000), "blockNumber": hex(223344)}
            raise AssertionError(method)

        argv_ovr = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345",
            "--send", "--account", "keystore_submitter", "--password-file", self.pw_path,
            "--rpc", "http://127.0.0.1:8545", "--max-fee-per-gas-gwei", "1.0",
            "--priority-fee-per-gas-gwei", "0.2",
        ]
        with patch("script.submit_share.get_keystore_address", return_value=TEST_MINER), \
             patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=fake_rpc_override), \
             patch("script.submit_share.cast_send", return_value=TEST_TX_HASH) as mock_cast, \
             patch("builtins.input", return_value="yes"):
            code, out, err = _run_cli(submit_share.main, argv_ovr)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_methods, ["eth_chainId", "eth_getTransactionCount", "eth_getTransactionReceipt"])
        self.assertEqual(mock_cast.call_args[1]["gas_price"], 1_000_000_000)
        self.assertEqual(mock_cast.call_args[1]["priority_gas_price"], 200_000_000)
        self.assertIn("eth_gasPrice skipped", out)
        self.assertIn("eth_maxPriorityFeePerGas skipped", out)

        # Explicit maxFee only & explicit priorityFee only
        seen_max_only = []
        def fake_rpc_max_only(url, method, params=None):
            seen_max_only.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            raise AssertionError(method)
        with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=fake_rpc_max_only):
            code, out, err = _run_cli(submit_share.main, [
                "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345",
                "--rpc", "http://127.0.0.1:8545", "--max-fee-per-gas-gwei", "1.0",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_max_only, ["eth_chainId", "eth_maxPriorityFeePerGas"])
        self.assertIn("eth_gasPrice skipped", out)

        seen_prio_only = []
        def fake_rpc_prio_only(url, method, params=None):
            seen_prio_only.append(method)
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(50_000_000)
            raise AssertionError(method)
        with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=fake_rpc_prio_only):
            code, out, err = _run_cli(submit_share.main, [
                "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345",
                "--rpc", "http://127.0.0.1:8545", "--priority-fee-per-gas-gwei", "0.2",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen_prio_only, ["eth_chainId", "eth_gasPrice"])
        self.assertIn("Raising maxFeePerGas to match priority fee", out)

        for bad_args in (
            ["--max-fee-per-gas-gwei", "0.1", "--priority-fee-per-gas-gwei", "0.2"],
            ["--max-fee-per-gas-gwei", "1.0000000001"],
            ["--max-fee-per-gas-gwei", "1.0", "--gas-price-gwei", "1.0"],
            ["--gas-limit", "0"],
        ):
            with patch("script.submit_share.rpc_call", side_effect=AssertionError("RPC must not run")), \
                 patch("script.submit_share.cast_send") as mock_cast:
                code, out, err = _run_cli(submit_share.main, [
                    "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345", *bad_args
                ])
            self.assertNotEqual(code, 0)
            self.assertEqual(mock_cast.call_count, 0)

        def bad_prio_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": return "not_hex"
            raise AssertionError(method)
        with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=bad_prio_rpc), \
             patch("script.submit_share.cast_send") as mock_cast:
            code, out, err = _run_cli(submit_share.main, [
                "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345", "--rpc", "http://127.0.0.1:8545"
            ])
        self.assertNotEqual(code, 0)
        self.assertEqual(mock_cast.call_count, 0)

        with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=lambda u, m, p=None: "0x61" if m == "eth_chainId" else (hex(100_000_000) if m == "eth_maxPriorityFeePerGas" else hex(2_000_000_000))), \
             patch("script.submit_share.cast_send") as mock_cast:
            code, out, err = _run_cli(submit_share.main, [
                "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "12345", "--rpc", "http://127.0.0.1:8545"
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(mock_cast.call_count, 0)
        self.assertIn("SIMULATION COMPLETED (DRY-RUN)", out)
        self.assertIn("Max Gas Fee Ceiling:", out)
        self.assertIn("--account", out)
        self.assertIn("--password-file", out)
        self.assertNotIn("PRIVATE_KEY", out)

    def test_submit_share_legacy_env_pk_builds_type2_tx(self):
        signed_mock = MagicMock(raw_transaction=bytes.fromhex("cd" * 16))
        def fake_rpc(url, method, params=None):
            if method == "eth_chainId": return "0x61"
            if method == "eth_gasPrice": return hex(2_000_000_000)
            if method == "eth_maxPriorityFeePerGas": return hex(100_000_000)
            if method == "eth_getTransactionCount": return "0x3"
            if method == "eth_sendRawTransaction": return TEST_TX_HASH
            if method == "eth_getTransactionReceipt": return {"status": "0x1", "gasUsed": hex(65000), "blockNumber": hex(456)}
            raise AssertionError(method)
        with patch("script.submit_share.simulate_call", return_value=(True, 150000, None)), \
             patch("script.submit_share.rpc_call", side_effect=fake_rpc), \
             patch("eth_account.Account.sign_transaction", return_value=signed_mock) as mock_sign, \
             patch("builtins.input", return_value="yes"):
            code, out, err = _run_cli(submit_share.main, [
                "submit_share.py", "--miner", DERIVED_PK_ADDR, "--k", "0", "--nonce", "12345",
                "--send", "--rpc", "http://127.0.0.1:8545",
            ], env_overrides={"MINER_PRIVATE_KEY": DUMMY_PK})
        self.assertEqual(code, 0, err)
        tx_dict = mock_sign.call_args[0][0]
        self.assertEqual(tx_dict["type"], 2)
        self.assertEqual(tx_dict["maxFeePerGas"], 2_300_000_000)
        self.assertEqual(tx_dict["maxPriorityFeePerGas"], 100_000_000)
        self.assertNotIn("gasPrice", tx_dict)


class TestSecretsAndOutputSanitization(OfflineTestCase):
    def test_error_messages_do_not_leak_sensitive_values(self):
        sensitive_pw_name = "super_classified_pw_file_2026.txt"
        with self.assertRaises(CastSignerError) as ctx:
            validate_password_file(f"/custom/path/{sensitive_pw_name}")
        self.assertNotIn(sensitive_pw_name, str(ctx.exception))


class TestPythonOptimizedModeSafety(unittest.TestCase):
    def test_python_dash_O_rejects_invalid_inputs_and_post_fee_checks_offline(self):
        probe_code = """
import sys
import urllib.request
from unittest.mock import patch
sys.path.insert(0, '.')
from script import claim, submit_share

assert not __debug__, 'must run under -O'

def forbid(*args, **kwargs):
    raise AssertionError('network/send must not execute')

for bad_args in (
    ['--max-fee-per-gas-gwei', '0.1', '--priority-fee-per-gas-gwei', '0.2'],
    ['--max-fee-per-gas-gwei', '1e1000000'],
    ['--max-fee-per-gas-gwei', '10000000000000000000000000000.0000000005'],
    ['--gas-multiplier', '1e10000'],
    ['--gas-limit', '0'],
):
    with patch('urllib.request.urlopen', side_effect=forbid) as m_url, \\
         patch('script.claim.rpc_call', side_effect=forbid) as m_rpc, \\
         patch('script.claim.cast_send', side_effect=forbid) as m_cast, \\
         patch('eth_account.Account.sign_transaction', side_effect=forbid) as m_sign, \\
         patch('subprocess.run', side_effect=forbid) as m_sub:
        try:
            sys.argv = ['claim.py', '--epoch', '2', '--miner', '0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf', '--rpc', 'http://127.0.0.1:8545', *bad_args]
            try:
                claim.main()
            except SystemExit as exc:
                assert exc.code != 0
            else:
                raise AssertionError('claim.main should fail')
        finally:
            assert m_url.call_count == 0
            assert m_rpc.call_count == 0
            assert m_cast.call_count == 0
            assert m_sign.call_count == 0
            assert m_sub.call_count == 0

    with patch('urllib.request.urlopen', side_effect=forbid) as m_url, \\
         patch('script.submit_share.rpc_call', side_effect=forbid) as m_rpc, \\
         patch('script.submit_share.cast_send', side_effect=forbid) as m_cast, \\
         patch('eth_account.Account.sign_transaction', side_effect=forbid) as m_sign, \\
         patch('subprocess.run', side_effect=forbid) as m_sub:
        try:
            sys.argv = ['submit_share.py', '--miner', '0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf', '--k', '0', '--nonce', '1', '--rpc', 'http://127.0.0.1:8545', *bad_args]
            try:
                submit_share.main()
            except SystemExit as exc:
                assert exc.code != 0
            else:
                raise AssertionError('submit_share.main should fail')
        finally:
            assert m_url.call_count == 0
            assert m_rpc.call_count == 0
            assert m_cast.call_count == 0
            assert m_sign.call_count == 0
            assert m_sub.call_count == 0

bad_fee_info = {
    'network_gas_price_wei': 2_000_000_000,
    'network_priority_fee_wei': 500_000_000,
    'max_fee_per_gas_wei': 400_000_000,
    'priority_fee_per_gas_wei': 500_000_000,
    'effective_multiplier': 1,
    'explicit_max_fee': False,
    'explicit_priority_fee': False,
    'warnings': [],
}
settled = {
    'effectiveQuota': 100, 'totalWeight': 100, 'wMin': 10, 'userWeight': 10,
    'capBps': 2000, 'claimed': False, 'expectedReward': 10,
    'currentEpoch': 5, 'lastSynced': 5, 'epochEnded': True, 'epochSettled': True,
}
with patch('urllib.request.urlopen', side_effect=forbid), \\
     patch('script.claim.calculate_reward_details', return_value=settled), \\
     patch('script.claim.simulate_claim', return_value=(True, 120000, None, '0x1234')), \\
     patch('script.claim.rpc_call', return_value='0x61'), \\
     patch('script.claim.resolve_eip1559_fees', return_value=bad_fee_info), \\
     patch('script.claim.cast_send', side_effect=forbid) as m_cast, \\
     patch('eth_account.Account.sign_transaction', side_effect=forbid) as m_sign:
    sys.argv = ['claim.py', '--epoch', '2', '--miner', '0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf', '--rpc', 'http://127.0.0.1:8545']
    try:
        claim.main()
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError('claim post-check should fail under -O')
    assert m_cast.call_count == 0 and m_sign.call_count == 0

with patch('urllib.request.urlopen', side_effect=forbid), \\
     patch('script.submit_share.simulate_call', return_value=(True, 150000, None)), \\
     patch('script.submit_share.rpc_call', return_value='0x61'), \\
     patch('script.submit_share.resolve_eip1559_fees', return_value=bad_fee_info), \\
     patch('script.submit_share.cast_send', side_effect=forbid) as m_cast, \\
     patch('eth_account.Account.sign_transaction', side_effect=forbid) as m_sign:
    sys.argv = ['submit_share.py', '--miner', '0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf', '--k', '0', '--nonce', '1', '--rpc', 'http://127.0.0.1:8545']
    try:
        submit_share.main()
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError('submit_share post-check should fail under -O')
    assert m_cast.call_count == 0 and m_sign.call_count == 0
print('OPT_OK')
"""
        clean_env = {k: v for k, v in os.environ.items() if k.upper() not in ISOLATED_HOST_ENV_KEYS}
        clean_env["PYTHONUTF8"] = "1"
        clean_env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-O", "-c", probe_code],
            cwd=str(REPO_ROOT),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertIn("OPT_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
