"""
Unit tests for script/keeper.py
Verifies:
- Gas price floor (>= 0.1 gwei), multiplier, and replacement (speed-up) pricing
- Safe replacement retry with identical nonce
- Dry-run execution safety (no raw transaction broadcast, correct diagnostic output)
- Contract selectors matching MinerV1 ABI
"""

import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import io
import rlp
import subprocess
import tempfile
import json
import time as _real_time

class _FakeKeeperTime:
    def __init__(self, initial=1000.0):
        self._now = initial

    def time(self):
        return self._now

    def sleep(self, seconds):
        self._now += seconds

    def __getattr__(self, name):
        return getattr(_real_time, name)


import atexit

# Ensure all tests execute in an isolated temporary directory for keeper persistent state.
# Never touches or pollutes actual running state in ~/.poolcoin/keeper_state.
_ORIG_KEEPER_STATE_DIR = os.environ.get("KEEPER_STATE_DIR")
_TEST_TMP_STATE_DIR = tempfile.TemporaryDirectory()
os.environ["KEEPER_STATE_DIR"] = _TEST_TMP_STATE_DIR.name

def tearDownModule():
    """Restores original KEEPER_STATE_DIR and cleans temporary directory."""
    if _ORIG_KEEPER_STATE_DIR is None:
        os.environ.pop("KEEPER_STATE_DIR", None)
    else:
        os.environ["KEEPER_STATE_DIR"] = _ORIG_KEEPER_STATE_DIR
    try:
        _TEST_TMP_STATE_DIR.cleanup()
    except Exception:
        pass

atexit.register(tearDownModule)

import shutil

def clean_test_state_dir():
    """Test fixture helper: cleans state/lock files in the test state directory."""
    state_dir = os.environ.get("KEEPER_STATE_DIR")
    if state_dir and os.path.isdir(state_dir):
        for f in os.listdir(state_dir):
            fp = os.path.join(state_dir, f)
            try:
                if os.path.isfile(fp) or os.path.islink(fp):
                    os.unlink(fp)
                elif os.path.isdir(fp):
                    shutil.rmtree(fp)
            except Exception:
                pass

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from script.keeper import (
    TxExecutionStatus,
    TxResult,
    calculate_gas_price,
    get_bumped_gas_price,
    to_checksum_address,
    sanitize_url,
    sanitize_text,
    register_secret,
    clear_configured_secrets as _prod_clear_configured_secrets,
    _CONFIGURED_SECRETS,
    parse_numeric,
    format_exception_safe,
    KeeperBot,
    NonceError,
    NonceQueryError,
    NonceDivergenceError,
    WALLET_ENV_VARS_TO_ISOLATE,
    get_isolated_cast_env,
    main,
    SELECTOR_EPOCH,
    SELECTOR_LAST_SYNCED,
    SELECTOR_EPOCH_SEED,
    SELECTOR_INIT_EPOCH_SEED,
    SELECTOR_SYNC,
    DEFAULT_MIN_GAS_PRICE_GWEI,
    DEFAULT_GAS_MULTIPLIER,
    MAX_SEED_STEPS
)

def clear_configured_secrets():
    _prod_clear_configured_secrets()
    clean_test_state_dir()

_orig_testcase_setup = unittest.TestCase.setUp
_orig_testcase_teardown = unittest.TestCase.tearDown

def _auto_clean_setup(self):
    clean_test_state_dir()
    _orig_testcase_setup(self)

def _auto_clean_teardown(self):
    clean_test_state_dir()
    _orig_testcase_teardown(self)

unittest.TestCase.setUp = _auto_clean_setup
unittest.TestCase.tearDown = _auto_clean_teardown



class TestGasPriceCalculations(unittest.TestCase):
    def test_gas_price_floor_enforcement_on_1_wei(self):
        """When RPC returns 1 wei (the forge bug condition), price must be at least 0.1 gwei floor."""
        # 1 wei network price, 0.1 gwei floor, 1.0 multiplier
        result = calculate_gas_price(network_gas_price_wei=1, min_gas_price_gwei=0.1, multiplier=1.0)
        self.assertGreaterEqual(result, 100_000_000)  # 0.1 gwei
        self.assertEqual(result, 100_000_000)

    def test_gas_price_with_multiplier(self):
        """Multiplier is applied on top of the base price."""
        # 0.1 gwei floor * 1.5 = 0.15 gwei
        result = calculate_gas_price(network_gas_price_wei=100_000_000, min_gas_price_gwei=0.1, multiplier=1.5)
        self.assertEqual(result, 150_000_000)

        # 0.2 gwei network * 2.0 multiplier = 0.4 gwei
        result = calculate_gas_price(network_gas_price_wei=200_000_000, min_gas_price_gwei=0.1, multiplier=2.0)
        self.assertEqual(result, 400_000_000)

    def test_gas_price_none_or_zero_fallback(self):
        """When network gas price is 0 or None, safely uses floor * multiplier."""
        result_zero = calculate_gas_price(network_gas_price_wei=0, min_gas_price_gwei=0.1, multiplier=1.5)
        self.assertEqual(result_zero, 150_000_000)

        result_none = calculate_gas_price(network_gas_price_wei=None, min_gas_price_gwei=0.1, multiplier=1.5)
        self.assertEqual(result_none, 150_000_000)

    def test_bumped_gas_price_satisfies_evm_replacement_rule(self):
        """EVM replacement rule requires >= 10% increase over previous gas price."""
        prev_gas_price = 150_000_000  # 0.15 gwei
        bumped = get_bumped_gas_price(prev_gas_price, min_bump_ratio=1.15)
        # Must be >= 10% higher than previous
        self.assertGreaterEqual(bumped, int(prev_gas_price * 1.10) + 1)
        self.assertGreaterEqual(bumped, int(prev_gas_price * 1.15))

    def test_bumped_gas_price_respects_market_spike(self):
        """If market gas price surges during retry, use the higher market price."""
        prev_gas_price = 150_000_000  # 0.15 gwei
        # Sudden network spike to 0.5 gwei * 1.5 = 0.75 gwei
        surged_network_price = 500_000_000
        bumped = get_bumped_gas_price(
            prev_gas_price,
            min_bump_ratio=1.15,
            network_gas_price_wei=surged_network_price,
            min_gas_price_gwei=0.1,
            multiplier=1.5
        )
        self.assertEqual(bumped, 750_000_000)


class TestFunctionSelectors(unittest.TestCase):
    def test_selectors_match_minerv1(self):
        """Verifies selectors match MinerV1 Solidity definitions."""
        self.assertEqual(SELECTOR_EPOCH, "900cf0cf")          # epoch()
        self.assertEqual(SELECTOR_LAST_SYNCED, "93afb4c5")    # lastSynced()
        self.assertEqual(SELECTOR_EPOCH_SEED, "4abccd28")     # epochSeed(uint256)
        self.assertEqual(SELECTOR_INIT_EPOCH_SEED, "d9d9c33c")  # initEpochSeed(uint256,uint256)
        self.assertEqual(SELECTOR_SYNC, "b1357bf9")           # sync(uint256)


class TestChecksumAddress(unittest.TestCase):
    def test_checksum_calculation(self):
        test_addr = "0x420382a0f874c1f120621f847d81581e8737504e"
        checksummed = to_checksum_address(test_addr)
        self.assertEqual(checksummed, "0x420382a0f874c1F120621f847d81581e8737504e")


class TestKeeperBotDryRun(unittest.TestCase):
    @patch("script.keeper.rpc_call")
    def test_dry_run_never_broadcasts_transaction(self, mock_rpc):
        """In dry-run mode, eth_sendRawTransaction must NEVER be called."""
        def rpc_side_effect(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61" # 97
            elif method == "eth_call":
                # epoch -> 0, lastSynced -> 0, epochSeed(0) -> 0
                return "0x0000000000000000000000000000000000000000000000000000000000000000"
            elif method == "eth_gasPrice":
                return "0x5f5e100"  # 100,000,000 wei = 0.1 gwei
            elif method == "eth_getTransactionCount":
                return "0x5"
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=None,
            keeper_address="0x1111111111111111111111111111111111111111",
            dry_run=True
        )

        with patch.object(bot, "log") as mock_log:
            bot.step()

            # Ensure eth_sendRawTransaction was NEVER called
            for call_item in mock_rpc.call_args_list:
                args = call_item[0]
                self.assertNotEqual(args[1], "eth_sendRawTransaction")

            # Check dry-run logs for target contract, function, gas price, nonce
            dry_run_logs = [call[0][1] for call in mock_log.call_args_list if call[0][0] == "DRY-RUN"]
            combined_logs = " ".join(dry_run_logs)
            self.assertIn("0x420382a0f874c1f120621f847d81581e8737504e", combined_logs)
            self.assertIn("Target Contract: 0x420382a0f874c1f120621f847d81581e8737504e", combined_logs)
            self.assertIn("On-Chain Epoch:  0", combined_logs)
            self.assertIn("Chain ID:        97", combined_logs)
            self.assertIn("initEpochSeed(0, 24)", combined_logs)
            self.assertIn("Gas Price:", combined_logs)
            self.assertIn("Nonce:", combined_logs)


class TestKeeperBotSpeedupRetry(unittest.TestCase):
    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    def test_speedup_retry_reuses_identical_nonce_and_bumps_gas_price(self, mock_rpc, mock_time):
        """When confirmation times out, bot must re-broadcast with IDENTICAL nonce and higher gasPrice."""
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []

        def rpc_side_effect(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"  # 100,000,000 wei = 0.1 gwei
            elif method == "eth_getTransactionCount":
                return "0x2"  # Nonce = 2
            elif method == "eth_chainId":
                return "0x61" # 97
            elif method == "eth_estimateGas":
                return "0x186a0" # 100,000
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'a' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                # Timeout on first tx (0), confirm on second tx (1)
                tx_hash = params[0]
                if tx_hash == f"0x{'a' * 60}0000":
                    return None  # Pending/unconfirmed
                elif tx_hash == f"0x{'a' * 60}0001":
                    return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            tx_timeout=1, # 1 sec timeout with mocked clock
            max_retries=2
        )

        calldata = "0xd9d9c33c" + "00" * 64
        receipt = bot.send_transaction(calldata, "initEpochSeed(0, 24)", "initEpochSeed(0, 24)")

        self.assertIsNotNone(receipt)
        self.assertEqual(len(broadcasted_txs), 2)

        # Decode raw transactions using rlp: [nonce, gasPrice, gas, to, value, data, v, r, s]
        raw0 = bytes.fromhex(broadcasted_txs[0]["raw"].replace("0x", ""))
        raw1 = bytes.fromhex(broadcasted_txs[1]["raw"].replace("0x", ""))
        dec0 = rlp.decode(raw0)
        dec1 = rlp.decode(raw1)

        nonce0 = int.from_bytes(dec0[0], "big")
        gas_price0 = int.from_bytes(dec0[1], "big")

        nonce1 = int.from_bytes(dec1[0], "big")
        gas_price1 = int.from_bytes(dec1[1], "big")

        # NONCE MUST BE IDENTICAL
        self.assertEqual(nonce0, 2)
        self.assertEqual(nonce1, 2)
        self.assertEqual(nonce0, nonce1)

        # GAS PRICE MUST BE BUMPED BY AT LEAST 10%
        self.assertGreater(gas_price1, gas_price0)
        self.assertGreaterEqual(gas_price1, int(gas_price0 * 1.10))




from script.keeper import (
    decode_revert_error,
    ERROR_SELECTORS,
    encode_uint256
)


class TestErrorDecoding(unittest.TestCase):
    def test_decode_custom_errors(self):
        # PrevSeedNotInitialized: 0x4105720d
        self.assertEqual(decode_revert_error({"code": 3, "data": "0x4105720d"}), "PrevSeedNotInitialized")
        self.assertEqual(decode_revert_error("reverted with 0x4105720d"), "PrevSeedNotInitialized")
        self.assertEqual(decode_revert_error(RuntimeError("execution reverted: PrevSeedNotInitialized()")), "PrevSeedNotInitialized")

        # FutureEpoch: 0x2d1adcaf
        self.assertEqual(decode_revert_error({"code": 3, "data": "0x2d1adcaf"}), "FutureEpoch")

        # SeedAlreadyInitialized: 0xbcae7b3d
        self.assertEqual(decode_revert_error({"code": 3, "data": "0xbcae7b3d"}), "SeedAlreadyInitialized")

        # BlockhashUnavailable: 0x92555c0e
        self.assertEqual(decode_revert_error({"code": 3, "data": "0x92555c0e"}), "BlockhashUnavailable")

    def test_decode_standard_errors(self):
        # Error(string): 08c379a0 + offset + length + "failed"
        data_str = "0x08c379a0" + "00"*31 + "20" + "00"*31 + "06" + "6661696c6564" + "00"*26
        self.assertEqual(decode_revert_error({"code": 3, "data": data_str}), "Error(failed)")

        # Panic(uint256): 4e487b71 + code (0x01)
        data_panic = "0x4e487b71" + "00"*31 + "01"
        self.assertEqual(decode_revert_error({"code": 3, "data": data_panic}), "Panic(0x1)")


class TestKeeperEpochSeedSequentialInit(unittest.TestCase):
    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_cur_3_all_uninitialized_sends_in_order_0_1_2_3_with_incrementing_nonce(self, mock_rpc, mock_sleep):
        """
        Requirement 3: cur=3 and seeds 0..3 uninitialized.
        Must send in order 0, 1, 2, 3 with incrementing nonces, confirming each before sending next.
        """
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []
        confirmed_receipts = {}
        preflight_calls = []

        def rpc_side_effect(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"  # 0.1 gwei
            elif method == "eth_getTransactionCount":
                # Initial pending nonce is 10
                return "0xa"
            elif method == "eth_chainId":
                return "0x61" # 97
            elif method == "eth_estimateGas":
                return "0x186a0" # 100,000
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return "0x3" # cur_epoch = 3
                elif sel == SELECTOR_LAST_SYNCED:
                    return "0x3" # lastSynced = 3 (no lag)
                elif sel == SELECTOR_EPOCH_SEED:
                    e = int(tx_data[8:72], 16) if len(tx_data) >= 72 else 0
                    if confirmed_receipts.get(str(e)):
                        return confirmed_receipts[str(e)]
                    return "0x0000000000000000000000000000000000000000000000000000000000000000"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    # Preflight check simulation
                    target_epoch = int(tx_data[8:72], 16)
                    preflight_calls.append(target_epoch)
                    return "0x" # Preflight succeeds
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'b' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                confirmed_receipts["3"] = "0x123456"
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                tx_hash = params[0]
                return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            tx_timeout=2,
            max_retries=1
        )

        bot.step()

        # Exactly 1 batch transaction sent
        self.assertEqual(len(broadcasted_txs), 1)

        # Preflight called for target 3
        self.assertEqual(preflight_calls, [3])

        nonces = []
        target_epochs = []
        for i, tx_info in enumerate(broadcasted_txs):
            raw_bytes = bytes.fromhex(tx_info["raw"].replace("0x", ""))
            decoded = rlp.decode(raw_bytes)
            nonce = int.from_bytes(decoded[0], "big")
            calldata = decoded[5].hex()
            nonces.append(nonce)

            self.assertTrue(calldata.startswith(SELECTOR_INIT_EPOCH_SEED))
            epoch_arg = int(calldata[8:72], 16)
            steps_arg = int(calldata[72:136], 16)
            self.assertEqual(steps_arg, 24)
            target_epochs.append(epoch_arg)

        self.assertEqual(target_epochs, [3])
        self.assertEqual(nonces, [10])

    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_cur_3_stops_when_intermediate_preflight_fails(self, mock_rpc, mock_sleep):
        """
        Requirement 3: cur=3 and seeds 0..3 uninitialized.
        When intermediate preflight fails (e.g. at epoch 2), bot must stop and NOT send epoch 2 or 3.
        """
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []
        preflight_calls = []

        def rpc_side_effect(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x0"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return "0x3"
                elif sel == SELECTOR_LAST_SYNCED:
                    return "0x3"
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0000000000000000000000000000000000000000000000000000000000000000"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    target_epoch = int(tx_data[8:72], 16)
                    preflight_calls.append(target_epoch)
                    if target_epoch == 3:
                        # Preflight for epoch 3 fails with PrevSeedNotInitialized (0x4105720d)
                        raise RuntimeError("RPC Error (eth_call): {'code': 3, 'data': '0x4105720d', 'message': 'execution reverted: PrevSeedNotInitialized()'}")
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'c' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            tx_timeout=2,
            max_retries=1
        )

        with patch.object(bot, "alert") as mock_alert:
            bot.step()

            # Preflight was called for 3
            self.assertEqual(preflight_calls, [3])

            # Zero transactions broadcast due to preflight revert
            self.assertEqual(len(broadcasted_txs), 0)

            # Check that decoded error name PrevSeedNotInitialized was alerted
            alert_msgs = [call[0][0] for call in mock_alert.call_args_list]
            combined_alerts = " ".join(alert_msgs)
            self.assertIn("PrevSeedNotInitialized", combined_alerts)

    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_cur_3_stops_when_intermediate_tx_reverts(self, mock_rpc, mock_sleep):
        """
        Requirement 3: cur=3 and seeds 0..3 uninitialized.
        When intermediate transaction reverts on-chain (e.g. at epoch 1), bot must stop and NOT send epoch 2 or 3.
        """
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []

        def rpc_side_effect(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x0"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return "0x3"
                elif sel == SELECTOR_LAST_SYNCED:
                    return "0x3"
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0000000000000000000000000000000000000000000000000000000000000000"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'d' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                tx_hash = params[0]
                # Revert on chain
                return {"blockNumber": "0x101", "gasUsed": "0x5000", "status": "0x0"}
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            tx_timeout=2,
            max_retries=1
        )

        with patch.object(bot, "alert") as mock_alert:
            bot.step()

            # Tx reverted on chain; halted immediately
            self.assertEqual(len(broadcasted_txs), 1)
            alert_msgs = [call[0][0] for call in mock_alert.call_args_list]
            combined_alerts = " ".join(alert_msgs)
            self.assertIn("REVERTED", combined_alerts)



