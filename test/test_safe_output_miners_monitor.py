#!/usr/bin/env python3
"""Offline mock tests verifying safe_output integration in miner.py, miner_gpu.py, monitor.py, and validate_share_batch.py."""

import builtins
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from eth_abi import encode
from eth_hash.auto import keccak

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from script import miner, miner_gpu, monitor, validate_share_batch


CRED_USER = "rpc_user_stage3"
CRED_PASS = "S3cr3tStage3RpcPass"
CRED_URL = f"https://{CRED_USER}:{CRED_PASS}@rpc.stage3.example.com:8545/v1/privpath"
TOKEN_SECRET = "tok_stage3_99887766"
TOKEN_URL = f"https://node-a.example.com/rpc?token={TOKEN_SECRET}&mode=fast"
KEY_SECRET = "api_stage3_55443322"
KEY_URL = f"https://node-b.example.com/rpc?key={KEY_SECRET}"
PK_BARE = "ab" * 32
PK_HEX = "0x" + "cd" * 32
PW_FILENAME = "miner_stage3_secret_pw.txt"
PW_PATH_POSIX = f"/var/secrets/{PW_FILENAME}"
PW_PATH_WIN = rf"C:\secrets\{PW_FILENAME}"

ALL_SENSITIVE_FRAGMENTS = [
    CRED_USER,
    CRED_PASS,
    f"{CRED_USER}:{CRED_PASS}",
    "/v1/privpath",
    TOKEN_SECRET,
    f"token={TOKEN_SECRET}",
    KEY_SECRET,
    f"key={KEY_SECRET}",
    PK_BARE,
    PK_HEX,
    PW_FILENAME,
    PW_PATH_POSIX,
    PW_PATH_WIN,
]

TEST_MINER = "0x1111111111111111111111111111111111111111"
TEST_CONTRACT = miner.DEFAULT_MINER_CONTRACT
TEST_VAULT = "0x3333333333333333333333333333333333333333"
TEST_TOKEN = monitor.DEFAULT_TOKEN_CONTRACT
TEST_EPOCH = 5
TEST_SEED_BYTES = bytes.fromhex("12" * 32)
TEST_SEED_HEX = "0x" + TEST_SEED_BYTES.hex()
EASY_TARGET_INT = (1 << 255) - 1
EASY_TARGET_HEX = "0x" + EASY_TARGET_INT.to_bytes(32, "big").hex()


class _FakeHTTPResponse:
    def __init__(self, payload_obj):
        self._raw = json.dumps(payload_obj).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _abi_word_uint(val: int) -> str:
    return "0x" + encode(["uint256"], [val]).hex()


def _abi_addr(addr: str) -> str:
    return "0x" + encode(["address"], [addr]).hex()


def _build_combined_leak_message() -> str:
    return (
        f"upstream failed on {CRED_URL} and {TOKEN_URL} and {KEY_URL}; "
        f"bare_pk={PK_BARE} signer_pk={PK_HEX} pw={PW_PATH_WIN} alt_pw={PW_PATH_POSIX}"
    )


def _run_cli(main_fn, argv, env_overrides=None):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    env_patch = env_overrides or {}
    exit_code = 0
    with patch.dict(os.environ, env_patch, clear=False):
        with patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                try:
                    main_fn()
                except SystemExit as exc:
                    exit_code = exc.code if isinstance(exc.code, int) else 1
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _find_valid_nonce(sub_bytes: bytes, target_int: int, start_nonce: int = 0):
    for nonce in range(start_nonce, start_nonce + 1000):
        h_bytes = keccak(sub_bytes + nonce.to_bytes(32, "big"))
        if int.from_bytes(h_bytes, "big") <= target_int:
            return nonce, h_bytes
    raise AssertionError("Failed to find valid nonce with easy target")


