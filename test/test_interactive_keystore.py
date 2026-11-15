#!/usr/bin/env python3
"""Offline unit and integration tests for custom --keystore and interactive TTY signing in PoolCoin."""

import io
import os
import sys
import tempfile
import unittest
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
    get_keystore_address,
    validate_keystore_path,
)

TEST_MINER = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
OTHER_MINER = "0x1111111111111111111111111111111111111111"
TEST_TX_HASH = "0x" + "1234567890abcdef" * 4

ISOLATED_HOST_ENV_KEYS = (
    "PRIVATE_KEY", "MINER_PRIVATE_KEY", "KEEPER_PRIVATE_KEY", "DEPLOYER_PRIVATE_KEY",
    "SIGNER_PRIVATE_KEY", "ETH_PRIVATE_KEY", "ETH_PASSWORD", "ETH_KEYSTORE",
    "ETH_KEYSTORE_ACCOUNT", "CAST_ACCOUNT", "CAST_PASSWORD_FILE", "FOUNDRY_PRIVATE_KEY",
    "FOUNDRY_ACCOUNT", "FOUNDRY_PASSWORD", "FOUNDRY_PASSWORD_FILE",
)


def _run_cli(main_fn, argv, env_overrides=None):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    clean_env = {k: v for k, v in os.environ.items() if k.upper() not in ISOLATED_HOST_ENV_KEYS}
    if env_overrides:
        clean_env.update(env_overrides)
    exit_code = 0
    with patch.dict(os.environ, clean_env, clear=True):
        with patch.object(sys, "argv", argv):
            with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
                try:
                    main_fn()
                except SystemExit as exc:
                    exit_code = exc.code if isinstance(exc.code, int) else 1
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