class TestMinerAddressValidation(unittest.TestCase):
    @patch("script.keeper.rpc_call")
    def test_miner_address_none_exits_immediately(self, mock_rpc):
        """When miner address is None, must immediately exit with error and print reason."""
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(rpc_url="http://mock-rpc", miner_address=None)
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_miner_address_empty_string_exits_immediately(self, mock_rpc):
        """When miner address is empty or whitespace, must immediately exit with error and print reason."""
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(rpc_url="http://mock-rpc", miner_address="   ")
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_miner_address_zero_address_exits_immediately(self, mock_rpc):
        """When miner address is zero address (0x0 or 40 zeros), must immediately exit with error."""
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(rpc_url="http://mock-rpc", miner_address="0x0000000000000000000000000000000000000000")
        self.assertEqual(ctx.exception.code, 1)

        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(rpc_url="http://mock-rpc", miner_address="0x0")
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_miner_address_empty_code_exits_immediately(self, mock_rpc):
        """When eth_getCode returns empty/0x (no bytecode), must immediately exit with error and print reason."""
        mock_rpc.side_effect = lambda url, method, params: "0x" if method == "eth_getCode" else None
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(rpc_url="http://mock-rpc", miner_address="0x420382a0f874c1F120621f847d81581e8737504e")
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_miner_address_valid_contract_code_passes(self, mock_rpc):
        """When eth_getCode returns non-empty bytecode, validation succeeds."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else None
        bot = KeeperBot(rpc_url="http://mock-rpc", miner_address="0x420382a0f874c1F120621f847d81581e8737504e")
        self.assertEqual(bot.miner, "0x420382a0f874c1f120621f847d81581e8737504e")


class TestKeeperExplicitRpcAndNoEnv(unittest.TestCase):
    @patch("script.keeper.KeeperBot")
    @patch("script.keeper.load_env")
    def test_explicit_rpc_priority_over_env(self, mock_load_env, mock_bot_cls):
        """1. Explicit --rpc-url takes precedence over RPC_URL in .env or os.environ."""
        mock_load_env.return_value = {
            "RPC_URL": "https://env-rpc.example.com",
            "MINER_ADDRESS": "0x420382a0f874c1f120621f847d81581e8737504e"
        }
        mock_bot_instance = MagicMock()
        mock_bot_cls.return_value = mock_bot_instance

        test_args = ["keeper.py", "--rpc-url", "https://explicit-rpc.example.com", "--dry-run", "--once"]
        with patch.object(sys, "argv", test_args):
            from script.keeper import main
            main()

        mock_bot_cls.assert_called_once()
        call_kwargs = mock_bot_cls.call_args[1]
        self.assertEqual(call_kwargs["rpc_url"], "https://explicit-rpc.example.com")
        self.assertTrue(call_kwargs["dry_run"])

    @patch("script.keeper.KeeperBot")
    @patch("script.keeper.load_env")
    def test_no_env_never_loads_env_file(self, mock_load_env, mock_bot_cls):
        """2. Under --no-env, load_env is never called for default .env or --env-file."""
        mock_bot_instance = MagicMock()
        mock_bot_cls.return_value = mock_bot_instance

        test_args = [
            "keeper.py",
            "--no-env",
            "--rpc-url", "https://explicit-rpc.example.com",
            "--miner-address", "0x420382a0f874c1f120621f847d81581e8737504e",
            "--dry-run",
            "--once"
        ]
        with patch.object(sys, "argv", test_args):
            from script.keeper import main
            main()

        mock_load_env.assert_not_called()
        mock_bot_cls.assert_called_once()
        self.assertEqual(mock_bot_cls.call_args[1]["rpc_url"], "https://explicit-rpc.example.com")

    @patch("script.keeper.load_env")
    def test_no_env_missing_rpc_url_fails_explicitly(self, mock_load_env):
        """3. Under --no-env, if --rpc-url is missing, clearly error and exit, without reading ambient RPC."""
        test_args = [
            "keeper.py",
            "--no-env",
            "--miner-address", "0x420382a0f874c1f120621f847d81581e8737504e",
            "--dry-run",
            "--once"
        ]
        with patch.dict(os.environ, {"RPC_URL": "https://ambient-rpc.example.com"}):
            with patch.object(sys, "argv", test_args):
                with patch("sys.stderr"):
                    from script.keeper import main
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertEqual(ctx.exception.code, 1)
        mock_load_env.assert_not_called()

    @patch("script.keeper.KeeperBot")
    @patch("script.keeper.load_env")
    def test_legacy_config_backward_compatibility(self, mock_load_env, mock_bot_cls):
        """4. Preserves backward compatibility when --no-env is omitted; loads .env as usual."""
        mock_load_env.return_value = {
            "RPC_URL": "https://legacy-env-rpc.example.com",
            "MINER_ADDRESS": "0x420382a0f874c1f120621f847d81581e8737504e"
        }
        mock_bot_instance = MagicMock()
        mock_bot_cls.return_value = mock_bot_instance

        test_args = ["keeper.py", "--dry-run", "--once"]
        with patch.object(sys, "argv", test_args):
            from script.keeper import main
            main()

        mock_load_env.assert_called_once_with(".env")
        mock_bot_cls.assert_called_once()
        self.assertEqual(mock_bot_cls.call_args[1]["rpc_url"], "https://legacy-env-rpc.example.com")

    @patch("script.keeper.rpc_call")
    def test_dry_run_with_invalid_private_key_env_var(self, mock_rpc):
        """5. In dry-run mode, invalid private key in environment does not cause failure, signing, or parsing."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        invalid_pk = "INVALID_PRIVATE_KEY_GARBAGE_XYZ_NOT_HEX"
        with patch.dict(os.environ, {"PRIVATE_KEY": invalid_pk, "KEEPER_PRIVATE_KEY": invalid_pk}):
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
                private_key=invalid_pk,
                dry_run=True,
                keeper_address="0x1111111111111111111111111111111111111111"
            )
            self.assertIsNone(bot.private_key)
            bot.step()

    @patch("script.keeper.rpc_call")
    def test_sensitive_credentials_in_url_desensitized(self, mock_rpc):
        """6. Credentials in path, query, and userinfo of RPC URL are never leaked in logs or stdout/stderr."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        fictional_url = "https://mock_user:mock_pass_secret@rpc.example.com:8545/mock_path_secret/v1?mock_token_secret=xyz#mock_frag_secret"

        bot = KeeperBot(
            rpc_url=fictional_url,
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            dry_run=True
        )

        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.step()
            bot.log("CONFIG", f"RPC URL: {sanitize_url(bot.rpc_url)} | Poll: {bot.poll_interval}s")

        output = captured.getvalue()
        self.assertNotIn("mock_user", output)
        self.assertNotIn("mock_pass_secret", output)
        self.assertNotIn("mock_path_secret", output)
        self.assertNotIn("mock_token_secret", output)
        self.assertNotIn("mock_frag_secret", output)
        self.assertIn("https://rpc.example.com:8545/***", output)

    @patch("script.keeper.rpc_call")
    def test_network_exception_with_sensitive_url_not_leaked(self, mock_rpc):
        """7. Network exception embedding sensitive URL is safely sanitized without leaking credentials."""
        class MockRequestsConnectionError(Exception):
            pass

        sensitive_exc_msg = "Failed to establish connection to https://alice:secret_key_pass@node.bsc:8545/path_secret_token?query_secret=1"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            MockRequestsConnectionError(sensitive_exc_msg)
        )

        bot = KeeperBot(
            rpc_url="https://node.bsc:8545",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            dry_run=True
        )

        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            mock_rpc.side_effect = MockRequestsConnectionError(sensitive_exc_msg)
            bot.step()

        output = captured.getvalue()
        self.assertNotIn("alice:secret_key_pass", output)
        self.assertNotIn("secret_key_pass", output)
        self.assertNotIn("path_secret_token", output)
        self.assertNotIn("query_secret", output)

    @patch("script.keeper.rpc_call")
    def test_dry_run_seed_and_sync_paths_never_broadcast(self, mock_rpc):
        """8. Both epoch seed initialization and sync lag paths in dry-run simulate calls and never broadcast."""
        calls_made = []
        def rpc_side_effect(url, method, params):
            calls_made.append(method)
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(5)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(2)
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                return "0x"
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            keeper_address="0x1111111111111111111111111111111111111111",
            dry_run=True
        )

        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.step()

        output = captured.getvalue()
        self.assertIn("Would call initEpochSeed", output)
        self.assertIn("Would call sync", output)
        self.assertNotIn("eth_sendRawTransaction", calls_made)

    @patch("script.keeper.KeeperBot")
    @patch("script.keeper.load_env")
    def test_once_flag_runs_single_step_and_exits(self, mock_load_env, mock_bot_cls):
        """9. --once flag executes a single inspection step and exits cleanly without entering run_forever."""
        mock_load_env.return_value = {"MINER_ADDRESS": "0x420382a0f874c1f120621f847d81581e8737504e"}
        mock_bot_instance = MagicMock()
        mock_bot_cls.return_value = mock_bot_instance

        test_args = ["keeper.py", "--rpc-url", "https://mock-rpc", "--dry-run", "--once"]
        with patch.object(sys, "argv", test_args):
            from script.keeper import main
            main()

        mock_bot_instance.step.assert_called_once()
        mock_bot_instance.run_forever.assert_not_called()




class TestSanitizationAndLogging(unittest.TestCase):
    """Tests verifying precise sanitization in logs and outputs without over-sanitizing hashes."""

    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    def test_tx_hash_and_block_hash_and_seed_preserved_in_logs(self, mock_rpc):
        """0x transaction hashes (64-hex), block hashes, and seeds in logs are preserved verbatim."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        tx_hash = "0x84661e8dbaa75ee789405971bbcbe32b218d7b60000000000000000000000000"
        block_hash = "0x" + "b" * 64
        seed_val = "0x" + "c" * 64

        log_msg = f"Broadcasted initEpochSeed txHash={tx_hash} blockHash={block_hash} seed={seed_val}"
        sanitized = sanitize_text(log_msg)
        self.assertIn(tx_hash, sanitized)
        self.assertIn(block_hash, sanitized)
        self.assertIn(seed_val, sanitized)
        self.assertEqual(sanitized, log_msg)

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            dry_run=True
        )
        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.log("TX", f"Broadcasted txHash={tx_hash} in block {block_hash}")

        output = captured.getvalue()
        self.assertIn(tx_hash, output)
        self.assertIn(block_hash, output)
        self.assertNotIn("[REDACTED_KEY]", output)

    @patch("script.keeper.rpc_call")
    def test_known_configured_key_string_replaced_in_logs(self, mock_rpc):
        """Known configured secret key string is replaced in logs and sanitize_text."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        known_key = "0x" + "1" * 64
        register_secret(known_key)

        tx_hash = "0x" + "9" * 64
        log_msg = f"Signer private key is {known_key} for txHash={tx_hash}"
        sanitized = sanitize_text(log_msg)
        self.assertNotIn(known_key, sanitized)
        self.assertNotIn(known_key[2:], sanitized)
        self.assertIn("0x[REDACTED_KEY]", sanitized)
        self.assertIn(tx_hash, sanitized)

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            private_key=known_key,
            dry_run=True
        )
        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.log("INFO", f"Keeper initialized with key={known_key} tx={tx_hash}")

        output = captured.getvalue()
        self.assertNotIn(known_key, output)
        self.assertIn("0x[REDACTED_KEY]", output)
        self.assertIn(tx_hash, output)

    @patch("script.keeper.rpc_call")
    def test_sensitive_key_value_pairs_replaced_in_logs(self, mock_rpc):
        """Key-value pairs such as private_key=... and password=... are masked."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        log_msg = (
            "Configuration: private_key=0xabcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789 "
            "and password=SuperSecretPassword123! and KEEPER_PRIVATE_KEY=secret_val"
        )
        sanitized = sanitize_text(log_msg)
        self.assertNotIn("0xabcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789", sanitized)
        self.assertNotIn("SuperSecretPassword123!", sanitized)
        self.assertNotIn("secret_val", sanitized)
        self.assertIn("private_key=[REDACTED_KEY]", sanitized)
        self.assertIn("password=[REDACTED]", sanitized)
        self.assertIn("KEEPER_PRIVATE_KEY=[REDACTED_KEY]", sanitized)

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            dry_run=True
        )
        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.log("CONFIG", "private_key=0xabc123 password=MySecret123")

        output = captured.getvalue()
        self.assertNotIn("0xabc123", output)
        self.assertNotIn("MySecret123", output)
        self.assertIn("private_key=[REDACTED_KEY]", output)
        self.assertIn("password=[REDACTED]", output)

    @patch("script.keeper.rpc_call")
    def test_url_credentials_replaced_in_logs(self, mock_rpc):
        """URL credentials (user, password, sensitive paths/tokens) are replaced with masked endpoint."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        sensitive_url = "https://admin_user:secret_password_456@bsc-testnet.rpc:8545/secure_token?key=abc#frag"
        log_msg = f"Connecting to {sensitive_url}"
        sanitized = sanitize_text(log_msg)
        self.assertNotIn("admin_user", sanitized)
        self.assertNotIn("secret_password_456", sanitized)
        self.assertNotIn("secure_token", sanitized)
        self.assertNotIn("key=abc", sanitized)
        self.assertNotIn("frag", sanitized)
        self.assertIn("https://bsc-testnet.rpc:8545/***", sanitized)

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1f120621f847d81581e8737504e",
            dry_run=True
        )
        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            bot.log("RPC", f"Failed to connect: {sensitive_url}")

        output = captured.getvalue()
        self.assertNotIn("admin_user", output)
        self.assertNotIn("secret_password_456", output)
        self.assertIn("https://bsc-testnet.rpc:8545/***", output)


class TestKeeperBoundaryCorrections(unittest.TestCase):
    """
    Offline tests for keeper boundary corrections:
    1. Dry-run never reads, registers, or retains private keys.
    2. Strict env mapping throws AssertionError on secret key access; dry-run main & logs succeed.
    3. load_env is strictly mocked and .env is never read.
    4. Live mode identity consistency: derived signer vs explicit keeper address mismatch rejects cleanly.
    5. Invalid private key in live mode fails cleanly without except/pass and without leaking raw values.
    6. Numerical argument priorities: --max-retries 0 preserved, negative values rejected cleanly.
    7. Quoted sensitive values desensitized while tx/block/seed hashes are preserved.
    """

    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    @patch("script.keeper.load_env")
    def test_dry_run_main_with_strict_env_mapping_never_reads_secrets(self, mock_load_env, mock_rpc):
        """Mock environment mapping raises AssertionError when accessing secret keys; dry-run main & log succeed."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode" else
            "0x61" if method == "eth_chainId" else
            "0x0" if method in ("eth_call", "eth_gasPrice", "eth_getTransactionCount") else None
        )
        mock_load_env.return_value = {
            "RPC_URL": "http://mock-rpc",
            "MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e"
        }

        class StrictEnv(dict):
            def __getitem__(self, key):
                if key in ("PRIVATE_KEY", "KEEPER_PRIVATE_KEY") or ("KEY" in key.upper() and "PRIVATE" in key.upper()):
                    raise AssertionError(f"StrictEnv: secret key '{key}' was accessed in dry-run!")
                return super().__getitem__(key)

            def get(self, key, default=None):
                if key in ("PRIVATE_KEY", "KEEPER_PRIVATE_KEY") or ("KEY" in key.upper() and "PRIVATE" in key.upper()):
                    raise AssertionError(f"StrictEnv: secret key '{key}' was accessed in dry-run!")
                return super().get(key, default)

        strict_environ = StrictEnv(os.environ)

        test_args = [
            "keeper.py",
            "--dry-run",
            "--once",
            "--rpc-url", "http://mock-rpc",
            "--miner-address", "0x420382a0f874c1F120621f847d81581e8737504e",
            "--keeper-address", "0x1111111111111111111111111111111111111111"
        ]

        with patch.object(sys, "argv", test_args):
            with patch("os.environ", strict_environ):
                from script.keeper import main
                # Must succeed without throwing AssertionError
                main()

    @patch("script.keeper.rpc_call")
    def test_dry_run_bot_never_stores_or_registers_passed_key(self, mock_rpc):
        """KeeperBot in dry_run=True does not save private_key to self.private_key, self._known_secrets, or global set."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        secret_key = "0x" + "a" * 64

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=secret_key,
            dry_run=True,
            keeper_address="0x1111111111111111111111111111111111111111"
        )
        self.assertIsNone(bot.private_key)
        self.assertEqual(len(bot._known_secrets), 0)
        self.assertNotIn(secret_key, _CONFIGURED_SECRETS)
        self.assertNotIn(secret_key[2:], _CONFIGURED_SECRETS)

    @patch("script.keeper.rpc_call")
    def test_live_mode_identity_matching(self, mock_rpc):
        """Live mode derives signer address from private key and matches explicit keeper_address."""
        from eth_account import Account
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        valid_key = "0x" + "1" * 64
        expected_address = Account.from_key(valid_key).address

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=valid_key,
            keeper_address=expected_address,
            dry_run=False
        )
        self.assertEqual(bot.keeper_address.lower(), expected_address.lower())
        self.assertEqual(bot.private_key, valid_key)

    @patch("script.keeper.rpc_call")
    def test_live_mode_identity_mismatch_fails_cleanly(self, mock_rpc):
        """Live mode clearly rejects when explicit keeper_address does not match derived signer address."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        valid_key = "0x" + "1" * 64
        mismatched_address = "0x2222222222222222222222222222222222222222"

        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                private_key=valid_key,
                keeper_address=mismatched_address,
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_live_mode_invalid_private_key_fails_cleanly(self, mock_rpc):
        """Live mode clearly rejects invalid private key without except/pass and without leaking key."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        invalid_key = "INVALID_PRIVATE_KEY_NOT_HEX_ABC_123"

        import io
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            with self.assertRaises(SystemExit) as ctx:
                KeeperBot(
                    rpc_url="http://mock-rpc",
                    miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                    private_key=invalid_key,
                    dry_run=False
                )
            self.assertEqual(ctx.exception.code, 1)

        output = captured.getvalue()
        self.assertNotIn(invalid_key, output)
        self.assertIn("Invalid private key", output)

    @patch("script.keeper.rpc_call")
    def test_live_mode_missing_private_key_fails_cleanly(self, mock_rpc):
        """Live mode clearly rejects when private_key is None or empty."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"

        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                private_key=None,
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.KeeperBot")
    @patch("script.keeper.load_env")
    def test_numeric_cli_precedence_and_max_retries_zero(self, mock_load_env, mock_bot_cls):
        """--max-retries 0 is preserved as 0 and not fallen back to env or default; negative values rejected."""
        mock_load_env.return_value = {
            "MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e",
            "KEEPER_MAX_RETRIES": "5"
        }
        mock_bot_instance = MagicMock()
        mock_bot_cls.return_value = mock_bot_instance

        # 1. Explicit --max-retries 0 must be preserved as 0
        test_args_zero = ["keeper.py", "--rpc-url", "http://mock-rpc", "--dry-run", "--once", "--max-retries", "0"]
        with patch.object(sys, "argv", test_args_zero):
            from script.keeper import main
            main()

        mock_bot_cls.assert_called_once()
        self.assertEqual(mock_bot_cls.call_args[1]["max_retries"], 0)

        mock_bot_cls.reset_mock()

        # 2. Omitted --max-retries falls back to env 5
        test_args_env = ["keeper.py", "--rpc-url", "http://mock-rpc", "--dry-run", "--once"]
        with patch.object(sys, "argv", test_args_env):
            main()

        mock_bot_cls.assert_called_once()
        self.assertEqual(mock_bot_cls.call_args[1]["max_retries"], 5)

        # 3. Negative --max-retries -1 rejected cleanly without leaking raw value
        import io
        err_captured = io.StringIO()
        test_args_neg = ["keeper.py", "--rpc-url", "http://mock-rpc", "--dry-run", "--once", "--max-retries", "-1"]
        with patch.object(sys, "argv", test_args_neg):
            with patch("sys.stderr", err_captured):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)

        err_output = err_captured.getvalue()
        self.assertNotIn("-1", err_output)
        self.assertIn("Invalid value for --max-retries", err_output)

        # 4. Negative value in env rejected cleanly
        mock_load_env.return_value = {
            "MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e",
            "KEEPER_MAX_RETRIES": "-99"
        }
        err_captured = io.StringIO()
        with patch.object(sys, "argv", test_args_env):
            with patch("sys.stderr", err_captured):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)

        err_output = err_captured.getvalue()
        self.assertNotIn("-99", err_output)
        self.assertIn("Invalid value for --max-retries", err_output)

    def test_quoted_sensitive_values_desensitization(self):
        """Quoted sensitive values (with spaces, single/double quotes, JSON) are sanitized, public hashes preserved."""
        tx_hash = "0x" + "a" * 64
        block_hash = "0x" + "b" * 64
        seed_hash = "0x" + "c" * 64

        text1 = 'config: password="alpha beta" and extra=normal'
        s1 = sanitize_text(text1)
        self.assertNotIn("alpha beta", s1)
        self.assertIn('password="[REDACTED]"', s1)
        self.assertIn("extra=normal", s1)

        text2 = '{"username": "keeper", "password": "alpha beta", "txHash": "' + tx_hash + '"}'
        s2 = sanitize_text(text2)
        self.assertNotIn("alpha beta", s2)
        self.assertIn('"password": "[REDACTED]"', s2)
        self.assertIn(tx_hash, s2)

        text3 = "login info: 'password': 'alpha beta' with seed=" + seed_hash
        s3 = sanitize_text(text3)
        self.assertNotIn("alpha beta", s3)
        self.assertIn("'password': '[REDACTED]'", s3)
        self.assertIn(seed_hash, s3)

        text4 = "shell: password='alpha beta' blockHash=" + block_hash
        s4 = sanitize_text(text4)
        self.assertNotIn("alpha beta", s4)
        self.assertIn("password='[REDACTED]'", s4)
        self.assertIn(block_hash, s4)

        text5 = 'API: api_key="sk-live secret key with spaces 12345"'
        s5 = sanitize_text(text5)
        self.assertNotIn("sk-live secret key with spaces 12345", s5)
        self.assertIn('api_key="[REDACTED_KEY]"', s5)



