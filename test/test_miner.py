import os, sys, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import script.miner as miner
import script.miner_gpu as miner_gpu
import script.claim as claim
import script.status as status
import script.monitor as monitor

class TestMinerSeedBackfillAndJitter(unittest.TestCase):
    def test_backfill_epoch_seed_signs_and_broadcasts_tx(self):
        txs_sent = []
        def mock_rpc_call(rpc_url, method, params):
            if method == "eth_chainId": return "0x61"
            elif method == "eth_getTransactionCount": return "0x1"
            elif method == "eth_gasPrice": return "0x5f5e100"
            elif method == "eth_sendRawTransaction":
                txs_sent.append(params[0])
                return "0xhash_backfill_123"
            return None
        with patch("script.miner.rpc_call", side_effect=mock_rpc_call), patch("time.sleep"):
            pk = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
            tx_hash = miner.backfill_epoch_seed("http://mock-rpc", "0x420382a0f874c1F120621f847d81581e8737504e", 5, private_key=pk, max_steps=24)
            self.assertEqual(tx_hash, "0xhash_backfill_123")
            self.assertEqual(len(txs_sent), 1)

    def test_miner_discovers_zero_seed_applies_jitter(self):
        args = MagicMock()
        args.miner = "0x1111111111111111111111111111111111111111"
        args.epoch = 5
        args.seed = None
        args.skip_stake_check = True
        args.rpc = "http://mock-rpc"
        args.miner_contract = "0x420382a0f874c1F120621f847d81581e8737504e"
        args.private_key = None
        with patch("script.miner.rpc_call", return_value="0x61"), patch("script.miner.eth_call", return_value="0x0"), patch("time.sleep") as mock_sleep, patch("random.uniform", return_value=2.45) as mock_random, patch("script.miner.backfill_epoch_seed") as mock_backfill:
            with self.assertRaises(RuntimeError):
                miner.mine(args)
            mock_random.assert_called_with(0.0, 3.0)
            mock_sleep.assert_any_call(2.45)
            mock_backfill.assert_called_once()

    def test_claim_script_has_event_constants(self):
        self.assertEqual(claim.SELECTOR_INIT_EPOCH_SEED, "0xd9d9c33c")
        self.assertEqual(claim.EVENT_EPOCH_SETTLED_TOPIC, "0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5")
        self.assertEqual(claim.EVENT_REWARD_CLAIMED_TOPIC, "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a")

    def test_status_script_has_unallocated_gap_selector(self):
        self.assertEqual(status.SELECTOR_INIT_EPOCH_SEED, "0xd9d9c33c")
        self.assertEqual(status.SEL_UNALLOCATED_GAP, "0xca6875f7")

    def test_monitor_script_reward_claimed_topic(self):
        self.assertEqual(monitor.TOPIC_REWARD_CLAIMED, "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a")
        self.assertFalse(hasattr(monitor, "TOPIC_CLAIMED"))

    def test_status_script_reward_claimed_parsing(self):
        self.assertEqual(status.EVENT_REWARD_CLAIMED_TOPIC, "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a")

        # Test 4-parameter RewardClaimed log parsing
        miner_addr = "0x1111111111111111111111111111111111111111"
        epoch = 12
        claimed_amt = 500 * 10**18
        overflow = 100 * 10**18
        topic1 = "0x000000000000000000000000" + miner_addr[2:].lower()
        topic2 = hex(epoch)
        data = "0x" + hex(claimed_amt)[2:].zfill(64) + hex(overflow)[2:].zfill(64)
        mock_log = {
            "topics": [status.EVENT_REWARD_CLAIMED_TOPIC, topic1, topic2],
            "data": data
        }
        miner_out, epoch_out, amt_out, overflow_out = status.parse_reward_claimed(mock_log)
        self.assertEqual(miner_out, miner_addr.lower())
        self.assertEqual(epoch_out, epoch)
        self.assertEqual(amt_out, claimed_amt)
        self.assertEqual(overflow_out, overflow)

    def test_claim_and_keeper_scripts_reward_claimed_parsing(self):
        self.assertEqual(claim.EVENT_REWARD_CLAIMED_TOPIC, "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a")
        import script.keeper as keeper
        self.assertEqual(keeper.EVENT_REWARD_CLAIMED_TOPIC, "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a")

        miner_addr = "0x2222222222222222222222222222222222222222"
        epoch = 25
        claimed_amt = 123456789
        overflow = 987654321
        topic1 = "0x000000000000000000000000" + miner_addr[2:].lower()
        topic2 = hex(epoch)
        data = "0x" + hex(claimed_amt)[2:].zfill(64) + hex(overflow)[2:].zfill(64)
        mock_log = {
            "topics": [claim.EVENT_REWARD_CLAIMED_TOPIC, topic1, topic2],
            "data": data
        }
        res_claim = claim.parse_reward_claimed(mock_log)
        self.assertEqual(res_claim, (miner_addr.lower(), epoch, claimed_amt, overflow))
        res_keeper = keeper.parse_reward_claimed(mock_log)
        self.assertEqual(res_keeper, (miner_addr.lower(), epoch, claimed_amt, overflow))

    def test_parameter_updates_cap_and_difficulty(self):
        self.assertEqual(miner.CAP, 6)
        self.assertEqual(miner_gpu.CAP, 6)
        self.assertEqual(status.CAP, 6)
        self.assertEqual(miner.DEFAULT_DIFFICULTY_BITS, 32)
        self.assertEqual(miner_gpu.DEFAULT_DIFFICULTY_BITS, 32)
        self.assertEqual(miner.DEFAULT_EASIEST_TARGET, 1 << 224)
        self.assertEqual(miner.DEFAULT_HARDEST_TARGET, 1 << 221)
        self.assertEqual(miner_gpu.DEFAULT_EASIEST_TARGET, 1 << 224)
        self.assertEqual(miner_gpu.DEFAULT_HARDEST_TARGET, 1 << 221)

