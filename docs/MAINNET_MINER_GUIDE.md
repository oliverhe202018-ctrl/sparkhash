# SparkHash（SPKH）BSC 主网矿工指南

SparkHash 将矿工找到的工作量证明（share）记入对应 Epoch。参与者先在 StakeVault 质押原生 BNB，再用同一钱包向 Miner Proxy 提交 share、领取 SPKH 和赎回本金。以下命令使用 **BNB Smart Chain 主网（Chain ID `56`）**。

## 主网合约

地址来自仓库的主网部署记录。七个地址的创建字节码与运行时字节码均在 Sourcify 显示 `exact_match`。[主网部署记录](../reports/06-mainnet-deployment-record.md) · [Sourcify 校验示例（StakeVault）](https://sourcify.dev/server/v2/contract/56/0x45eb4e8a7a3070c8160e9a91fae002934d296591)

- **Token（SPKH）** `0xcce53217a4f2850c536d751cfb676da26bd372dc` — 查询余额、在钱包导入代币（18 位小数）。[BscScan](https://bscscan.com/address/0xcce53217a4f2850c536d751cfb676da26bd372dc#code)
- **Miner Proxy** `0x614b5efd21cfa99cb893abf2fb02d791b3aea843` — 查询任务、提交 share 和领取奖励的入口。[BscScan](https://bscscan.com/address/0x614b5efd21cfa99cb893abf2fb02d791b3aea843#code)
- **StakeVault** `0x45eb4e8a7a3070c8160e9a91fae002934d296591` — 存入质押 BNB、申请退出和赎回本金的入口。[BscScan](https://bscscan.com/address/0x45eb4e8a7a3070c8160e9a91fae002934d296591#code)
- Miner Implementation（MinerV1） `0xc74756ad1ca5ea01f46b81cc86f6068e76467457` — 代理当前使用的逻辑实现。挖矿任务的哈希和交易都使用 Miner Proxy 地址。[BscScan](https://bscscan.com/address/0xc74756ad1ca5ea01f46b81cc86f6068e76467457#code)
- TeamVestingWallet `0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e` — 团队代币归属合约。[BscScan](https://bscscan.com/address/0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e#code)
- ProxyAdmin `0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f` — 代理管理合约。[BscScan](https://bscscan.com/address/0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f#code)
- TimelockController `0x565f007310ee7d28aa526a7a94df7b95d554be00` — 治理时间锁。[BscScan](https://bscscan.com/address/0x565f007310ee7d28aa526a7a94df7b95d554be00#code)

## 1. 准备环境

在 Linux 或 WSL 的 Bash 中进入仓库根目录。安装 Python 3（示例使用 Python 3.14）、`venv` 以及 Foundry 的 `cast`，然后在同一个终端执行以下命令。[Foundry 安装说明](https://www.getfoundry.sh/introduction/installation)

```bash
python3 -m venv .venv-miner
source .venv-miner/bin/activate
python -m pip install eth-abi 'eth-hash[pycryptodome]'
cast --version

export RPC_URL=https://bsc-rpc.publicnode.com
export MINER_PROXY=0x614b5efd21cfa99cb893abf2fb02d791b3aea843
export VAULT=0x45eb4e8a7a3070c8160e9a91fae002934d296591
export TOKEN=0xcce53217a4f2850c536d751cfb676da26bd372dc

mainnet() {
  [ "$(cast chain-id --rpc-url "$RPC_URL")" = 56 ] || {
    printf '%s\n' 'RPC 未返回 BSC 主网 Chain ID 56，请检查连接。' >&2
    return 1
  }
}

uint() {
  local result
  result=$(cast call "$@" --rpc-url "$RPC_URL") || return
  printf '%s\n' "${result%% *}"
}

mainnet
```

主网 CLI 包括 miner.py、miner_gpu.py、submit_share.py、status.py 和 claim.py。加上 **--chain-id 56** 即使用主网默认 RPC 和代理地址，省略时仍使用测试网（97）。CLI 会核对 RPC 返回的实际 Chain ID。

使用币安 Agentic Wallet 签名的流程见[币安钱包挖矿教程](../skills/sparkhash-agentic-miner/docs/AGENTIC_WALLET_MINING_GUIDE.md)。

## 2. 准备钱包、Gas 和质押 BNB

用 Foundry 创建一个加密钱包，或将 `ACCOUNT` 改为已有的 Foundry keystore 别名并跳过创建命令。按提示设置或输入密码，备份加密 keystore 和密码。下文的 `cast send` 会交互式解锁这个账户。

```bash
ACCOUNT=spkh-miner
cast wallet new "$ACCOUNT"
unset MINER_ADDR
MINER_ADDR=$(cast wallet address --account "$ACCOUNT") && export MINER_ADDR &&
  printf '矿工地址：%s\n' "$MINER_ADDR"

cast balance "$MINER_ADDR" --ether --rpc-url "$RPC_URL"
cast call "$VAULT" 'DEPOSIT()(uint256)' --rpc-url "$RPC_URL"
```

向该地址转入 **BSC 主网的原生 BNB**。核对时，主网质押额为 **0.01 BNB**（`10000000000000000` wei），实际存入金额以交易前读到的 `DEPOSIT()` 为准。余额须覆盖该金额，并留出提交、领奖、申请退出和赎回所需的 Gas。整个流程使用同一个钱包地址，PoW 也绑定到这个地址。

## 3. 质押并等待激活

先确认金库未暂停，且 `stakes` 的第三项 `has` 为 `false`。`stakes` 依次返回质押轮次、退出请求轮次、是否有质押记录、存入金额（wei）。

```bash
cast call "$VAULT" 'paused()(bool)' --rpc-url "$RPC_URL"
cast call "$VAULT" 'stakes(address)(uint64,uint64,bool,uint128)' "$MINER_ADDR" --rpc-url "$RPC_URL"
```

```bash
mainnet && DEPOSIT_WEI=$(uint "$VAULT" 'DEPOSIT()(uint256)') &&
  cast call "$VAULT" 'deposit()' --from "$MINER_ADDR" --value "$DEPOSIT_WEI" --rpc-url "$RPC_URL" &&
  cast send "$VAULT" 'deposit()' --value "$DEPOSIT_WEI" --account "$ACCOUNT" --rpc-url "$RPC_URL"
```

等待成功回执，再检查 `status`。它返回 `active` 和 `since`（质押时的 Epoch）。即使 `active=true`，也要等到 Miner Proxy 的 `epoch() >= since + 1` 才能提交 share。主网每轮 3,600 秒，以链上轮次为准。

```bash
cast call "$VAULT" 'status(address)(bool,uint256)' "$MINER_ADDR" --rpc-url "$RPC_URL"
cast call "$MINER_PROXY" 'epoch()(uint256)' --rpc-url "$RPC_URL"
```

## 4. 搜索并提交 share

质押达到可挖矿轮次后，在仓库根目录一键启动 CPU 挖矿。脚本会读链上的当前 Epoch、种子、难度阈值和已用索引，找到有效解后保存 JSON。每轮最多计入 6 份，同一轮的每个索引只能使用一次。若换轮时仍在搜索，重新运行即可读取新任务。

```bash
SHARE_FILE="share-mainnet-$(date +%s).json"
python script/miner.py --chain-id 56 --miner "$MINER_ADDR" --threads 4 --out "$SHARE_FILE"
```

有支持 OpenCL 的 GPU 时，可改用下面的命令。先安装显卡驱动和 OpenCL 运行时，设备编号以脚本列出的设备为准。

```bash
python -m pip install numpy pyopencl
SHARE_FILE="share-mainnet-gpu-$(date +%s).json"
python script/miner_gpu.py --chain-id 56 --miner "$MINER_ADDR" --device-idx 0 --out "$SHARE_FILE"
```

只有脚本成功生成 JSON 文件后才继续。提交脚本会核对文件中的 Chain ID、Miner Proxy 和 Epoch。换轮后的 share 需要重新计算。

需要离线校验同轮的 6 份 share 时，可运行 `python script/validate_share_batch.py --help` 查看参数。校验主网批次时指定 `--chain-id 56`，并提供主网代理地址和该轮实际参数。

```bash
python script/submit_share.py --chain-id 56 --file "$SHARE_FILE"
```

预检通过后，用原质押钱包提交。命令在终端交互式解锁 keystore，并要求输入 yes 确认。保留成功回执和对应的 Epoch。

```bash
python script/submit_share.py --chain-id 56 --file "$SHARE_FILE" --send \
  --keystore "$HOME/.foundry/keystores/$ACCOUNT"
python script/status.py --chain-id 56 --miner "$MINER_ADDR"
```

## 5. 查询并领取 SPKH

输入你实际提交成功的奖励轮次。确认 `epoch() > REWARD_EPOCH`、`lastSynced() > REWARD_EPOCH`、该轮 `userWeight > 0`，且 `claimed=false`。已结算配额 `settledEpochQuota` 为 0 时，该轮领取结果也是 0。每次领取一个 Epoch。

```bash
read -r -p '输入要领取的 Epoch：' REWARD_EPOCH
cast call "$MINER_PROXY" 'epoch()(uint256)' --rpc-url "$RPC_URL"
cast call "$MINER_PROXY" 'lastSynced()(uint256)' --rpc-url "$RPC_URL"
cast call "$MINER_PROXY" 'userWeight(uint256,address)(uint256)' "$REWARD_EPOCH" "$MINER_ADDR" --rpc-url "$RPC_URL"
cast call "$MINER_PROXY" 'settledEpochQuota(uint256)(uint256)' "$REWARD_EPOCH" --rpc-url "$RPC_URL"
cast call "$MINER_PROXY" 'claimed(uint256,address)(bool)' "$REWARD_EPOCH" "$MINER_ADDR" --rpc-url "$RPC_URL"
```

```bash
python script/claim.py --chain-id 56 --epoch "$REWARD_EPOCH" --miner "$MINER_ADDR"
# 预检通过后，再在交互式终端发送。
python script/claim.py --chain-id 56 --epoch "$REWARD_EPOCH" --miner "$MINER_ADDR" --send \
  --keystore "$HOME/.foundry/keystores/$ACCOUNT"
```

用交易回执中的 `RewardClaimed` 事件核对该轮实际发放量。下面可以查领取标记和钱包的 SPKH 总余额（最小单位，除以 `10^18` 换算 SPKH）。

```bash
cast call "$MINER_PROXY" 'claimed(uint256,address)(bool)' "$REWARD_EPOCH" "$MINER_ADDR" --rpc-url "$RPC_URL"
cast call "$TOKEN" 'balanceOf(address)(uint256)' "$MINER_ADDR" --rpc-url "$RPC_URL"
```

## 6. 退出并赎回质押

停止本地搜索。用原质押钱包申请退出。成功后该钱包立即失去有效质押资格，本金留在金库等待冷却。已经计入的历史奖励仍可按上一步领取。

```bash
mainnet &&
  cast call "$VAULT" 'requestWithdraw()' --from "$MINER_ADDR" --rpc-url "$RPC_URL" &&
  cast send "$VAULT" 'requestWithdraw()' --account "$ACCOUNT" --rpc-url "$RPC_URL"

cast call "$VAULT" 'stakes(address)(uint64,uint64,bool,uint128)' "$MINER_ADDR" --rpc-url "$RPC_URL"
cast call "$VAULT" 'epoch()(uint256)' --rpc-url "$RPC_URL"
cast call "$VAULT" 'COOLDOWN_EPOCHS()(uint256)' --rpc-url "$RPC_URL"
```

主网冷却为 **48 个 Epoch**。`stakes` 的第二项 `withdrawReqEpoch` 等于申请时的 Epoch 加 1。等金库当前轮次达到 `withdrawReqEpoch - 1 + 48` 后，调用 `withdraw()`，本金按原实存金额返还给调用的钱包。赎回交易也要支付 BNB Gas。赎回完成后可检查 `stakes` 的 `has=false`。

```bash
mainnet &&
  cast call "$VAULT" 'withdraw()' --from "$MINER_ADDR" --rpc-url "$RPC_URL" &&
  cast send "$VAULT" 'withdraw()' --account "$ACCOUNT" --rpc-url "$RPC_URL"

cast call "$VAULT" 'stakes(address)(uint64,uint64,bool,uint128)' "$MINER_ADDR" --rpc-url "$RPC_URL"
cast balance "$MINER_ADDR" --ether --rpc-url "$RPC_URL"
```

## 钱包集成接口

- StakeVault `deposit()` 为 payable，`value` 必须等于当前 `DEPOSIT()`。
- Miner Proxy `target(uint256)` 返回指定 Epoch 的 `uint256` 难度阈值。用 `epoch()` 获取当前轮次，并读取同一轮的 `epochSeed(uint256)` 与该钱包的 `usedMask(uint256,address)`。
- Miner Proxy `submitShares(uint8[],uint256[])` 接受等长的索引和 PoW nonce 数组，校验发送者、代理地址、Chain ID 和当前种子。
- Miner Proxy `claim(uint256)` 领取发送者在指定历史 Epoch 的奖励。
- StakeVault `requestWithdraw()` 发起退出。冷却到期后，再用同一签名账户调用 `withdraw()` 完成赎回。

合约接口见 [`MinerV1.sol`](../src/MinerV1.sol) 和 [`StakeVault.sol`](../src/StakeVault.sol)。