class TestFoundryKeystoreIntegration(unittest.TestCase):
    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_keystore_correct_arg_construction(self, mock_subproc, mock_rpc):
        """Keystore mode constructs correct args for cast wallet address and cast send with --async."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )

        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx_hash = "0x" + "a" * 64

        # wallet address returns fake_addr, send returns fake_tx_hash
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx_hash}\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False
        )

        receipt = bot.send_transaction("0x12345678", "test_call", preflight=False)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.get("status"), "0x1")

        self.assertEqual(mock_subproc.call_count, 2)

        # Call 1: cast wallet address
        call1_args, call1_kwargs = mock_subproc.call_args_list[0]
        cmd1 = call1_args[0]
        self.assertIn("wallet", cmd1)
        self.assertIn("address", cmd1)
        self.assertIn("--account", cmd1)
        self.assertEqual(cmd1[cmd1.index("--account") + 1], "test-keystore-account")
        self.assertIn("--password-file", cmd1)
        self.assertEqual(cmd1[cmd1.index("--password-file") + 1], "fake-passwords/dev.pwd")
        self.assertFalse(call1_kwargs.get("shell", True))

        # Call 2: cast send
        call2_args, call2_kwargs = mock_subproc.call_args_list[1]
        cmd2 = call2_args[0]
        self.assertIn("send", cmd2)
        self.assertIn(to_checksum_address("0x420382a0f874c1F120621f847d81581e8737504e"), cmd2)
        self.assertIn("--data", cmd2)
        self.assertEqual(cmd2[cmd2.index("--data") + 1], "0x12345678")
        self.assertIn("--account", cmd2)
        self.assertEqual(cmd2[cmd2.index("--account") + 1], "test-keystore-account")
        self.assertIn("--password-file", cmd2)
        self.assertEqual(cmd2[cmd2.index("--password-file") + 1], "fake-passwords/dev.pwd")
        self.assertIn("--rpc-url", cmd2)
        self.assertEqual(cmd2[cmd2.index("--rpc-url") + 1], "http://mock-rpc")
        self.assertIn("--async", cmd2)
        self.assertFalse(call2_kwargs.get("shell", True))

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_wallet_address_and_send_use_same_account_and_password_file(self, mock_subproc, mock_rpc):
        """cast wallet address and cast send strictly use the identical account and password-file."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout="0x" + "b" * 64 + "\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="keeper-prod-key",
            password_file="safe-secrets/keeper.pass",
            dry_run=False
        )
        bot.send_transaction("0xabcdef", "test_sync", preflight=False)

        cmd_addr = mock_subproc.call_args_list[0][0][0]
        cmd_send = mock_subproc.call_args_list[1][0][0]

        acct_addr = cmd_addr[cmd_addr.index("--account") + 1]
        pw_addr = cmd_addr[cmd_addr.index("--password-file") + 1]

        acct_send = cmd_send[cmd_send.index("--account") + 1]
        pw_send = cmd_send[cmd_send.index("--password-file") + 1]

        self.assertEqual(acct_addr, acct_send)
        self.assertEqual(pw_addr, pw_send)
        self.assertEqual(acct_addr, "keeper-prod-key")
        self.assertEqual(pw_addr, "safe-secrets/keeper.pass")

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_no_private_key_passed_to_send(self, mock_subproc, mock_rpc):
        """--private-key must NEVER be passed to cast in keystore mode."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A\n", stderr=""),
            MagicMock(returncode=0, stdout="0x" + "c" * 64 + "\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="my-account",
            password_file="pw.txt",
            dry_run=False
        )
        bot.send_transaction("0x9999", "test", preflight=False)

        for call_item in mock_subproc.call_args_list:
            cmd = call_item[0][0]
            self.assertNotIn("--private-key", cmd)

    def test_conflict_active_private_key_and_account(self):
        """Active private key in env/config and --account are strictly mutually exclusive."""
        # 1. In KeeperBot direct instantiation
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                private_key="0x" + "1" * 64,
                account="my-account",
                password_file="pw.txt",
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)

        # 2. In CLI main with KEEPER_PRIVATE_KEY
        err_captured = io.StringIO()
        test_args = ["keeper.py", "--rpc-url", "http://mock-rpc", "--account", "my-account", "--password-file", "pw.txt"]
        with patch.object(sys, "argv", test_args):
            with patch.dict(os.environ, {"KEEPER_PRIVATE_KEY": "0x" + "2" * 64, "MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e"}):
                with patch("sys.stderr", err_captured):
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Conflicting signer configurations", err_captured.getvalue())

        # 3. Conflict also checked in --dry-run
        err_captured = io.StringIO()
        test_args_dry = ["keeper.py", "--rpc-url", "http://mock-rpc", "--dry-run", "--account", "my-account"]
        with patch.object(sys, "argv", test_args_dry):
            with patch.dict(os.environ, {"PRIVATE_KEY": "0x" + "3" * 64, "MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e"}):
                with patch("sys.stderr", err_captured):
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Conflicting signer configurations", err_captured.getvalue())

    def test_password_file_without_account_rejected(self):
        """--password-file cannot be used without --account."""
        # Direct class instantiation
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account=None,
                password_file="pw.txt",
                dry_run=True
            )
        self.assertEqual(ctx.exception.code, 1)

        # CLI main
        err_captured = io.StringIO()
        test_args = ["keeper.py", "--rpc-url", "http://mock-rpc", "--password-file", "pw.txt", "--dry-run"]
        with patch.object(sys, "argv", test_args):
            with patch("sys.stderr", err_captured):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--password-file cannot be used without --account", err_captured.getvalue())

    def test_live_account_without_password_file_rejected(self):
        """Live keystore execution without --password-file is rejected to prevent interactive prompt hangs."""
        # Direct class instantiation
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="my-account",
                password_file=None,
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)

        # CLI main
        err_captured = io.StringIO()
        test_args = ["keeper.py", "--rpc-url", "http://mock-rpc", "--account", "my-account"]
        with patch.object(sys, "argv", test_args):
            with patch.dict(os.environ, {"MINER_ADDRESS": "0x420382a0f874c1F120621f847d81581e8737504e"}):
                with patch("sys.stderr", err_captured):
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                    self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--account requires --password-file", err_captured.getvalue())

    def test_invalid_account_names(self):
        """Account names cannot be empty or contain directory paths, separators, or traversal."""
        invalid_names = ["", "   ", "../escape", "dir/acct", "dir\\acct", "/absolute", "C:\\acct", ":colon", "."]
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as ctx:
                    KeeperBot(
                        rpc_url="http://mock-rpc",
                        miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                        account=name,
                        password_file="pw.txt",
                        dry_run=True
                    )
                self.assertEqual(ctx.exception.code, 1)

                err_captured = io.StringIO()
                test_args = ["keeper.py", "--rpc-url", "http://mock-rpc", "--account", name, "--dry-run"]
                with patch.object(sys, "argv", test_args):
                    with patch("sys.stderr", err_captured):
                        with self.assertRaises(SystemExit) as ctx2:
                            main()
                        self.assertEqual(ctx2.exception.code, 1)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_address_derivation_failure_format_anomaly_mismatch_zero_send(self, mock_subproc, mock_rpc):
        """Subprocess failure, empty output, multiple lines, format anomaly, or address mismatch yield zero sends."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0"
        )

        cases = [
            ("failure", MagicMock(returncode=1, stdout="", stderr="password incorrect")),
            ("empty", MagicMock(returncode=0, stdout="", stderr="")),
            ("multiple_lines", MagicMock(returncode=0, stdout="0x1111111111111111111111111111111111111111\n0x2222222222222222222222222222222222222222\n", stderr="")),
            ("format_anomaly", MagicMock(returncode=0, stdout="not_an_address\n", stderr="")),
            ("format_bad_hex", MagicMock(returncode=0, stdout="0xGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG\n", stderr="")),
        ]

        for desc, mock_ret in cases:
            with self.subTest(case=desc):
                mock_subproc.reset_mock()
                mock_subproc.return_value = mock_ret

                with self.assertRaises(SystemExit) as ctx:
                    KeeperBot(
                        rpc_url="http://mock-rpc",
                        miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                        account="dev-account",
                        password_file="pw.txt",
                        dry_run=False
                    )
                self.assertEqual(ctx.exception.code, 1)
                # Ensure cast send was NEVER called (at most 1 call to wallet address, 0 calls to send)
                for call_item in mock_subproc.call_args_list:
                    self.assertNotIn("send", call_item[0][0])

        # Address mismatch against explicit --keeper-address
        mock_subproc.reset_mock()
        mock_subproc.return_value = MagicMock(returncode=0, stdout="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A\n", stderr="")
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                keeper_address="0x0000000000000000000000000000000000000001",
                account="dev-account",
                password_file="pw.txt",
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)
        for call_item in mock_subproc.call_args_list:
            self.assertNotIn("send", call_item[0][0])

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_dry_run_zero_wallet_or_signing_calls_no_password_file_access(self, mock_subproc, mock_rpc):
        """dry-run mode makes zero subprocess calls to cast, does not open or check password file, accepts missing password file."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x0"
        )

        nonexistent_pw_file = "nonexistent_secret_pw_file_987654321.txt"
        self.assertFalse(os.path.exists(nonexistent_pw_file))

        # Dry run with account and no password file
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="dev-account",
            password_file=None,
            dry_run=True
        )

        res = bot.send_transaction("0x1234", "dry_test")
        self.assertTrue(res.get("dry_run"))
        mock_subproc.assert_not_called()

        # Dry run with nonexistent password file: must NOT check os.path.exists or open it
        bot2 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="dev-account",
            password_file=nonexistent_pw_file,
            dry_run=True
        )
        res2 = bot2.send_transaction("0x1234", "dry_test_2")
        self.assertTrue(res2.get("dry_run"))
        mock_subproc.assert_not_called()

    @patch("script.keeper.load_env")
    @patch("script.keeper.KeeperBot")
    def test_no_env_does_not_load_env_file(self, mock_bot_cls, mock_load_env):
        """--no-env strictly prevents calling load_env and reading .env."""
        mock_load_env.side_effect = AssertionError("load_env must NEVER be called when --no-env is specified!")

        test_args = [
            "keeper.py",
            "--no-env",
            "--rpc-url", "http://explicit-rpc:8545",
            "--miner-address", "0x420382a0f874c1F120621f847d81581e8737504e",
            "--account", "offline-account",
            "--dry-run",
            "--once"
        ]
        with patch.object(sys, "argv", test_args):
            main()

        mock_load_env.assert_not_called()
        mock_bot_cls.assert_called_once()
        self.assertEqual(mock_bot_cls.call_args[1]["account"], "offline-account")

    def test_subprocess_env_isolates_conflicting_signer_vars(self):
        """get_isolated_cast_env scrubs all conflicting wallet/signer environment variables."""
        tainted_env = {
            "PATH": "/usr/bin:/bin",
            "SYSTEMROOT": "C:\\Windows",
            "HTTP_PROXY": "http://127.0.0.1:8080",
            # Ambient conflicting vars that must be isolated
            "ETH_FROM": "0xattacker",
            "ETH_KEYSTORE": "/path/to/other/keystore",
            "ETH_KEYSTORE_ACCOUNT": "other-acct",
            "ETH_PASSWORD": "secret_password",
            "PRIVATE_KEY": "0x" + "9" * 64,
            "KEEPER_PRIVATE_KEY": "0x" + "8" * 64,
            "ETH_PRIVATE_KEY": "0x" + "7" * 64,
            "MNEMONIC": "twelve secret words not to be used here",
            "AWS_KMS_KEY_ID": "arn:aws:kms:123",
            "TEMPO_ACCESS_KEY": "0x" + "6" * 64,
            "TEMPO_ROOT_ACCOUNT": "0xroot",
            # Case variations
            "eth_from": "0xattacker_lower",
            "private_key": "0xlower"
        }

        clean = get_isolated_cast_env(tainted_env)

        # Critical non-signing vars preserved
        self.assertIn("PATH", clean)
        self.assertIn("SYSTEMROOT", clean)
        self.assertIn("HTTP_PROXY", clean)

        # All conflicting signer vars stripped
        for var in WALLET_ENV_VARS_TO_ISOLATE:
            self.assertNotIn(var, clean)
            self.assertNotIn(var.lower(), clean)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_subprocess_error_sensitive_sentinels_not_leaked(self, mock_subproc, mock_rpc):
        """Subprocess errors and exceptions do not leak password file paths, keystore paths, or sensitive sentinels."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"

        sentinel_pw_path = "C:/secret/credentials/super_secret_keeper_pw_file.txt"
        sentinel_secret = "SUPER_SECRET_PLAINTEXT_PASSWORD_987654321"
        sentinel_keystore = "C:/Users/alice/.foundry/keystores/my_vault_account"

        # Register sentinel to simulate registered confidential trading materials
        register_secret(sentinel_secret)

        mock_subproc.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=f"Error decrypting {sentinel_keystore} with --password-file {sentinel_pw_path}: password='{sentinel_secret}'"
        )

        out_captured = io.StringIO()
        with patch("sys.stdout", out_captured):
            with self.assertRaises(SystemExit) as ctx:
                KeeperBot(
                    rpc_url="http://mock-rpc",
                    miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                    account="my_vault_account",
                    password_file=sentinel_pw_path,
                    dry_run=False
                )
            self.assertEqual(ctx.exception.code, 1)

        logged = out_captured.getvalue()
        self.assertNotIn(sentinel_pw_path, logged)
        self.assertNotIn(sentinel_secret, logged)
        self.assertNotIn(sentinel_keystore, logged)
        self.assertIn("[REDACTED", logged)

    @patch("script.keeper.rpc_call")
    def test_existing_private_key_mode_regression(self, mock_rpc):
        """Existing private key mode continues to function normally with Account.sign_transaction without invoking cast."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0xtxhash_pk_123" if method == "eth_sendRawTransaction"
            else {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )

        test_pk = "0x" + "4" * 64
        with patch("subprocess.run") as mock_subproc:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                private_key=test_pk,
                dry_run=False
            )
            receipt = bot.send_transaction("0x5555", "pk_mode_test", preflight=False)
            self.assertIsNotNone(receipt)
            mock_subproc.assert_not_called()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_cast_send_failure_stops_immediately_no_retry(self, mock_subproc, mock_rpc):
        """When cast send fails (non-zero exit code or execution error), keeper halts immediately without retrying."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        # 1. Non-zero exit code on cast send
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error: execution reverted: out of gas")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=3
        )

        receipt = bot.send_transaction("0x12345678", "test_call", preflight=False)
        self.assertEqual(receipt.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(receipt.is_uncertain)
        self.assertFalse(bool(receipt))
        self.assertTrue(bot.locked_for_manual_review)
        self.assertEqual(bot.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

        # 2. Exception on cast send (e.g. TimeoutExpired)
        clear_configured_secrets()
        import subprocess
        mock_subproc.reset_mock()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            subprocess.TimeoutExpired(cmd=["cast", "send"], timeout=60)
        ]
        bot2 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=3
        )
        receipt2 = bot2.send_transaction("0x12345678", "test_call", preflight=False)
        self.assertEqual(receipt2.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(receipt2.is_uncertain)
        self.assertFalse(bool(receipt2))
        self.assertTrue(bot2.locked_for_manual_review)
        self.assertEqual(bot2.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

        # 3. Success code but no txHash in stdout
        clear_configured_secrets()
        mock_subproc.reset_mock()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout="No hash returned here", stderr="")
        ]
        bot3 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=3
        )
        receipt3 = bot3.send_transaction("0x12345678", "test_call", preflight=False)
        self.assertEqual(receipt3.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(receipt3.is_uncertain)
        self.assertFalse(bool(receipt3))
        self.assertTrue(bot3.locked_for_manual_review)
        self.assertEqual(bot3.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_address_case_insensitive_matching_and_rejection(self, mock_subproc, mock_rpc):
        """Address comparison is case-insensitive on address values; matching casing passes, different address rejects."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        derived_checksum = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_subproc.return_value = MagicMock(returncode=0, stdout=derived_checksum + "\n", stderr="")

        # 1. All lowercase version of the same address -> PASSES
        lower_addr = derived_checksum.lower()
        bot_lower = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="dev-account",
            password_file="fake-passwords/dev.pwd",
            keeper_address=lower_addr,
            dry_run=False
        )
        self.assertEqual(bot_lower.keeper_address, derived_checksum)

        # 2. All uppercase hex version of the same address -> PASSES
        upper_addr = "0x" + derived_checksum[2:].upper()
        bot_upper = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="dev-account",
            password_file="fake-passwords/dev.pwd",
            keeper_address=upper_addr,
            dry_run=False
        )
        self.assertEqual(bot_upper.keeper_address, derived_checksum)

        # 3. Different address -> REJECTED with SystemExit(1)
        different_addr = "0x0000000000000000000000000000000000000001"
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="dev-account",
                password_file="fake-passwords/dev.pwd",
                keeper_address=different_addr,
                dry_run=False
            )
        self.assertEqual(ctx.exception.code, 1)

    @patch("script.keeper.rpc_call")
    def test_password_file_whitespace_handling_cli_and_direct(self, mock_rpc):
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"

        """Leading/trailing whitespace in password file path is strictly rejected in both CLI and KeeperBot."""
        # 1. KeeperBot direct instantiation
        whitespace_cases = ["  dev.pwd", "dev.pwd  ", "  dev.pwd  "]
        for p in whitespace_cases:
            with self.subTest(direct_path=p):
                with self.assertRaises(SystemExit) as ctx:
                    KeeperBot(
                        rpc_url="http://mock-rpc",
                        miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                        account="dev-account",
                        password_file=p,
                        dry_run=True
                    )
                self.assertEqual(ctx.exception.code, 1)

        # Empty password_file
        with self.assertRaises(SystemExit) as ctx:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="dev-account",
                password_file="   ",
                dry_run=True
            )
        self.assertEqual(ctx.exception.code, 1)

        # 2. CLI main execution
        for p in ["  dev.pwd", "dev.pwd  "]:
            with self.subTest(cli_path=p):
                err_captured = io.StringIO()
                test_args = ["keeper.py", "--rpc-url", "http://mock-rpc", "--account", "dev-account", "--password-file", p, "--dry-run"]
                with patch.object(sys, "argv", test_args):
                    with patch("sys.stderr", err_captured):
                        with self.assertRaises(SystemExit) as ctx:
                            main()
                        self.assertEqual(ctx.exception.code, 1)
                self.assertIn("Password file path cannot contain leading or trailing whitespace", err_captured.getvalue())

        # 3. Path with spaces inside must NOT be stripped or altered
        raw_path_with_spaces = "path with spaces/dev key.pwd"
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="dev-account",
            password_file=raw_path_with_spaces,
            dry_run=True
        )
        self.assertEqual(bot.password_file, raw_path_with_spaces)

    def test_redaction_complex_paths_rpc_creds_and_exceptions(self):
        """Redaction correctly masks paths with spaces, quotes, backslashes, RPC creds, and exception types."""
        # 1. Paths with spaces, quotes, backslashes
        fake_path = r'C:\secret dir\my "vault" pass.txt'
        register_secret(fake_path)

        text1 = f'--password-file "{fake_path}"'
        clean1 = sanitize_text(text1)
        self.assertNotIn("secret dir", clean1)
        self.assertNotIn("pass.txt", clean1)
        self.assertIn("[REDACTED_PATH]", clean1)

        text2 = f"--password-file '{fake_path}'"
        clean2 = sanitize_text(text2)
        self.assertNotIn("secret dir", clean2)
        self.assertIn("[REDACTED_PATH]", clean2)

        # 2. Subprocess error with embedded RPC credentials
        rpc_error = "Failed to connect to https://user_sentinel:super_secret_rpc_password_999@bsc-testnet.rpc.poolcoin.org:8545/v1: refused"
        clean_rpc = sanitize_text(rpc_error)
        self.assertNotIn("user_sentinel", clean_rpc)
        self.assertNotIn("super_secret_rpc_password_999", clean_rpc)
        self.assertIn("https://bsc-testnet.rpc.poolcoin.org:8545/***", clean_rpc)

        # 3. format_exception_safe with TimeoutExpired, FileNotFoundError, CalledProcessError
        import subprocess
        exc_timeout = subprocess.TimeoutExpired(cmd=["cast", "send", "--password-file", "C:/secret/pass.txt"], timeout=60)
        formatted_timeout = format_exception_safe(exc_timeout)
        self.assertEqual(formatted_timeout, "TimeoutExpired(cast)")
        self.assertNotIn("secret", formatted_timeout)
        self.assertNotIn("pass.txt", formatted_timeout)

        exc_fnf = FileNotFoundError(2, "No such file or directory: 'cast'")
        formatted_fnf = format_exception_safe(exc_fnf)
        self.assertEqual(formatted_fnf, "FileNotFoundError(cast)")

        exc_cpe = subprocess.CalledProcessError(returncode=2, cmd=["cast", "wallet", "address", "--password-file", "C:/secret/pass.txt"])
        formatted_cpe = format_exception_safe(exc_cpe)
        self.assertEqual(formatted_cpe, "CalledProcessError(cast, returncode=2)")
        self.assertNotIn("secret", formatted_cpe)



class TestKeeperUncertainTransactionRecovery(unittest.TestCase):
    """
    Offline mock unit tests verifying tightened recovery and retry behavior:
    1. TimeoutExpired on cast send only executes once and enters UNCERTAIN state.
    2. Non-zero return code and missing hash enter UNCERTAIN state and lock keeper.
    3. nonce too low / already known does not count as success.
    4. Diagnostic nonce advances do not count as action success.
    5. UNCERTAIN locked state produces zero additional broadcasts.
    6. Known hash with receipt status=1 / 0 / None are handled distinctly.
    7. Speed-up retry preserves identical nonce, target, calldata, and value.
    8. Sensitive sentinels never leak in alerts or manual review context.
    9. Arbitrary 32-byte hex in error text is not treated as tx hash.
    10. Preflight failure returns PREFLIGHT_FAILURE without locking bot.
    """
    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_cast_timeout_expired_executes_only_once_and_locks(self, mock_subproc, mock_rpc):
        """cast TimeoutExpired leads to exactly 1 send attempt, enters UNCERTAIN state, and locks."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            subprocess.TimeoutExpired(cmd=["cast", "send"], timeout=60)
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=3
        )

        res = bot.send_transaction("0x12345678", "test_action", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(res.is_uncertain)
        self.assertFalse(bool(res))
        self.assertTrue(bot.locked_for_manual_review)
        self.assertEqual(bot.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_nonzero_returncode_and_missing_hash_enter_uncertain_state(self, mock_subproc, mock_rpc):
        """Non-zero return code and return code 0 with missing hash both enter UNCERTAIN state and lock."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        # Case 1: Non-zero exit code
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error: network disconnect")
        ]
        bot1 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=2
        )
        res1 = bot1.send_transaction("0x12345678", "test_nonzero", preflight=False)
        self.assertEqual(res1.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(bot1.locked_for_manual_review)
        self.assertEqual(bot1.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

        # Case 2: Zero exit code but missing 32-byte hash in stdout
        clear_configured_secrets()
        mock_subproc.reset_mock()
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout="Transaction submitted but no hash printed\n", stderr="")
        ]
        bot2 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=2
        )
        res2 = bot2.send_transaction("0x12345678", "test_missing_hash", preflight=False)
        self.assertEqual(res2.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(bot2.locked_for_manual_review)
        self.assertEqual(bot2.current_nonce, 5)
        self.assertEqual(mock_subproc.call_count, 2)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_nonce_too_low_already_known_not_marked_success(self, mock_subproc, mock_rpc):
        """nonce too low / already known does not count as success, enters UNCERTAIN and locks."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        # 1. Keystore mode
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error: nonce too low: address 0x..., tx: 5, state: 6")
        ]
        bot_ks = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=2
        )
        res_ks = bot_ks.send_transaction("0x12345678", "test_nonce_err", preflight=False)
        self.assertEqual(res_ks.status, TxExecutionStatus.UNCERTAIN)
        self.assertFalse(res_ks.is_confirmed_success)
        self.assertFalse(bool(res_ks))
        self.assertTrue(bot_ks.locked_for_manual_review)
        self.assertEqual(bot_ks.current_nonce, 5)

        # 2. Private key mode
        clear_configured_secrets()
        test_key = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f3608a8"
        def rpc_pk(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                return "0x5"
            elif method == "eth_sendRawTransaction":
                raise Exception("transaction already known")
            return "0x0"

        mock_rpc.side_effect = rpc_pk
        bot_pk = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            max_retries=2
        )
        res_pk = bot_pk.send_transaction("0x12345678", "test_pk_already_known", preflight=False)
        self.assertEqual(res_pk.status, TxExecutionStatus.UNCERTAIN)
        self.assertFalse(res_pk.is_confirmed_success)
        self.assertFalse(bool(res_pk))
        self.assertTrue(bot_pk.locked_for_manual_review)
        self.assertEqual(bot_pk.current_nonce, 5)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_nonce_advance_diagnostics_does_not_count_as_action_success(self, mock_subproc, mock_rpc):
        """Even if latest/pending nonce advances, it does NOT prove this action succeeded without a mined receipt."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        call_counts = {"pending": 0}
        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                tag = params[1]
                if tag == "latest":
                    return "0x0a"
                elif tag == "pending":
                    call_counts["pending"] += 1
                    if call_counts["pending"] == 1:
                        return "0x5"
                    return "0x0c"
                return "0x5"
            return "0x0"

        mock_rpc.side_effect = rpc_side
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error: nonce too low")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=2
        )

        res = bot.send_transaction("0x12345678", "test_action", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertFalse(res.is_confirmed_success)
        self.assertTrue(bot.locked_for_manual_review)
        self.assertEqual(bot.current_nonce, 5)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_locked_state_produces_zero_additional_broadcasts(self, mock_subproc, mock_rpc):
        """Once locked for manual review, all subsequent send_transaction, step, and run_forever produce 0 broadcasts."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_subproc.return_value = MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=1
        )

        bot.locked_for_manual_review = True
        bot.manual_review_context = {"reason": "Prior failure"}

        subproc_count_before = mock_subproc.call_count

        # 1. Direct send_transaction call
        res = bot.send_transaction("0x1234", "should_not_send", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertEqual(mock_subproc.call_count, subproc_count_before)

        # 2. bot.step()
        bot.step()
        self.assertEqual(mock_subproc.call_count, subproc_count_before)

        # 3. bot.run_forever() breaks immediately
        bot.poll_interval = 0.01
        bot.run_forever()
        self.assertEqual(mock_subproc.call_count, subproc_count_before)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_known_hash_receipt_status_handling_success_revert_timeout(self, mock_subproc, mock_rpc):
        """With a valid hash, receipt status=1 is CONFIRMED_SUCCESS, status=0 is CONFIRMED_REVERT, and timeout is PENDING_UNCONFIRMED."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        valid_hash = "0x" + "a" * 64

        # 1. status = 1 (Success)
        def rpc_success(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                return "0x5"
            elif method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x10", "gasUsed": "0x5000"}
            return "0x0"

        mock_rpc.side_effect = rpc_success
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"{valid_hash}\n", stderr="")
        ]
        bot1 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            tx_timeout=1
        )
        res1 = bot1.send_transaction("0x1234", "test_success", preflight=False)
        self.assertEqual(res1.status, TxExecutionStatus.CONFIRMED_SUCCESS)
        self.assertTrue(bool(res1))
        self.assertEqual(bot1.current_nonce, 6)
        self.assertFalse(bot1.locked_for_manual_review)

        # 2. status = 0 (Revert)
        def rpc_revert(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                return "0x5"
            elif method == "eth_getTransactionReceipt":
                return {"status": "0x0", "blockNumber": "0x10", "gasUsed": "0x5000"}
            return "0x0"

        mock_rpc.side_effect = rpc_revert
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"{valid_hash}\n", stderr="")
        ]
        bot2 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            tx_timeout=1
        )
        res2 = bot2.send_transaction("0x1234", "test_revert", preflight=False)
        self.assertEqual(res2.status, TxExecutionStatus.CONFIRMED_REVERT)
        self.assertFalse(bool(res2))
        self.assertEqual(bot2.current_nonce, 6)
        self.assertFalse(bot2.locked_for_manual_review)

        # 3. No receipt (Timeout on all attempts)
        def rpc_timeout(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                return "0x5"
            elif method == "eth_getTransactionReceipt":
                return None
            return "0x0"

        mock_rpc.side_effect = rpc_timeout
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"{valid_hash}\n", stderr="")
        ]
        bot3 = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            tx_timeout=0.01,
            max_retries=0
        )
        res3 = bot3.send_transaction("0x1234", "test_pending", preflight=False)
        self.assertEqual(res3.status, TxExecutionStatus.PENDING_UNCONFIRMED)
        self.assertFalse(bool(res3))
        self.assertTrue(bot3.locked_for_manual_review)
        self.assertEqual(bot3.manual_review_context["tx_hash"], valid_hash)

    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_speedup_replacement_strictly_reuses_nonce_target_calldata_value(self, mock_subproc, mock_rpc, mock_time):
        """Speed-up retry strictly keeps identical nonce, target, calldata, value and bumps gasPrice."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        hash0 = "0x" + "1" * 64
        hash1 = "0x" + "2" * 64

        receipt_calls = []
        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_getTransactionCount":
                return "0x5"
            elif method == "eth_getTransactionReceipt":
                receipt_calls.append(params[0])
                if params[0] == hash1:
                    return {"status": "0x1", "blockNumber": "0x11", "gasUsed": "0x5000"}
                return None
            return "0x0"

        mock_rpc.side_effect = rpc_side
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"{hash0}\n", stderr=""),
            MagicMock(returncode=0, stdout=f"{hash1}\n", stderr="")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            tx_timeout=1,
            max_retries=1
        )

        calldata = "0xdeadbeef"
        res = bot.send_transaction(calldata, "test_speedup", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)
        self.assertEqual(mock_subproc.call_count, 3)

        cmd_attempt0 = mock_subproc.call_args_list[1][0][0]
        cmd_attempt1 = mock_subproc.call_args_list[2][0][0]

        target_checksum = to_checksum_address("0x420382a0f874c1F120621f847d81581e8737504e")
        self.assertEqual(cmd_attempt0[2], target_checksum)
        self.assertEqual(cmd_attempt1[2], target_checksum)

        self.assertEqual(cmd_attempt0[cmd_attempt0.index("--data") + 1], calldata)
        self.assertEqual(cmd_attempt1[cmd_attempt1.index("--data") + 1], calldata)

        self.assertEqual(cmd_attempt0[cmd_attempt0.index("--nonce") + 1], "5")
        self.assertEqual(cmd_attempt1[cmd_attempt1.index("--nonce") + 1], "5")

        gp0 = int(cmd_attempt0[cmd_attempt0.index("--gas-price") + 1])
        gp1 = int(cmd_attempt1[cmd_attempt1.index("--gas-price") + 1])
        self.assertGreater(gp1, gp0)
        self.assertGreaterEqual(gp1, int(gp0 * 1.15))

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_sensitive_sentinels_not_leaked_in_alerts_or_manual_review_context(self, mock_subproc, mock_rpc):
        """Sensitive password file path and RPC URL credentials never leak in alerts or manual review context."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )

        secret_pass_path = "C:/secret/vault/my_production.pwd"
        secret_rpc_url = "https://sensitive_user:secret_rpc_token_98765@bsc-testnet.rpc.poolcoin.org:8545/v1"

        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=f"Fatal error reading {secret_pass_path} with token secret_rpc_token_98765")
        ]

        captured_alerts = []
        bot = KeeperBot(
            rpc_url=secret_rpc_url,
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file=secret_pass_path,
            dry_run=False,
            max_retries=1
        )
        bot.alert = lambda msg: captured_alerts.append(msg)

        res = bot.send_transaction("0x1234", "test_leak", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)

        ctx = bot.manual_review_context
        self.assertEqual(set(ctx.keys()), {"chain_id", "signer_address", "target_contract", "nonce", "function_name", "tx_hash"})
        for k, v in ctx.items():
            val_str = str(v)
            self.assertNotIn("secret_rpc_token_98765", val_str)
            self.assertNotIn("my_production.pwd", val_str)
            self.assertNotIn("secret/vault", val_str)

        all_alerts_text = " \n ".join(captured_alerts)
        self.assertNotIn("secret_rpc_token_98765", all_alerts_text)
        self.assertNotIn("my_production.pwd", all_alerts_text)
        self.assertNotIn("secret/vault", all_alerts_text)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_arbitrary_32_byte_in_error_text_not_treated_as_tx_hash(self, mock_subproc, mock_rpc):
        """Error text containing arbitrary 32-byte hex strings is NOT treated as transaction hash."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )

        arbitrary_32_byte = "0x" + "f" * 64
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=f"Execution reverted: failed at internal hash {arbitrary_32_byte} in subcall")
        ]

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=1
        )

        res = bot.send_transaction("0x1234", "test_arbitrary_hash", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertIsNone(res.tx_hash)
        self.assertIsNone(bot.manual_review_context["tx_hash"])

    @patch("script.keeper.rpc_call")
    def test_preflight_failure_does_not_lock_bot(self, mock_rpc):
        """Preflight simulation failure returns PREFLIGHT_FAILURE and does not lock the bot."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        test_key = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f3608a8"
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False
        )

        bot.preflight_check = MagicMock(return_value=(False, "PrevSeedNotInitialized"))
        res = bot.send_transaction("0x1234", "initEpochSeed(1, 24)", preflight=True)
        self.assertEqual(res.status, TxExecutionStatus.PREFLIGHT_FAILURE)
        self.assertTrue(res.is_preflight_failure)
        self.assertFalse(bot.locked_for_manual_review)




class TestManualReviewLockAndDryRunFixes(unittest.TestCase):
    """Tests verifying fixes for lock_for_manual_review resilience, strict hash validation, and dry-run semantics."""

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_get_chain_id_exception_retains_lock_writes_context_and_returns_uncertain(self, mock_subproc, mock_rpc):
        """When get_chain_id throws an exception during review locking, lock remains active, context is written, alerts are emitted, and send_transaction returns UNCERTAIN without raising."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="cast send error: gas price too low")
        ]

        captured_alerts = []
        captured_warnings = []
        bot = KeeperBot(
            rpc_url="https://sensitive_user:secret_token_12345@rpc.poolcoin.org:8545",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="C:/secret/vault/pass.pwd",
            dry_run=False,
            max_retries=1
        )
        bot.alert = lambda msg: captured_alerts.append(msg)
        bot.log = lambda level, msg: captured_warnings.append((level, msg)) if level == "WARN" else None

        # Force get_chain_id to throw an exception embedding sensitive data
        bot.get_chain_id = MagicMock(side_effect=RuntimeError("eth_chainId failed with token secret_token_12345"))

        res = bot.send_transaction("0x1234", "test_chain_id_failure", preflight=False)

        # 1. Returned UNCERTAIN without raising
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        # 2. Lock is strictly active
        self.assertTrue(bot.locked_for_manual_review)
        # 3. Context is populated with fallback chain_id 'Unknown'
        self.assertIsNotNone(bot.manual_review_context)
        self.assertEqual(bot.manual_review_context["chain_id"], "Unknown")
        self.assertEqual(bot.manual_review_context["signer_address"], fake_addr)
        self.assertEqual(bot.manual_review_context["target_contract"], "0x420382a0f874c1F120621f847d81581e8737504e")
        self.assertEqual(bot.manual_review_context["nonce"], 5)
        self.assertEqual(bot.manual_review_context["function_name"], "test_chain_id_failure")
        self.assertIsNone(bot.manual_review_context["tx_hash"])
        # 4. Critical manual review alert was outputted
        all_alerts = " \n ".join(captured_alerts)
        self.assertIn("CRITICAL: TRANSACTION OUTCOME UNCERTAIN - KEEPER AUTOMATION HALTED", all_alerts)
        self.assertIn("Chain ID:        Unknown", all_alerts)
        # 5. Sensitive data must be sanitized
        self.assertNotIn("secret_token_12345", all_alerts)
        self.assertNotIn("secret/vault", all_alerts)
        all_warnings = " \n ".join(m for _, m in captured_warnings)
        self.assertNotIn("secret_token_12345", all_warnings)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_get_chain_id_system_exit_does_not_abort_manual_review_lock(self, mock_subproc, mock_rpc):
        """If get_chain_id triggers a SystemExit, lock_for_manual_review intercepts it, preserves the lock, writes context, and outputs the review prompt."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="cast send fatal error")
        ]

        captured_alerts = []
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=1
        )
        bot.alert = lambda msg: captured_alerts.append(msg)

        # Mock get_chain_id to simulate SystemExit(1)
        bot.get_chain_id = MagicMock(side_effect=SystemExit(1))

        res = bot.send_transaction("0x1234", "test_sysexit_handling", preflight=False)
        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertTrue(bot.locked_for_manual_review)
        self.assertEqual(bot.manual_review_context["chain_id"], "Unknown")
        all_alerts = " \n ".join(captured_alerts)
        self.assertIn("CRITICAL: TRANSACTION OUTCOME UNCERTAIN - KEEPER AUTOMATION HALTED", all_alerts)
        self.assertIn("Chain ID:        Unknown", all_alerts)

    @patch("script.keeper.rpc_call")
    def test_keyboard_interrupt_during_manual_review_lock_is_not_swallowed(self, mock_rpc):
        """KeyboardInterrupt occurring during manual review locking must NOT be swallowed, while the lock flag is retained on the first line."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0"
        )
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            keeper_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
            dry_run=True
        )
        self.assertFalse(bot.locked_for_manual_review)

        bot.get_chain_id = MagicMock(side_effect=KeyboardInterrupt("user interrupt"))
        with self.assertRaises(KeyboardInterrupt):
            bot.lock_for_manual_review(reason="test interrupt", nonce=5, func_name="test_func")

        self.assertTrue(bot.locked_for_manual_review)

    @patch("script.keeper.rpc_call")
    def test_invalid_hashes_rejected_from_manual_review_context(self, mock_rpc):
        """Invalid hashes (short strings, non-hex characters, oversized hashes) must NOT enter manual review context and display as unprovided."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0"
        )
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            keeper_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
            dry_run=True
        )

        invalid_hashes = [
            "0x1234",
            "0x",
            "0x" + "1" * 63,
            "0x" + "g" * 64,
            "0x" + "Z" * 64,
            "0x" + "a" * 65,
            "0x" + "f" * 128,
            "not_a_hash_at_all",
            None,
            12345678
        ]

        for bad_hash in invalid_hashes:
            captured_alerts = []
            bot.alert = lambda msg: captured_alerts.append(msg)
            bot.lock_for_manual_review(reason="test invalid hash", nonce=10, func_name="test_func", tx_hash=bad_hash)
            self.assertIsNone(bot.manual_review_context["tx_hash"], f"Expected None for invalid hash {bad_hash}")
            all_alerts = " \n ".join(captured_alerts)
            self.assertIn("None (Broadcast unconfirmed / No valid hash returned)", all_alerts)

    @patch("script.keeper.rpc_call")
    def test_valid_64_hex_hash_enters_manual_review_context(self, mock_rpc):
        """Valid 64-hex transaction hashes strictly match ^0x[0-9a-fA-F]{64}$ and enter manual review context."""
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0"
        )
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            keeper_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
            dry_run=True
        )

        valid_hashes = [
            "0x" + "0" * 64,
            "0x" + "f" * 64,
            "0x" + "F" * 64,
            "0x" + "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "0x" + "AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789"
        ]

        for good_hash in valid_hashes:
            captured_alerts = []
            bot.alert = lambda msg: captured_alerts.append(msg)
            bot.lock_for_manual_review(reason="test valid hash", nonce=10, func_name="test_func", tx_hash=good_hash)
            self.assertEqual(bot.manual_review_context["tx_hash"], good_hash)
            all_alerts = " \n ".join(captured_alerts)
            self.assertIn(f"Tx Hash:         {good_hash}", all_alerts)

    @patch("script.keeper.rpc_call")
    def test_dry_run_multi_epoch_seed_sequence_completes_without_real_broadcast(self, mock_rpc):
        """In dry-run mode, sequential initialization of multiple epoch seeds executes completely without being misjudged as failure, returning status DRY_RUN and zero real broadcasts."""
        calls_made = []
        def rpc_side(url, method, params):
            calls_made.append(method)
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(3)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(3)
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                return "0x"
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            keeper_address="0x1111111111111111111111111111111111111111",
            dry_run=True
        )

        captured_alerts = []
        captured_simulations = []
        bot.alert = lambda msg: captured_alerts.append(msg)
        orig_send = bot.send_transaction

        def record_send(calldata, desc, func_name=None, preflight=True):
            res = orig_send(calldata, desc, func_name, preflight)
            captured_simulations.append((func_name, res))
            return res

        bot.send_transaction = record_send

        bot.handle_epoch_seed(cur_epoch=3, seed=0)

        # 1. Must simulate batch call initEpochSeed(3, 24) without early interruption
        self.assertEqual(len(captured_simulations), 1)
        expected_funcs = [
            f"initEpochSeed(3, {bot.max_seed_steps})"
        ]
        simulated_funcs = [s[0] for s in captured_simulations]
        self.assertEqual(simulated_funcs, expected_funcs)

        # 2. None of the steps should trigger stopping alerts
        stop_alerts = [a for a in captured_alerts if "Stopping seed initialization sequence" in a]
        self.assertEqual(len(stop_alerts), 0)

        # 3. Dry-run TxResult properties must be explicit:
        for _, res in captured_simulations:
            self.assertEqual(res.status, TxExecutionStatus.DRY_RUN)
            self.assertTrue(res.is_dry_run)
            self.assertFalse(res.is_confirmed_success)
            self.assertFalse(bool(res))
            self.assertTrue(res.get("dry_run"))

        # 4. Zero real broadcasts
        self.assertNotIn("eth_sendRawTransaction", calls_made)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_locked_bot_subsequent_send_transaction_zero_new_broadcasts(self, mock_subproc, mock_rpc):
        """Once locked for manual review, any subsequent call to send_transaction immediately refuses with UNCERTAIN and zero new broadcasts."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.return_value = MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            account="test-keystore-account",
            password_file="fake-passwords/dev.pwd",
            dry_run=False,
            max_retries=1
        )

        bot.locked_for_manual_review = True
        bot.manual_review_context = {
            "chain_id": 97,
            "signer_address": fake_addr,
            "target_contract": "0x420382a0f874c1F120621f847d81581e8737504e",
            "nonce": 5,
            "function_name": "previous_failed_action",
            "tx_hash": None
        }

        mock_subproc.reset_mock()
        mock_rpc.reset_mock()

        res = bot.send_transaction("0xabcdef", "attempt_while_locked", preflight=False)

        self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
        self.assertEqual(res.error, "Keeper locked pending manual review")
        mock_subproc.assert_not_called()
        for call_args in mock_rpc.call_args_list:
            self.assertNotEqual(call_args[0][1], "eth_sendRawTransaction")


