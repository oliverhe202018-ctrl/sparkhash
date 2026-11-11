#!/usr/bin/env python3
"""Offline mock tests for cast_signer and keystore signing in claim.py and submit_share.py."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
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
    cast_send,
    get_isolated_cast_env,
    get_keystore_address,
    to_checksum_address,
    parse_pending_nonce,
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
            ("0x1a", 26), ("0X5", 5), ("0", 0), ("42", 42),
        ]
        for inp, exp in valid_cases:
            with self.subTest(inp=inp):
                self.assertEqual(parse_pending_nonce(inp), exp)

    def test_parse_pending_nonce_invalid_inputs(self):
        invalid_cases = [
            True, False, -1, -10, "-1", "-0x1", None, "", "   ",
            "0x", "0xgh", "abc", "12.34", [0], {"nonce": 0},
        ]
        for inv in invalid_cases:
            with self.subTest(inv=inv):
                with self.assertRaises(ValueError):
                    parse_pending_nonce(inv)

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
            self.assertIn("--gas-limit", cmd)
            self.assertEqual(cmd[cmd.index("--gas-limit") + 1], "150000")
            self.assertIn("--async", cmd)

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
                    )
            self.assertIn("code 1:", str(ctx.exception))
        finally:
            if os.path.exists(pw_path):
                os.unlink(pw_path)


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
        }
        argv = ["claim.py", "--epoch", "2", "--send", "--rpc", "http://127.0.0.1:8545"]
        with patch("script.claim.rpc_call", side_effect=lambda url, m, p=None: "0x61" if m == "eth_chainId" else hex(2_000_000_000)):
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
        with patch("script.submit_share.rpc_call", side_effect=lambda url, m, p=None: "0x61" if m == "eth_chainId" else hex(2_000_000_000)):
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


class TestSecretsAndOutputSanitization(OfflineTestCase):
    def test_error_messages_do_not_leak_sensitive_values(self):
        sensitive_pw_name = "super_classified_pw_file_2026.txt"
        with self.assertRaises(CastSignerError) as ctx:
            validate_password_file(f"/custom/path/{sensitive_pw_name}")
        self.assertNotIn(sensitive_pw_name, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