class TestStage3SafeOutput(unittest.TestCase):
    def _assert_no_secrets(self, stdout_text: str, stderr_text: str):
        combined = stdout_text + "\n" + stderr_text
        for fragment in ALL_SENSITIVE_FRAGMENTS:
            self.assertNotIn(fragment, combined, f"Leaked sensitive fragment: {fragment}")

    def _secret_env(self):
        return {
            "PRIVATE_KEY": PK_HEX,
            "MINER_PRIVATE_KEY": PK_HEX,
            "PASSWORD_FILE": PW_PATH_WIN,
            "KEEPER_PASSWORD_FILE": PW_PATH_POSIX,
        }

    # ------------------------------------------------------------------
    # miner.py
    # ------------------------------------------------------------------
    def test_miner_rpc_and_backfill_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        argv = ["miner.py", "--miner", TEST_MINER, "--rpc", CRED_URL]

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
            code, out, err = _run_cli(miner.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("URLError:", err)

        http_err = urllib.error.HTTPError(
            url=TOKEN_URL,
            code=401,
            msg=leak_msg,
            hdrs=None,
            fp=io.BytesIO(b"Unauthorized raw body"),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            code, out, err = _run_cli(miner.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("HTTPError(401):", err)

        rpc_err_resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(rpc_err_resp)):
            code, out, err = _run_cli(miner.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("RPC Error", err)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, env, clear=False),              patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)),              contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            res_bf = miner.backfill_epoch_seed(CRED_URL, TEST_CONTRACT, TEST_EPOCH, private_key=PK_HEX)
        self.assertIsNone(res_bf)
        self._assert_no_secrets(stdout_buf.getvalue(), stderr_buf.getvalue())
        self.assertIn("[BACKFILL] Failed to broadcast backfill tx: RuntimeError: URLError:", stdout_buf.getvalue())

    def test_miner_normal_output_preserves_fields(self):
        sub_bytes = miner.compute_sub_hash(TEST_SEED_BYTES, TEST_MINER, 0, TEST_CONTRACT, 97)
        valid_nonce, valid_hash_bytes = _find_valid_nonce(sub_bytes, EASY_TARGET_INT)
        valid_hash_hex = valid_hash_bytes.hex()

        def fake_urlopen(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            method = payload["method"]
            if method == "eth_chainId":
                result = "0x61"
            elif method == "eth_call":
                data = payload["params"][0]["data"]
                if data == "0x900cf0cf":
                    result = _abi_word_uint(TEST_EPOCH)
                elif data == "0x5c975abb":
                    result = _abi_word_uint(0)
                elif data == "0x93afb4c5":
                    result = _abi_word_uint(TEST_EPOCH)
                elif data.startswith("0x834f9a82"):
                    result = _abi_word_uint(0)
                elif data.startswith("0x" + miner.SELECTOR_VAULT):
                    result = _abi_addr(TEST_VAULT)
                elif data.startswith("0x" + miner.SELECTOR_STATUS):
                    result = "0x" + encode(["bool", "uint256"], [True, 1]).hex()
                elif data.startswith("0x4abccd28"):
                    result = TEST_SEED_HEX
                elif data.startswith("0x9811c7c1"):
                    result = EASY_TARGET_HEX
                elif data.startswith("0x1107d89b"):
                    result = _abi_word_uint(0)
                else:
                    raise AssertionError(f"Unexpected eth_call data: {data}")
            else:
                raise AssertionError(f"Unexpected method: {method}")
            return _FakeHTTPResponse({"jsonrpc": "2.0", "id": payload["id"], "result": result})

        class _FakeQueue:
            def __init__(self, items=None):
                self._items = list(items or [])

            def get(self):
                return self._items.pop(0)

            def empty(self):
                return len(self._items) == 0

            def get_nowait(self):
                return self._items.pop(0)

        fake_result_q = _FakeQueue([(valid_nonce, valid_hash_hex)])
        fake_stats_q = _FakeQueue([12345])

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "mined_share.json")
            argv = [
                "miner.py",
                "--miner", TEST_MINER,
                "--rpc", CRED_URL,
                "--target", EASY_TARGET_HEX,
                "--threads", "1",
                "--out", out_file,
            ]
            with patch("urllib.request.urlopen", side_effect=fake_urlopen),                  patch.object(miner.mp, "Event", return_value=MagicMock()),                  patch.object(miner.mp, "Queue", side_effect=[fake_result_q, fake_stats_q]),                  patch.object(miner.mp, "Process", return_value=MagicMock()),                  patch.object(miner.threading, "Thread", return_value=MagicMock()):
                code, out, err = _run_cli(miner.main, argv)

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertNotIn(CRED_USER, out)
            self.assertNotIn(CRED_PASS, out)
            self.assertIn(f"Miner Contract:   {TEST_CONTRACT}", out)
            self.assertIn(f"Miner Address:    {TEST_MINER}", out)
            self.assertIn(f"Current Epoch:    {TEST_EPOCH}", out)
            self.assertIn(f"StakeVault:       {TEST_VAULT}", out)
            self.assertIn(f"Epoch Seed:       {TEST_SEED_HEX}", out)
            self.assertIn(f"Sub Hash:         0x{sub_bytes.hex()}", out)
            self.assertIn(f"Nonce:            {valid_nonce} (0x{valid_nonce:x})", out)
            self.assertIn(f"Share Hash:       0x{valid_hash_hex}", out)
            self.assertTrue(os.path.isfile(out_file))

    # ------------------------------------------------------------------
    # miner_gpu.py
    # ------------------------------------------------------------------
    def test_miner_gpu_rpc_and_opencl_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        argv = ["miner_gpu.py", "--miner", TEST_MINER, "--rpc", TOKEN_URL]
        fake_devices = [{"device_obj": MagicMock(), "name": "MockGPU", "backend": "MockOpenCL"}]

        with patch.object(miner_gpu, "HAS_PYOPENCL", True),              patch.object(miner_gpu, "HAS_NUMPY", True),              patch.object(miner_gpu, "detect_devices", return_value=fake_devices):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
                code, out, err = _run_cli(miner_gpu.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("URLError:", err)

            http_err = urllib.error.HTTPError(
                url=KEY_URL,
                code=403,
                msg=leak_msg,
                hdrs=None,
                fp=io.BytesIO(b"Forbidden"),
            )
            with patch("urllib.request.urlopen", side_effect=http_err):
                code, out, err = _run_cli(miner_gpu.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("HTTPError(403):", err)

            rpc_err_resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}}
            with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(rpc_err_resp)):
                code, out, err = _run_cli(miner_gpu.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("RPC Error", err)

        fake_cl = MagicMock()
        fake_cl.get_platforms.side_effect = RuntimeError(leak_msg)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, env, clear=False),              patch.object(miner_gpu, "HAS_PYOPENCL", True),              patch.object(miner_gpu, "cl", fake_cl, create=True),              contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            detected = miner_gpu.detect_devices()
        self.assertEqual(detected, [])
        self._assert_no_secrets(stdout_buf.getvalue(), stderr_buf.getvalue())
        self.assertIn("[OpenCL Warning] Failed to query OpenCL platforms: RuntimeError:", stdout_buf.getvalue())

    def test_miner_gpu_normal_output_preserves_fields(self):
        sub_bytes = miner_gpu.compute_sub_hash(TEST_SEED_BYTES, TEST_MINER, 0, TEST_CONTRACT, 97)
        valid_nonce, valid_hash_bytes = _find_valid_nonce(sub_bytes, EASY_TARGET_INT)
        valid_hash_hex = valid_hash_bytes.hex()
        hash_u64 = struct.unpack(">4Q", valid_hash_bytes)

        def fake_urlopen(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            method = payload["method"]
            if method == "eth_chainId":
                result = "0x61"
            elif method == "eth_call":
                data = payload["params"][0]["data"]
                if data == "0x900cf0cf":
                    result = _abi_word_uint(TEST_EPOCH)
                elif data == "0x5c975abb":
                    result = _abi_word_uint(0)
                elif data == "0x93afb4c5":
                    result = _abi_word_uint(TEST_EPOCH)
                elif data.startswith("0x834f9a82"):
                    result = _abi_word_uint(0)
                elif data.startswith("0x" + miner_gpu.SELECTOR_VAULT):
                    result = _abi_addr(TEST_VAULT)
                elif data.startswith("0x" + miner_gpu.SELECTOR_STATUS):
                    result = "0x" + encode(["bool", "uint256"], [True, 1]).hex()
                elif data.startswith("0x4abccd28"):
                    result = TEST_SEED_HEX
                elif data.startswith("0x9811c7c1"):
                    result = EASY_TARGET_HEX
                elif data.startswith("0x1107d89b"):
                    result = _abi_word_uint(0)
                else:
                    raise AssertionError(f"Unexpected eth_call data: {data}")
            else:
                raise AssertionError(f"Unexpected method: {method}")
            return _FakeHTTPResponse({"jsonrpc": "2.0", "id": payload["id"], "result": result})

        class _FakeNP:
            uint32 = int
            uint64 = int

            @staticmethod
            def zeros(n, dtype=None):
                return [0] * n

            @staticmethod
            def empty(n, dtype=None):
                return [0] * n

        count_buf = object()
        nonce_buf = object()
        hash_buf = object()

        fake_cl = MagicMock()
        fake_cl.Buffer.side_effect = [count_buf, nonce_buf, hash_buf]

        def fake_enqueue_copy(queue, dst, src):
            if src is count_buf:
                dst[0] = 1
            elif src is nonce_buf:
                dst[0] = valid_nonce
            elif src is hash_buf:
                for i in range(4):
                    dst[i] = hash_u64[i]

        fake_cl.enqueue_copy.side_effect = fake_enqueue_copy
        fake_devices = [{"device_obj": MagicMock(), "name": "MockGPU", "backend": "MockOpenCL"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "gpu_share.json")
            argv = [
                "miner_gpu.py",
                "--miner", TEST_MINER,
                "--rpc", CRED_URL,
                "--target", EASY_TARGET_HEX,
                "--batch-size", "256",
                "--out", out_file,
            ]
            with patch("urllib.request.urlopen", side_effect=fake_urlopen),                  patch.object(miner_gpu, "HAS_PYOPENCL", True),                  patch.object(miner_gpu, "HAS_NUMPY", True),                  patch.object(miner_gpu, "np", _FakeNP(), create=True),                  patch.object(miner_gpu, "cl", fake_cl, create=True),                  patch.object(miner_gpu, "detect_devices", return_value=fake_devices),                  patch("time.sleep"):
                code, out, err = _run_cli(miner_gpu.main, argv)

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertNotIn(CRED_USER, out)
            self.assertNotIn(CRED_PASS, out)
            self.assertIn(f"Miner Contract:   {TEST_CONTRACT}", out)
            self.assertIn(f"Miner Address:    {TEST_MINER}", out)
            self.assertIn(f"Current Epoch:    {TEST_EPOCH}", out)
            self.assertIn(f"StakeVault:       {TEST_VAULT}", out)
            self.assertIn(f"Epoch Seed:       {TEST_SEED_HEX}", out)
            self.assertIn(f"Sub Hash:         0x{sub_bytes.hex()}", out)
            self.assertIn("Selected GPU:     MockGPU via MockOpenCL", out)
            self.assertIn(f"Nonce:            {valid_nonce} (0x{valid_nonce:x})", out)
            self.assertIn(f"Share Hash:       0x{valid_hash_hex}", out)
            self.assertTrue(os.path.isfile(out_file))

    # ------------------------------------------------------------------
    # monitor.py
    # ------------------------------------------------------------------
    def test_monitor_rpc_and_main_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        env["RPC_URL"] = CRED_URL

        with tempfile.TemporaryDirectory() as tmpdir:
            out_json = os.path.join(tmpdir, "report.json")
            out_csv = os.path.join(tmpdir, "report.csv")
            argv = [
                "monitor.py",
                "--start-round", "5",
                "--end-round", "5",
                "--output-json", out_json,
                "--output-csv", out_csv,
            ]

            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
                code, out, err = _run_cli(monitor.main, argv, env)
            self.assertEqual(code, 0)
            self._assert_no_secrets(out, err)
            self.assertIn("URLError:", err)

            http_err = urllib.error.HTTPError(
                url=TOKEN_URL,
                code=502,
                msg=leak_msg,
                hdrs=None,
                fp=io.BytesIO(b"Bad Gateway"),
            )
            with patch("urllib.request.urlopen", side_effect=http_err):
                code, out, err = _run_cli(monitor.main, argv, env)
            self.assertEqual(code, 0)
            self._assert_no_secrets(out, err)
            self.assertIn("HTTPError(502):", err)

            rpc_err_resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}}
            with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(rpc_err_resp)):
                code, out, err = _run_cli(monitor.main, argv, env)
            self.assertEqual(code, 0)
            self._assert_no_secrets(out, err)
            self.assertIn("[WARN] RPC error (eth_blockNumber):", err)

            with patch.object(monitor, "rpc_request", side_effect=RuntimeError(leak_msg)):
                code, out, err = _run_cli(monitor.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("RuntimeError:", err)

    def test_monitor_normal_output_preserves_fields(self):
        target_32b = 1 << 224
        target_rpc_hex = "0x" + target_32b.to_bytes(32, "big").hex()
        expected_target_hex = hex(target_32b)

        def fake_urlopen(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            method = payload["method"]
            if method == "eth_blockNumber":
                result = hex(12345)
            elif method == "eth_getLogs":
                result = []
            elif method == "eth_call":
                data = payload["params"][0]["data"]
                if data == monitor.SEL_EPOCH:
                    result = _abi_word_uint(TEST_EPOCH)
                elif data == monitor.SEL_LAST_SYNCED:
                    result = _abi_word_uint(TEST_EPOCH)
                elif data == monitor.SEL_PAUSED:
                    result = _abi_word_uint(0)
                elif data == monitor.SEL_S_STAR:
                    result = _abi_word_uint(50_000)
                elif data == monitor.SEL_EMA:
                    result = _abi_word_uint(25_000)
                elif data == monitor.SEL_ADDR_CAP_BPS:
                    result = _abi_word_uint(2000)
                elif data == monitor.SEL_START:
                    result = _abi_word_uint(1_700_000_000)
                elif data == monitor.SEL_TOTAL_MINED:
                    result = _abi_word_uint(1000 * 10**18)
                elif data == monitor.SEL_TOTAL_SUPPLY:
                    result = _abi_word_uint(1000 * 10**18)
                elif data == monitor.SEL_MINING_CAP:
                    result = _abi_word_uint(900_000_000 * 10**18)
                elif data == monitor.SEL_REMAINING_EMISSION:
                    result = _abi_word_uint(899_999_000 * 10**18)
                elif data.startswith(monitor.SEL_TOTAL_SHARES):
                    result = _abi_word_uint(12)
                elif data.startswith(monitor.SEL_TOTAL_WEIGHT):
                    result = _abi_word_uint(20 * monitor.WSCALE)
                elif data.startswith(monitor.SEL_TARGET):
                    result = target_rpc_hex
                elif data.startswith(monitor.SEL_EPOCH_SEED):
                    result = TEST_SEED_HEX
                elif data.startswith(monitor.SEL_WMIN):
                    result = _abi_word_uint(10 * monitor.WSCALE)
                elif data.startswith(monitor.SEL_UNALLOCATED_GAP):
                    result = _abi_word_uint(0)
                elif data.startswith(monitor.SEL_SETTLED_EPOCH_QUOTA):
                    result = _abi_word_uint(100 * 10**18)
                else:
                    raise AssertionError(f"Unexpected eth_call data: {data}")
            else:
                raise AssertionError(f"Unexpected method: {method}")
            return _FakeHTTPResponse({"jsonrpc": "2.0", "id": payload["id"], "result": result})

        with tempfile.TemporaryDirectory() as tmpdir:
            out_json = os.path.join(tmpdir, "report.json")
            out_csv = os.path.join(tmpdir, "report.csv")
            argv = [
                "monitor.py",
                "--start-round", "5",
                "--end-round", "5",
                "--output-json", out_json,
                "--output-csv", out_csv,
            ]
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                code, out, err = _run_cli(monitor.main, argv, {"RPC_URL": CRED_URL})

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self._assert_no_secrets(out, err)
            self.assertIn("Latest Block Height: 12345", out)
            self.assertIn("Current On-Chain Epoch : 5", out)
            self.assertIn("Contract Engine Version: rc2 (Progress Emission)", out)
            self.assertIn("1,000.0000 TTK", out)
            self.assertIn(f"Target Bitmask         : {expected_target_hex}", out)
            self.assertIn("Difficulty Bits        : 32.0 bits", out)
            self.assertTrue(os.path.isfile(out_json))
            self.assertTrue(os.path.isfile(out_csv))

    # ------------------------------------------------------------------
    # validate_share_batch.py
    # ------------------------------------------------------------------
    def _create_valid_batch_files(self, tmpdir: str, tamper_k0_hash=None):
        files = []
        for k in range(6):
            sub_bytes = validate_share_batch.compute_sub_hash(
                TEST_SEED_BYTES, TEST_MINER, k, TEST_CONTRACT, 97
            )
            nonce, h_bytes = _find_valid_nonce(sub_bytes, EASY_TARGET_INT, start_nonce=100 * k)
            h_hex = "0x" + h_bytes.hex()
            if k == 0 and tamper_k0_hash is not None:
                h_hex = tamper_k0_hash
            data = {
                "miner": TEST_MINER,
                "minerContract": TEST_CONTRACT,
                "chainId": 97,
                "epoch": TEST_EPOCH,
                "k": k,
                "epochSeed": TEST_SEED_HEX,
                "target": EASY_TARGET_HEX,
                "nonce": hex(nonce),
                "nonceDecimal": nonce,
                "hash": h_hex,
            }
            fp = os.path.join(tmpdir, f"share_k{k}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            files.append(fp)
        return files

    def test_validate_share_batch_file_and_unexpected_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()

        with tempfile.TemporaryDirectory() as tmpdir:
            files = self._create_valid_batch_files(tmpdir)
            out_file = os.path.join(tmpdir, "batch_out.json")
            argv = [
                "validate_share_batch.py",
                "--files", *files,
                "--chain-id", "97",
                "--proxy", TEST_CONTRACT,
                "--miner", TEST_MINER,
                "--epoch", str(TEST_EPOCH),
                "--seed", TEST_SEED_HEX,
                "--target", EASY_TARGET_HEX,
                "--out", out_file,
            ]

            real_open = builtins.open

            def failing_open(file, *args, **kwargs):
                if str(file) == files[0]:
                    raise OSError(13, leak_msg, PW_PATH_WIN)
                return real_open(file, *args, **kwargs)

            with patch("builtins.open", side_effect=failing_open):
                code, out, err = _run_cli(validate_share_batch.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("[VALIDATION FAILED]", err)

            with patch.object(validate_share_batch.json, "loads", side_effect=ValueError(leak_msg)):
                code, out, err = _run_cli(validate_share_batch.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("Failed to parse JSON", err)

            with patch.object(validate_share_batch, "validate_batch", side_effect=RuntimeError(leak_msg)):
                code, out, err = _run_cli(validate_share_batch.main, argv, env)
            self.assertEqual(code, 1)
            self._assert_no_secrets(out, err)
            self.assertIn("RuntimeError:", err)

    def test_validate_share_batch_preserves_unlabelled_64hex_in_validation_errors(self):
        tampered_bare_hash = "1234567890abcdef" * 4
        with tempfile.TemporaryDirectory() as tmpdir:
            files = self._create_valid_batch_files(tmpdir, tamper_k0_hash=tampered_bare_hash)
            out_file = os.path.join(tmpdir, "batch_fail_out.json")
            argv = [
                "validate_share_batch.py",
                "--files", *files,
                "--chain-id", "97",
                "--proxy", TEST_CONTRACT,
                "--miner", TEST_MINER,
                "--epoch", str(TEST_EPOCH),
                "--seed", TEST_SEED_HEX,
                "--target", EASY_TARGET_HEX,
                "--out", out_file,
            ]
            code, out, err = _run_cli(validate_share_batch.main, argv)
        self.assertEqual(code, 1)
        self.assertIn("[VALIDATION FAILED]", err)
        self.assertIn("Hash mismatch! File declared", err)
        self.assertIn(tampered_bare_hash, err)
        self.assertNotIn("[REDACTED_KEY]", err)

    def test_validate_share_batch_normal_output_preserves_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files = self._create_valid_batch_files(tmpdir)
            out_file = os.path.join(tmpdir, "batch_ok_out.json")
            argv = [
                "validate_share_batch.py",
                "--files", *files,
                "--chain-id", "97",
                "--proxy", TEST_CONTRACT,
                "--miner", TEST_MINER,
                "--epoch", str(TEST_EPOCH),
                "--seed", TEST_SEED_HEX,
                "--target", EASY_TARGET_HEX,
                "--out", out_file,
            ]
            code, out, err = _run_cli(validate_share_batch.main, argv)
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("Validation Result:    SUCCESS (6/6 shares verified)", out)
            self.assertIn(f"Miner Proxy:          {TEST_CONTRACT.lower()}", out)
            self.assertIn(f"Miner Address:        {TEST_MINER.lower()}", out)
            self.assertIn(f"Epoch Seed:           {TEST_SEED_HEX}", out)
            self.assertIn("Calldata:             0x6efb5ee9", out)
            self.assertTrue(os.path.isfile(out_file))


if __name__ == "__main__":
    unittest.main()
