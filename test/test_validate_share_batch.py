#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline Unit and Integration Tests for script/validate_share_batch.py

Safety constraints:
- Purely offline: No RPC, no network calls, no wallet, no private keys.
- Uses synthetic low-difficulty proofs with fictitious addresses and parameters.
- All test parameters (epoch, seed, target, miner address) are purely illustrative schematics,
  not actual on-chain query results.
- Actual rc2 Miner proxy contract: 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf.
- Never reads real production share files.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from decimal import Decimal
from typing import Dict, Any, List, Tuple

_SENTINEL = object()

# Independent reference dependencies check
try:
    import eth_abi
    HAS_TEST_ETH_ABI = True
except ImportError:
    HAS_TEST_ETH_ABI = False

try:
    from Crypto.Hash import keccak as pycrypto_keccak
    def independent_keccak256(data: bytes) -> bytes:
        h = pycrypto_keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
    HAS_TEST_KECCAK = True
except ImportError:
    try:
        from eth_hash.auto import keccak as eth_keccak
        def independent_keccak256(data: bytes) -> bytes:
            return eth_keccak(data)
        HAS_TEST_KECCAK = True
    except ImportError:
        HAS_TEST_KECCAK = False

if not HAS_TEST_ETH_ABI or not HAS_TEST_KECCAK:
    raise RuntimeError(
        "CRITICAL ERROR: Required independent reference dependencies (eth_abi and Keccak implementation) "
        "are not available. Halting as instructed by safety requirements."
    )

import script.validate_share_batch as vsb
from script.validate_share_batch import (
    REQUIRED_CHAIN_ID,
    CAP,
    BATCH_SIZE,
    RC2_MINER_PROXY,
    compute_sub_hash,
    verify_share_pow,
    validate_batch,
    validate_single_share_file,
    encode_submit_shares,
    normalize_address,
    normalize_bytes32,
    normalize_uint256,
)


def reference_compute_sub_hash(
    epoch_seed_bytes: bytes,
    miner_address: str,
    k: int,
    miner_contract: str,
    chain_id: int
) -> bytes:
    """
    Independent reference computation of sub-hash using eth_abi.encode:
    ["bytes32", "address", "uint256", "address", "uint256"]
    followed by independent Keccak-256.
    """
    norm_miner = normalize_address(miner_address)
    norm_contract = normalize_address(miner_contract)
    encoded = eth_abi.encode(
        ["bytes32", "address", "uint256", "address", "uint256"],
        [epoch_seed_bytes, norm_miner, k, norm_contract, chain_id]
    )
    return independent_keccak256(encoded)


def reference_verify_share_pow(
    sub_bytes: bytes,
    nonce: int,
    target_int: int
) -> Tuple[bool, bytes, int]:
    """
    Independent reference verification of PoW outer hash using eth_abi.encode:
    ["bytes32", "uint256"]
    followed by independent Keccak-256.
    """
    encoded = eth_abi.encode(["bytes32", "uint256"], [sub_bytes, nonce])
    h_bytes = independent_keccak256(encoded)
    h_int = int.from_bytes(h_bytes, "big")
    return (h_int <= target_int, h_bytes, h_int)