class TestKeystoreDirect(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.keystore_file = os.path.join(self.tmp_dir.name, "test_keystore.json")
        with open(self.keystore_file, "w") as f:
            f.write('{"address":"565d06e8d0313e2f8b5aa4b19de569ca2200c4cf","crypto":{}}')
        self.pw_file = os.path.join(self.tmp_dir.name, "pw.txt")
        with open(self.pw_file, "w") as f:
            f.write("secret_password\n")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_validate_keystore_path_success(self):
        clean = validate_keystore_path(self.keystore_file)
        self.assertEqual(clean, self.keystore_file)

    def test_validate_keystore_path_nonexistent_fails_and_sanitizes(self):
        nonexistent = os.path.join(self.tmp_dir.name, "missing_key.json")
        with self.assertRaises(CastSignerError) as ctx:
            validate_keystore_path(nonexistent)
        self.assertIn("Keystore file does not exist", str(ctx.exception))

    def test_validate_keystore_path_directory_fails(self):
        with self.assertRaises(CastSignerError) as ctx:
            validate_keystore_path(self.tmp_dir.name)
        self.assertIn("is a directory", str(ctx.exception))

    def test_validate_keystore_path_empty_fails(self):
        with self.assertRaises(CastSignerError):
            validate_keystore_path("")
        with self.assertRaises(CastSignerError):
            validate_keystore_path("   ")

    def test_get_keystore_address_mutex(self):
        with self.assertRaises(CastSignerError) as ctx:
            get_keystore_address(account="my_acc", keystore=self.keystore_file)
        self.assertIn("Cannot specify both 'account' and 'keystore'", str(ctx.exception))

        with self.assertRaises(CastSignerError) as ctx:
            get_keystore_address()
        self.assertIn("Either 'account' or 'keystore' must be specified", str(ctx.exception))

    def test_get_keystore_address_with_password_file(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout=f"{TEST_MINER}\n", stderr="")
            addr = get_keystore_address(keystore=self.keystore_file, password_file=self.pw_file)
            self.assertEqual(addr, TEST_MINER)
            cmd = mock_sub.call_args[0][0]
            self.assertIn("--keystore", cmd)
            self.assertIn(self.keystore_file, cmd)
            self.assertIn("--password-file", cmd)

    def test_get_keystore_address_interactive_refused_when_no_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CastSignerError) as ctx:
                get_keystore_address(keystore=self.keystore_file, password_file=None)
            self.assertIn("requires an interactive TTY terminal", str(ctx.exception))

    def test_get_keystore_address_interactive_success_on_tty(self):
        with patch("sys.stdin.isatty", return_value=True):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(returncode=0, stdout=f"{TEST_MINER}\n", stderr="")
                addr = get_keystore_address(keystore=self.keystore_file, password_file=None)
                self.assertEqual(addr, TEST_MINER)
                cmd = mock_sub.call_args[0][0]
                self.assertIn("--keystore", cmd)
                self.assertNotIn("--password-file", cmd)

    def test_cast_send_keystore_with_password_file(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout=f"{TEST_TX_HASH}\n", stderr="")
            tx = cast_send(
                contract_address=TEST_MINER,
                calldata="0x1234",
                account=None,
                password_file=self.pw_file,
                rpc_url="http://127.0.0.1:8545",
                nonce=0,
                gas_price=1000,
                gas_limit=21000,
                priority_gas_price=100,
                keystore=self.keystore_file,
            )
            self.assertEqual(tx, TEST_TX_HASH)
            cmd = mock_sub.call_args[0][0]
            self.assertIn("--keystore", cmd)
            self.assertIn(self.keystore_file, cmd)
            self.assertIn("--password-file", cmd)

    def test_cast_send_keystore_interactive_refused_when_no_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(CastSignerError) as ctx:
                cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x1234",
                    account=None,
                    password_file=None,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=1000,
                    gas_limit=21000,
                    priority_gas_price=100,
                    keystore=self.keystore_file,
                )
            self.assertIn("requires an interactive TTY terminal", str(ctx.exception))

    def test_cast_send_keystore_interactive_success_on_tty(self):
        with patch("sys.stdin.isatty", return_value=True):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(returncode=0, stdout=f"{TEST_TX_HASH}\n", stderr="")
                tx = cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x1234",
                    account=None,
                    password_file=None,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=1000,
                    gas_limit=21000,
                    priority_gas_price=100,
                    keystore=self.keystore_file,
                )
                self.assertEqual(tx, TEST_TX_HASH)
                cmd = mock_sub.call_args[0][0]
                self.assertIn("--keystore", cmd)
                self.assertNotIn("--password-file", cmd)

    def test_cast_send_keystore_timeout_raises_uncertain(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="cast", timeout=5.0)):
            with self.assertRaises(CastSendUncertainError):
                cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x1234",
                    account=None,
                    password_file=self.pw_file,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=1000,
                    gas_limit=21000,
                    priority_gas_price=100,
                    keystore=self.keystore_file,
                )

    def test_cast_send_keystore_missing_hash_raises_uncertain(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="transaction sent successfully without hash\n", stderr="")
            with self.assertRaises(CastSendUncertainError):
                cast_send(
                    contract_address=TEST_MINER,
                    calldata="0x1234",
                    account=None,
                    password_file=self.pw_file,
                    rpc_url="http://127.0.0.1:8545",
                    nonce=0,
                    gas_price=1000,
                    gas_limit=21000,
                    priority_gas_price=100,
                    keystore=self.keystore_file,
                )


class TestCliKeystoreIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.keystore_file = os.path.join(self.tmp_dir.name, "test_keystore.json")
        with open(self.keystore_file, "w") as f:
            f.write('{"address":"565d06e8d0313e2f8b5aa4b19de569ca2200c4cf"}')
        self.pw_file = os.path.join(self.tmp_dir.name, "pw.txt")
        with open(self.pw_file, "w") as f:
            f.write("pw\n")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_claim_keystore_mutex_account_rejected(self):
        argv = [
            "claim.py", "--epoch", "1", "--send",
            "--account", "my_account",
            "--keystore", self.keystore_file,
            "--password-file", self.pw_file,
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(claim.main, argv)
        self.assertNotEqual(code, 0)
        self.assertIn("参数冲突", out + err)

    def test_claim_keystore_mutex_env_pk_rejected(self):
        argv = [
            "claim.py", "--epoch", "1", "--send",
            "--keystore", self.keystore_file,
            "--password-file", self.pw_file,
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(claim.main, argv, env_overrides={"PRIVATE_KEY": "0x" + "11" * 32})
        self.assertNotEqual(code, 0)
        self.assertIn("同时提供了 keystore 参数", out + err)

    def test_claim_keystore_interactive_fails_when_no_tty(self):
        argv = [
            "claim.py", "--epoch", "1", "--send",
            "--keystore", self.keystore_file,
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            with patch("sys.stdin.isatty", return_value=False):
                code, out, err = _run_cli(claim.main, argv)
        self.assertNotEqual(code, 0)
        self.assertIn("requires an interactive TTY terminal", out + err)

    def test_claim_keystore_miner_address_mismatch_rejected(self):
        argv = [
            "claim.py", "--epoch", "1", "--send",
            "--keystore", self.keystore_file,
            "--password-file", self.pw_file,
            "--miner", OTHER_MINER,
        ]
        with patch("script.claim.rpc_call", return_value="0x61"):
            with patch("script.claim.get_keystore_address", return_value=TEST_MINER):
                code, out, err = _run_cli(claim.main, argv)
        self.assertNotEqual(code, 0)
        self.assertIn("不一致", out + err)

    def test_claim_user_cancels_broadcast_zero_send(self):
        argv = [
            "claim.py", "--epoch", "1", "--send",
            "--keystore", self.keystore_file,
            "--password-file", self.pw_file,
        ]
        mock_r_details = {
            "claimed": False, "epochEnded": True, "epochSettled": True,
            "currentEpoch": 2, "lastSynced": 2, "effectiveQuota": 10**18,
            "userWeight": 100, "totalWeight": 1000, "wMin": 100, "capBps": 2000,
            "expectedReward": 10**17,
        }
        with patch("script.claim.rpc_call", return_value="0x61"), \
             patch("script.claim.calculate_reward_details", return_value=mock_r_details), \
             patch("script.claim.simulate_claim", return_value=(True, 21000, "", "0x1234")), \
             patch("script.claim.resolve_eip1559_fees", return_value={
                 "network_gas_price_wei": 1000, "network_priority_fee_wei": 100,
                 "max_fee_per_gas_wei": 1000, "priority_fee_per_gas_wei": 100,
                 "effective_multiplier": 1.15, "explicit_max_fee": False,
                 "explicit_priority_fee": False, "warnings": [],
             }), \
             patch("script.claim.get_keystore_address", return_value=TEST_MINER), \
             patch("builtins.input", return_value="no"), \
             patch("script.claim.cast_send") as mock_send:
            code, out, err = _run_cli(claim.main, argv)

        self.assertEqual(code, 0)
        self.assertIn("取消", out + err)
        mock_send.assert_not_called()

    def test_submit_share_keystore_mutex_account_rejected(self):
        argv = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "1", "--send",
            "--account", "acc1", "--keystore", self.keystore_file, "--password-file", self.pw_file,
        ]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            code, out, err = _run_cli(submit_share.main, argv)
        self.assertNotEqual(code, 0)
        self.assertIn("Cannot specify both --account and --keystore", out + err)

    def test_submit_share_keystore_interactive_fails_when_no_tty(self):
        argv = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "1", "--send",
            "--keystore", self.keystore_file,
        ]
        with patch("script.submit_share.rpc_call", return_value="0x61"):
            with patch("sys.stdin.isatty", return_value=False):
                code, out, err = _run_cli(submit_share.main, argv)
        self.assertNotEqual(code, 0)
        self.assertIn("requires an interactive TTY terminal", out + err)

    def test_submit_share_user_cancels_broadcast_zero_send(self):
        argv = [
            "submit_share.py", "--miner", TEST_MINER, "--k", "0", "--nonce", "1", "--send",
            "--keystore", self.keystore_file, "--password-file", self.pw_file,
        ]
        with patch("script.submit_share.rpc_call", return_value="0x61"), \
             patch("script.submit_share.simulate_call", return_value=(True, 50000, "")), \
             patch("script.submit_share.resolve_eip1559_fees", return_value={
                 "network_gas_price_wei": 1000, "network_priority_fee_wei": 100,
                 "max_fee_per_gas_wei": 1000, "priority_fee_per_gas_wei": 100,
                 "effective_multiplier": 1.15, "explicit_max_fee": False,
                 "explicit_priority_fee": False, "warnings": [],
             }), \
             patch("script.submit_share.get_keystore_address", return_value=TEST_MINER), \
             patch("builtins.input", return_value="no"), \
             patch("script.submit_share.cast_send") as mock_send:
            code, out, err = _run_cli(submit_share.main, argv)

        self.assertEqual(code, 0)
        self.assertIn("cancelled", out + err)
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
