#!/usr/bin/env python3
"""Search a BSC mainnet SparkHash share and emit an unsigned contract call."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import json
import os
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from eth_abi import decode, encode
from eth_hash.auto import keccak


CHAIN_ID = 56
MINER_PROXY = "0x614b5efd21cfa99cb893abf2fb02d791b3aea843"
STAKE_VAULT = "0x45eb4e8a7a3070c8160e9a91fae002934d296591"
READ_METHODS = {"eth_chainId", "eth_getBlockByNumber", "eth_call"}
BATCH_SIZE = 20_000
SIGNING_BUFFER = 60


class SolverError(Exception):
    pass


def calldata(signature, types=(), values=()):
    return "0x" + (keccak(signature.encode("ascii"))[:4] + encode(types, values)).hex()


def read_rpc(url, method, params):
    if method not in READ_METHODS:
        raise SolverError("RPC method is outside the read-only allowlist")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = Request(url, body, {"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
    except (URLError, OSError, ValueError):
        raise SolverError(f"RPC {method} request failed") from None
    if not isinstance(result, dict) or result.get("error") or result.get("result") is None:
        raise SolverError(f"RPC {method} did not return a successful result")
    return result["result"]


def view(rpc, to, signature, outputs, block="latest", types=(), values=()):
    response = rpc("eth_call", [{"to": to, "data": calldata(signature, types, values)}, block])
    return decode(outputs, bytes.fromhex(response[2:]))


@dataclass(frozen=True)
class Task:
    epoch: int
    k: int
    seed: int
    target: int
    seconds_left: int


def read_task(rpc, wallet):
    if int(rpc("eth_chainId", []), 16) != CHAIN_ID:
        raise SolverError("RPC must be BSC mainnet (chain ID 56)")
    block = rpc("eth_getBlockByNumber", ["latest", False])
    block_number = block["number"]

    def miner_uint(signature, types=(), values=()):
        return view(rpc, MINER_PROXY, signature, ["uint256"], block_number, types, values)[0]

    epoch = miner_uint("epoch()")
    active, since = view(rpc, STAKE_VAULT, "status(address)", ["bool", "uint256"],
                         block_number, ["address"], [wallet])
    if not active or epoch <= since:
        raise SolverError("Wallet must have an active stake deposited in an earlier epoch")
    if view(rpc, MINER_PROXY, "paused()", ["bool"], block_number)[0]:
        raise SolverError("Share submissions are paused")

    seed = miner_uint("epochSeed(uint256)", ["uint256"], [epoch])
    target = miner_uint("target(uint256)", ["uint256"], [epoch])
    if not seed or not target or miner_uint("lastSynced()") != epoch:
        raise SolverError("The current epoch has not been initialized and synced")
    mask = view(rpc, MINER_PROXY, "usedMask(uint256,address)", ["uint16"], block_number,
                ["uint256", "address"], [epoch, wallet])[0]
    cap = miner_uint("CAP()")
    k = next((idx for idx in range(cap) if not mask & (1 << idx)), None)
    if k is None:
        raise SolverError("All share indexes for this wallet are used in the current epoch")

    ends_at = miner_uint("START()") + (epoch + 1) * miner_uint("EPOCH_LEN()")
    seconds_left = int(ends_at - max(int(block["timestamp"], 16), time.time()))
    if seconds_left <= SIGNING_BUFFER:
        raise SolverError("Epoch is too close to ending. Start again in the next epoch.")
    return Task(epoch, k, seed, target, seconds_left)


def search_batch(sub_hash, target, start):
    for nonce in range(start, start + BATCH_SIZE):
        digest = keccak(sub_hash + nonce.to_bytes(32, "big"))
        if digest <= target:
            return nonce
    return None


def solve(task, wallet, workers, timeout):
    sub_hash = keccak(encode(
        ["bytes32", "address", "uint256", "address", "uint256"],
        [task.seed.to_bytes(32, "big"), wallet, task.k, MINER_PROXY, CHAIN_ID],
    ))
    target = task.target.to_bytes(32, "big")
    deadline = time.monotonic() + min(timeout, task.seconds_left - SIGNING_BUFFER)
    next_nonce = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while time.monotonic() < deadline:
            starts = range(next_nonce, next_nonce + workers * BATCH_SIZE, BATCH_SIZE)
            jobs = [pool.submit(search_batch, sub_hash, target, start) for start in starts]
            for job in jobs:
                nonce = job.result()
                if nonce is not None:
                    if time.monotonic() >= deadline:
                        raise SolverError("Search ended before a share could be prepared for signing")
                    return nonce
            next_nonce += workers * BATCH_SIZE
    raise SolverError("Search ended without a valid share. Retry in time or start in the next epoch.")


def share_call(task, wallet, nonce):
    return {
        "to": MINER_PROXY,
        "data": calldata("submitShares(uint8[],uint256[])", ["uint8[]", "uint256[]"], [[task.k], [nonce]]),
        "value": "0",
        "summary": f"BSC 56 | {wallet} | submitShares | epoch={task.epoch} | k={task.k} | nonce={nonce}",
    }


def check_share(rpc, task, wallet, tx):
    current_epoch, = view(rpc, MINER_PROXY, "epoch()", ["uint256"])
    if current_epoch != task.epoch:
        raise SolverError("Epoch changed before preflight. Discard this share and start again.")
    result = rpc("eth_call", [{"from": wallet, "to": tx["to"], "data": tx["data"], "value": "0x0"}, "latest"])
    if result != "0x":
        raise SolverError("Unexpected reply from submitShares preflight")


def self_test():
    wallet = "0x1111111111111111111111111111111111111111"
    task = Task(16, 2, 123, (1 << 252) - 1, 3600)
    nonce = solve(task, wallet, workers=1, timeout=10)
    tx = share_call(task, wallet, nonce)
    args = decode(["uint8[]", "uint256[]"], bytes.fromhex(tx["data"][10:]))
    padded = b"".join((
        task.seed.to_bytes(32, "big"), bytes.fromhex(wallet[2:]).rjust(32, b"\x00"),
        task.k.to_bytes(32, "big"), bytes.fromhex(MINER_PROXY[2:]).rjust(32, b"\x00"),
        CHAIN_ID.to_bytes(32, "big"),
    ))
    proof = int.from_bytes(keccak(keccak(padded) + nonce.to_bytes(32, "big")), "big")
    if (nonce != 60 or proof > task.target or args != ((task.k,), (nonce,)) or not tx["data"].startswith("0x6efb5ee9")
            or tx["value"] != "0" or tx["to"] != MINER_PROXY):
        raise SolverError("Offline proof or calldata verification failed")
    return tx


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", help="Public address of the Binance Agentic Wallet")
    parser.add_argument("--rpc", help="BSC mainnet JSON-RPC URL")
    parser.add_argument("--workers", type=positive_int, default=min(os.cpu_count() or 1, 16))
    parser.add_argument("--timeout", type=positive_int, default=600, help="Maximum search duration in seconds")
    parser.add_argument("--self-test", action="store_true", help="Run a deterministic offline proof and calldata test")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test()))
        return 0
    if not args.wallet or not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.wallet) or int(args.wallet, 16) == 0:
        parser.error("--wallet must be a nonzero 20-byte EVM address")
    if not args.rpc or urlsplit(args.rpc).scheme not in ("https", "http") or not urlsplit(args.rpc).hostname:
        parser.error("--rpc must be an HTTP(S) URL")

    def rpc(method, params):
        return read_rpc(args.rpc, method, params)

    task = read_task(rpc, args.wallet)
    print(f"Searching epoch {task.epoch}, k={task.k} with {args.workers} CPU workers", file=sys.stderr)
    nonce = solve(task, args.wallet, args.workers, args.timeout)
    tx = share_call(task, args.wallet, nonce)
    check_share(rpc, task, args.wallet, tx)
    print(json.dumps(tx))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SolverError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"Error: solver could not complete ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
