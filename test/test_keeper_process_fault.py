"""
Dedicated subprocess fault and crash recovery tests for PoolCoin Keeper persistent state.

Test classifications explicitly covered:
1. 独立子进程测试 (Independent Subprocess Test):
   - Two distinct OS processes competing for the same sender lock.
   - Race condition between manual unlock and active transaction sending.
2. 进程强制终止测试 (Process Force-Termination Test):
   - Uncatchable process death (proc.kill() / TerminateProcess / SIGKILL).
   - OS-level file lock release verification.
   - NOTE: This verifies OS process termination lock cleanup and atomic file persistence;
     it is NOT a hardware power-loss (断电) durability test.
3. 跨Miner发送者级互斥测试 (Cross-Miner Sender Concurrency Test):
   - Same chainId and sender across different Miner contracts.
   - Sender-level mutex blocks concurrent broadcasts.
   - Startup protection detects cross-miner manual review locks.
4. 模拟故障/同进程测试 (In-Process Simulated Failure Test):
   - Broadcast succeeds on-chain but disk state persistence fails; verifies no automatic rebroadcast.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from script.keeper_state import (
    KeeperStateManager,
    StateDomain,
    DomainLockedError,
    InvalidStateError,
    StateWriteError,
)
from script.keeper import (
    KeeperBot,
    TxExecutionStatus,
    clear_configured_secrets,
)


class TestKeeperSubprocessFaultAndRecovery(unittest.TestCase):
    """Subprocess fault, concurrency, and crash recovery tests."""

    def setUp(self):
        clear_configured_secrets()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = self.tmp_dir.name

    def tearDown(self):
        clear_configured_secrets()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def assert_lock_released_after_process_exit(self, state_manager):
        # Windows may still be releasing byte-range locks after the killed process has exited.
        deadline = time.monotonic() + (2.0 if os.name == "nt" else 0.0)
        while not state_manager.acquire_lock():
            if time.monotonic() >= deadline:
                self.fail("Lock must be acquirable after holding process was forcefully killed")
            time.sleep(0.01)

    def test_subprocess_lock_mutual_exclusion(self):
        """[独立子进程测试] Subprocess A holds lock, Subprocess B is refused."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        code_holder = f"""
import sys, time, os
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper_state import KeeperStateManager, StateDomain

domain = StateDomain(chain_id=97, miner_address="{miner}", sender_address="{sender}")
sm = KeeperStateManager(state_dir=r"{self.state_dir}", domain=domain)
if not sm.acquire_lock():
    sys.stdout.write("ACQUIRE_FAILED\\n")
    sys.stdout.flush()
    sys.exit(1)

sys.stdout.write("LOCKED\\n")
sys.stdout.flush()
time.sleep(10)
sm.release_lock()
sys.stdout.write("RELEASED\\n")
sys.stdout.flush()
"""
        proc_a = subprocess.Popen(
            [sys.executable, "-c", code_holder],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            line = proc_a.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            # Now Subprocess B tries to acquire the same lock
            domain_b = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm_b = KeeperStateManager(state_dir=self.state_dir, domain=domain_b)
            self.assertFalse(sm_b.acquire_lock(), "Subprocess B must be refused when Subprocess A holds the lock")

        finally:
            proc_a.kill()
            proc_a.wait()

    def test_process_termination_releases_os_file_lock(self):
        """[进程强制终止测试] Forcefully killing Subprocess A releases OS lock; next process can acquire."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        code_holder = f"""
import sys, time, os
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper_state import KeeperStateManager, StateDomain

