import unittest
import tempfile
import os
import json
import time
import sys
from unittest.mock import patch, MagicMock

from script.keeper_supervisor import KeeperSupervisor

class TestKeeperRecoverySupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clean_exit_code_zero_does_not_restart(self):
        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
        )
        can_restart, reason, backoff = sup.evaluate_crash(0)
        self.assertFalse(can_restart)
        self.assertIn("code 0", reason)
        self.assertEqual(backoff, 0.0)

    def test_exit_code_two_cli_syntax_error_halts(self):
        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
        )
        can_restart, reason, backoff = sup.evaluate_crash(2)
        self.assertFalse(can_restart)
        self.assertIn("code 2", reason)
        self.assertEqual(backoff, 0.0)

    def test_preparing_to_send_state_halts_immediately(self):
        # Write PREPARING_TO_SEND state file
        state_file = os.path.join(self.state_dir, "keeper_97_test.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "status": "PREPARING_TO_SEND",
                "operation": "sync(1)",
                "nonce": 78,
                "tx_hashes": []
            }, f)

        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
        )
        can_restart, reason, backoff = sup.evaluate_crash(1)
        self.assertFalse(can_restart)
        self.assertIn("PREPARING_TO_SEND", reason)
        self.assertIn("Auto-restart forbidden", reason)

    def test_manual_review_locked_state_halts_immediately(self):
        state_file = os.path.join(self.state_dir, "keeper_97_test.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "status": "MANUAL_REVIEW_LOCKED",
                "operation": "initEpochSeed",
                "nonce": 79
            }, f)

        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
        )
        can_restart, reason, backoff = sup.evaluate_crash(1)
        self.assertFalse(can_restart)
        self.assertIn("MANUAL_REVIEW_LOCKED", reason)

    def test_corrupted_state_file_halts(self):
        state_file = os.path.join(self.state_dir, "keeper_97_bad.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("NOT_A_VALID_JSON{")

        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
        )
        can_restart, reason, backoff = sup.evaluate_crash(1)
        self.assertFalse(can_restart)
        self.assertIn("corrupted or unreadable", reason)

    def test_lock_file_held_by_another_process_halts(self):
        lock_file = os.path.join(self.state_dir, "keeper_97_sender_test.lock")
        # Hold lock with mock
        with patch.object(KeeperSupervisor, "check_lock_availability", return_value=(False, "Lock held by active process")):
            sup = KeeperSupervisor(
                keeper_cmd=["python", "-m", "script.keeper"],
                state_dir=self.state_dir,
            )
            can_restart, reason, backoff = sup.evaluate_crash(1)
            self.assertFalse(can_restart)
            self.assertIn("Lock held", reason)

    def test_transient_failure_with_clean_state_restarts_with_backoff(self):
        state_file = os.path.join(self.state_dir, "keeper_97_clean.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "status": "CONFIRMED_SUCCESS",
                "operation": "sync(1)",
                "nonce": 78
            }, f)

        sup = KeeperSupervisor(
            keeper_cmd=["python", "-m", "script.keeper"],
            state_dir=self.state_dir,
            max_restarts=3,
            window_seconds=10.0,
            base_backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        )

        # 1st crash -> backoff 1.0s
        can_r, reason, backoff = sup.evaluate_crash(1)
        self.assertTrue(can_r)
        self.assertEqual(backoff, 1.0)

        # 2nd crash -> backoff 2.0s
        can_r, reason, backoff = sup.evaluate_crash(1)
        self.assertTrue(can_r)
        self.assertEqual(backoff, 2.0)

        # 3rd crash -> backoff 4.0s
        can_r, reason, backoff = sup.evaluate_crash(1)
        self.assertTrue(can_r)
        self.assertEqual(backoff, 4.0)

        # 4th crash -> rate limit exceeded -> HALT!
        can_r, reason, backoff = sup.evaluate_crash(1)
        self.assertFalse(can_r)
        self.assertIn("Crash frequency limit exceeded", reason)

    def test_run_loop_handles_clean_child_exit_safely(self):
        # Run dummy python command that exits 0
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        sup = KeeperSupervisor(
            keeper_cmd=cmd,
            state_dir=self.state_dir,
            max_restarts=2,
            base_backoff_seconds=0.1,
        )
        exit_code = sup.run_loop(max_total_runs=1)
        self.assertEqual(exit_code, 0)

if __name__ == "__main__":
    unittest.main()