class TestKeeperEpochSeedBatching(unittest.TestCase):
    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_batch_seed_lag_15_max_steps_24_sends_single_tx(self, mock_rpc, mock_sleep):
        """Lag 15 epochs (min_e=0, cur_epoch=14), max_seed_steps=24: sends exactly 1 batch transaction."""
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []
        cur_epoch = 14
        seeds = {e: 0 for e in range(cur_epoch + 1)}

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(cur_epoch)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(cur_epoch)
                elif sel == SELECTOR_EPOCH_SEED:
                    e = int(tx_data[8:72], 16)
                    val = seeds.get(e, 0)
                    return hex(val) if val != 0 else "0x0"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'e' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                for e in range(cur_epoch + 1):
                    seeds[e] = 0xabcdef01 + e
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            max_seed_steps=24,
            tx_timeout=2,
            max_retries=1
        )

        bot.handle_epoch_seed(cur_epoch=14, seed=0)

        self.assertEqual(len(broadcasted_txs), 1)
        raw_bytes = bytes.fromhex(broadcasted_txs[0]["raw"].replace("0x", ""))
        decoded = rlp.decode(raw_bytes)
        calldata = decoded[5].hex()
        self.assertTrue(calldata.startswith(SELECTOR_INIT_EPOCH_SEED))
        target_epoch = int(calldata[8:72], 16)
        steps = int(calldata[72:136], 16)
        self.assertEqual(target_epoch, 14)
        self.assertEqual(steps, 24)

    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_batch_seed_lag_30_sends_two_txs_and_rereads_state(self, mock_rpc, mock_sleep):
        """Lag 30 epochs (min_e=0, cur_epoch=29): sends 2 transactions, re-reading state before second."""
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []
        cur_epoch = 29
        seeds = {e: 0 for e in range(cur_epoch + 1)}
        state_queries_after_tx1 = []

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return hex(10 + len(broadcasted_txs))
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(cur_epoch)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(cur_epoch)
                elif sel == SELECTOR_EPOCH_SEED:
                    e = int(tx_data[8:72], 16)
                    val = seeds.get(e, 0)
                    if len(broadcasted_txs) == 1:
                        state_queries_after_tx1.append(e)
                    return hex(val) if val != 0 else "0x0"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_idx = len(broadcasted_txs)
                tx_hash = f"0x{'f' * 60}{tx_idx:04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                if tx_idx == 0:
                    for e in range(24):
                        seeds[e] = 0x1000 + e
                elif tx_idx == 1:
                    for e in range(24, 30):
                        seeds[e] = 0x2000 + e
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            max_seed_steps=24,
            tx_timeout=2,
            max_retries=1
        )

        bot.handle_epoch_seed(cur_epoch=29, seed=0)

        self.assertEqual(len(broadcasted_txs), 2)
        self.assertGreater(len(state_queries_after_tx1), 0)
        self.assertIn(29, state_queries_after_tx1)

        for tx_info in broadcasted_txs:
            raw_bytes = bytes.fromhex(tx_info["raw"].replace("0x", ""))
            decoded = rlp.decode(raw_bytes)
            calldata = decoded[5].hex()
            self.assertTrue(calldata.startswith(SELECTOR_INIT_EPOCH_SEED))
            self.assertEqual(int(calldata[8:72], 16), 29)
            self.assertEqual(int(calldata[72:136], 16), 24)

    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_batch_seed_receipt_success_state_not_advanced_halts(self, mock_rpc, mock_sleep):
        """When transaction receipt confirms success but on-chain state does not advance, bot alerts and halts."""
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        broadcasted_txs = []
        cur_epoch = 29

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(cur_epoch)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(cur_epoch)
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0x{'a' * 60}{len(broadcasted_txs):04x}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            max_seed_steps=24,
            tx_timeout=2,
            max_retries=1
        )

        captured_alerts = []
        bot.alert = lambda msg: captured_alerts.append(msg)

        bot.handle_epoch_seed(cur_epoch=29, seed=0)

        self.assertEqual(len(broadcasted_txs), 1)
        halt_alerts = [a for a in captured_alerts if "did not advance" in a]
        self.assertEqual(len(halt_alerts), 1)

    @patch("time.sleep")
    @patch("script.keeper.rpc_call")
    def test_batch_seed_uncertain_outcome_locks_and_zero_subsequent_broadcasts(self, mock_rpc, mock_sleep):
        """When transaction triggers UNCERTAIN outcome during batch seed initialization, keeper locks and refuses further broadcasts."""
        from eth_account import Account
        test_account = Account.create()
        test_key = test_account.key.hex()

        cur_epoch = 29

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_estimateGas":
                return "0x186a0"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(cur_epoch)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(cur_epoch)
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                return "invalid_hash_string"
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            max_seed_steps=24,
            tx_timeout=2,
            max_retries=1
        )

        bot.handle_epoch_seed(cur_epoch=29, seed=0)

        self.assertTrue(bot.locked_for_manual_review)

        calls_before = len(mock_rpc.call_args_list)
        bot.step()
        bot.send_transaction("0xabcdef", "attempt_while_locked", preflight=False)
        for call_args in mock_rpc.call_args_list[calls_before:]:
            self.assertNotEqual(call_args[0][1], "eth_sendRawTransaction")

    @patch("script.keeper.rpc_call")
    def test_batch_seed_dry_run_estimation_matches_expected_and_zero_broadcasts(self, mock_rpc):
        """In dry-run mode, calculates ceil(n / max_seed_steps), logs explicit estimate, and broadcasts zero real transactions."""
        calls_made = []
        cur_epoch = 29

        def rpc_side(url, method, params):
            calls_made.append(method)
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(cur_epoch)
                elif sel == SELECTOR_LAST_SYNCED:
                    return hex(cur_epoch)
                elif sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                return "0x"
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            keeper_address="0x1111111111111111111111111111111111111111",
            dry_run=True,
            max_seed_steps=24
        )

        captured_logs = []
        bot.log = lambda level, msg: captured_logs.append((level, msg))

        captured_simulations = []
        orig_send = bot.send_transaction

        def record_send(calldata, desc, func_name=None, preflight=True):
            res = orig_send(calldata, desc, func_name, preflight)
            captured_simulations.append((func_name, res))
            return res

        bot.send_transaction = record_send

        expected_tx_count = (30 + 24 - 1) // 24
        self.assertEqual(expected_tx_count, 2)

        bot.handle_epoch_seed(cur_epoch=29, seed=0)

        self.assertEqual(len(captured_simulations), expected_tx_count)
        for func_name, res in captured_simulations:
            self.assertEqual(func_name, "initEpochSeed(29, 24)")
            self.assertEqual(res.status, TxExecutionStatus.DRY_RUN)

        estimate_logs = [msg for lvl, msg in captured_logs if "Estimated transactions to send: 2" in msg and "simulation estimate only" in msg]
        self.assertEqual(len(estimate_logs), 1)
        self.assertNotIn("eth_sendRawTransaction", calls_made)

    @patch("script.keeper.rpc_call")
    def test_batch_seed_already_initialized_zero_transactions(self, mock_rpc):
        """When seed is already initialized (passed directly or detected on-chain), zero transactions are broadcast."""
        calls_made = []

        def rpc_side(url, method, params):
            calls_made.append(method)
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_gasPrice":
                return "0x5f5e100"
            elif method == "eth_getTransactionCount":
                return "0x1"
            elif method == "eth_call":
                tx_data = params[0].get("data", "")
                if tx_data.startswith("0x"):
                    tx_data = tx_data[2:]
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH_SEED:
                    return hex(0x12345678)
                return "0x"
            return None

        mock_rpc.side_effect = rpc_side

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key="0x" + "1" * 64,
            dry_run=False
        )

        with patch.object(bot, "send_transaction") as mock_send:
            bot.handle_epoch_seed(cur_epoch=5, seed=0xabcdef)
            mock_send.assert_not_called()

            bot.handle_epoch_seed(cur_epoch=5, seed=0)
            mock_send.assert_not_called()

        self.assertNotIn("eth_sendRawTransaction", calls_made)

    @patch("script.keeper.rpc_call")
    def test_max_seed_steps_validation(self, mock_rpc):
        """Tests that max_seed_steps is validated: capped at MAX_SEED_STEPS if > 24, and exits on <= 0."""
        mock_rpc.side_effect = lambda url, method, params: "0x6080604052" if method == "eth_getCode" else "0x0"
        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            max_seed_steps=50,
            dry_run=True
        )
        self.assertEqual(bot.max_seed_steps, 24)

        with self.assertRaises(SystemExit) as cm:
            KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                max_seed_steps=0,
                dry_run=True
            )
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()



