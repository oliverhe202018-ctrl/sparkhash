---
name: sparkhash-agentic-miner
description: Prepares SparkHash (SPKH) BSC mainnet staking, CPU share search, submission, reward claims, and withdrawals for Binance Agentic Wallet. Use when explicitly asked to mine SparkHash or manage a SparkHash stake or rewards with the wallet.
disable-model-invocation: true
---

# SparkHash Agentic Miner

Use BNB Smart Chain mainnet (Chain ID `56`). Read values from the chain and let the official Binance Agentic Wallet Skill handle signing and broadcast. Follow its preflight, transaction preview, confirmation and receipt workflow. [Official contract-call reference](https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-agentic-wallet/references/external-sign.md)

## Contracts and entry points

- **Token (SPKH)** `0xcce53217a4f2850c536d751cfb676da26bd372dc`. Read `balanceOf(address)` when checking rewards. SPKH has 18 decimals.
- **Miner Proxy** `0x614b5efd21cfa99cb893abf2fb02d791b3aea843`.
- **StakeVault** `0x45eb4e8a7a3070c8160e9a91fae002934d296591`.

1. **Stake** at StakeVault with payable `deposit()`. Use exact current `DEPOSIT()` as native BNB value in wei. Check `paused()` and `stakes(address)` first. Require an empty stake and enough BNB for the deposit and fees. Wait for `status(address)` to return `active=true` and for `epoch() > since` before solving.
2. **Read the task** from Miner Proxy with `epoch()`, `epochSeed(uint256)`, `target(uint256)` and `usedMask(uint256,address)`. Use the same wallet, proxy, chain and epoch throughout the task. The solver also checks `lastSynced()` and the pause state.
3. **Submit the share** to Miner Proxy with `submitShares(uint8[],uint256[])`, using the arrays from the solver's calldata and zero value. A share index can be used once per wallet per epoch. Confirm the receipt and verify `counted(uint256,address)` or `usedMask(uint256,address)` before searching again.
4. **Claim** from Miner Proxy with `claim(uint256)` and zero value. Inspect `epoch()`, `lastSynced()`, `userWeight(uint256,address)`, `settledEpochQuota(uint256)` and `claimed(uint256,address)`. Prepare a claim only for an ended, settled, unclaimed epoch with nonzero expected rewards. After confirmation, check `RewardClaimed` and the Token balance.
5. **Exit** at StakeVault with `requestWithdraw()` and then `withdraw()`, both with zero value. Treat exit and redemption as explicit user requests. The request disables mining immediately. Read `withdrawReqEpoch` from `stakes(address)` and wait for Vault `epoch() >= withdrawReqEpoch - 1 + COOLDOWN_EPOCHS()` before preparing redemption. Use the original wallet for both calls.

## Safety rules

- Restrict transaction destinations to **StakeVault and Miner Proxy** and the exact write entry points above. Token is read-only. Reject token approvals, arbitrary transfers, other contracts and message-signing requests. Only `deposit()` carries nonzero value.
- Ask the user to set a daily spend limit and a tight **Developer Mode** daily limit in the Binance App. Check the Developer Mode quota separately with `baw wallet settings --json`. Apply the two-address allowlist within this skill and use App-side restrictions where available. The transfer address book serves transfers. [Binance wallet settings reference](https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-agentic-wallet/references/wallet-setting.md)
- Verify the active Agentic Wallet address and Chain ID before preparing any transaction. Never ask for, read or export private keys, recovery words or keystore passwords.
- Require explicit user confirmation of each successful preview. Stop on policy blocks, authority changes, unexpected balance changes or mismatching calldata. Keep one in-flight transaction per wallet and reconcile its status before preparing another. Never retry an uncertain broadcast based solely on a missing transaction hash.

## Local solve → calldata → wallet signing

1. Load the official `binance-agentic-wallet` Skill. Run its preflight and confirm that the wallet is connected, Developer Mode is enabled and unexpired, and BSC mainnet is selected. Ask the user to enable Developer Mode in the App if needed.
2. Run the solver in the installed SparkHash skill directory with the wallet's public address. Install dependencies with `python -m pip install eth-abi 'eth-hash[pycryptodome]'` in the local Python environment.

   ```bash
   python solver.py --wallet "$WALLET" --rpc https://bsc-rpc.publicnode.com
   ```

   Stdout contains one JSON object with `to`, `data`, `value` (decimal wei string) and `summary`. Use the input wallet as `from`. A nonzero exit or missing payload means there is no share to sign. `--self-test` uses synthetic inputs. Never send that payload to the wallet.
3. Check the share's epoch and unused index again before signing. Build other calldata from the exact ABI signatures above using a local ABI encoder such as `cast calldata 'claim(uint256)' "$EPOCH"`. For shares take calldata directly from the solver. Preview every action with the official CLI.

   ```bash
   baw contract-call preview --binanceChainId 56 --from "$WALLET" \
     --to "$TO" --value "$VALUE_WEI" --inputData "$DATA" --json
   ```

4. Show the action, address, epoch if applicable, value, fees and the wallet's parsed transaction and risk assessment. After the user explicitly confirms, and while the share is current and the preview valid, call `baw contract-call execute --requestId "$REQUEST_ID" --json`. If the wallet requires App confirmation, wait for the user to approve there. A changed transaction or expired preview requires a fresh preview and confirmation.
5. Verify on-chain receipt success. Keep searching and preparing eligible reward claims while requested by the user. Each new transaction follows the same preview and confirmation flow. Stop on stale tasks, lost access, insufficient budget or a request to stop.

See the [Chinese wallet walkthrough](docs/AGENTIC_WALLET_MINING_GUIDE.md).