class TestValidateShareBatch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_poolcoin_batch_")
        self.chain_id = 97
        # Fictitious miner containing letters a-f to test case normalization
        self.fictitious_miner = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"
        self.rc2_proxy = RC2_MINER_PROXY  # 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf
        self.epoch = 42
        self.seed_bytes = bytes.fromhex("1234567890abcdef" * 4)
        self.seed_hex = "0x" + self.seed_bytes.hex()
        # Easy target: all 1s (2^256 - 1), so any proof passes difficulty check
        self.easy_target_int = (1 << 256) - 1
        self.easy_target_hex = "0x" + self.easy_target_int.to_bytes(32, "big").hex()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_synthetic_share_dict(
        self,
        k: int,
        nonce: int,
        miner: str = None,
        proxy: str = None,
        chain_id: int = None,
        epoch: int = None,
        seed_bytes: bytes = None,
        target_int: int = None,
        tamper_hash: str = None,
        include_nonce_field: bool = True,
        nonce_field_value: Any = _SENTINEL
    ) -> Dict[str, Any]:
        """
        Generates a mathematically valid synthetic share dictionary using the INDEPENDENT
        reference computation (eth_abi + independent Keccak-256) to eliminate circular self-proof.
        """
        miner = miner or self.fictitious_miner
        proxy = proxy or self.rc2_proxy
        chain_id = chain_id if chain_id is not None else self.chain_id
        epoch = epoch if epoch is not None else self.epoch
        seed_bytes = seed_bytes if seed_bytes is not None else self.seed_bytes
        target_int = target_int if target_int is not None else self.easy_target_int

        sub_bytes = reference_compute_sub_hash(
            epoch_seed_bytes=seed_bytes,
            miner_address=miner,
            k=k,
            miner_contract=proxy,
            chain_id=chain_id
        )
        _, h_bytes, _ = reference_verify_share_pow(sub_bytes, nonce, target_int)
        h_hex = tamper_hash if tamper_hash is not None else ("0x" + h_bytes.hex())

        d: Dict[str, Any] = {
            "miner": miner,
            "minerContract": proxy,
            "chainId": chain_id,
            "epoch": epoch,
            "k": k,
            "epochSeed": "0x" + seed_bytes.hex(),
            "target": "0x" + target_int.to_bytes(32, "big").hex(),
            "difficultyBits": 0,
            "actualDifficultyBits": 0,
            "nonceDecimal": nonce,
            "hash": h_hex,
            "timestamp": 1700000000,
            "elapsedSeconds": 0.01,
            "hashesChecked": 1,
            "averageHashrate": 100.0,
        }
        if include_nonce_field:
            d["nonce"] = hex(nonce) if nonce_field_value is _SENTINEL else nonce_field_value
        return d

    def _write_share_file(self, data: Dict[str, Any], filename: str) -> str:
        fp = os.path.join(self.temp_dir, filename)

        class DecimalEncoder(json.JSONEncoder):
            def default(self, o):
                if isinstance(o, Decimal):
                    return float(o)
                return super().default(o)

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, cls=DecimalEncoder)
        return fp

    def _generate_valid_batch_files(self, reverse_order: bool = False) -> List[str]:
        files = []
        indices = list(range(BATCH_SIZE))
        if reverse_order:
            indices = list(reversed(indices))

        for idx in indices:
            data = self._create_synthetic_share_dict(k=idx, nonce=1000 + idx)
            fp = self._write_share_file(data, f"synthetic_share_k{idx}.json")
            files.append(fp)
        return files
    # -------------------------------------------------------------------------
    # Requirement 3: Independent verification of proof encoding
    # -------------------------------------------------------------------------
    def test_proof_encoding_against_independent_reference(self):
        """
        Covers Requirement 3:
        Independent reference calculation using installed eth_abi for
        ['bytes32','address','uint256','address','uint256'] and ['bytes32','uint256'],
        combined with independent Keccak-256, compared against compute_sub_hash and verify_share_pow.
        """
        test_seeds = [
            bytes.fromhex("00" * 32),
            bytes.fromhex("ff" * 32),
            bytes.fromhex("1234567890abcdef" * 4),
        ]
        test_users = [
            "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01",
            "0x0000000000000000000000000000000000000001",
            "0xffffffffffffffffffffffffffffffffffffffff",
        ]
        test_contracts = [
            self.rc2_proxy,
            "0x1111111111111111111111111111111111111111",
        ]
        test_nonces = [
            0,
            1,
            (1 << 53) + 1,
            1 << 255,
            (1 << 256) - 1,
        ]

        # 1. Compare sub_hash calculation across various inputs and k=0..5
        for seed in test_seeds:
            for user in test_users:
                for contract in test_contracts:
                    for k in range(CAP):
                        tested_sub = compute_sub_hash(seed, user, k, contract, self.chain_id)
                        ref_sub = reference_compute_sub_hash(seed, user, k, contract, self.chain_id)
                        self.assertEqual(tested_sub, ref_sub, f"sub_hash mismatch for k={k}, user={user}")

        # 2. Compare verify_share_pow calculation across various nonces and targets
        sub_sample = test_seeds[2]
        test_targets = [0, 1, 1 << 128, (1 << 256) - 1]
        for n in test_nonces:
            for tgt in test_targets:
                tested_valid, tested_bytes, tested_int = verify_share_pow(sub_sample, n, tgt)
                ref_valid, ref_bytes, ref_int = reference_verify_share_pow(sub_sample, n, tgt)
                self.assertEqual(tested_valid, ref_valid)
                self.assertEqual(tested_bytes, ref_bytes)
                self.assertEqual(tested_int, ref_int)

    # -------------------------------------------------------------------------
    # Requirement 4: Independent verification of full calldata
    # -------------------------------------------------------------------------
    def test_calldata_encoding_against_independent_reference(self):
        """
        Covers Requirement 4:
        - Independent calculation of selector from canonical signature 'submitShares(uint8[],uint256[])'.
        - Full expected calldata constructed via independent eth_abi.encode.
        - Byte-by-byte comparison against encode_submit_shares (not just prefix and length).
        - Explicitly covers both HAS_ETH_ABI=True and HAS_ETH_ABI=False branches.
        - Nonces must include: 0, 2^53+1, 2^255, 2^256-1.
        - ks uses 0..5.
        """
        # Independent selector calculation (never using tested constant)
        expected_selector = independent_keccak256(b"submitShares(uint8[],uint256[])")[:4]
        self.assertEqual(expected_selector, bytes.fromhex("6efb5ee9"))

        ks = [0, 1, 2, 3, 4, 5]
        nonces = [
            0,                  # Boundary: 0
            (1 << 53) + 1,      # JavaScript MAX_SAFE_INTEGER + 2 precision boundary
            1 << 255,           # High bit uint256
            (1 << 256) - 1,     # Max uint256
            42,                 # Arbitrary mid value
            999999999999999999, # Large integer
        ]

        # Construct full expected calldata independently using eth_abi
        expected_body = eth_abi.encode(["uint8[]", "uint256[]"], [ks, nonces])
        expected_full_calldata = expected_selector + expected_body

        orig_flag = vsb.HAS_ETH_ABI
        try:
            # 1. Test HAS_ETH_ABI = True branch
            vsb.HAS_ETH_ABI = True
            calldata_true = encode_submit_shares(ks, nonces)
            self.assertEqual(calldata_true, expected_full_calldata)
            self.assertEqual(len(calldata_true), len(expected_full_calldata))

            # 2. Test HAS_ETH_ABI = False branch (manual fallback)
            vsb.HAS_ETH_ABI = False
            calldata_false = encode_submit_shares(ks, nonces)
            self.assertEqual(calldata_false, expected_full_calldata)
            self.assertEqual(len(calldata_false), len(expected_full_calldata))

            # 3. Direct byte-by-byte equality between both paths and independent reference
            self.assertEqual(calldata_true, calldata_false)
        finally:
            vsb.HAS_ETH_ABI = orig_flag
    # -------------------------------------------------------------------------
    # Requirement 1: Optional nonce type leak rejection & no output on failure
    # -------------------------------------------------------------------------
    def test_optional_nonce_absent_is_allowed(self):
        """Covers Requirement 1: When nonce field is absent, validation succeeds."""
        files = []
        for idx in range(BATCH_SIZE):
            data = self._create_synthetic_share_dict(
                k=idx, nonce=1000 + idx, include_nonce_field=False
            )
            files.append(self._write_share_file(data, f"no_nonce_k{idx}.json"))

        out_path = os.path.join(self.temp_dir, "no_nonce_out.json")
        res = validate_batch(
            files=files,
            chain_id=self.chain_id,
            proxy=self.rc2_proxy,
            miner=self.fictitious_miner,
            epoch=self.epoch,
            seed=self.seed_hex,
            target=self.easy_target_hex,
            out_path=out_path
        )
        self.assertEqual(res["ks"], list(range(6)))
        self.assertTrue(os.path.isfile(out_path))

    def test_nonce_type_rejection_branches_and_no_output(self):
        """
        Covers Requirement 1:
        When nonce field exists:
        - Only allows non-bool int or exact integer string.
        - Rejects bool (True, False), null (None), float, Decimal, list, dict.
        - Rejects out of uint256 bounds.
        - Rejects nonces not matching nonceDecimal.
        - Rejects floats disguised as strings ("123.0", "1e5") without int(float) acceptance.
        - Verifies via validate_batch that failure does NOT produce any output file.
        """
        invalid_nonces = [
            ("bool_true", True),
            ("bool_false", False),
            ("null_val", None),
            ("float_val", 12345.0),
            ("float_zero", 0.0),
            ("decimal_val", Decimal("12345.5")),
            ("list_val", [12345]),
            ("dict_val", {"n": 12345}),
            ("negative_int", -1),
            ("overflow_int", 1 << 256),
            ("string_float", "12345.0"),
            ("string_float_fraction", "12345.67"),
            ("string_scientific", "1e10"),
            ("string_spaces_internal", "12 345"),
            ("string_invalid_chars", "12345abc"),
            ("string_empty", ""),
            ("hex_internal_space", "0x12 34"),
            ("hex_invalid_char", "0xZZ"),
            ("hex_empty", "0x"),
            ("hex_negative", "-0x10"),
            ("mismatch_with_decimal", "99999"),  # nd is 1000, string is 99999
        ]

        for label, bad_nonce in invalid_nonces:
            with self.subTest(label=label, bad_nonce=bad_nonce):
                bad_data = self._create_synthetic_share_dict(
                    k=0,
                    nonce=1000,
                    include_nonce_field=True,
                    nonce_field_value=bad_nonce
                )
                bad_fp = self._write_share_file(bad_data, f"bad_nonce_{label}.json")

                # 1. validate_single_share_file must reject
                with self.assertRaises(ValueError):
                    validate_single_share_file(
                        bad_fp, self.chain_id, self.rc2_proxy.lower(),
                        self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
                    )

                # 2. validate_batch must fail and produce NO output file
                out_path = os.path.join(self.temp_dir, f"out_{label}.json")
                files = [bad_fp]
                for idx in range(1, BATCH_SIZE):
                    valid_data = self._create_synthetic_share_dict(k=idx, nonce=1000 + idx)
                    files.append(self._write_share_file(valid_data, f"valid_{label}_k{idx}.json"))

                with self.assertRaises(ValueError):
                    validate_batch(
                        files=files,
                        chain_id=self.chain_id,
                        proxy=self.rc2_proxy,
                        miner=self.fictitious_miner,
                        epoch=self.epoch,
                        seed=self.seed_hex,
                        target=self.easy_target_hex,
                        out_path=out_path
                    )
                self.assertFalse(os.path.exists(out_path), f"Output file should NOT exist on failure: {out_path}")
    # -------------------------------------------------------------------------
    # Requirement 2: Strict normalization of seed / address
    # -------------------------------------------------------------------------
    def test_strict_normalize_bytes32(self):
        """
        Covers Requirement 2:
        - Preserves legal integer and short hex string padding.
        - Allows outer whitespace strip.
        - Rejects internal whitespace.
        - Rejects non-ASCII hex / illegal characters.
        - Guarantees len(result) == 32.
        """
        # 1. Valid integers
        res_0 = normalize_bytes32(0)
        self.assertEqual(res_0, b"\x00" * 32)
        self.assertEqual(len(res_0), 32)

        res_max = normalize_bytes32((1 << 256) - 1)
        self.assertEqual(res_max, b"\xff" * 32)
        self.assertEqual(len(res_max), 32)

        # 2. Valid short hex string padding & outer strip
        res_short = normalize_bytes32("  0x1234  ")
        self.assertEqual(res_short, bytes.fromhex("1234").rjust(32, b"\x00"))
        self.assertEqual(len(res_short), 32)

        res_noprefix = normalize_bytes32("1")
        self.assertEqual(res_noprefix, b"\x00" * 31 + b"\x01")
        self.assertEqual(len(res_noprefix), 32)

        # 3. Rejects internal whitespace
        with self.assertRaises(ValueError):
            normalize_bytes32("0x12 34")
        with self.assertRaises(ValueError):
            normalize_bytes32("12\t34")
        with self.assertRaises(ValueError):
            normalize_bytes32("12\n34")

        # 4. Rejects non-ASCII and non-hex characters
        with self.assertRaises(ValueError):
            normalize_bytes32("0x123g")
        with self.assertRaises(ValueError):
            normalize_bytes32("0x1234\u00e9")

        # 5. Rejects overlong (> 64 hex characters)
        with self.assertRaises(ValueError):
            normalize_bytes32("0x" + "1" * 65)

        # 6. Rejects empty, bool, and invalid types
        with self.assertRaises(ValueError):
            normalize_bytes32("")
        with self.assertRaises(ValueError):
            normalize_bytes32(True)
        with self.assertRaises(ValueError):
            normalize_bytes32(False)
        with self.assertRaises(ValueError):
            normalize_bytes32(-1)
        with self.assertRaises(ValueError):
            normalize_bytes32(1 << 256)

    def test_strict_normalize_address(self):
        """
        Covers Requirement 2:
        - Only accepts exactly 40 ASCII hex characters (optional 0x/0X, outer strip allowed).
        - Does not rely solely on int(s, 16).
        - Rejects internal whitespace, illegal chars, overlength, underlength.
        - Address casing test: uses fictitious address with letters a-f,
          verifying file address and explicit expected address with different casing still passes.
        """
        # 1. Valid address with outer whitespace and case variation
        addr_raw = "  0xAbCdEf0123456789aBcDeF0123456789AbCdEf01  "
        norm = normalize_address(addr_raw)
        self.assertEqual(norm, "0xabcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual(len(norm), 42)

        # 2. Rejects internal whitespace
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 20 + " " + "b" * 19)
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 20 + "\t" + "b" * 19)

        # 3. Rejects illegal characters (signs, g-z, unicode)
        with self.assertRaises(ValueError):
            normalize_address("+0000000000000000000000000000000000000001")
        with self.assertRaises(ValueError):
            normalize_address("0x" + "g" * 40)
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 39 + "\u00e9")

        # 4. Rejects overlength and underlength
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 41)
        with self.assertRaises(ValueError):
            normalize_address("0x" + "a" * 39)

        # 5. Address casing test: file address vs explicit expected address with different casing
        file_miner_upper = "0xABCDEF0123456789ABCDEF0123456789ABCDEF01"
        expected_miner_lower = "0xabcdef0123456789abcdef0123456789abcdef01"

        data = self._create_synthetic_share_dict(k=0, nonce=1000, miner=file_miner_upper)
        fp = self._write_share_file(data, "case_test_miner.json")

        # Passing expected address with completely different casing must succeed
        res = validate_single_share_file(
            file_path=fp,
            expected_chain_id=self.chain_id,
            expected_proxy_norm=normalize_address(self.rc2_proxy),
            expected_miner_norm=normalize_address(expected_miner_lower),
            expected_epoch=self.epoch,
            expected_seed_bytes=self.seed_bytes,
            expected_target_int=self.easy_target_int
        )
        self.assertEqual(res["k"], 0)

    # -------------------------------------------------------------------------
    # Batch Validation & Sorting Tests
    # -------------------------------------------------------------------------
    def test_valid_batch_success_and_sorted_by_k(self):
        """Covers: Six valid proofs succeed and are sorted by k in ascending order (0..5)."""
        files = []
        order = [4, 2, 0, 5, 1, 3]
        for k_idx in order:
            data = self._create_synthetic_share_dict(k=k_idx, nonce=10000 + k_idx)
            files.append(self._write_share_file(data, f"share_k{k_idx}.json"))

        out_path = os.path.join(self.temp_dir, "batch_out.json")
        result = validate_batch(
            files=files,
            chain_id=self.chain_id,
            proxy=self.rc2_proxy,
            miner=self.fictitious_miner,
            epoch=self.epoch,
            seed=self.seed_hex,
            target=self.easy_target_hex,
            out_path=out_path
        )

        self.assertEqual(result["ks"], [0, 1, 2, 3, 4, 5])
        self.assertEqual(result["nonces"], [10000 + k for k in range(6)])
        self.assertTrue(result["calldata"].startswith("0x6efb5ee9"))
        self.assertTrue(os.path.isfile(out_path))

    def test_duplicate_and_missing_k(self):
        """Covers: Duplicate k, missing k, and incomplete batch coverage."""
        files = []
        for k_idx in [0, 1, 2, 3, 4, 4]:  # Duplicate 4, missing 5
            data = self._create_synthetic_share_dict(k=k_idx, nonce=1000 + k_idx)
            files.append(self._write_share_file(data, f"dup_share_{len(files)}.json"))

        out_path = os.path.join(self.temp_dir, "batch_dup_out.json")
        with self.assertRaises(ValueError) as ctx:
            validate_batch(
                files=files,
                chain_id=self.chain_id,
                proxy=self.rc2_proxy,
                miner=self.fictitious_miner,
                epoch=self.epoch,
                seed=self.seed_hex,
                target=self.easy_target_hex,
                out_path=out_path
            )
        self.assertIn("Duplicate share index k=4", str(ctx.exception))
        self.assertFalse(os.path.exists(out_path))

    def test_duplicate_input_file_paths(self):
        """Covers: Duplicate file paths in input arguments are rejected."""
        files = self._generate_valid_batch_files()
        dup_files = [files[0]] * 6
        out_path = os.path.join(self.temp_dir, "batch_dup_paths_out.json")
        with self.assertRaises(ValueError) as ctx:
            validate_batch(
                files=dup_files,
                chain_id=self.chain_id,
                proxy=self.rc2_proxy,
                miner=self.fictitious_miner,
                epoch=self.epoch,
                seed=self.seed_hex,
                target=self.easy_target_hex,
                out_path=out_path
            )
        self.assertIn("Duplicate input file paths detected", str(ctx.exception))
        self.assertFalse(os.path.exists(out_path))

    def test_float_or_scientific_notation_rejection(self):
        """Covers: Floating-point or scientific notation in JSON is rejected without precision loss."""
        float_fields = [
            ("epoch", 42.0),
            ("chainId", 97.0),
            ("k", 3.0),
            ("nonceDecimal", 12345.0),
        ]
        for field, bad_val in float_fields:
            data = self._create_synthetic_share_dict(k=0, nonce=1000)
            data[field] = bad_val
            fp = self._write_share_file(data, f"float_{field}.json")

            with self.assertRaises(ValueError) as ctx:
                validate_single_share_file(
                    fp, self.chain_id, self.rc2_proxy.lower(),
                    self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
                )
            self.assertIn("cannot be float", str(ctx.exception))

    def test_missing_required_fields(self):
        """Covers: Missing required fields in JSON are rejected."""
        required_fields = [
            "miner", "minerContract", "chainId", "epoch", "k",
            "epochSeed", "target", "nonceDecimal", "hash"
        ]
        for rf in required_fields:
            data = self._create_synthetic_share_dict(k=0, nonce=1000)
            del data[rf]
            fp = self._write_share_file(data, f"missing_{rf}.json")
            with self.assertRaises(ValueError) as ctx:
                validate_single_share_file(
                    fp, self.chain_id, self.rc2_proxy.lower(),
                    self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
                )
            self.assertIn(f"Missing required field {rf!r}", str(ctx.exception))
    def test_field_type_errors(self):
        """Covers: Field type errors (bool for int, string for k, etc.)."""
        type_errors = [
            ("k", True, "must be integer"),
            ("k", "0", "must be integer"),
            ("epoch", False, "must be integer"),
            ("chainId", True, "must be integer"),
            ("miner", 12345, "must be a string"),
            ("minerContract", None, "must be a string"),
            ("hash", 12345, "must be string"),
        ]
        for field, bad_val, err_msg in type_errors:
            data = self._create_synthetic_share_dict(k=0, nonce=1000)
            data[field] = bad_val
            fp = self._write_share_file(data, f"type_err_{field}.json")
            with self.assertRaises(ValueError) as ctx:
                validate_single_share_file(
                    fp, self.chain_id, self.rc2_proxy.lower(),
                    self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
                )
            self.assertIn(err_msg, str(ctx.exception))

    def test_parameter_mismatches(self):
        """Covers: chainId / proxy / miner / epoch / seed / target mismatches."""
        mismatches = [
            ("chainId", 56, "chainId 56 != 97"),
            ("miner", "0x9999999999999999999999999999999999999999", "miner address value"),
            ("minerContract", "0x8888888888888888888888888888888888888888", "proxy address value"),
            ("epoch", 99, "epoch 99 != expected epoch 42"),
            ("epochSeed", "0x" + "00" * 32, "epochSeed"),
            ("target", "0x" + "00" * 32, "target"),
        ]
        for field, bad_val, err_msg in mismatches:
            data = self._create_synthetic_share_dict(k=0, nonce=1000)
            data[field] = bad_val
            fp = self._write_share_file(data, f"mismatch_{field}.json")
            with self.assertRaises(ValueError) as ctx:
                validate_single_share_file(
                    fp, self.chain_id, self.rc2_proxy.lower(),
                    self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
                )
            self.assertIn(err_msg, str(ctx.exception))

    def test_declared_hash_mismatch_with_recomputed(self):
        """Covers: Tampered hash string in file does not match recomputed hash."""
        tampered_hash = "0x" + "00" * 32
        data = self._create_synthetic_share_dict(k=0, nonce=1000, tamper_hash=tampered_hash)
        fp = self._write_share_file(data, "tampered_hash.json")
        with self.assertRaises(ValueError) as ctx:
            validate_single_share_file(
                fp, self.chain_id, self.rc2_proxy.lower(),
                self.fictitious_miner.lower(), self.epoch, self.seed_bytes, self.easy_target_int
            )
        self.assertIn("Hash mismatch! File declared", str(ctx.exception))

    def test_target_boundary_conditions(self):
        """
        Covers: Target difficulty boundary condition uint256(h) <= target:
        - When computed hash int == target int: MUST SUCCEED (<= condition).
        - When computed hash int == target int + 1 (i.e. target = h_int - 1): MUST FAIL.
        """
        k = 0
        nonce = 12345
        sub_bytes = reference_compute_sub_hash(
            epoch_seed_bytes=self.seed_bytes,
            miner_address=self.fictitious_miner,
            k=k,
            miner_contract=self.rc2_proxy,
            chain_id=self.chain_id
        )
        _, h_bytes, h_int = reference_verify_share_pow(sub_bytes, nonce, self.easy_target_int)

        # 1. Exact boundary: target == h_int
        data_exact = self._create_synthetic_share_dict(k=k, nonce=nonce, target_int=h_int)
        fp_exact = self._write_share_file(data_exact, "exact_target.json")
        res = validate_single_share_file(
            fp_exact, self.chain_id, self.rc2_proxy.lower(),
            self.fictitious_miner.lower(), self.epoch, self.seed_bytes, h_int
        )
        self.assertEqual(res["hashInt"], h_int)

        # 2. Boundary off by 1: target == h_int - 1 (h_int > target)
        strict_target_int = h_int - 1
        data_fail = self._create_synthetic_share_dict(k=k, nonce=nonce, target_int=strict_target_int)
        fp_fail = self._write_share_file(data_fail, "target_exceeded.json")
        with self.assertRaises(ValueError) as ctx:
            validate_single_share_file(
                fp_fail, self.chain_id, self.rc2_proxy.lower(),
                self.fictitious_miner.lower(), self.epoch, self.seed_bytes, strict_target_int
            )
        self.assertIn("PoW difficulty check failed", str(ctx.exception))

    def test_refuse_overwrite_existing_output(self):
        """Covers: Refuses to overwrite existing output file using exclusive creation."""
        files = self._generate_valid_batch_files()
        out_path = os.path.join(self.temp_dir, "already_exists.json")

        # Pre-create the output file
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("existing content\n")

        with self.assertRaises(FileExistsError) as ctx:
            validate_batch(
                files=files,
                chain_id=self.chain_id,
                proxy=self.rc2_proxy,
                miner=self.fictitious_miner,
                epoch=self.epoch,
                seed=self.seed_hex,
                target=self.easy_target_hex,
                out_path=out_path
            )
        self.assertIn("already exists", str(ctx.exception))

        # Check existing content was NOT overwritten
        with open(out_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "existing content\n")

    # -------------------------------------------------------------------------
    # Requirements 5 & 6: CLI execution, security notice, removed broadcast command
    # -------------------------------------------------------------------------
    def test_cli_execution_success_and_failure(self):
        """
        Covers Requirements 5 & 6:
        - Output does NOT contain '--private-key' or 'cast send'.
        - Success output contains static parameters, ks, nonces, calldata.
        - Mandatory on-chain security notice asserts counted == 0 for full 6-share batch,
          and usedMask & 0x3F == 0.
        - Validates rc2 Miner proxy address (0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf).
        """
        files = self._generate_valid_batch_files()
        out_path = os.path.join(self.temp_dir, "cli_success_out.json")

        cmd_success = [
            sys.executable,
            "script/validate_share_batch.py",
            "--files", *files,
            "--chain-id", str(self.chain_id),
            "--proxy", self.rc2_proxy,
            "--miner", self.fictitious_miner,
            "--epoch", str(self.epoch),
            "--seed", self.seed_hex,
            "--target", self.easy_target_hex,
            "--out", out_path
        ]

        sub_env = os.environ.copy()
        sub_env["PYTHONIOENCODING"] = "utf-8"

        # 1. Success run
        proc = subprocess.run(cmd_success, capture_output=True, text=True, encoding="utf-8", env=sub_env)
        self.assertEqual(proc.returncode, 0, f"CLI stderr: {proc.stderr}")
        self.assertIn("Validation Result:    SUCCESS (6/6 shares verified)", proc.stdout)
        self.assertIn(f"Miner Proxy:          {self.rc2_proxy.lower()}", proc.stdout)
        self.assertIn("Function Arguments for submitShares(uint8[],uint256[]):", proc.stdout)
        self.assertIn("【重要安全警示 (MANDATORY SECURITY NOTICE)】", proc.stdout)
        self.assertIn("该矿工 counted 必须严格为 0", proc.stdout)
        self.assertIn("usedMask & 0x3F == 0", proc.stdout)
        self.assertIn("严禁在未做链上核对的情况下宣称‘离线通过即可发送’！", proc.stdout)

        # Requirement 5 assertions: NEVER output --private-key or cast send
        self.assertNotIn("--private-key", proc.stdout)
        self.assertNotIn("cast send", proc.stdout)

        self.assertTrue(os.path.isfile(out_path))

        # 2. Failure run: chainId mismatch (pass 1 instead of 97)
        fail_out_path = os.path.join(self.temp_dir, "cli_fail_out.json")
        cmd_fail = list(cmd_success)
        cmd_fail[cmd_fail.index("--chain-id") + 1] = "1"
        cmd_fail[cmd_fail.index("--out") + 1] = fail_out_path

        proc_fail = subprocess.run(cmd_fail, capture_output=True, text=True, encoding="utf-8", env=sub_env)
        self.assertNotEqual(proc_fail.returncode, 0)
        self.assertIn("[VALIDATION FAILED]", proc_fail.stderr)
        self.assertFalse(os.path.exists(fail_out_path))


if __name__ == "__main__":
    unittest.main()