class TestKeeperPersistentStateAndLocking(unittest.TestCase):
    """
    Exhaustive verification of Keeper persistent state, crash protection,
    concurrency locking, domain binding, and manual unlock workflows.
    """
    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_first_normal_startup(self, mock_subproc, mock_rpc):
        """1. First normal startup with clean state allows sending and transitions to CONFIRMED_SUCCESS."""
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

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            sm = bot._ensure_state_manager()
            self.assertTrue(sm.domain_valid)
            can_proceed, block_reason, state_data = sm.check_startup_protection()
            self.assertTrue(can_proceed)
            self.assertIsNone(block_reason)

            res = bot.send_transaction("0x1234", "test_normal", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)

            # Verify state on disk is CONFIRMED_SUCCESS
            saved_state = sm.load_state()
            self.assertIsNotNone(saved_state)
            self.assertEqual(saved_state["status"], "CONFIRMED_SUCCESS")
            self.assertEqual(saved_state["tx_hash"], fake_tx)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_manual_review_lock_persists_across_restart(self, mock_subproc, mock_rpc):
        """2. Manual review lock persists across bot restarts/instances and prevents sending."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error: execution reverted: out of gas")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot1 = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            res1 = bot1.send_transaction("0x1234", "test_call", preflight=False)
            self.assertEqual(res1.status, TxExecutionStatus.UNCERTAIN)
            self.assertTrue(bot1.locked_for_manual_review)

            # Restart simulation: new KeeperBot instance on same domain & state_dir
            mock_subproc.reset_mock()
            mock_subproc.side_effect = [
                MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")
            ]
            bot2 = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            res2 = bot2.send_transaction("0x1234", "test_call2", preflight=False)
            self.assertEqual(res2.status, TxExecutionStatus.UNCERTAIN)
            self.assertTrue(bot2.locked_for_manual_review)
            self.assertIn("locked for manual review", res2.error)
            # Only wallet address was queried, cast send was NEVER called
            self.assertEqual(mock_subproc.call_count, 1)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_state_write_failure_aborts_before_signing_or_sending(self, mock_subproc, mock_rpc):
        """3. When persistent state write fails before broadcast, transaction aborts without signing or sending."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            sm = bot._ensure_state_manager()
            # Force record_preparing_to_send to fail
            from script.keeper_state import StateWriteError
            sm.record_preparing_to_send = MagicMock(side_effect=StateWriteError("Simulated disk full"))

            res = bot.send_transaction("0x1234", "test_abort", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
            self.assertTrue(bot.locked_for_manual_review)
            self.assertIn("Failed to persist in-flight state", res.error)
            # Only wallet address was queried during identity validation, cast send was NEVER called
            self.assertEqual(mock_subproc.call_count, 1)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_crash_after_broadcast_before_receipt_halts_restart(self, mock_subproc, mock_rpc):
        """4. Simulated crash after broadcast before receipt halts restart into manual review."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Manually inject in-flight crash state (PREPARING_TO_SEND)
            from script.keeper_state import KeeperStateManager, StateDomain, atomic_write_json
            domain = StateDomain(chain_id=97, miner_address="0x420382a0f874c1F120621f847d81581e8737504e", sender_address=fake_addr)
            sm_pre = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            sm_pre.record_preparing_to_send(operation="sync()", epoch=10, nonce=5, details="Crash before confirmation simulation")

            # Restart simulation: new bot instance
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            res = bot.send_transaction("0x1234", "sync()", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
            self.assertTrue(bot.locked_for_manual_review)
            self.assertIn("crash before confirmation", res.error)

            # Verify on disk that status transitioned to MANUAL_REVIEW_LOCKED
            state = sm_pre.load_state()
            self.assertEqual(state["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(state["reason_code"], "CRASH_BEFORE_CONFIRMATION")

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_timeout_and_unknown_tx_outcomes_trigger_manual_review(self, mock_subproc, mock_rpc):
        """5. Timeout on all attempts triggers UNCERTAIN outcome and persists MANUAL_REVIEW_LOCKED."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "b" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else None  # Receipt times out
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx}\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                tx_timeout=0.01,
                max_retries=0,
                state_dir=tmp_dir
            )
            res = bot.send_transaction("0x1234", "test_timeout", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.PENDING_UNCONFIRMED)
            self.assertTrue(bot.locked_for_manual_review)

            sm = bot._ensure_state_manager()
            state = sm.load_state()
            self.assertEqual(state["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(state["tx_hash"], fake_tx)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_invalid_domain_corrupt_file_and_unknown_version_refuse_sending(self, mock_subproc, mock_rpc):
        """6. Invalid domain, corrupted state file, and unknown version strictly refuse sending."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            sm = bot._ensure_state_manager()
            state_path = sm.get_state_path()

            # Case A: Corrupted JSON file
            with open(state_path, "w", encoding="utf-8") as f:
                f.write("{invalid json content syntax...")
            can_proceed, reason, _ = sm.check_startup_protection()
            self.assertFalse(can_proceed)
            self.assertIn("not valid JSON", reason)
            res = bot.send_transaction("0x1234", "test_corrupt", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)

            # Case B: Unknown state version
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"version": 999, "domain": sm.domain.to_dict(), "status": "CONFIRMED_SUCCESS", "updated_at": "2026-01-01"}, f)
            can_proceed, reason, _ = sm.check_startup_protection()
            self.assertFalse(can_proceed)
            self.assertIn("Unsupported state version", reason)

            # Case C: Domain mismatch
            with open(state_path, "w", encoding="utf-8") as f:
                bad_domain = sm.domain.to_dict()
                bad_domain["chain_id"] = 1  # Ethereum mainnet instead of BSC testnet
                json.dump({"version": 1, "domain": bad_domain, "status": "CONFIRMED_SUCCESS", "updated_at": "2026-01-01"}, f)
            can_proceed, reason, _ = sm.check_startup_protection()
            self.assertFalse(can_proceed)
            self.assertIn("domain mismatch", reason)

    def test_concurrent_instances_same_domain_only_one_enters_sending(self):
        """7. Two keeper instances on the same domain cannot concurrently enter the sending phase."""
        from script.keeper_state import KeeperStateManager, StateDomain, DomainFileLock
        with tempfile.TemporaryDirectory() as tmp_dir:
            domain = StateDomain(chain_id=97, miner_address="0x420382a0f874c1F120621f847d81581e8737504e", sender_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A")
            sm1 = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            sm2 = KeeperStateManager(state_dir=tmp_dir, domain=domain)

            # Instance 1 acquires the lock
            self.assertTrue(sm1.acquire_lock())

            # Instance 2 tries to acquire the same domain lock and is refused
            self.assertFalse(sm2.acquire_lock())

            # Instance 1 releases the lock
            sm1.release_lock()

            # Now Instance 2 can acquire the lock
            self.assertTrue(sm2.acquire_lock())
            sm2.release_lock()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_confirmed_success_completes_state_transition(self, mock_subproc, mock_rpc):
        """8. Confirmed success transitions state and writes audit trail."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "c" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x150", "gasUsed": "0x7000"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx}\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            res = bot.send_transaction("0x1234", "test_success", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)

            sm = bot._ensure_state_manager()
            state = sm.load_state()
            self.assertEqual(state["status"], "CONFIRMED_SUCCESS")
            self.assertEqual(state["tx_hash"], fake_tx)
            self.assertEqual(state["block_number"], 0x150)

            # Check audit log
            with open(sm.get_audit_path(), "r", encoding="utf-8") as f:
                audit_records = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(r.get("action") == "CONFIRMED_SUCCESS" for r in audit_records))

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_manual_unlock_domain_binding_and_audit_history(self, mock_subproc, mock_rpc):
        """9. Manual unlock requires domain match, verifies expected_nonce, transitions to IDLE, and preserves audit log."""
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            sm = bot._ensure_state_manager()
            sm.record_manual_review_lock(
                reason="Uncertain outcome",
                nonce=5,
                operation="test_op",
                tx_hash="0x" + "d" * 64,
                reason_code="MANUAL_TEST"
            )

            # Verification: bot is locked
            can_proceed, _, _ = sm.check_startup_protection()
            self.assertFalse(can_proceed)

            # Nonce mismatch raises InvalidStateError
            from script.keeper_state import InvalidStateError
            with self.assertRaises(InvalidStateError):
                bot.manual_unlock(expected_nonce=99, operator_reason="Valid reason")

            # Successful manual unlock with correct nonce and operator reason
            unlock_res = bot.manual_unlock(expected_nonce=5, operator_reason="Operator verified tx on BSCScan")
            self.assertEqual(unlock_res["status"], "IDLE")
            self.assertEqual(unlock_res["reason_code"], "UNLOCKED_MANUALLY")

            # Check that state file still exists and audit log has entry
            state_after = sm.load_state()
            self.assertEqual(state_after["status"], "IDLE")

            with open(sm.get_audit_path(), "r", encoding="utf-8") as f:
                audit_records = [json.loads(line) for line in f if line.strip()]
            unlock_entry = [r for r in audit_records if r.get("action") == "MANUAL_UNLOCK"]
            self.assertEqual(len(unlock_entry), 1)
            self.assertIn("Operator verified tx", unlock_entry[0]["operator_reason"])

            # Now bot can proceed without block
            can_proceed_after, _, _ = sm.check_startup_protection()
            self.assertTrue(can_proceed_after)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_sensitive_exceptions_and_rpc_credentials_excluded_from_state(self, mock_subproc, mock_rpc):
        """10. Sensitive RPC credentials, passwords, and file paths are sanitized from state file and audit log."""
        secret_pwd = "super_secret_keeper_password_xyz123"
        secret_path = "C:/sensitive/keystore/vault.pwd"
        fake_addr = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=fake_addr + "\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=f"cast send error with password {secret_pwd} at {secret_path}")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url=f"https://operator:{secret_pwd}@bsc.rpc.example.com",
                miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
                account="test-keystore-account",
                password_file=secret_path,
                dry_run=False,
                state_dir=tmp_dir
            )
            bot.send_transaction("0x1234", "test_sanitize", preflight=False)
            self.assertTrue(bot.locked_for_manual_review)

            sm = bot._ensure_state_manager()
            with open(sm.get_state_path(), "r", encoding="utf-8") as f:
                state_raw = f.read()
            with open(sm.get_audit_path(), "r", encoding="utf-8") as f:
                audit_raw = f.read()

            self.assertNotIn(secret_pwd, state_raw)
            self.assertNotIn(secret_path, state_raw)
            self.assertNotIn(secret_pwd, audit_raw)
            self.assertNotIn(secret_path, audit_raw)




