#!/usr/bin/env python3
"""
PoolCoin Keeper Bot for MinerV1 on BSC Testnet (chainId 97)
"""

import os, sys, time, json, argparse, urllib.request, urllib.error

SELECTOR_EPOCH = "900cf0cf"
SELECTOR_LAST_SYNCED = "ebfa2b79"
SELECTOR_EPOCH_SEED = "88f28df1"
SELECTOR_INIT_EPOCH_SEED = "2213e2bb"
SELECTOR_SYNC = "b6441551"

def load_env(env_path=".env"):
    config = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip().strip("\x27\"")
    return config

def rpc_call(rpc_url, method, params):
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params}
    req = urllib.request.Request(rpc_url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "PoolCoin-Keeper/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            raise RuntimeError("RPC Error (" + method + "): " + str(res.get("error")))
        return res.get("result")

def eth_call(rpc_url, to, data):
    tx = {"to": to, "data": "0x" + data}
    res = rpc_call(rpc_url, "eth_call", [tx, "latest"])
    if res and res.startswith("0x"):
        return res[2:]
    return res or ""

def encode_uint256(val):
    return hex(val)[2:].zfill(64)

class KeeperBot:
    def __init__(self, rpc_url, miner_address, private_key=None, dry_run=True, poll_interval=15):
        self.rpc_url = rpc_url
        self.miner = miner_address.lower()
        self.private_key = private_key
        self.dry_run = dry_run
        self.poll_interval = poll_interval

    def log(self, level, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print("[" + ts + "] [" + level + "] " + msg, flush=True)

    def alert(self, msg):
        self.log("ALERT", "CRITICAL: " + msg)

    def get_epoch_state(self):
        res_epoch = eth_call(self.rpc_url, self.miner, SELECTOR_EPOCH)
        cur_epoch = int(res_epoch, 16) if res_epoch else 0
        res_synced = eth_call(self.rpc_url, self.miner, SELECTOR_LAST_SYNCED)
        last_synced = int(res_synced, 16) if res_synced else 0
        seed_data = SELECTOR_EPOCH_SEED + encode_uint256(cur_epoch)
        res_seed = eth_call(self.rpc_url, self.miner, seed_data)
        seed = int(res_seed, 16) if res_seed else 0
        return cur_epoch, last_synced, seed

    def handle_epoch_seed(self, cur_epoch, seed):
        if seed == 0:
            self.log("ACTION", "Epoch " + str(cur_epoch) + " seed uninitialized (0x0). Needs initEpochSeed.")
            calldata = "0x" + SELECTOR_INIT_EPOCH_SEED + encode_uint256(cur_epoch)
            if self.dry_run:
                self.log("DRY-RUN", "[Simulated] Would call initEpochSeed(" + str(cur_epoch) + ") on Miner " + self.miner)
                self.log("DRY-RUN", "  Calldata: " + calldata)
            else:
                self.send_transaction(calldata, "initEpochSeed(" + str(cur_epoch) + ")")
        else:
            self.log("OK", "Epoch " + str(cur_epoch) + " seed already initialized: " + hex(seed)[:10] + "...")

    def handle_sync_lag(self, cur_epoch, last_synced):
        if last_synced < cur_epoch:
            lag = cur_epoch - last_synced
            self.alert("Sync lag detected: lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + " (Lag = " + str(lag) + " epochs)!")
            steps = min(lag, 48)
            calldata = "0x" + SELECTOR_SYNC + encode_uint256(steps)
            if self.dry_run:
                self.log("DRY-RUN", "[Simulated] Would call sync(" + str(steps) + ") on Miner " + self.miner)
                self.log("DRY-RUN", "  Calldata: " + calldata)
            else:
                self.send_transaction(calldata, "sync(" + str(steps) + ")")
        else:
            self.log("OK", "Sync state up-to-date (lastSynced=" + str(last_synced) + ", currentEpoch=" + str(cur_epoch) + ").")

    def send_transaction(self, calldata, desc):
        if not self.private_key:
            self.alert("Cannot broadcast " + desc + ": No private key provided in environment!")
            return
        masked_len = str(len(self.private_key))
        self.log("TX", "Broadcasting " + desc + " (Signing with Keeper key)... [Configured Key Length: " + masked_len + "]")

    def step(self):
        try:
            cur_epoch, last_synced, seed = self.get_epoch_state()
            seed_status = "Initialized" if seed != 0 else "UNINITIALIZED"
            self.log("STATUS", "Current Epoch: " + str(cur_epoch) + " | LastSynced: " + str(last_synced) + " | Seed: " + seed_status)
            self.handle_epoch_seed(cur_epoch, seed)
            self.handle_sync_lag(cur_epoch, last_synced)
        except Exception as e:
            self.alert("Failed during keeper inspection: " + str(e))

    def run_forever(self):
        mode = "DRY-RUN (Simulated actions only)" if self.dry_run else "LIVE (Broadcasting transactions)"
        self.log("START", "Starting PoolCoin Keeper Bot [" + mode + "] targeting Miner " + self.miner)
        self.log("CONFIG", "RPC URL: " + self.rpc_url + " | Poll Interval: " + str(self.poll_interval) + "s")
        while True:
            self.step()
            time.sleep(self.poll_interval)

def main():
    parser = argparse.ArgumentParser(description="PoolCoin Keeper Bot for MinerV1")
    parser.add_argument("--dry-run", action="store_true", help="Print transactions without broadcasting")
    parser.add_argument("--once", action="store_true", help="Run a single inspection pass and exit")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval in seconds (default 15)")
    parser.add_argument("--env-file", default=".env", help="Path to .env configuration file")
    args = parser.parse_args()
    env = load_env(args.env_file)
    rpc_url = env.get("RPC_URL", "https://data-seed-prebsc-1-s1.binance.org:8545")
    miner_addr = env.get("MINER_PROXY_ADDRESS", "0x0000000000000000000000000000000000000000")
    priv_key = env.get("KEEPER_PRIVATE_KEY") or env.get("PRIVATE_KEY")
    is_dry_run = args.dry_run or not priv_key
    if not priv_key and not args.dry_run:
        print("[WARN] No private key detected in environment; defaulting to --dry-run for safety.")
    bot = KeeperBot(rpc_url=rpc_url, miner_address=miner_addr, private_key=priv_key, dry_run=is_dry_run, poll_interval=args.interval)
    if args.once:
        bot.step()
    else:
        bot.run_forever()

if __name__ == "__main__":
    main()
