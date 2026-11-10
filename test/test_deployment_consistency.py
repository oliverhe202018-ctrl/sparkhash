import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import script.claim as claim
import script.miner as miner
import script.miner_gpu as miner_gpu
import script.monitor as monitor
import script.status as status
import script.submit_share as submit_share
import script.validate_share_batch as validate_share_batch


class TestDeploymentConsistency(unittest.TestCase):
    """
    Consistency test to prevent regressions between deployment documentation
    and default contract addresses hardcoded in operational scripts.
    Guarantees that replacing deployment documentation will fail tests if
    scripts are not updated in lockstep.
    Offline only: strictly no network access.
    """

    @classmethod
    def setUpClass(cls):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        doc_path = os.path.join(repo_root, "docs", "deployment-bsc-testnet-rc2.md")
        if not os.path.isfile(doc_path):
            raise FileNotFoundError(f"Deployment document not found: {doc_path}")

        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        miner_match = re.search(
            r"\|\s*\*\*Miner\s*\([^)]*(?:代理|proxy)[^)]*\)\*\*\s*\|[^|]*\|\s*`?(0x[0-9a-fA-F]{40})`?",
            content,
            re.IGNORECASE,
        )
        vault_match = re.search(
            r"\|\s*\*\*StakeVault\*\*\s*\|[^|]*\|\s*`?(0x[0-9a-fA-F]{40})`?",
            content,
        )
        token_match = re.search(
            r"\|\s*\*\*Token(?:\s*\([^)]*\))?\*\*\s*\|[^|]*\|\s*`?(0x[0-9a-fA-F]{40})`?",
            content,
        )

        if not miner_match:
            raise ValueError("Failed to extract Miner (代理) address from deployment-bsc-testnet-rc2.md")
        if not vault_match:
            raise ValueError("Failed to extract StakeVault address from deployment-bsc-testnet-rc2.md")
        if not token_match:
            raise ValueError("Failed to extract Token address from deployment-bsc-testnet-rc2.md")

        cls.doc_miner_proxy = miner_match.group(1)
        cls.doc_vault = vault_match.group(1)
        cls.doc_token = token_match.group(1)

    @staticmethod
    def _get_constant(module, file_rel_path: str, const_name: str) -> str:
        """
        Safely retrieves an address constant from an imported module,
        or statically parses the script file to prevent side-effects (e.g. CLI argument
        parsing or network requests).
        Raises ValueError with a clear explanation if the constant cannot be resolved.
        """
        if module is not None and hasattr(module, const_name):
            val = getattr(module, const_name)
            if val:
                return str(val)

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(repo_root, file_rel_path)
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Script file not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        match = re.search(
            r"^" + re.escape(const_name) + r"\s*=\s*[\x22\x27](0x[0-9a-fA-F]{40})[\x22\x27]",
            content,
            re.MULTILINE,
        )
        if match:
            return match.group(1)

        raise ValueError(f"Failed to resolve address constant '{const_name}' in {file_rel_path}")

    def test_extracted_addresses_valid_format(self):
        eth_addr_pattern = re.compile(r"^0x[0-9a-fA-F]{40}$")
        self.assertTrue(eth_addr_pattern.match(self.doc_miner_proxy))
        self.assertTrue(eth_addr_pattern.match(self.doc_vault))
        self.assertTrue(eth_addr_pattern.match(self.doc_token))

    def test_claim_script_default_address(self):
        addr = self._get_constant(claim, "script/claim.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            addr.lower(),
            self.doc_miner_proxy.lower(),
            "claim.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )

    def test_miner_script_default_addresses(self):
        miner_addr = self._get_constant(miner, "script/miner.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            miner_addr.lower(),
            self.doc_miner_proxy.lower(),
            "miner.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )
        vault_addr = self._get_constant(miner, "script/miner.py", "DEFAULT_VAULT_CONTRACT")
        self.assertEqual(
            vault_addr.lower(),
            self.doc_vault.lower(),
            "miner.py DEFAULT_VAULT_CONTRACT does not match deployment doc",
        )

    def test_miner_gpu_script_default_addresses(self):
        miner_addr = self._get_constant(miner_gpu, "script/miner_gpu.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            miner_addr.lower(),
            self.doc_miner_proxy.lower(),
            "miner_gpu.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )
        vault_addr = self._get_constant(miner_gpu, "script/miner_gpu.py", "DEFAULT_VAULT_CONTRACT")
        self.assertEqual(
            vault_addr.lower(),
            self.doc_vault.lower(),
            "miner_gpu.py DEFAULT_VAULT_CONTRACT does not match deployment doc",
        )

    def test_monitor_script_default_addresses(self):
        miner_addr = self._get_constant(monitor, "script/monitor.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            miner_addr.lower(),
            self.doc_miner_proxy.lower(),
            "monitor.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )
        vault_addr = self._get_constant(monitor, "script/monitor.py", "DEFAULT_VAULT_CONTRACT")
        self.assertEqual(
            vault_addr.lower(),
            self.doc_vault.lower(),
            "monitor.py DEFAULT_VAULT_CONTRACT does not match deployment doc",
        )
        token_addr = self._get_constant(monitor, "script/monitor.py", "DEFAULT_TOKEN_CONTRACT")
        self.assertEqual(
            token_addr.lower(),
            self.doc_token.lower(),
            "monitor.py DEFAULT_TOKEN_CONTRACT does not match deployment doc",
        )

    def test_status_script_default_address(self):
        addr = self._get_constant(status, "script/status.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            addr.lower(),
            self.doc_miner_proxy.lower(),
            "status.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )

    def test_submit_share_script_default_address(self):
        addr = self._get_constant(submit_share, "script/submit_share.py", "DEFAULT_MINER_CONTRACT")
        self.assertEqual(
            addr.lower(),
            self.doc_miner_proxy.lower(),
            "submit_share.py DEFAULT_MINER_CONTRACT does not match deployment doc",
        )

    def test_validate_share_batch_script_constant(self):
        addr = self._get_constant(validate_share_batch, "script/validate_share_batch.py", "RC2_MINER_PROXY")
        self.assertEqual(
            addr.lower(),
            self.doc_miner_proxy.lower(),
            "validate_share_batch.py RC2_MINER_PROXY does not match deployment doc",
        )


if __name__ == "__main__":
    unittest.main()
