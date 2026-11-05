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
        self.assertEqual(SELECTOR_INIT_EPOCH_SEED, "0e2a5ea2")# initEpochSeed(uint256)
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
            self.assertIn("initEpochSeed(0)", combined_logs)
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

        calldata = "0x0e2a5ea2" + "00" * 32
        receipt = bot.send_transaction(calldata, "initEpochSeed(0)", "initEpochSeed(0)")

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
    @patch("script.keeper.rpc_call")
    def test_cur_3_all_uninitialized_sends_in_order_0_1_2_3_with_incrementing_nonce(self, mock_rpc):
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
                    target_epoch = int(tx_data[8:], 16)
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
            epoch_arg = int(calldata[8:], 16)
            target_epochs.append(epoch_arg)

        # Order must be strictly 0, 1, 2, 3
        self.assertEqual(target_epochs, [0, 1, 2, 3])

        # Nonces must strictly increment: e.g. 10, 11, 12, 13
        self.assertEqual(nonces, [10, 11, 12, 13])

    @patch("script.keeper.rpc_call")
    def test_cur_3_stops_when_intermediate_preflight_fails(self, mock_rpc):
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
                    target_epoch = int(tx_data[8:], 16)
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

    @patch("script.keeper.rpc_call")
    def test_cur_3_stops_when_intermediate_tx_reverts(self, mock_rpc):
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

if __name__ == "__main__":
    unittest.main()