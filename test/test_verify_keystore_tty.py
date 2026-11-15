import unittest
from unittest.mock import patch
from script.verify_keystore_tty import verify_tty_keystore
from script.cast_signer import CastSignerError

class TestVerifyKeystoreTty(unittest.TestCase):
    def test_fails_when_no_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            code = verify_tty_keystore("dummy_path", "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
            self.assertEqual(code, 1)

    @patch("script.verify_keystore_tty.get_keystore_address", return_value="0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
    def test_succeeds_on_tty_when_address_matches(self, mock_get):
        with patch("sys.stdin.isatty", return_value=True):
            code = verify_tty_keystore("dummy_path", "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
            self.assertEqual(code, 0)
            mock_get.assert_called_once_with(keystore="dummy_path")

    @patch("script.verify_keystore_tty.get_keystore_address", return_value="0x1111111111111111111111111111111111111111")
    def test_fails_when_address_mismatches(self, mock_get):
        with patch("sys.stdin.isatty", return_value=True):
            code = verify_tty_keystore("dummy_path", "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
            self.assertEqual(code, 1)

    @patch("script.verify_keystore_tty.get_keystore_address", side_effect=CastSignerError("Invalid password"))
    def test_fails_gracefully_on_wrong_password(self, mock_get):
        with patch("sys.stdin.isatty", return_value=True):
            code = verify_tty_keystore("dummy_path", "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
            self.assertEqual(code, 1)

    @patch("script.verify_keystore_tty.get_keystore_address", side_effect=KeyboardInterrupt)
    def test_fails_gracefully_on_user_cancel(self, mock_get):
        with patch("sys.stdin.isatty", return_value=True):
            code = verify_tty_keystore("dummy_path", "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484")
            self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()
