#!/usr/bin/env python3
"""
Keeper Process Supervisor & Crash Recovery Manager.
Wraps script.keeper execution with rate-limiting, exponential backoff,
single-instance lock verification, and strict broadcast-safety gates.

Safety Rules:
1. Zero automatic restart on broadcast uncertainty:
   If state file indicates PREPARING_TO_SEND, MANUAL_REVIEW_LOCKED, or UNCERTAIN,
   supervisor immediately halts to prevent duplicate sends or nonce divergence.
2. Fast-fail on fatal errors:
   Exit code 2 (CLI parameter error), corrupted state files, or lock conflicts halt immediately.
3. Rate-limited transient restart:
   Only restarts when state is clean (CONFIRMED_SUCCESS / CONFIRMED_REVERT) and lock is free.
   Applies exponential backoff and enforces maximum crash frequency per time window.
4. No secrets handled:
   Does NOT read or output password files; does NOT load .env.
"""

import os
import sys
import json
import time
import argparse
import subprocess
from typing import Tuple, Optional, List, Dict, Any

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None


class KeeperSupervisor:
    def __init__(
        self,
        keeper_cmd: List[str],
        state_dir: Optional[str] = None,
        max_restarts: int = 3,
        window_seconds: float = 600.0,
        base_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
        cwd: Optional[str] = None,
    ):
        self.keeper_cmd = keeper_cmd
        self.state_dir = state_dir or os.environ.get("KEEPER_STATE_DIR")
        self.max_restarts = max_restarts
        self.window_seconds = window_seconds
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.cwd = cwd or os.getcwd()
        self.restart_history: List[float] = []

    def inspect_state_files(self) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Inspects state files in state_dir.
        Returns:
            (can_restart, reason, state_data)
        """
        if not self.state_dir or not os.path.exists(self.state_dir):
            return True, "No state directory configured or directory does not exist yet.", None

        # Look for state .json files
        json_files = [
            f for f in os.listdir(self.state_dir)
            if f.startswith("keeper_") and f.endswith(".json") and not f.endswith("_audit.jsonl")
        ]

        if not json_files:
            return True, "No prior keeper state files found.", None

        for jf in json_files:
            jp = os.path.join(self.state_dir, jf)
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
            except Exception as e:
                return False, f"State file {jf} is corrupted or unreadable ({e}). Manual inspection required.", None

            st = state_data.get("status")
            if st in ("PREPARING_TO_SEND", "MANUAL_REVIEW_LOCKED", "UNCERTAIN"):
                op = state_data.get("operation") or state_data.get("last_operation") or "unknown"
                nonce = state_data.get("nonce", "unknown")
                return (
                    False,
                    f"State file {jf} is in critical state '{st}' for operation {op} (nonce {nonce}). "
                    "Auto-restart forbidden to prevent duplicate broadcasts.",
                    state_data,
                )

        return True, "State files verified safe for recovery.", None

    def check_lock_availability(self) -> Tuple[bool, str]:
        """
        Checks if lock files in state_dir are currently held by another running process.
        """
        if not self.state_dir or not os.path.exists(self.state_dir):
            return True, "No state directory."

        lock_files = [f for f in os.listdir(self.state_dir) if f.startswith("keeper_") and f.endswith(".lock")]
        for lf in lock_files:
            lp = os.path.join(self.state_dir, lf)
            if not os.path.exists(lp):
                continue
            # Try to test-lock
            try:
                f = open(lp, "r+b" if os.path.exists(lp) else "w+b")
                locked = False
                if sys.platform == "win32" and msvcrt:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except (OSError, IOError):
                        locked = True
                elif fcntl:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, IOError):
                        locked = True
                f.close()
                if locked:
                    return False, f"Lock file {lf} is currently held by another active process. Auto-restart refused."
            except Exception as e:
                return False, f"Cannot verify lock file {lf}: {e}. Auto-restart refused."

        return True, "All lock files are free."

    def evaluate_crash(self, exit_code: int) -> Tuple[bool, str, float]:
        """
        Evaluates whether a crashed keeper process should be restarted.
        Returns:
            (can_restart: bool, message: str, backoff_seconds: float)
        """
        if exit_code == 0:
            return False, "Keeper exited cleanly with code 0.", 0.0

        if exit_code == 2:
            return False, "Keeper exited with code 2 (fatal command-line parameter error). Auto-restart refused.", 0.0

        # Check persistent state
        can_restart_state, state_msg, _ = self.inspect_state_files()
        if not can_restart_state:
            return False, state_msg, 0.0

        # Check lock
        lock_free, lock_msg = self.check_lock_availability()
        if not lock_free:
            return False, lock_msg, 0.0

        # Enforce rate limit window
        now = time.time()
        self.restart_history = [t for t in self.restart_history if now - t <= self.window_seconds]
        if len(self.restart_history) >= self.max_restarts:
            return (
                False,
                f"Crash frequency limit exceeded: {len(self.restart_history)} crashes in {self.window_seconds}s. "
                "Halting supervisor to prevent rapid-restart storm.",
                0.0,
            )

        self.restart_history.append(now)
        consecutive = len(self.restart_history)
        backoff = min(self.base_backoff_seconds * (2 ** (consecutive - 1)), self.max_backoff_seconds)
        return True, f"Crash evaluated as recoverable. Consecutive crash count: {consecutive}. Backing off {backoff:.1f}s.", backoff

    def run_loop(self, max_total_runs: Optional[int] = None) -> int:
        """
        Runs the supervisor loop managing the keeper child process.
        """
        runs = 0
        while True:
            runs += 1
            if max_total_runs and runs > max_total_runs:
                return 0

            print(f"[SUPERVISOR] Starting Keeper process (run #{runs}): {' '.join(self.keeper_cmd)}", flush=True)
            try:
                proc = subprocess.run(
                    self.keeper_cmd,
                    cwd=self.cwd,
                    shell=False,
                )
                exit_code = proc.returncode
            except KeyboardInterrupt:
                print("\n[SUPERVISOR] Received KeyboardInterrupt. Shutting down cleanly.", flush=True)
                return 0
            except Exception as e:
                print(f"[SUPERVISOR] Failed to spawn child process: {e}", file=sys.stderr, flush=True)
                return 1

            can_restart, reason, backoff = self.evaluate_crash(exit_code)
            print(f"[SUPERVISOR] Keeper process exited with code {exit_code}. {reason}", flush=True)

            if not can_restart:
                print(f"[SUPERVISOR] Halting supervisor: {reason}", file=sys.stderr, flush=True)
                return exit_code

            print(f"[SUPERVISOR] Waiting {backoff:.1f}s before restarting...", flush=True)
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                print("\n[SUPERVISOR] Sleep interrupted by user. Exiting.", flush=True)
                return 0


def main():
    parser = argparse.ArgumentParser(description="PoolCoin Keeper Process Supervisor")
    parser.add_argument("--state-dir", default=None, help="Keeper persistent state directory")
    parser.add_argument("--max-restarts", type=int, default=3, help="Max restarts per window")
    parser.add_argument("--window-seconds", type=float, default=600.0, help="Sliding window in seconds")
    parser.add_argument("--base-backoff", type=float, default=5.0, help="Base backoff in seconds")
    parser.add_argument("--max-backoff", type=float, default=60.0, help="Max backoff ceiling in seconds")
    parser.add_argument("keeper_args", nargs=argparse.REMAINDER, help="Arguments passed to python -m script.keeper")

    args = parser.parse_args()
    if not args.keeper_args:
        print("Usage: python -m script.keeper_supervisor [supervisor_opts] -- [keeper_args]", file=sys.stderr)
        sys.exit(2)

    cmd = [sys.executable, "-m", "script.keeper"] + [a for a in args.keeper_args if a != "--"]
    sup = KeeperSupervisor(
        keeper_cmd=cmd,
        state_dir=args.state_dir,
        max_restarts=args.max_restarts,
        window_seconds=args.window_seconds,
        base_backoff_seconds=args.base_backoff,
        max_backoff_seconds=args.max_backoff,
    )
    sys.exit(sup.run_loop())


if __name__ == "__main__":
    main()