class TestMinerDynamicVaultAndStakeEligibility(unittest.TestCase):
    """
    Offline regression tests for dynamic StakeVault resolution and eligibility checks in miner.py.
    Fully mocked: no real network, no private keys, no real transactions.
    """
    MINER_PROXY_DEFAULT = miner.DEFAULT_MINER_CONTRACT
    MINER_PROXY_CUSTOM = "0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf"
    VAULT_NON_DEFAULT = "0xBaDC0de000000000000000000000000000000002"
    MINER_ADDR = "0x2e9e19f7F1D3a2b01ba5BbA7c37E58398568d82c"

    def _encode_address_ret(self, addr_hex: str) -> str:
        clean = addr_hex.lower().replace("0x", "")
        return "0" * 24 + clean

    def _encode_status_ret(self, active: bool, since: int) -> str:
        w0 = (1 if active else 0).to_bytes(32, "big").hex()
        w1 = since.to_bytes(32, "big").hex()
        return w0 + w1

    def test_dynamic_vault_resolution_non_default_address(self):
        """1. When vault() returns non-default address, status query is sent to that address, never to DEFAULT_VAULT_CONTRACT."""
        calls = []

        def mock_eth_call(rpc_url, to, data):
            calls.append((to.lower(), data))
            if to.lower() == self.MINER_PROXY_CUSTOM.lower() and data == miner.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_NON_DEFAULT)
            if to.lower() == self.VAULT_NON_DEFAULT.lower() and data.startswith(miner.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=0)
            return ""

        with patch("script.miner.eth_call", side_effect=mock_eth_call):
            vault_res, active, since = miner.verify_stake(
                "http://mock-rpc", self.MINER_PROXY_CUSTOM, self.MINER_ADDR, cur_epoch=1
            )
            self.assertEqual(vault_res.lower(), self.VAULT_NON_DEFAULT.lower())
            self.assertTrue(active)
            self.assertEqual(since, 0)

            queried_addresses = [c[0] for c in calls]
            self.assertIn(self.MINER_PROXY_CUSTOM.lower(), queried_addresses)
            self.assertIn(self.VAULT_NON_DEFAULT.lower(), queried_addresses)
            self.assertNotIn(miner.DEFAULT_VAULT_CONTRACT.lower(), queried_addresses)

    def test_vault_call_failure_aborts_without_fallback(self):
        """2. When vault() call fails, returns empty, or returns zero address, it raises RuntimeError and never falls back."""
        # 2a. RPC call error / exception
        with patch("script.miner.eth_call", side_effect=Exception("RPC timeout")):
            with self.assertRaises(RuntimeError) as ctx:
                miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
            self.assertIn("RPC call failed while querying vault()", str(ctx.exception))

        # 2b. Returns empty / None
        for empty_val in ["", None, "0x"]:
            with patch("script.miner.eth_call", return_value=empty_val):
                with self.assertRaises(RuntimeError) as ctx:
                    miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
                self.assertTrue("empty response" in str(ctx.exception) or "malformed ABI" in str(ctx.exception))

        # 2c. Returns zero address
        zero_addr_abi = "0x" + "0" * 64
        with patch("script.miner.eth_call", return_value=zero_addr_abi):
            with self.assertRaises(RuntimeError) as ctx:
                miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
            self.assertIn("zero address", str(ctx.exception))

        # 2d. Malformed length
        with patch("script.miner.eth_call", return_value="0x1234"):
            with self.assertRaises(RuntimeError) as ctx:
                miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
            self.assertIn("malformed ABI data length", str(ctx.exception))

        # 2e. Non-hex characters
        bad_hex = "0" * 24 + "zz" + self.VAULT_NON_DEFAULT[4:].lower()
        with patch("script.miner.eth_call", return_value=bad_hex):
            with self.assertRaises(RuntimeError) as ctx:
                miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
            self.assertIn("invalid hex encoding", str(ctx.exception))

        # 2f. Non-zero high 12 bytes
        bad_high = "0" * 22 + "01" + self.VAULT_NON_DEFAULT[2:].lower()
        with patch("script.miner.eth_call", return_value=bad_high):
            with self.assertRaises(RuntimeError) as ctx:
                miner.get_miner_vault("http://mock-rpc", self.MINER_PROXY_CUSTOM)
            self.assertIn("high 12 bytes are non-zero", str(ctx.exception))

    def test_default_path_normal_behavior(self):
        """3. Default path behaves normally when miner contract and vault return valid responses."""
        def mock_eth_call(rpc_url, to, data):
            if data == miner.SELECTOR_VAULT:
                return self._encode_address_ret(miner.DEFAULT_VAULT_CONTRACT)
            if data.startswith(miner.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=0)
            return ""

        with patch("script.miner.eth_call", side_effect=mock_eth_call):
            vault, active, since = miner.verify_stake(
                "http://mock-rpc", miner.DEFAULT_MINER_CONTRACT, self.MINER_ADDR, cur_epoch=1
            )
            self.assertTrue(active)
            self.assertEqual(since, 0)
            self.assertEqual(vault.lower(), miner.DEFAULT_VAULT_CONTRACT.lower())

    def test_not_staked_and_warmup_errors(self):
        """4. Inactive stake or insufficient warmup raises RuntimeError."""
        # Inactive stake
        def mock_inactive(rpc_url, to, data):
            if data == miner.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_NON_DEFAULT)
            if data.startswith(miner.SELECTOR_STATUS):
                return self._encode_status_ret(active=False, since=0)
            return ""

        with patch("script.miner.eth_call", side_effect=mock_inactive):
            with self.assertRaises(RuntimeError) as ctx:
                miner.verify_stake("http://mock-rpc", self.MINER_PROXY_CUSTOM, self.MINER_ADDR, cur_epoch=1)
            self.assertIn("does not have active stake", str(ctx.exception))

        # Warmup insufficient (deposited in epoch 2, current epoch is 2; needs epoch >= 3)
        def mock_warmup(rpc_url, to, data):
            if data == miner.SELECTOR_VAULT:
                return self._encode_address_ret(self.VAULT_NON_DEFAULT)
            if data.startswith(miner.SELECTOR_STATUS):
                return self._encode_status_ret(active=True, since=2)
            return ""

        with patch("script.miner.eth_call", side_effect=mock_warmup):
            with self.assertRaises(RuntimeError) as ctx:
                miner.verify_stake("http://mock-rpc", self.MINER_PROXY_CUSTOM, self.MINER_ADDR, cur_epoch=2)
            self.assertIn("warmup requires epoch >= 3", str(ctx.exception))

    def test_mine_lifecycle_aborts_on_failed_stake_check(self):
        """5. mine() lifecycle invokes verify_stake using passed args.miner_contract and terminates on failure."""
        args = MagicMock()
        args.miner = self.MINER_ADDR
        args.miner_contract = self.MINER_PROXY_CUSTOM
        args.epoch = 1
        args.rpc = "http://mock-rpc"
        args.skip_stake_check = False

        with patch("script.miner.verify_chain_id", return_value=97), \
             patch("script.miner.verify_stake", side_effect=RuntimeError("Stake check failed: no stake")) as mock_vs:
            with self.assertRaises(RuntimeError) as ctx:
                miner.mine(args)
            self.assertIn("Stake check failed: no stake", str(ctx.exception))
            mock_vs.assert_called_once_with("http://mock-rpc", self.MINER_PROXY_CUSTOM, miner.to_checksum_address(self.MINER_ADDR), 1)

    def test_mine_lifecycle_skips_stake_check_when_flag_set(self):
        """6. mine() lifecycle skips verify_stake when skip_stake_check is True."""
        class DummyArgs:
            miner = self.MINER_ADDR
            miner_contract = self.MINER_PROXY_CUSTOM
            rpc = "http://mock-rpc"
            epoch = 1
            skip_stake_check = True
            k = 0
            seed = "0x" + "12" * 32
            target = hex(1 << 224)
            difficulty_bits = 32
            start_nonce = 0
            out = None
            batch_size = 1024
            threads = 1

        with patch("script.miner.verify_chain_id", return_value=97), \
             patch("script.miner.verify_stake") as mock_vs, \
             patch("script.miner.mp.Process", side_effect=KeyboardInterrupt):
            try:
                miner.mine(DummyArgs())
            except KeyboardInterrupt:
                pass
            mock_vs.assert_not_called()

if __name__ == "__main__":
    unittest.main()