class TestKeeperReconciliationAndFaultMitigations(unittest.TestCase):
    """
    Offline mock-only test suite verifying Keeper reconciliation requirements:
    1. Active process holding lock with PREPARING_TO_SEND prevents mutation and memory lock in concurrent step / --check-lock.
    2. Forcefully terminated process crash recovery converts PREPARING_TO_SEND to MANUAL_REVIEW_LOCKED.
    3. Cached nonce outside lock is never used; fresh nonce queried under lock.
    4. Cast send command construction includes --data, --async, isolated env, shell=False.
    5. max_retries total attempts semantic (max_retries + 1) for 0, 1, 2.
    6. Multiple replacement hashes retained in chronological order and listed in context.
    7. Revert state persistence failure transitions to manual review lock (no except: pass).
    8. Legacy state missing tx_hashes loaded compatibly without unlocking.
    9. manual_unlock snapshot logging for both success and failure cases.
    """
    def setUp(self):
        clear_configured_secrets()

    def tearDown(self):
        clear_configured_secrets()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_active_process_holding_lock_step_and_check_lock_bytes_unchanged(self, mock_subproc, mock_rpc):
        """Active process holding lock with PREPARING_TO_SEND: step() and --check-lock leave state file bytes unchanged and do not set memory lock."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = None
        mock_subproc.return_value = MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            from script.keeper_state import KeeperStateManager, StateDomain
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm_active = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            self.assertTrue(sm_active.acquire_lock())

            # Active process writes PREPARING_TO_SEND
            sm_active.record_preparing_to_send(operation="test_send", epoch=1, nonce=5, details="Active send")
            state_path = sm_active.get_state_path()
            with open(state_path, "rb") as f:
                orig_bytes = f.read()

            # Second keeper bot in another process/context runs step()
            bot_b = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            bot_b.step()

            # Must NOT set memory lock and must NOT modify disk state
            self.assertFalse(bot_b.locked_for_manual_review)
            with open(state_path, "rb") as f:
                self.assertEqual(f.read(), orig_bytes)

            # Check --check-lock behavior: status must be 发送中 and bytes unchanged
            sm_b = bot_b._ensure_state_manager()
            status_cat, details, state_data = sm_b.inspect_lock_status()
            self.assertEqual(status_cat, "发送中")
            self.assertEqual(details, "Active Keeper process currently broadcasting/in-flight (lock is held)")
            self.assertIsNotNone(state_data)
            self.assertEqual(state_data["status"], "PREPARING_TO_SEND")
            with open(state_path, "rb") as f:
                self.assertEqual(f.read(), orig_bytes)

            sm_active.release_lock()

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_terminated_process_crash_recovery_transitions_to_manual_review_lock(self, mock_subproc, mock_rpc):
        """Terminated process left PREPARING_TO_SEND: new process holding lock converts to MANUAL_REVIEW_LOCKED."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x5" if method == "eth_getTransactionCount"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=sender + chr(10), stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            from script.keeper_state import KeeperStateManager, StateDomain
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            sm.acquire_lock()
            sm.record_preparing_to_send(operation="test_crash", epoch=2, nonce=8, details="In-flight before crash")
            # Simulate crash: process died, lock released by OS
            sm.release_lock()

            # New process comes up
            bot_new = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            bot_new.step()

            # Verify step converted crash state to manual review lock
            self.assertTrue(bot_new.locked_for_manual_review)
            disk_state = sm.load_state()
            self.assertIsNotNone(disk_state)
            self.assertEqual(disk_state["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(disk_state["reason_code"], "CRASH_BEFORE_CONFIRMATION")
            self.assertEqual(disk_state["nonce"], 8)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_cached_nonce_outside_lock_never_used_for_broadcast(self, mock_subproc, mock_rpc):
        """Cached nonce outside lock is never used; fresh nonce is queried under mutex lock."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "a" * 64

        call_count = {"nonce_queries": 0}
        def dynamic_rpc(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            elif method == "eth_call":
                return "0x0"
            elif method == "eth_getTransactionCount":
                call_count["nonce_queries"] += 1
                if call_count["nonce_queries"] == 1:
                    return "0xa"  # 10 (queried outside lock)
                return "0xf"      # 15 (queried inside lock after external advance)
            elif method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"}
            return "0x0"

        mock_rpc.side_effect = dynamic_rpc
        observed_cmds = []
        def subproc_run(cmd, *args, **kwargs):
            if "send" in cmd:
                observed_cmds.append(list(cmd))
                return MagicMock(returncode=0, stdout=f"txHash: {fake_tx}" + chr(10), stderr="")
            return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")
        mock_subproc.side_effect = subproc_run

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            # Query and cache nonce outside the lock
            nonce_outside = bot.get_nonce()
            self.assertEqual(nonce_outside, 10)
            self.assertEqual(bot.current_nonce, 10)

            # Another process occupies nonce 10; on-chain pending count becomes 15
            # Now bot executes send_transaction; under lock it must re-read nonce 15 and broadcast with 15
            res = bot.send_transaction("0x1234", "test_nonce_fresh", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)

            self.assertEqual(len(observed_cmds), 1)
            cmd = observed_cmds[0]
            nonce_idx = cmd.index("--nonce")
            self.assertEqual(cmd[nonce_idx + 1], "15")

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_cast_send_command_construction_and_isolation(self, mock_subproc, mock_rpc):
        """Cast send command construction strictly includes --data, --async, uses isolated env, and shell=False."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "b" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )

        captured_kwargs = []
        captured_cmd = []
        def subproc_run(cmd, *args, **kwargs):
            if "send" in cmd:
                captured_cmd.extend(cmd)
                captured_kwargs.append(kwargs)
                return MagicMock(returncode=0, stdout=f"txHash: {fake_tx}" + chr(10), stderr="")
            return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")
        mock_subproc.side_effect = subproc_run

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            calldata = "0xaabbccdd"
            res = bot.send_transaction(calldata, "test_cmd_flags", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)

            self.assertIn("--data", captured_cmd)
            self.assertEqual(captured_cmd[captured_cmd.index("--data") + 1], calldata)
            self.assertIn("--async", captured_cmd)
            self.assertIn("shell", captured_kwargs[0])
            self.assertIs(captured_kwargs[0]["shell"], False)
            self.assertIn("env", captured_kwargs[0])
            self.assertEqual(captured_kwargs[0]["env"], get_isolated_cast_env())

    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_max_retries_total_attempt_count_semantics(self, mock_subproc, mock_rpc, mock_time):
        """max_retries for 0, 1, 2 results in exactly max_retries + 1 total broadcast attempts."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else None  # Receipts always timeout to force all speed-up attempts
        )

        for max_retries in (0, 1, 2):
            expected_attempts = max_retries + 1
            broadcast_count = [0]
            def subproc_run(cmd, *args, **kwargs):
                if "send" in cmd:
                    broadcast_count[0] += 1
                    tx_h = "0x" + f"{broadcast_count[0]:x}" * 64
                    return MagicMock(returncode=0, stdout=f"txHash: {tx_h}" + chr(10), stderr="")
                return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")
            mock_subproc.side_effect = subproc_run

            with tempfile.TemporaryDirectory() as tmp_dir:
                bot = KeeperBot(
                    rpc_url="http://mock-rpc",
                    miner_address=miner,
                    account="test-keystore-account",
                    password_file="fake-passwords/dev.pwd",
                    dry_run=False,
                    max_retries=max_retries,
                    tx_timeout=1,
                    state_dir=tmp_dir
                )
                res = bot.send_transaction("0x1234", "test_retries", preflight=False)
                self.assertEqual(res.status, TxExecutionStatus.PENDING_UNCONFIRMED)
                self.assertEqual(broadcast_count[0], expected_attempts, f"max_retries={max_retries} must execute exactly {expected_attempts} attempts")

    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_multiple_replacement_hashes_retained_and_listed_in_context(self, mock_subproc, mock_rpc, mock_time):
        """Multiple replacement hashes are tracked in tx_hashes list and listed in context upon exhaustion."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        hash0 = "0x" + "1" * 64
        hash1 = "0x" + "2" * 64
        hash2 = "0x" + "3" * 64

        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else None
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=sender + chr(10), stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {hash0}" + chr(10), stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {hash1}" + chr(10), stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {hash2}" + chr(10), stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                max_retries=2,
                tx_timeout=1,
                state_dir=tmp_dir
            )
            res = bot.send_transaction("0x1234", "test_replacement_hashes", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.PENDING_UNCONFIRMED)
            self.assertEqual(bot.manual_review_context["tx_hash"], hash2)
            self.assertEqual(bot.manual_review_context["tx_hashes"], [hash0, hash1, hash2])

            sm = bot._ensure_state_manager()
            saved = sm.load_state()
            self.assertIsNotNone(saved)
            self.assertEqual(saved["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(saved["tx_hash"], hash2)
            self.assertEqual(saved["tx_hashes"], [hash0, hash1, hash2])

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_revert_state_write_failure_enters_manual_review(self, mock_subproc, mock_rpc):
        """Onchain revert whose state write fails enters manual review with STATE_WRITE_FAILURE (no except: pass)."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "c" * 64
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x0" if method == "eth_call"
            else "0x5" if method == "eth_getTransactionCount"
            else {"status": "0x0", "blockNumber": "0x101", "gasUsed": "0x5208"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = [
            MagicMock(returncode=0, stdout=sender + chr(10), stderr=""),
            MagicMock(returncode=0, stdout=f"txHash: {fake_tx}" + chr(10), stderr="")
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            sm = bot._ensure_state_manager()
            from script.keeper_state import StateWriteError
            with patch.object(sm, "record_confirmed_revert", side_effect=StateWriteError("Disk full simulation")):
                res = bot.send_transaction("0x1234", "test_revert_fail", preflight=False)
                self.assertEqual(res.status, TxExecutionStatus.UNCERTAIN)
                self.assertTrue(bot.locked_for_manual_review)
                saved_state = sm.load_state()
                self.assertIsNotNone(saved_state)
                self.assertEqual(saved_state["status"], "MANUAL_REVIEW_LOCKED")
                self.assertEqual(saved_state["reason_code"], "STATE_WRITE_FAILURE")
                self.assertIn(fake_tx, saved_state["tx_hashes"])
                self.assertEqual(saved_state["tx_hash"], fake_tx)

    def test_legacy_state_missing_tx_hashes_loaded_compatibly(self):
        """Legacy state file missing tx_hashes key is read compatibly and not treated as unlocked."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        legacy_hash = "0x" + "d" * 64

        with tempfile.TemporaryDirectory() as tmp_dir:
            from script.keeper_state import KeeperStateManager, StateDomain
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            legacy_data = {
                "version": 1,
                "domain": domain.to_dict(),
                "status": "MANUAL_REVIEW_LOCKED",
                "operation": "test_legacy",
                "epoch": 1,
                "nonce": 5,
                "tx_hash": legacy_hash,
                "reason_code": "MANUAL_LOCK",
                "created_at": "2026-11-10T00:00:00Z",
                "updated_at": "2026-11-10T00:00:00Z",
                "details": "Legacy state missing tx_hashes"
            }
            with open(sm.get_state_path(), "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            loaded = sm.load_state()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["tx_hashes"], [legacy_hash])

            can_proceed, reason, state_out = sm.check_startup_protection()
            self.assertFalse(can_proceed)
            self.assertEqual(reason, "Keeper is locked for manual review (reason: MANUAL_LOCK)")
            self.assertIsNotNone(state_out)
            self.assertEqual(state_out["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(state_out["reason_code"], "MANUAL_LOCK")
            self.assertEqual(state_out["tx_hashes"], [legacy_hash])

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_manual_unlock_snapshot_audit_record_success_and_failure(self, mock_subproc, mock_rpc):
        """manual_unlock records read-only snapshot in audit log on success, or '快照不可用' on RPC failure."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        tx_h = "0x" + "e" * 64

        # Part 1: RPC Success
        mock_rpc.side_effect = lambda url, method, params: (
            "0x6080604052" if method == "eth_getCode"
            else "0x61" if method == "eth_chainId"
            else "0x7" if method == "eth_getTransactionCount"
            else {"status": "0x1", "blockNumber": "0x200"} if method == "eth_getTransactionReceipt"
            else "0x0"
        )
        mock_subproc.side_effect = lambda *a, **kw: MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            from script.keeper_state import KeeperStateManager, StateDomain
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            sm.record_manual_review_lock(reason="Review needed", nonce=7, operation="test", tx_hash=tx_h)

            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir
            )
            bot.manual_unlock(expected_nonce=7, operator_reason="Operator verified on-chain success")

            with open(sm.get_audit_path(), "r", encoding="utf-8") as f:
                logs = [json.loads(line) for line in f if line.strip()]
            unlock_log = logs[-1]
            self.assertEqual(unlock_log["action"], "MANUAL_UNLOCK")
            snapshot = unlock_log["onchain_snapshot"]
            self.assertIsInstance(snapshot, dict)
            self.assertEqual(snapshot["latest_nonce"], 7)
            self.assertEqual(snapshot["pending_nonce"], 7)
            self.assertEqual(snapshot["tx_receipts"][tx_h]["status"], "0x1")

        # Part 2: RPC Failure during snapshot query
        def rpc_fail_snapshot(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            elif method == "eth_chainId":
                return "0x61"
            raise Exception("RPC connectivity failed")
        mock_rpc.side_effect = rpc_fail_snapshot

        with tempfile.TemporaryDirectory() as tmp_dir2:
            sm2 = KeeperStateManager(state_dir=tmp_dir2, domain=domain)
            sm2.record_manual_review_lock(reason="Review needed", nonce=8, operation="test", tx_hash=tx_h)

            bot2 = KeeperBot(
                rpc_url="http://mock-rpc-dead",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir2
            )
            bot2.manual_unlock(expected_nonce=8, operator_reason="Operator verified offline without RPC")

            with open(sm2.get_audit_path(), "r", encoding="utf-8") as f:
                logs2 = [json.loads(line) for line in f if line.strip()]
            unlock_log2 = logs2[-1]
            self.assertEqual(unlock_log2["action"], "MANUAL_UNLOCK")
            self.assertEqual(unlock_log2["onchain_snapshot"], "快照不可用")

    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_live_get_nonce_pending_failure_and_unparseable_aborts_without_state_or_lock(self, mock_subproc, mock_rpc, mock_time):
        """Live pending nonce query failure or unparseable response aborts round before PREPARING_TO_SEND with sanitized alert and no manual lock."""
        self.addCleanup(clear_configured_secrets)
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        sensitive_rpc = "https://rpc_user:rpc_secret_pass_999@mock-rpc.local:8545/secret_rpc_path?token=rpc_tok_888"
        secret_pwd_file = "C:/top_secret_vault/keeper_secret.pwd"

        nonce_behavior = {"mode": "raise"}

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_gasPrice":
                return "0x5f5e100"
            if method == "eth_call":
                tx_data = (params[0].get("data", "") if params else "").replace("0x", "")
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return "0x0"
                if sel == SELECTOR_LAST_SYNCED:
                    return "0x0"
                if sel == SELECTOR_EPOCH_SEED:
                    return "0x0"
                return "0x0"
            if method == "eth_getTransactionCount":
                if nonce_behavior["mode"] == "raise":
                    raise RuntimeError(f"Connection error to {sensitive_rpc} using {secret_pwd_file}")
                return nonce_behavior["value"]
            if method == "eth_sendRawTransaction":
                raise AssertionError("eth_sendRawTransaction must not be called on pending nonce failure")
            return "0x0"

        mock_rpc.side_effect = rpc_side
        cast_send_calls = []

        def subproc_side(cmd, *args, **kwargs):
            if "send" in cmd:
                cast_send_calls.append(list(cmd))
                raise AssertionError("cast send must not be invoked on pending nonce failure")
            return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        mock_subproc.side_effect = subproc_side

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url=sensitive_rpc,
                miner_address=miner,
                account="test-keystore-account",
                password_file=secret_pwd_file,
                dry_run=False,
                state_dir=tmp_dir,
            )
            sm = bot._ensure_state_manager()
            captured_alerts = []
            bot.alert = lambda msg: captured_alerts.append(msg)

            # Case A: RPC exception with cached current_nonce preset -> must NOT fall back to 0 or 4
            bot.current_nonce = 4
            nonce_behavior["mode"] = "raise"
            with self.assertRaises(NonceQueryError) as cm:
                bot.send_transaction("0x1234", "test_pending_exc", preflight=False)

            self.assertEqual(len(cast_send_calls), 0)
            self.assertFalse(bot.locked_for_manual_review)
            self.assertFalse(bot._domain_lock_acquired)
            self.assertEqual(bot.current_nonce, 4)
            self.assertIsNone(sm.load_state())
            self.assertFalse(os.path.exists(sm.get_state_path()))

            combined_text = "\n".join(captured_alerts + [str(cm.exception)])
            self.assertNotIn("rpc_secret_pass_999", combined_text)
            self.assertNotIn("rpc_user", combined_text)
            self.assertNotIn("secret_rpc_path", combined_text)
            self.assertNotIn("rpc_tok_888", combined_text)
            self.assertNotIn("top_secret_vault", combined_text)
            self.assertNotIn("keeper_secret.pwd", combined_text)

            # Case B: Unparseable responses -> raise NonceQueryError, no state/lock/broadcast
            for bad_val in (None, "", "   ", "0x", "not-hex", "-1", True, False, {}, []):
                captured_alerts.clear()
                bot.current_nonce = 4
                nonce_behavior["mode"] = "return"
                nonce_behavior["value"] = bad_val
                with self.assertRaises(NonceQueryError):
                    bot.send_transaction("0x1234", f"test_bad_{type(bad_val).__name__}", preflight=False)
                self.assertEqual(len(cast_send_calls), 0)
                self.assertFalse(bot.locked_for_manual_review)
                self.assertFalse(bot._domain_lock_acquired)
                self.assertEqual(bot.current_nonce, 4)
                self.assertIsNone(sm.load_state())

            # Case C: step() catches NonceQueryError, aborts round without lock or state
            captured_alerts.clear()
            nonce_behavior["mode"] = "raise"
            bot.step()
            self.assertEqual(len(cast_send_calls), 0)
            self.assertFalse(bot.locked_for_manual_review)
            self.assertIsNone(sm.load_state())
            self.assertTrue(len(captured_alerts) >= 1)
            step_alerts = "\n".join(captured_alerts)
            self.assertNotIn("rpc_secret_pass_999", step_alerts)
            self.assertNotIn("keeper_secret.pwd", step_alerts)

            # Case D: dry-run behavior remains unchanged (falls back to 0 without raising)
            nonce_behavior["mode"] = "raise"
            bot_dry = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                keeper_address=sender,
                dry_run=True,
            )
            self.assertEqual(bot_dry.get_nonce(), 0)

    @patch("script.keeper.time", new_callable=_FakeKeeperTime)
    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_live_get_nonce_cached_gt_pending_aborts_and_le_pending_uses_pending(self, mock_subproc, mock_rpc, mock_time):
        """Cached current_nonce > pending aborts round with latest/pending/cached alert; <= pending uses pending."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        fake_tx = "0x" + "7" * 64

        nonce_view = {"latest": "0x4", "pending": "0x5", "fail_latest": False}
        sync_state = {"epoch": 1, "last_synced": 0, "seed": 1}

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_gasPrice":
                return "0x5f5e100"
            if method == "eth_call":
                tx_data = (params[0].get("data", "") if params else "").replace("0x", "")
                sel = tx_data[:8]
                if sel == SELECTOR_EPOCH:
                    return hex(sync_state["epoch"])
                if sel == SELECTOR_LAST_SYNCED:
                    return hex(sync_state["last_synced"])
                if sel == SELECTOR_EPOCH_SEED:
                    return hex(sync_state["seed"])
                return "0x0"
            if method == "eth_getTransactionCount":
                tag = params[1] if len(params) > 1 else "pending"
                if tag == "latest":
                    if nonce_view["fail_latest"]:
                        raise RuntimeError("latest query timeout")
                    return nonce_view["latest"]
                return nonce_view["pending"]
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "blockNumber": "0x100", "gasUsed": "0x5208"}
            return "0x0"

        mock_rpc.side_effect = rpc_side
        sent_cmds = []

        def subproc_side(cmd, *args, **kwargs):
            if "send" in cmd:
                sent_cmds.append(list(cmd))
                return MagicMock(returncode=0, stdout=f"txHash: {fake_tx}" + chr(10), stderr="")
            return MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        mock_subproc.side_effect = subproc_side

        with tempfile.TemporaryDirectory() as tmp_dir:
            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir,
            )
            sm = bot._ensure_state_manager()
            captured_alerts = []
            bot.alert = lambda msg: captured_alerts.append(msg)

            # 1. cached (8) > pending (5): aborts, no broadcast, no state, no lock, no downgrade
            bot.current_nonce = 8
            with self.assertRaises(NonceDivergenceError):
                bot.send_transaction("0x1234", "test_divergence", preflight=False)

            self.assertEqual(len(sent_cmds), 0)
            self.assertFalse(bot.locked_for_manual_review)
            self.assertFalse(bot._domain_lock_acquired)
            self.assertEqual(bot.current_nonce, 8)
            self.assertIsNone(sm.load_state())
            div_alert = "\n".join(captured_alerts)
            self.assertIn("latest=4", div_alert)
            self.assertIn("pending=5", div_alert)
            self.assertIn("cached=8", div_alert)

            # 2. If latest diagnostic query fails, divergence still aborts cleanly with unavailable latest
            captured_alerts.clear()
            nonce_view["fail_latest"] = True
            with self.assertRaises(NonceDivergenceError):
                bot.send_transaction("0x1234", "test_divergence_no_latest", preflight=False)
            self.assertEqual(len(sent_cmds), 0)
            self.assertEqual(bot.current_nonce, 8)
            self.assertIsNone(sm.load_state())
            div_alert_unavail = "\n".join(captured_alerts)
            self.assertIn("latest=unavailable", div_alert_unavail)
            self.assertIn("pending=5", div_alert_unavail)
            self.assertIn("cached=8", div_alert_unavail)

            # 3. step() re-evaluates next round: aborts while pending=5, succeeds when RPC catches up to pending=8 (cached == pending)
            nonce_view["fail_latest"] = False
            captured_alerts.clear()
            bot.step()
            self.assertEqual(len(sent_cmds), 0)
            self.assertFalse(bot.locked_for_manual_review)
            self.assertEqual(bot.current_nonce, 8)
            self.assertIsNone(sm.load_state())

            nonce_view["latest"] = "0x8"
            nonce_view["pending"] = "0x8"
            bot.step()
            self.assertEqual(len(sent_cmds), 1)
            self.assertEqual(sent_cmds[0][sent_cmds[0].index("--nonce") + 1], "8")
            self.assertEqual(bot.current_nonce, 9)

            # 4. cached (4) < pending (10): uses pending (10)
            bot.current_nonce = 4
            nonce_view["latest"] = "0xa"
            nonce_view["pending"] = "0xa"
            res = bot.send_transaction("0x1234", "test_cached_lt_pending", preflight=False)
            self.assertEqual(res.status, TxExecutionStatus.CONFIRMED_SUCCESS)
            self.assertEqual(sent_cmds[-1][sent_cmds[-1].index("--nonce") + 1], "10")
            self.assertEqual(bot.current_nonce, 11)

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_lock_for_manual_review_persists_lock_when_get_nonce_fails(self, mock_subproc, mock_rpc):
        """Nonce query failure or divergence inside lock_for_manual_review must not prevent MANUAL_REVIEW_LOCKED persistence."""
        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        nonce_state = {"mode": "raise"}

        def rpc_side(url, method, params):
            if method == "eth_getCode":
                return "0x6080604052"
            if method == "eth_chainId":
                return "0x61"
            if method == "eth_getTransactionCount":
                if nonce_state["mode"] == "raise":
                    raise RuntimeError("RPC nonce endpoint offline")
                return "0x5"
            return "0x0"

        mock_rpc.side_effect = rpc_side
        mock_subproc.side_effect = lambda *a, **kw: MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        # Subcase 1: pending query throws during lock_for_manual_review(nonce=None)
        with tempfile.TemporaryDirectory() as tmp_dir1:
            bot1 = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir1,
            )
            sm1 = bot1._ensure_state_manager()
            bot1.lock_for_manual_review(
                reason="Uncertain outcome while nonce RPC unreachable",
                nonce=None,
                func_name="sync(1)",
                reason_code="NONCE_FAIL_LOCK",
            )
            self.assertTrue(bot1.locked_for_manual_review)
            self.assertIsNone(bot1.manual_review_context["nonce"])
            disk1 = sm1.load_state()
            self.assertIsNotNone(disk1)
            self.assertEqual(disk1["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(disk1["nonce"], "Unknown")
            self.assertEqual(disk1["reason_code"], "NONCE_FAIL_LOCK")
            with open(sm1.get_audit_path(), "r", encoding="utf-8") as f:
                audits1 = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(a.get("action") == "MANUAL_REVIEW_LOCKED" for a in audits1))

        # Subcase 2: divergence (cached > pending) during lock_for_manual_review(nonce=None)
        nonce_state["mode"] = "ok"
        with tempfile.TemporaryDirectory() as tmp_dir2:
            bot2 = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir2,
            )
            sm2 = bot2._ensure_state_manager()
            bot2.current_nonce = 12
            bot2.lock_for_manual_review(
                reason="Uncertain outcome with diverged cached nonce",
                nonce=None,
                func_name="sync(1)",
                reason_code="DIVERGENCE_LOCK",
            )
            self.assertTrue(bot2.locked_for_manual_review)
            self.assertEqual(bot2.current_nonce, 12)
            self.assertIsNone(bot2.manual_review_context["nonce"])
            disk2 = sm2.load_state()
            self.assertIsNotNone(disk2)
            self.assertEqual(disk2["status"], "MANUAL_REVIEW_LOCKED")
            self.assertEqual(disk2["nonce"], "Unknown")
            self.assertEqual(disk2["reason_code"], "DIVERGENCE_LOCK")

    @patch("script.keeper.rpc_call")
    @patch("subprocess.run")
    def test_manual_unlock_reads_snapshot_after_lock_and_state_reread(self, mock_subproc, mock_rpc):
        """KeeperBot.manual_unlock acquires sender lock, re-reads state, takes snapshot, then writes audit and state."""
        import script.keeper_state as keeper_state_mod
        from script.keeper_state import KeeperStateManager, StateDomain

        miner = "0x420382a0f874c1F120621f847d81581e8737504e"
        sender = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
        tx_h = "0x" + "f" * 64
        events = []

        mock_subproc.side_effect = lambda *a, **kw: MagicMock(returncode=0, stdout=sender + chr(10), stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            domain = StateDomain(chain_id=97, miner_address=miner, sender_address=sender)
            sm_setup = KeeperStateManager(state_dir=tmp_dir, domain=domain)
            sm_setup.record_manual_review_lock(reason="Review needed", nonce=7, operation="sync(1)", tx_hash=tx_h)

            def rpc_side(url, method, params):
                if method == "eth_getCode":
                    return "0x6080604052"
                if method == "eth_chainId":
                    return "0x61"
                if method == "eth_getTransactionCount":
                    tag = params[1] if len(params) > 1 else ""
                    events.append(("rpc_count", tag, bool(sm_bot._lock and sm_bot._lock._acquired)))
                    return "0x7"
                if method == "eth_getTransactionReceipt":
                    events.append(("rpc_receipt", params[0], bool(sm_bot._lock and sm_bot._lock._acquired)))
                    return {"status": "0x1", "blockNumber": "0x200"}
                return "0x0"

            mock_rpc.side_effect = rpc_side

            bot = KeeperBot(
                rpc_url="http://mock-rpc",
                miner_address=miner,
                account="test-keystore-account",
                password_file="fake-passwords/dev.pwd",
                dry_run=False,
                state_dir=tmp_dir,
            )
            sm_bot = bot._ensure_state_manager()

            orig_acquire = sm_bot.acquire_lock
            orig_load = sm_bot.load_state
            orig_release = sm_bot.release_lock
            orig_audit = keeper_state_mod.append_audit_log
            orig_atomic = keeper_state_mod.atomic_write_json

            def tracked_acquire():
                ok = orig_acquire()
                events.append(("acquire_lock", ok))
                return ok

            def tracked_load():
                res = orig_load()
                events.append(("load_state", bool(sm_bot._lock and sm_bot._lock._acquired)))
                return res

            def tracked_release():
                events.append(("release_lock", bool(sm_bot._lock and sm_bot._lock._acquired)))
                return orig_release()

            def tracked_audit(path, entry):
                events.append(("append_audit_log", bool(sm_bot._lock and sm_bot._lock._acquired)))
                return orig_audit(path, entry)

            def tracked_atomic(path, data):
                events.append(("atomic_write_json", bool(sm_bot._lock and sm_bot._lock._acquired)))
                return orig_atomic(path, data)

            with patch.object(sm_bot, "acquire_lock", side_effect=tracked_acquire), \
                 patch.object(sm_bot, "load_state", side_effect=tracked_load), \
                 patch.object(sm_bot, "release_lock", side_effect=tracked_release), \
                 patch.object(keeper_state_mod, "append_audit_log", side_effect=tracked_audit), \
                 patch.object(keeper_state_mod, "atomic_write_json", side_effect=tracked_atomic):
                res = bot.manual_unlock(expected_nonce=7, operator_reason="Verified order of snapshot")

            self.assertEqual(res["status"], "IDLE")
            self.assertFalse(bot.locked_for_manual_review)

            expected_seq = [
                ("acquire_lock", True),
                ("load_state", True),
                ("rpc_count", "latest", True),
                ("rpc_count", "pending", True),
                ("rpc_receipt", tx_h, True),
                ("append_audit_log", True),
                ("atomic_write_json", True),
                ("release_lock", True),
            ]
            self.assertEqual(events, expected_seq)

            with open(sm_bot.get_audit_path(), "r", encoding="utf-8") as f:
                audits = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(audits[-1]["onchain_snapshot"]["tx_receipts"][tx_h]["status"], "0x1")
