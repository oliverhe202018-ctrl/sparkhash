# SparkHash 主网矿工预览版实盘验收报告

初版验收日期：**2026-11-21（Asia/Shanghai，UTC+8）**；实盘补录与复核日期：**2026-11-26（Asia/Shanghai，UTC+8）**。网络：**BSC 主网，Chain ID 56**。

本报告汇集 2026-11-21 两条实盘路径及 2026-11-26 传统 CLI 实盘补录的 BSC 主网只读 RPC 复核结果。2026-11-21 复核了两钱包 deposit、CLI 的 submitShares / claim / requestWithdraw 成功回执，并定位、核对 Agentic Wallet 的 Epoch 19 提交与领奖交易；2026-11-26 补齐并核验了传统 CLI 钱包在 Epoch 129 完成的 `withdraw()` 本金赎回闭环（原“Epoch 67 起待验证”），以及复挖质押、Epoch 130 满额 6 份 share 领奖和 Epoch 131 常驻矿工 6 份批量打包单笔提交记录。Keeper 段落仍为 2026-11-21 操作者记录，未在本次重新验收。当前版本为主网矿工预览版。

## VPS Keeper 自动换期

当日实盘记录确认 Epoch **16 → 17** 自动换期。相关两笔交易如下（不对两笔交易分别归属哪个具体函数作推断）：

