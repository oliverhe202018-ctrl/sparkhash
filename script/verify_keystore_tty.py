#!/usr/bin/env python3
"""
Minimal Read-Only Keystore Interactive TTY Verification Tool.
Reuses get_keystore_address from script/cast_signer.py.

Security Boundaries:
1. Native cast password prompt in host TTY; Python never receives or logs passwords.
2. Does NOT load .env files; isolated environment filters all ETH_* and private keys.
3. Zero network transaction broadcast, zero signing of arbitrary calldata, zero deposit, zero mining.
4. Purely verifies that the provided keystore file can be unlocked via TTY password prompt
   and yields the expected miner address.
"""

import sys
import argparse
from script.cast_signer import (
    get_keystore_address,
    to_checksum_address,
    CastSignerError,
    format_exception_safe,
)

DEFAULT_TEST_MINER = "0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484"

def verify_tty_keystore(keystore_path: str, expected_address: str = DEFAULT_TEST_MINER) -> int:
    if not sys.stdin.isatty():
        print(
            "[ERROR] Interactive TTY required. sys.stdin.isatty() is False.\n"
            "This verification must be run directly in an interactive host terminal (PowerShell or CMD).",
            file=sys.stderr,
        )
        return 1

    expected_clean = to_checksum_address(expected_address)
    print(f"=== Keystore Read-Only TTY Verification ===")
    print(f"Target keystore:  [REDACTED_PATH]")
    print(f"Expected address: {expected_clean}")
    print(f"Please enter keystore password at the native cast prompt below:")
    print("---------------------------------------------------------------", flush=True)

    try:
        derived_address = get_keystore_address(keystore=keystore_path)
    except CastSignerError as e:
        safe_msg = format_exception_safe(e, sensitive_values=[keystore_path])
        print(f"\n[FAIL] Keystore unlock or address derivation failed: {safe_msg}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[CANCELLED] User aborted keystore password prompt. Zero actions performed.")
        return 1

    derived_clean = to_checksum_address(derived_address)
    print(f"\nDerived Address:  {derived_clean}")

    if derived_clean.lower() == expected_clean.lower():
        print(f"[SUCCESS] Keystore successfully unlocked via TTY. Derived address matches expected miner address.")
        return 0
    else:
        print(
            f"[MISMATCH] Derived address {derived_clean} does NOT match expected address {expected_clean}!",
            file=sys.stderr,
        )
        return 1

def main():
    parser = argparse.ArgumentParser(
        description="Verify that an encrypted keystore file can be unlocked interactively via cast in a real TTY without broadcasting."
    )
    parser.add_argument("--keystore", required=True, help="Path to encrypted keystore file")
    parser.add_argument(
        "--expected-address",
        default=DEFAULT_TEST_MINER,
        help=f"Expected address to verify against (default: {DEFAULT_TEST_MINER})",
    )
    args = parser.parse_args()

    sys.exit(verify_tty_keystore(args.keystore, args.expected_address))

if __name__ == "__main__":
    main()
