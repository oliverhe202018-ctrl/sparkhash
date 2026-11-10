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

if __name__ == "__main__":
    unittest.main()