- [0xfd42c16c1563c3466876bfb3aae7b4d9b05d50ef5b9e6a9d3c9f942e1ac83d4c](https://bscscan.com/tx/0xfd42c16c1563c3466876bfb3aae7b4d9b05d50ef5b9e6a9d3c9f942e1ac83d4c)
- [0xf06a459736022b4da6630209edea08f2819daa08fd65c9d9fcd9880fbea0ad7f](https://bscscan.com/tx/0xf06a459736022b4da6630209edea08f2819daa08fd65c9d9fcd9880fbea0ad7f)

## 传统矿工 CLI

设备：**RTX 4060 Ti**。测试钱包：`0xE681561bCA5F08d50cD2135df25a3fafAdf933B8`。

### 2026-11-21 首轮实盘与冷却退出申请

- **deposit，0.01 BNB，已完成：** [0xbe337360d7b85352d8ea4f7ea56938e996b06afa12cbdc8fb8f02bdc9fd1900e](https://bscscan.com/tx/0xbe337360d7b85352d8ea4f7ea56938e996b06afa12cbdc8fb8f02bdc9fd1900e)
- **submitShares，Epoch 18，已完成：** [0x47c7b087fad9f60326a40ac392845311ea94825da5c3c55fe55ba75898245a07](https://bscscan.com/tx/0x47c7b087fad9f60326a40ac392845311ea94825da5c3c55fe55ba75898245a07)
- **claim，Epoch 18，已完成，领取 3096.8794294814475 SPKH：** [0x0ea736371298293b5fa66112fb28bb5e4709d13298f4f795723ff78ba0cb8417](https://bscscan.com/tx/0x0ea736371298293b5fa66112fb28bb5e4709d13298f4f795723ff78ba0cb8417)
- **requestWithdraw，已完成，冷却至 Epoch 67：** [0x431cd289ebf3b721eac23c32a6a685ae8deaf1008024def92a0a08f5b3341bf0](https://bscscan.com/tx/0x431cd289ebf3b721eac23c32a6a685ae8deaf1008024def92a0a08f5b3341bf0)

2026-11-21 只读复核：退出申请回执 `status=0x1`，区块 `134884344`，Gas used `28621`；Vault 的 `WithdrawRequested` 事件指向上述 CLI 钱包、申请 Epoch `19`。当时 `stakes(wallet)` 返回 `withdrawReqEpoch=20`、`has=true`、`depositAmount=10000000000000000` wei；`COOLDOWN_EPOCHS()=48`。按 `20 - 1 + 48`，最早 **Epoch 67（含）** 可赎回。该笔本金赎回原先标注为“Epoch 67 起待验证”，现已于 2026-11-26 在 Epoch 129 完成 `withdraw()` 并核验为 **【已验证】**（见下节）。

### 2026-11-26 本金赎回闭环、复挖与批量提交验证

- **withdraw() 赎回 0.01 BNB（Epoch 129），【已验证】：** [0x41b229aa999f3fedbdba3864c9544a5b41d1a79652adf2641c61db67ecd81403](https://bscscan.com/tx/0x41b229aa999f3fedbdba3864c9544a5b41d1a79652adf2641c61db67ecd81403)。成功状态 `0x1`，区块 `135767459`（`2026-11-26 10:33:35` UTC+8），发送者为上述 CLI 钱包，目标为 StakeVault `0x45eb4e8a7a3070c8160e9a91fae002934d296591`，`value=0`，输入选择器 `0x3ccfd60b` 对应 `withdraw()`，Gas used `36,780`，effective gas price `50,000,000` wei，总 Gas 费 `0.000001839 BNB`。Vault 发出 `Withdrawn(address,uint256)` 事件，向该钱包全额返还 `10000000000000000` wei（`0.01 BNB`），原先标注“Epoch 67 起待验证”的本金赎回至此完成闭环。
- **重新质押 0.01 BNB（Epoch 129），【已验证】：** [0x0257e87f39442bc97ecea2d499368e0ec2cced8639635e8a521c8aac1743fb02](https://bscscan.com/tx/0x0257e87f39442bc97ecea2d499368e0ec2cced8639635e8a521c8aac1743fb02)。成功状态 `0x1`，区块 `135767483`（`2026-11-26 10:33:46` UTC+8），发送者为上述钱包，目标为 StakeVault `0x45eb4e8a7a3070c8160e9a91fae002934d296591`，`value=0.01 BNB`（`10000000000000000` wei），输入选择器 `0xd0e30db0` 对应 `deposit()`，Gas used `72,447`，effective gas price `50,000,000` wei，总 Gas 费 `0.00000362235 BNB`。Vault 发出 `Deposited(address,uint256,uint256)` 事件，记录该钱包、Epoch `129`、`amount=10000000000000000` wei。只读 `stakes(wallet)` 与 `status(wallet)` 确认 `has=true`、`depositEpoch=129`、`withdrawReqEpoch=0`、`active=true`、`since=129`。
- **Epoch 130 满额 6 份 share 领奖（7,585.719891994091704744 SPKH），【已验证】：** [0x05aa3218e8e673240ae21f229a8c51fc08fab9bcf9909b674bed662782647f04](https://bscscan.com/tx/0x05aa3218e8e673240ae21f229a8c51fc08fab9bcf9909b674bed662782647f04)。成功状态 `0x1`，区块 `135779082`（`2026-11-26 12:00:47` UTC+8），发送者和领奖地址均为上述钱包，目标为 Miner Proxy `0x614b5efd21cfa99cb893abf2fb02d791b3aea843`，`value=0`，输入为 `claim(130)`（选择器 `0x379607f5`），Gas used `92,815`，effective gas price `50,000,000` wei，总 Gas 费 `0.00000464075 BNB`。代理的 `RewardClaimed(address,uint256,uint256,uint256)` 事件记录该钱包、Epoch `130`、`claimedAmount=7585719891994091704744`、`cappedOverflow=0`；Token `0xcce53217a4f2850c536d751cfb676da26bd372dc` 同回执 `Transfer` 从零地址铸造同额至该钱包（18 位小数）。只读复核 `counted(130, wallet)=6`、`userWeight(130, wallet)=6123722500`、`claimed(130, wallet)=true`。
- **Epoch 131 常驻矿工 6 份批量打包单笔提交（value=0，费用 0.000008895 BNB），【已验证】：** [0x48943cd492e4855cae6f364230d9b43a6d0d32c0bf824a8570b4ac683cc81b54](https://bscscan.com/tx/0x48943cd492e4855cae6f364230d9b43a6d0d32c0bf824a8570b4ac683cc81b54)。成功状态 `0x1`，区块 `135779485`（`2026-11-26 12:03:49` UTC+8），发送者为上述钱包，目标为 Miner Proxy `0x614b5efd21cfa99cb893abf2fb02d791b3aea843`，`value=0`，输入选择器 `0x6efb5ee9` 对应 `submitShares(uint8[],uint256[])`，ABI 解码为 `ks=[0, 1, 2, 3, 4, 5]`、`nonces=[7109050110, 261555831, 5004786497, 943335687, 10563484986, 4930131949]`，Gas used `177,900`，effective gas price `50,000,000` wei，总 Gas 费 `0.000008895 BNB`。代理发出 `SharesSubmitted(address,uint256,uint256,uint256)` 事件，记录矿工地址匹配、Epoch `131`、`n=6`、`weightAdded=6887963068`，与只读 `counted(131, wallet)=6`、`userWeight(131, wallet)=6887963068` 一致。

## 币安 Agentic Wallet

测试钱包：`0x61D32A0D16B0C6306E0E2EF2435e355478Cd8e6b`。

- **deposit，0.01 BNB，已完成：** [0xf4c08e3c6616fc1c9d1da423674c40ccff0e7d1a5049896b4bcfaada32f2608c](https://bscscan.com/tx/0xf4c08e3c6616fc1c9d1da423674c40ccff0e7d1a5049896b4bcfaada32f2608c)
- **submitShares，Epoch 19，已复核：** [公开回执](https://bscscan.com/tx/0x337e72d4cf9c866f8c2a3d6fa8337b98eaca6f7149a99b75af766e9cc729049d)，哈希 `0x337e72d4cf9c866f8c2a3d6fa8337b98eaca6f7149a99b75af766e9cc729049d`。成功状态 `0x1`，区块 `134888352`，Gas used `171453`。发送者为上述钱包，目标为 Miner Proxy `0x614b5efd21cfa99cb893abf2fb02d791b3aea843`，value `0`。输入选择器 `0x6efb5ee9` 对应 `submitShares(uint8[],uint256[])`，ABI 解码为 `ks=[0]`、`nonces=[1525678336]`。代理发出 `SharesSubmitted`，矿工地址匹配、Epoch `19`、`n=1`，与只读 `counted(19, wallet)=1` 一致。
- **claim，Epoch 19，已复核，准确到账 3096.868773190113338515 SPKH：** [公开回执](https://bscscan.com/tx/0xea46f7f402981ca75f92b130c1823ab98290d2053cf5d584bf517bd75e92a348)，哈希 `0xea46f7f402981ca75f92b130c1823ab98290d2053cf5d584bf517bd75e92a348`。成功状态 `0x1`，区块 `134892335`，Gas used `109915`，effective gas price `77855129` wei，总 Gas 费 `0.000008557446504035 BNB`。发送者和领奖地址均为上述钱包，目标为上述 Miner Proxy，value `0`。输入为 `claim(19)`（选择器 `0x379607f5`）。代理的 `RewardClaimed(address,uint256,uint256,uint256)` 事件记录该钱包、Epoch `19`、`claimedAmount=3096868773190113338515`、`cappedOverflow=0`；Token `0xcce53217a4f2850c536d751cfb676da26bd372dc` 同回执的 `Transfer` 从零地址铸造同额至该钱包（18 位小数）。

原提供的领奖字符串 `0xea46f7f402981ca75f92b130c1823ab98290d2053cf5d584bf517bd75e92a3482100` 含 68 个十六进制字符，RPC 因哈希长度错误拒绝。上列有效哈希通过 Miner Proxy 日志中匹配的钱包地址、Epoch 19 和 RewardClaimed 事件独立定位，再查询交易与成功回执确认；未以删尾猜测代替取证。领奖哈希与提交哈希分别记录。

### 可复现工作流与取证方法

本次流程：本机 RTX 4060 Ti GPU 为币安钱包的**公开地址**求解，仅处理公开链上参数并产出 k / nonce / calldata；币安 Agent 重新核对 Chain ID 56、Miner Proxy、钱包地址、Epoch、seed、target、未用 k 和 PoW 哈希，解码比对 calldata 并展示 preview；**用户确认本笔交易后**由 Agentic Wallet 签名提交；随后只读核对回执和事件。Epoch 过期时必须重读任务、重新求解和预览。

本次币安 App AI 沙盒实测为 2 核 CPU，运行 400 秒**本次实测未出解**。这只是该次运行结果，不代表所有版本、设备或沙盒均存在相同限制，也不保证求解耗时或奖励。

取证使用 `eth_chainId`（`0x38`）、`eth_getLogs`、`eth_getTransactionByHash`、`eth_getTransactionReceipt` 及只读 `eth_call`。RPC 为 `https://bsc-rpc.publicnode.com` 和 `https://bsc-dataseed.binance.org`；按仓库 Solidity ABI 解码。提交通过 SharesSubmitted 日志定位，领奖通过 RewardClaimed 日志定位，再分别核对发送者、目标、输入、回执和 Token 铸造事件。本次未连接签名钱包或执行链上写入。

2026-11-26 补充核验同样通过 `https://bsc-rpc.publicnode.com`（`eth_chainId=0x38`）以只读 `eth_getTransactionByHash`、`eth_getTransactionReceipt`、`eth_getBlockByNumber` 与 `eth_call` 完成，按仓库 [`StakeVault.sol`](../src/StakeVault.sol) 与 [`MinerV1.sol`](../src/MinerV1.sol) ABI 解码核对，未连接签名钱包或修改链上状态。

公开回执链接使用 RPC 核对的交易哈希。本次直接访问 BscScan 网页返回 HTTP 403，未将网页可访问性记为通过；上述成功状态和事件以主网 RPC 回执为取证依据。

## 三条实盘经验与预览版边界

1. **算力架构：** 本次实盘采用本地 GPU 为钱包公开地址离线求解、币安 Agent 复核参数和 preview、用户确认后由 Agentic Wallet 签名提交。沙盒结果仅按上述单次实测记录。
2. **安全风控：** 本次币安 App 实测日限额最低为 $1,000，且暂未提供合约白名单界面。强烈建议新建独立小额隔离钱包，仅转入 0.03–0.05 BNB 用于质押与 Gas，并逐笔核对 preview 的目标合约与 value。此为当日 App 实测情况。
3. **路径指引：** 完整教程路径为 [`skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md`](../skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md)，入口为 [`skills/sparkhash-agentic-miner/SKILL.md`](../skills/sparkhash-agentic-miner/SKILL.md)。外部 AI Agent 应从仓库根目录按完整路径读取。

矿工 CLI 的网络误标已在提交 `2351a032ae3b2c4b068395f96921f1be5c7cd60b` 修复：广播警告根据已验证的 chain_id 动态显示 `BSC MAINNET` / `BSC TESTNET`。CPU/GPU 求解器也按 chain_id 显示网络名称。该显示修复不改变合约规则。

前次版本记录的本地回归：`python -m pytest test/test_miner.py test/test_miner_gpu.py test/test_safe_output_miners_monitor.py test/test_scripts_safe_output.py -q`，结果 **55 passed**，包含主网/测试网广播文案回归。本次只改文档，未改动 Solidity 或 Python 代码，也未重跑该代码测试；该历史结果不是新增的链上交易证据。
