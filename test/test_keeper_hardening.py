import unittest
from unittest.mock import patch, MagicMock
from script.keeper import KeeperBot

class TestKeeperHardening(unittest.TestCase):
    def setUp(self):
        with patch.object(KeeperBot, "validate_miner", return_value=None), \
             patch.object(KeeperBot, "validate_identity", return_value=None):
            self.bot = KeeperBot(
                miner_address="0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf",
                rpc_url="http://127.0.0.1:8545",
                account=None,
                password_file=None,
                private_key=None,
                dry_run=True,
                state_dir=None,
            )

    def test_default_read_max_retries(self):
        self.assertEqual(getattr(self.bot, "read_max_retries", None), 3)

    @patch("script.keeper.eth_call")
    @patch("time.sleep")
    def test_get_epoch_state_recovers_after_transient_failures(self, mock_sleep, mock_eth_call):
        # Fail twice with transient network error, succeed on 3rd attempt
        # Call sequence per attempt: SELECTOR_EPOCH, SELECTOR_LAST_SYNCED, SELECTOR_EPOCH_SEED
        mock_eth_call.side_effect = [
            RuntimeError("HTTP Error 502: Bad Gateway"),
            RuntimeError("URLError: Connection reset by peer"),
            "0x96",  # epoch 150
            "0x95",  # lastSynced 149
            "0x12345678",  # seed
        ]

        cur_epoch, last_synced, seed = self.bot.get_epoch_state(max_retries=3, initial_backoff=0.1)

        self.assertEqual(cur_epoch, 150)
        self.assertEqual(last_synced, 149)
        self.assertEqual(seed, 0x12345678)
        # Should have slept twice for backoff
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.2)

    @patch("script.keeper.eth_call")
    @patch("time.sleep")
    def test_get_epoch_state_exhausts_retries_and_raises(self, mock_sleep, mock_eth_call):
        # Fails every time
        mock_eth_call.side_effect = RuntimeError("Persistent RPC outage")

        with self.assertRaises(RuntimeError) as ctx:
            self.bot.get_epoch_state(max_retries=3, initial_backoff=0.1)

        self.assertIn("Persistent RPC outage", str(ctx.exception))
        # Slept 2 times before 3rd attempt failed
        self.assertEqual(mock_sleep.call_count, 2)

if __name__ == "__main__":
    unittest.main()
