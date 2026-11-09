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
import rlp

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from script.keeper import (
    calculate_gas_price,
    get_bumped_gas_price,
    to_checksum_address,
    sanitize_url,
    sanitize_text,
    register_secret,
    clear_configured_secrets,
    _CONFIGURED_SECRETS,
    parse_numeric,
    format_exception_safe,
    KeeperBot,
    SELECTOR_EPOCH,
    SELECTOR_LAST_SYNCED,
    SELECTOR_EPOCH_SEED,
    SELECTOR_INIT_EPOCH_SEED,
    SELECTOR_SYNC,
    DEFAULT_MIN_GAS_PRICE_GWEI,
    DEFAULT_GAS_MULTIPLIER
)


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
    @patch("script.keeper.rpc_call")
    def test_speedup_retry_reuses_identical_nonce_and_bumps_gas_price(self, mock_rpc):
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
                tx_hash = f"0xhash_{len(broadcasted_txs)}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                # Timeout on first tx (0), confirm on second tx (1)
                tx_hash = params[0]
                if tx_hash == "0xhash_0":
                    return None  # Pending/unconfirmed
                elif tx_hash == "0xhash_1":
                    return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
            return None

        mock_rpc.side_effect = rpc_side_effect

        bot = KeeperBot(
            rpc_url="http://mock-rpc",
            miner_address="0x420382a0f874c1F120621f847d81581e8737504e",
            private_key=test_key,
            dry_run=False,
            tx_timeout=1, # 1 sec timeout for test speed
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
                    # All seeds 0..3 uninitialized (return 0)
                    return "0x0000000000000000000000000000000000000000000000000000000000000000"
                elif sel == SELECTOR_INIT_EPOCH_SEED:
                    # Preflight check simulation
                    target_epoch = int(tx_data[8:72], 16)
                    preflight_calls.append(target_epoch)
                    return "0x" # Preflight succeeds
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0xhash_{len(broadcasted_txs)}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
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

        # Exactly 4 transactions must have been sent
        self.assertEqual(len(broadcasted_txs), 4)

        # Preflight must have been called for 0, 1, 2, 3 in order
        self.assertEqual(preflight_calls, [0, 1, 2, 3])

        # Decode raw transactions and verify sequential order, calldata and incrementing nonce
        nonces = []
        target_epochs = []
        for i, tx_info in enumerate(broadcasted_txs):
            raw_bytes = bytes.fromhex(tx_info["raw"].replace("0x", ""))
            decoded = rlp.decode(raw_bytes)
            nonce = int.from_bytes(decoded[0], "big")
            calldata = decoded[5].hex()
            nonces.append(nonce)

            # Function selector must be initEpochSeed (0e2a5ea2)
            self.assertTrue(calldata.startswith(SELECTOR_INIT_EPOCH_SEED))
            epoch_arg = int(calldata[8:72], 16)
            steps_arg = int(calldata[72:136], 16)
            self.assertEqual(steps_arg, 24)
            target_epochs.append(epoch_arg)

        # Order must be strictly 0, 1, 2, 3
        self.assertEqual(target_epochs, [0, 1, 2, 3])

        # Nonces must strictly increment: e.g. 10, 11, 12, 13
        self.assertEqual(nonces, [10, 11, 12, 13])

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
                    if target_epoch == 2:
                        # Preflight for epoch 2 fails with PrevSeedNotInitialized (0x4105720d)
                        raise RuntimeError("RPC Error (eth_call): {'code': 3, 'data': '0x4105720d', 'message': 'execution reverted: PrevSeedNotInitialized()'}")
                    return "0x"
                return "0x"
            elif method == "eth_sendRawTransaction":
                tx_hash = f"0xhash_{len(broadcasted_txs)}"
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

            # Preflight was called for 0, 1, and 2 (stopped before 3)
            self.assertEqual(preflight_calls, [0, 1, 2])

            # Only transactions 0 and 1 were broadcast! Epoch 2 and 3 were never broadcast!
            self.assertEqual(len(broadcasted_txs), 2)

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
                tx_hash = f"0xhash_{len(broadcasted_txs)}"
                broadcasted_txs.append({"raw": params[0], "hash": tx_hash})
                return tx_hash
            elif method == "eth_getTransactionReceipt":
                tx_hash = params[0]
                if tx_hash == "0xhash_0":
                    return {"blockNumber": "0x100", "gasUsed": "0x12000", "status": "0x1"}
                elif tx_hash == "0xhash_1":
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

            # Epoch 0 succeeded, Epoch 1 reverted; Epoch 2 and 3 were never broadcast!
            self.assertEqual(len(broadcasted_txs), 2)
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


if __name__ == "__main__":
    unittest.main()