domain = StateDomain(chain_id=97, miner_address="{miner}", sender_address="{sender}")
sm = KeeperStateManager(state_dir=r"{self.state_dir}", domain=domain)
if sm.acquire_lock():
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    time.sleep(30)
"""
        proc_a = subprocess.Popen(
            [sys.executable, "-c", code_holder],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            line = proc_a.stdout.readline()
            self.assertEqual(line.strip(), "LOCKED")

            # Force kill Subprocess A (simulate ungraceful crash / kill -9 / TerminateProcess)
            proc_a.kill()
            proc_a.wait()
            self.assertNotEqual(proc_a.returncode, 0)

            domain_c = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm_c = KeeperStateManager(state_dir=self.state_dir, domain=domain_c)
            self.assert_lock_released_after_process_exit(sm_c)
            sm_c.release_lock()
        finally:
            if proc_a.poll() is None:
                proc_a.kill()
                proc_a.wait()

    def test_crash_preserves_preparing_to_send_and_triggers_manual_review(self):
        """[进程强制终止与重启测试] PREPARING_TO_SEND survives process death; restart halts automated broadcast."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        code_crash = f"""
import sys, time, os
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper_state import KeeperStateManager, StateDomain

domain = StateDomain(chain_id=97, miner_address="{miner}", sender_address="{sender}")
sm = KeeperStateManager(state_dir=r"{self.state_dir}", domain=domain)
sm.acquire_lock()
sm.record_preparing_to_send(operation="syncEmptyRound", epoch=42, nonce=7, details="Simulated in-flight tx")
sys.stdout.write("STATE_WRITTEN\\n")
sys.stdout.flush()
time.sleep(30)
"""
        proc_crash = subprocess.Popen(
            [sys.executable, "-c", code_crash],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            line = proc_crash.stdout.readline()
            self.assertEqual(line.strip(), "STATE_WRITTEN")

            # Simulate sudden crash right after broadcast
            proc_crash.kill()
            proc_crash.wait()

            # Verify that state file exists on disk and still has PREPARING_TO_SEND
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm_new = KeeperStateManager(state_dir=self.state_dir, domain=domain)
            raw_state = sm_new.load_state()
            self.assertIsNotNone(raw_state)
            self.assertEqual(raw_state["status"], "PREPARING_TO_SEND")
            self.assertEqual(raw_state["nonce"], 7)

            # Check startup protection: pure read-only check
            can_proceed, block_reason, state_data = sm_new.check_startup_protection()
            self.assertFalse(can_proceed)
            self.assertIn("crash before confirmation", block_reason.lower())

            # Verify that check_startup_protection is strictly read-only and did not modify state on disk
            raw_state_check = sm_new.load_state()
            self.assertEqual(raw_state_check["status"], "PREPARING_TO_SEND")
            self.assertEqual(raw_state_check["nonce"], 7)

            # Calling recover_crash_to_manual_review WITHOUT holding lock must raise DomainLockedError
            with self.assertRaises(DomainLockedError):
                sm_new.recover_crash_to_manual_review(state_data)

            # Acquire lock and execute crash recovery transition
            self.assert_lock_released_after_process_exit(sm_new)
            try:
                recovered_state = sm_new.recover_crash_to_manual_review(state_data)
                self.assertEqual(recovered_state["status"], "MANUAL_REVIEW_LOCKED")
                self.assertEqual(recovered_state["reason_code"], "CRASH_BEFORE_CONFIRMATION")
            finally:
                sm_new.release_lock()

            # Verify transition on disk
            after_state = sm_new.load_state()
            self.assertEqual(after_state["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(after_state["reason_code"], "CRASH_BEFORE_CONFIRMATION")
            self.assertEqual(after_state["nonce"], 7)

        finally:
            if proc_crash.poll() is None:
                proc_crash.kill()
                proc_crash.wait()

    def test_manual_unlock_and_send_race_mutual_exclusion(self):
        """[独立子进程互斥测试] Manual unlock cannot enter critical section while send process holds lock."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        # Create an existing manual review lock in state
        domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
        sm_setup = KeeperStateManager(state_dir=self.state_dir, domain=domain)
        sm_setup.record_manual_review_lock(
            reason="Simulated crash lock",
            nonce=10,
            operation="drawWinner",
            tx_hash="0x" + "c" * 64,
            reason_code="MANUAL_RACE_TEST"
        )

        code_sender = f"""
import sys, time, os
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper_state import KeeperStateManager, StateDomain

domain = StateDomain(chain_id=97, miner_address="{miner}", sender_address="{sender}")
sm = KeeperStateManager(state_dir=r"{self.state_dir}", domain=domain)
if sm.acquire_lock():
    sys.stdout.write("IN_CRITICAL_SECTION\\n")
    sys.stdout.flush()
    time.sleep(3)
    sm.release_lock()
    sys.stdout.write("CRITICAL_SECTION_EXITED\\n")
    sys.stdout.flush()
"""
        proc_sender = subprocess.Popen(
            [sys.executable, "-c", code_sender],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            line = proc_sender.stdout.readline()
            self.assertEqual(line.strip(), "IN_CRITICAL_SECTION")

            # While sender is in critical section, manual_unlock MUST be refused by mutual exclusion
            with self.assertRaises(DomainLockedError):
                sm_setup.manual_unlock(expected_nonce=10, operator_reason="Operator verified on-chain")

            # Wait for sender to exit critical section
            exit_line = proc_sender.stdout.readline()
            self.assertEqual(exit_line.strip(), "CRITICAL_SECTION_EXITED")

            # Now manual unlock succeeds cleanly
            res = sm_setup.manual_unlock(expected_nonce=10, operator_reason="Operator verified on-chain after sender done")
            self.assertEqual(res["status"], "IDLE")
            self.assertEqual(res["reason_code"], "UNLOCKED_MANUALLY")

        finally:
            if proc_sender.poll() is None:
                proc_sender.kill()
                proc_sender.wait()

    def test_same_sender_cross_miner_concurrency_refused(self):
        """[跨Miner发送者级互斥测试] Same chainId and sender across different Miners blocks concurrent broadcast."""
        miner_1 = "0x420382a0f874c1F120621f847d81581e8737504e"
        miner_2 = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        # Subprocess A holds lock on Miner 1
        code_miner1 = f"""
import sys, time, os
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper_state import KeeperStateManager, StateDomain

domain = StateDomain(chain_id=97, miner_address="{miner_1}", sender_address="{sender}")
sm = KeeperStateManager(state_dir=r"{self.state_dir}", domain=domain)
if sm.acquire_lock():
    sys.stdout.write("MINER1_LOCKED\\n")
    sys.stdout.flush()
    time.sleep(10)
"""
        proc_1 = subprocess.Popen(
            [sys.executable, "-c", code_miner1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            line = proc_1.stdout.readline()
            self.assertEqual(line.strip(), "MINER1_LOCKED")

            # Subprocess B on Miner 2 (same sender, same chain) tries to acquire lock
            domain_2 = StateDomain(chain_id=97, miner_address=miner_2, sender_address=sender)
            sm_2 = KeeperStateManager(state_dir=self.state_dir, domain=domain_2)
            self.assertFalse(sm_2.acquire_lock(), "Cross-miner concurrency for the same sender must be blocked by sender mutex")

        finally:
            proc_1.kill()
            proc_1.wait()

        # In addition, test cross-miner persistent state collision detection
        # If Miner 1 is locked in manual review, Miner 2 checking startup protection must block
        domain_1 = StateDomain(chain_id=97, miner_address=miner_1, sender_address=sender)
        sm_1 = KeeperStateManager(state_dir=self.state_dir, domain=domain_1)
        sm_1.record_manual_review_lock(
            reason="Miner 1 unconfirmed tx",
            nonce=15,
            operation="initEpochSeed",
            tx_hash="0x" + "e" * 64,
            reason_code="MINER1_UNCONFIRMED"
        )

        domain_2 = StateDomain(chain_id=97, miner_address=miner_2, sender_address=sender)
        sm_2 = KeeperStateManager(state_dir=self.state_dir, domain=domain_2)
        can_proceed_2, block_reason_2, _ = sm_2.check_startup_protection()
        self.assertFalse(can_proceed_2)
        self.assertIn("cross-miner collision protection", block_reason_2.lower())

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_broadcast_succeeded_persistence_failure_prevents_rebroadcast(self, mock_subproc, mock_rpc):
        """[模拟故障/同进程测试] Broadcast succeeds on-chain but disk persistence fails: enters manual review without rebroadcast."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "f" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx}\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            state_dir=self.state_dir
        )
        sm = bot._ensure_state_manager()

        # Simulate persistence failure when saving CONFIRMED_SUCCESS
        with patch.object(sm, "record_confirmed_success", side_effect=StateWriteError("Disk quota exceeded")):
            res = bot.send_transaction("0x1234", "test_op", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
            self.assertTrue(bot.locked_for_manual_review)
            self.assertEqual(res.tx_hash, fake_tx)

        # Ensure bot is locked and subsequent step/send refuses to rebroadcast
        res2 = bot.send_transaction("0x1234", "test_op2", preflight=False)
        self.assertEqual(res2.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(bot.locked_for_manual_review)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_reread_state_and_fetch_nonce_under_lock(self, mock_subproc, mock_rpc):
        """[同进程/互斥范围测试] Verifies persistent state is re-read and nonce is queried strictly inside acquired mutex lock."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "a" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx}\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            state_dir=self.state_dir
        )
        sm = bot._ensure_state_manager()

        lock_held_during_nonce = []
        orig_get_nonce = bot.get_nonce

        def tracked_get_nonce():
            lock_held_during_nonce.append(bot._domain_lock_acquired)
            return orig_get_nonce()

        bot.get_nonce = tracked_get_nonce

        res = bot.send_transaction("0x1234", "test_lock_scope", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)
        self.assertTrue(len(lock_held_during_nonce) > 0)
        self.assertTrue(all(lock_held_during_nonce), "Nonce MUST be fetched only after mutex lock is acquired")
        self.assertFalse(bot._domain_lock_acquired, "Lock must be released upon completion")

        # Test Case B: Disk state locked prior to send -> re-read inside lock detects it and blocks
        sm.record_manual_review_lock(
            reason="Concurrent state lock",
            nonce=20,
            operation="test_op",
            tx_hash="0x" + "b" * 64,
            reason_code="MANUAL_LOCKED_RACE"
        )
        res_blocked = bot.send_transaction("0x1234", "test_blocked_by_reread", preflight=False)
        self.assertEqual(res_blocked.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(bot.locked_for_manual_review)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_send_refused_while_manual_unlock_holds_lock(self, mock_subproc, mock_rpc):
        """[反向竞态测试] Concurrent send_transaction is refused while manual_unlock holds the sender lock (cross-process & in-process, zero sleep)."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        tx_h = "0x" + "d" * 64

        domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
        sm_setup = KeeperStateManager(state_dir=self.state_dir, domain=domain)
        sm_setup.record_manual_review_lock(
            reason="Simulated crash lock before unlock",
            nonce=10,
            operation="sync(1)",
            tx_hash=tx_h,
            reason_code="REVERSE_RACE_TEST",
        )

        # Part 1: Independent subprocess runs KeeperBot.manual_unlock and pauses inside snapshot RPC while holding OS lock
        code_unlocker = f"""
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, r"{REPO_ROOT}")
from script.keeper import KeeperBot

def subproc_rpc(url, method, params):
    if method == "eth_getCode":
        return "0x6080604052"
    if method == "eth_chainId":
        return "0x61"
    if method == "eth_getTransactionCount":
        if len(params) > 1 and params[1] == "latest":
            sys.stdout.write("UNLOCK_HOLDING_LOCK" + chr(10))
            sys.stdout.flush()
            sys.stdin.readline()
        return "0xa"
    if method == "eth_getTransactionReceipt":
        return {{"status": "0x1", "blockNumber": "0x100"}}
    return "0x0"

with patch("script.keeper.rpc_call", side_effect=subproc_rpc), \\
     patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{sender}" + chr(10), stderr="")):
    bot = KeeperBot(
        rpc_url="http://mock-rpc",
        miner_address="{miner}",
        account="test-keystore-account",
        password_file="fake-passwords/dev.pwd",
        dry_run=False,
        state_dir=r"{self.state_dir}",
    )
    res = bot.manual_unlock(expected_nonce=10, operator_reason="Unlock holding lock during send race")
    sys.stdout.write("UNLOCK_DONE:" + res["status"] + chr(10))
    sys.stdout.flush()
"""
        proc_unlock = subprocess.Popen(
            [sys.executable, "-c", code_unlocker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        cast_send_calls = []

        def parent_rpc(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_sendRawTransaction":
                cast_send_calls.append(method)
                raise AssertionError("eth_sendRawTransaction must not be called while unlock holds lock")
            return "0x0"

        def parent_subproc(cmd, *args, **kwargs):
            if "send" in cmd:
                cast_send_calls.append(list(cmd))
                raise AssertionError("cast send must not be called while unlock holds lock")
            return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        mock_rpc.side_effect = parent_rpc
        mock_subproc.side_effect = parent_subproc

        try:
            line = proc_unlock.stdout.readline()
            self.assertEqual(line.strip(), "UNLOCK_HOLDING_LOCK")

            bot_sender = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=self.state_dir,
            )
            res_send = bot_sender.send_transaction("0x1234", "concurrent_send_during_unlock", preflight=False)
            self.assertEqual(res_send.status, TxExecutionStatus.UNCERTAIN)
            self.assertIn("locked by another active Keeper process", res_send.error)
            self.assertEqual(len(cast_send_calls), 0)
            self.assertEqual(sm_setup.load_state()["status"], "MANUAL_REVIEW_LOCKED")

            proc_unlock.stdin.write("CONTINUE\n")
            proc_unlock.stdin.flush()
            proc_unlock.stdin.close()
            done_line = proc_unlock.stdout.readline()
            self.assertEqual(done_line.strip(), "UNLOCK_DONE:IDLE")
            self.assertEqual(proc_unlock.wait(timeout=10), 0)
            self.assertEqual(sm_setup.load_state()["status"], "IDLE")
        finally:
            if proc_unlock.poll() is None:
                proc_unlock.kill()
                proc_unlock.wait()

        # Part 2: In-process thread race on both same and distinct KeeperBot instances via Event synchronization
        sm_setup.record_manual_review_lock(
            reason="Second lock for in-process reverse race",
            nonce=11,
            operation="sync(1)",
            tx_hash=tx_h,
            reason_code="REVERSE_RACE_THREAD_TEST",
        )
        entered_snapshot = threading.Event()
        allow_snapshot_finish = threading.Event()
        unlock_result = {}

        def thread_rpc(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_getTransactionCount":
                if len(params) > 1 and params[1] == "latest":
                    entered_snapshot.set()
                    self.assertTrue(allow_snapshot_finish.wait(timeout=10))
                return "0xb"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x101"}
            if method == "eth_sendRawTransaction":
                cast_send_calls.append(method)
                raise AssertionError("eth_sendRawTransaction must not be called during concurrent unlock")
            return "0x0"

        mock_rpc.side_effect = thread_rpc

        bot_unlock = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address=miner,
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            state_dir=self.state_dir,
        )
        bot_other = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address=miner,
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            state_dir=self.state_dir,
        )

        def run_unlock():
            try:
                unlock_result["state"] = bot_unlock.manual_unlock(
                    expected_nonce=11,
                    operator_reason="Thread unlock race test",
                )
            except Exception as exc:
                unlock_result["error"] = exc

        t = threading.Thread(target=run_unlock)
        t.start()
        try:
            self.assertTrue(entered_snapshot.wait(timeout=10))
            # Both same instance and another instance must be refused without entering critical section
            res_same = bot_unlock.send_transaction("0x1234", "same_instance_send", preflight=False)
            self.assertEqual(res_same.status, TxExecutionStatus.UNCERTAIN)
            res_other = bot_other.send_transaction("0x1234", "other_instance_send", preflight=False)
            self.assertEqual(res_other.status, TxExecutionStatus.UNCERTAIN)
            self.assertEqual(len(cast_send_calls), 0)
        finally:
            allow_snapshot_finish.set()
            t.join(timeout=10)

        self.assertNotIn("error", unlock_result)
        self.assertEqual(unlock_result["state"]["status"], "IDLE")
        self.assertTrue(sm_setup.acquire_lock())
        sm_setup.release_lock()


if __name__ == "__main__":
    unittest.main()
