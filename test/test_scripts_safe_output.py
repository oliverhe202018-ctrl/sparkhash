#!/usr/bin/env python3
"""Offline mock tests verifying safe_output integration in status.py, claim.py, and submit_share.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from eth_abi import encode
from eth_hash.auto import keccak

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from script import claim, status, submit_share


CRED_USER = "rpc_user_alpha"
CRED_PASS = "Sup3rS3cr3tRpcP4ss"
CRED_URL = f"https://{CRED_USER}:{CRED_PASS}@rpc.internal.example.com:8545/v1/privpath"
TOKEN_SECRET = "tok_secret_99887766"
TOKEN_URL = f"https://node-a.example.com/rpc?token={TOKEN_SECRET}&mode=fast"
KEY_SECRET = "api_secret_55443322"
KEY_URL = f"https://node-b.example.com/rpc?key={KEY_SECRET}"
PK_BARE = "ab" * 32
PK_HEX = "0x" + "cd" * 32
PW_FILENAME = "keeper_vault_secret_pw.txt"
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
TEST_CONTRACT = claim.DEFAULT_MINER_CONTRACT
TEST_TOKEN = "0x2222222222222222222222222222222222222222"
TEST_VAULT = "0x3333333333333333333333333333333333333333"
TEST_SHARE_HASH = "0x" + "ef" * 32


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


class TestScriptsSafeOutput(unittest.TestCase):
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
    # status.py
    # ------------------------------------------------------------------
    def test_status_rpc_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        argv = ["status.py", TEST_MINER, "--rpc", CRED_URL]

        # 1. URLError from urlopen
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
            code, out, err = _run_cli(status.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("URLError:", err)

        # 2. HTTPError with non-JSON body and sensitive reason + url
        http_err = urllib.error.HTTPError(
            url=TOKEN_URL,
            code=401,
            msg=leak_msg,
            hdrs=None,
            fp=io.BytesIO(b"Unauthorized raw body"),
        )
        try:
            with patch("urllib.request.urlopen", side_effect=http_err):
                code, out, err = _run_cli(status.main, argv, env)
        finally:
            http_err.close()
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("HTTPError(401):", err)

        # 3. JSON-RPC batch item error response
        batch_err_resp = [{"jsonrpc": "2.0", "id": 0, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}}]
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(batch_err_resp)):
            code, out, err = _run_cli(status.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("RPC Error", err)

        # 4. Direct mocked rpc_batch_call exception caught by main()
        with patch.object(status, "rpc_batch_call", side_effect=RuntimeError(leak_msg)):
            code, out, err = _run_cli(status.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("RuntimeError:", err)

    def test_status_normal_output_preserves_fields_and_sanitizes_rpc_url(self):
        sel_used_mask = "0x" + keccak(b"usedMask(uint256,address)").hex()[:8]
        sel_counted = "0x" + keccak(b"counted(uint256,address)").hex()[:8]
        sel_user_weight = "0x" + keccak(b"userWeight(uint256,address)").hex()[:8]
        sel_claimed = "0x" + keccak(b"claimed(uint256,address)").hex()[:8]
        sel_eff_q = "0x" + keccak(b"effectiveQuota(uint256)").hex()[:8]
        sel_total_w = "0x" + keccak(b"totalWeight(uint256)").hex()[:8]
        sel_w_min = "0x" + keccak(b"wMin(uint256)").hex()[:8]
        sel_cap_bps = "0x" + keccak(b"epochAddrCapBps(uint256)").hex()[:8]

        def fake_urlopen(req, timeout=15):
            calls = json.loads(req.data.decode("utf-8"))
            resp = []
            for item in calls:
                cid = item["id"]
                method = item["method"]
                if method == "eth_chainId":
                    result = "0x61"
                else:
                    tx = item["params"][0]
                    data = tx["data"]
                    if data == status.SEL_EPOCH:
                        result = _abi_word_uint(5)
                    elif data == status.SEL_LAST_SYNCED:
                        result = _abi_word_uint(5)
                    elif data == status.SEL_TOKEN:
                        result = _abi_addr(TEST_TOKEN)
                    elif data == status.SEL_VAULT:
                        result = _abi_addr(TEST_VAULT)
                    elif data == status.SEL_ADDR_CAP_BPS:
                        result = _abi_word_uint(2000)
                    elif data == status.SEL_REMAINING_EMISSION:
                        result = _abi_word_uint(800_000_000 * 10**18)
                    elif data.startswith(status.SEL_VAULT_STATUS):
                        result = "0x" + encode(["bool", "uint256"], [True, 3]).hex()
                    elif data.startswith(status.SEL_VAULT_STAKES):
                        result = "0x" + encode(["uint64", "uint64", "bool", "uint128"], [3, 0, True, 10**18]).hex()
                    elif data.startswith(status.SEL_TOKEN_BALANCE):
                        result = _abi_word_uint(42 * 10**18)
                    elif data.startswith(sel_used_mask):
                        result = "0x" + encode(["uint16"], [0b000011]).hex()
                    elif data.startswith(sel_counted):
                        result = "0x" + encode(["uint8"], [2]).hex()
                    elif data.startswith(sel_user_weight):
                        result = _abi_word_uint(1500)
                    elif data.startswith(sel_claimed):
                        result = "0x" + encode(["bool"], [False]).hex()
                    elif data.startswith(sel_eff_q):
                        result = _abi_word_uint(100 * 10**18)
                    elif data.startswith(sel_total_w):
                        result = _abi_word_uint(3000)
                    elif data.startswith(sel_w_min):
                        result = _abi_word_uint(1000)
                    elif data.startswith(sel_cap_bps):
                        result = _abi_word_uint(2000)
                    elif data.startswith(status.SEL_UNALLOCATED_GAP):
                        result = _abi_word_uint(0)
                    else:
                        raise AssertionError(f"Unexpected eth_call selector: {data}")
                resp.append({"jsonrpc": "2.0", "id": cid, "result": result})
            return _FakeHTTPResponse(resp)

        argv = ["status.py", TEST_MINER, "--rpc", CRED_URL]
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code, out, err = _run_cli(status.main, argv)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("RPC 节点:            https://rpc.internal.example.com:8545/***", out)
        self.assertNotIn(CRED_USER, out)
        self.assertNotIn(CRED_PASS, out)
        self.assertIn(f"矿工地址:            {TEST_MINER}", out)
        self.assertIn(f"MinerV1 代理合约:    {TEST_CONTRACT}", out)
        self.assertIn(f"StakeVault 质押合约: {TEST_VAULT}", out)
        self.assertIn(f"Token (TTK) 合约:    {TEST_TOKEN}", out)
        self.assertIn("1.0000 BNB (1000000000000000000 wei)", out)
        self.assertIn("42.000000 TTK (42000000000000000000 wei)", out)
        self.assertIn("800000000.0000 TTK", out)
        self.assertIn("20.000000 TTK (20000000000000000000 wei)", out)

    # ------------------------------------------------------------------
    # claim.py
    # ------------------------------------------------------------------
    def test_claim_rpc_and_key_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        argv = ["claim.py", "--epoch", "4", "--miner", TEST_MINER, "--rpc", TOKEN_URL]

        # 1. URLError at eth_chainId
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
            code, out, err = _run_cli(claim.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("URLError:", err)

        # 1b. HTTPError with non-JSON body at eth_chainId
        http_err_plain = urllib.error.HTTPError(
            url=CRED_URL,
            code=403,
            msg=leak_msg,
            hdrs=None,
            fp=io.BytesIO(b"Forbidden raw upstream body"),
        )
        try:
            with patch("urllib.request.urlopen", side_effect=http_err_plain):
                code, out, err = _run_cli(claim.main, argv, env)
        finally:
            http_err_plain.close()
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("HTTPError(403):", err)

        # 2. HTTPError with JSON-RPC error payload during eth_call (simulate_claim)
        def fake_urlopen_sim_http_err(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            if isinstance(payload, dict):
                method = payload["method"]
                if method == "eth_chainId":
                    return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x61"})
                if method == "eth_call":
                    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}}).encode("utf-8")
                    raise urllib.error.HTTPError(url=KEY_URL, code=500, msg=leak_msg, hdrs=None, fp=io.BytesIO(body))
            else:
                resp = [
                    {"jsonrpc": "2.0", "id": 0, "result": _abi_word_uint(100 * 10**18)},
                    {"jsonrpc": "2.0", "id": 1, "result": _abi_word_uint(3000)},
                    {"jsonrpc": "2.0", "id": 2, "result": _abi_word_uint(1000)},
                    {"jsonrpc": "2.0", "id": 3, "result": _abi_word_uint(1500)},
                    {"jsonrpc": "2.0", "id": 4, "result": _abi_word_uint(2000)},
                    {"jsonrpc": "2.0", "id": 5, "result": "0x" + encode(["bool"], [False]).hex()},
                    {"jsonrpc": "2.0", "id": 6, "result": _abi_word_uint(10)},
                    {"jsonrpc": "2.0", "id": 7, "result": _abi_word_uint(10)},
                ]
                return _FakeHTTPResponse(resp)
            raise AssertionError(f"Unexpected request: {payload}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen_sim_http_err):
            code, out, err = _run_cli(claim.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("RPC 节点:            https://node-a.example.com/***", out)
        self.assertIn("回滚原因:", out)

        # 3. Direct mocked rpc_call raising unsanitized RuntimeError in simulate_claim
        def fake_rpc_call_direct(rpc_url, method, params):
            if method == "eth_chainId":
                return "0x61"
            raise RuntimeError(leak_msg)

        batch_ok = [100 * 10**18, 3000, 1000, 1500, 2000, False, 10, 10]
        batch_hex = [_abi_word_uint(v) if isinstance(v, int) and not isinstance(v, bool) else ("0x" + encode(["bool"], [v]).hex()) for v in batch_ok]
        with patch.object(claim, "rpc_call", side_effect=fake_rpc_call_direct), patch.object(claim, "rpc_batch_call", return_value=batch_hex):
            code, out, err = _run_cli(claim.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("回滚原因:", out)

        # 4. Private key parsing exception in --send mode
        bad_pk = "0x" + "f" * 63 + "z"
        bad_env = {
            "PRIVATE_KEY": bad_pk,
            "PASSWORD_FILE": PW_PATH_WIN,
        }
        send_argv = ["claim.py", "--epoch", "4", "--send", "--rpc", CRED_URL]
        with patch.object(claim, "rpc_call", return_value="0x61"):
            with patch(
                "eth_account.Account.from_key",
                side_effect=ValueError(f"Invalid key {bad_pk} with {PW_PATH_WIN} on {CRED_URL}"),
            ):
                code, out, err = _run_cli(claim.main, send_argv, bad_env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertNotIn(bad_pk, out + err)
        self.assertIn("解析环境变量私钥失败:", out)

    def test_claim_normal_dry_run_preserves_output_and_revert_decoding(self):
        def fake_urlopen(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            if isinstance(payload, list):
                vals = [
                    _abi_word_uint(100 * 10**18),
                    _abi_word_uint(3000),
                    _abi_word_uint(1000),
                    _abi_word_uint(1500),
                    _abi_word_uint(2000),
                    "0x" + encode(["bool"], [False]).hex(),
                    _abi_word_uint(10),
                    _abi_word_uint(10),
                ]
                return _FakeHTTPResponse([{"jsonrpc": "2.0", "id": i, "result": v} for i, v in enumerate(vals)])
            method = payload["method"]
            if method == "eth_chainId":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x61"})
            if method == "eth_call":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x"})
            if method == "eth_estimateGas":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(120000)})
            if method == "eth_gasPrice":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(2_000_000_000)})
            if method == "eth_maxPriorityFeePerGas":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(100_000_000)})
            raise AssertionError(f"Unexpected RPC method: {method}")

        argv = ["claim.py", "--epoch", "4", "--miner", TEST_MINER, "--rpc", KEY_URL]
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code, out, err = _run_cli(claim.main, argv)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("RPC 节点:            https://node-b.example.com/***", out)
        self.assertNotIn(KEY_SECRET, out)
        self.assertIn(f"MinerV1 代理合约:    {TEST_CONTRACT}", out)
        self.assertIn(f"矿工地址 (msg.sender): {TEST_MINER}", out)
        self.assertIn("目标领取轮次:        Epoch 4", out)
        self.assertIn("20.000000 TTK (20000000000000000000 wei)", out)
        self.assertIn("预估 Gas Limit: 120,000", out)
        self.assertIn("最终 Max Fee (maxFeePerGas): 2.300 gwei (2300000000 wei)", out)
        self.assertIn("最终 Priority Fee (maxPriorityFeePerGas): 0.100 gwei (100000000 wei)", out)
        self.assertIn("最大 Gas 费用上限:", out)
        self.assertIn("DRY-RUN SIMULATION PASSED", out)
        self.assertIn("--account", out)
        self.assertIn("--password-file", out)
        self.assertNotIn("PRIVATE_KEY", out)

        with patch.object(claim, "rpc_call", side_effect=RuntimeError("RPC Error (eth_call): execution reverted | data: 0x756b7857")):
            ok, gas_est, rev_err, _ = claim.simulate_claim(KEY_URL, TEST_CONTRACT, TEST_MINER, 4)
        self.assertFalse(ok)
        self.assertIsNone(gas_est)
        self.assertIn("EpochNotEnded()", rev_err)
        self.assertIn("0x756b7857", rev_err)

    # ------------------------------------------------------------------
    # submit_share.py
    # ------------------------------------------------------------------
    def test_submit_share_rpc_and_key_failures_do_not_leak_secrets(self):
        leak_msg = _build_combined_leak_message()
        env = self._secret_env()
        argv = [
            "submit_share.py",
            "--miner", TEST_MINER,
            "--k", "2",
            "--nonce", "987654",
            "--epoch", "7",
            "--rpc", CRED_URL,
        ]

        # 1. URLError on verify_chain_id
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(leak_msg)):
            code, out, err = _run_cli(submit_share.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("URLError:", err)

        # 1b. HTTPError with non-JSON body on verify_chain_id
        http_err_plain = urllib.error.HTTPError(
            url=KEY_URL,
            code=401,
            msg=leak_msg,
            hdrs=None,
            fp=io.BytesIO(b"Unauthorized raw body"),
        )
        try:
            with patch("urllib.request.urlopen", side_effect=http_err_plain):
                code, out, err = _run_cli(submit_share.main, argv, env)
        finally:
            http_err_plain.close()
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("HTTPError(401):", err)

        # 2. JSON-RPC error during simulate_call (eth_call)
        def fake_urlopen_sim_err(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            method = payload["method"]
            if method == "eth_chainId":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x61"})
            if method == "eth_gasPrice":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(3_000_000_000)})
            if method == "eth_maxPriorityFeePerGas":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(100_000_000)})
            if method == "eth_call":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": leak_msg, "data": PK_HEX}})
            raise AssertionError(f"Unexpected method: {method}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen_sim_err):
            code, out, err = _run_cli(submit_share.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("Revert Reason:", out)

        # 3. Direct mocked rpc_call raising RuntimeError on eth_gasPrice
        def fake_rpc_call_direct(rpc_url, method, params):
            if method == "eth_chainId":
                return "0x61"
            raise RuntimeError(leak_msg)

        with patch.object(submit_share, "rpc_call", side_effect=fake_rpc_call_direct):
            code, out, err = _run_cli(submit_share.main, argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("RuntimeError:", err)

        # 4. Private key parsing exception in --send mode
        bad_pk = "0x" + "a" * 63 + "z"
        bad_env = {
            "MINER_PRIVATE_KEY": bad_pk,
            "PASSWORD_FILE": PW_PATH_WIN,
        }
        send_argv = argv + ["--send"]

        def fake_rpc_call_ok_preflight(rpc_url, method, params):
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_gasPrice":
                return hex(3_000_000_000)
            if method == "eth_maxPriorityFeePerGas":
                return hex(100_000_000)
            if method == "eth_call":
                return "0x"
            if method == "eth_estimateGas":
                return hex(150000)
            raise AssertionError(f"Unexpected method before key check: {method}")

        with patch.object(submit_share, "rpc_call", side_effect=fake_rpc_call_ok_preflight):
            with patch(
                "eth_account.Account.from_key",
                side_effect=ValueError(f"Bad private key {bad_pk} pw={PW_PATH_WIN} url={TOKEN_URL}"),
            ):
                code, out, err = _run_cli(submit_share.main, send_argv, bad_env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertNotIn(bad_pk, out + err)
        self.assertIn("Failed to load private key from environment:", out)

        # 5. FileNotFoundError: ordinary share JSON preserves filename, password file path is masked
        missing_share_argv = ["submit_share.py", "--file", "nonexistent_share_data.json", "--rpc", CRED_URL]
        with patch.object(submit_share, "rpc_call", return_value="0x61"):
            code, out, err = _run_cli(submit_share.main, missing_share_argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("FileNotFoundError:", err)
        self.assertIn("nonexistent_share_data.json", err)

        missing_pw_as_file_argv = ["submit_share.py", "--file", PW_PATH_WIN, "--rpc", CRED_URL]
        with patch.object(submit_share, "rpc_call", return_value="0x61"):
            code, out, err = _run_cli(submit_share.main, missing_pw_as_file_argv, env)
        self.assertEqual(code, 1)
        self._assert_no_secrets(out, err)
        self.assertIn("FileNotFoundError:", err)

    def test_submit_share_normal_dry_run_preserves_output_and_hashes(self):
        def fake_urlopen(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            method = payload["method"]
            if method == "eth_chainId":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x61"})
            if method == "eth_gasPrice":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(3_000_000_000)})
            if method == "eth_maxPriorityFeePerGas":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(100_000_000)})
            if method == "eth_call":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": "0x"})
            if method == "eth_estimateGas":
                return _FakeHTTPResponse({"jsonrpc": "2.0", "id": 1, "result": hex(140000)})
            raise AssertionError(f"Unexpected method: {method}")

        share_payload = {
            "miner": TEST_MINER,
            "minerContract": TEST_CONTRACT,
            "k": 3,
            "nonceDecimal": 123456789,
            "epoch": 12,
            "hash": TEST_SHARE_HASH,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            share_path = Path(tmpdir) / "share_ok.json"
            share_path.write_text(json.dumps(share_payload), encoding="utf-8")
            argv = ["submit_share.py", "--file", str(share_path), "--rpc", TOKEN_URL]
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                code, out, err = _run_cli(submit_share.main, argv)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Network:          BSC Testnet (Chain ID 97)", out)
        self.assertIn(f"Miner Contract:   {TEST_CONTRACT}", out)
        self.assertIn(f"Miner Address:    {TEST_MINER}", out)
        self.assertIn("Epoch:            12", out)
        self.assertIn("Share Index k:    3", out)
        self.assertIn("Nonce:            123456789 (0x75bcd15)", out)
        self.assertIn(f"Share Hash:       {TEST_SHARE_HASH}", out)
        self.assertIn("[SUCCESS] Preflight simulation passed! Estimated gas: 140,000", out)
        self.assertIn("SIMULATION COMPLETED (DRY-RUN)", out)
        self.assertIn("3.450 gwei (3450000000 wei)", out)
        self.assertIn("0.100 gwei (100000000 wei)", out)
        self.assertIn("Max Gas Fee Ceiling:", out)
        self.assertIn("--account", out)
        self.assertIn("--password-file", out)
        self.assertNotIn("MINER_PRIVATE_KEY", out)


if __name__ == "__main__":
    unittest.main()
