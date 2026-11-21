# SparkHash 主网矿工预览版实盘验收报告

验收日期：**2026-11-21（Asia/Shanghai，UTC+8）**。网络：**BSC 主网，Chain ID 56**。

本报告根据当日操作者提供的两次真实实盘验收结果整理。下列交易哈希提供公开链上复核入口；本次文档更新未重新逐笔查询链上回执。未提供的哈希明确待补，待完成事项不计为已验收。当前版本为主网矿工预览版，尚未完成本金赎回闭环。

## VPS Keeper 自动换期

当日实盘记录确认 Epoch **16 → 17** 自动换期。相关两笔交易如下（不对两笔交易分别归属哪个具体函数作推断）：

- [0xfd42c16c1563c3466876bfb3aae7b4d9b05d50ef5b9e6a9d3c9f942e1ac83d4c](https://bscscan.com/tx/0xfd42c16c1563c3466876bfb3aae7b4d9b05d50ef5b9e6a9d3c9f942e1ac83d4c)
- [0xf06a459736022b4da6630209edea08f2819daa08fd65c9d9fcd9880fbea0ad7f](https://bscscan.com/tx/0xf06a459736022b4da6630209edea08f2819daa08fd65c9d9fcd9880fbea0ad7f)

## 传统矿工 CLI

设备：**RTX 4060 Ti**。测试钱包：`0xE681561bCA5F08d50cD2135df25a3fafAdf933B8`。

- **deposit，0.01 BNB，已完成：** [0xbe337360d7b85352d8ea4f7ea56938e996b06afa12cbdc8fb8f02bdc9fd1900e](https://bscscan.com/tx/0xbe337360d7b85352d8ea4f7ea56938e996b06afa12cbdc8fb8f02bdc9fd1900e)
- **submitShares，Epoch 18，已完成：** [0x47c7b087fad9f60326a40ac392845311ea94825da5c3c55fe55ba75898245a07](https://bscscan.com/tx/0x47c7b087fad9f60326a40ac392845311ea94825da5c3c55fe55ba75898245a07)
- **claim，Epoch 18，已完成，领取 3096.8794294814475 SPKH：** [0x0ea736371298293b5fa66112fb28bb5e4709d13298f4f795723ff78ba0cb8417](https://bscscan.com/tx/0x0ea736371298293b5fa66112fb28bb5e4709d13298f4f795723ff78ba0cb8417)
- **requestWithdraw，已完成，冷却至 Epoch 67：** [0x431cd289ebf3b721eac23c32a6a685ae8deaf1008024def92a0a08f5b3341bf0](https://bscscan.com/tx/0x431cd289ebf3b721eac23c32a6a685ae8deaf1008024def92a0a08f5b3341bf0)

**待完成：0.01 BNB 本金赎回（withdraw）处于 48 Epoch 冷却中，待 Epoch 67 后补录回执。** 实际提交须先确认 Vault 当前 Epoch 达到冷却要求；申请退出成功不代表本金已经返还。

## 币安 Agentic Wallet

测试钱包：`0x61D32A0D16B0C6306E0E2EF2435e355478Cd8e6b`。

- **deposit，0.01 BNB，已完成：** [0xf4c08e3c6616fc1c9d1da423674c40ccff0e7d1a5049896b4bcfaada32f2608c](https://bscscan.com/tx/0xf4c08e3c6616fc1c9d1da423674c40ccff0e7d1a5049896b4bcfaada32f2608c)
- **submitShares，Epoch 19：** 本地 GPU 离线求解，币安 AI 复算哈希校验并由 Agentic Wallet 签名提交。实盘读数：`counted(19, 0x61D32A0D16B0C6306E0E2EF2435e355478Cd8e6b) = 1`。**提交交易哈希：待补录。** counted 读数与单笔交易回执分开记录。
- **claim，Epoch 19：待 2026-11-21 21:00（UTC+8）结算后补录。** 此处记录当日验收时的待办状态，未记录已领奖或奖励数量；实际领奖前仍须核对链上 Epoch、结算状态与领取标记。

## 三条实盘经验与预览版边界

1. **算力架构：** 币安 App 内置 AI 沙盒本次实测仅有 2 核 CPU，不适合直接跑主网 PoW。推荐生产模式为“本地 GPU/多核离线求解（不接触私钥，仅产出 k/nonce/calldata）+ 币安 Agentic Wallet 侧复算哈希校验、preview 预览并签名提交”。
2. **安全风控：** 本次币安 App 实测日限额最低为 $1,000，且暂未提供合约白名单界面。强烈建议新建独立小额隔离钱包，仅转入 0.03–0.05 BNB 用于质押与 Gas，并逐笔核对 preview 的目标合约与 value。此为当日 App 实测情况。
3. **路径指引：** 完整教程路径为 [`skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md`](../skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md)，入口为 [`skills/sparkhash-agentic-miner/SKILL.md`](../skills/sparkhash-agentic-miner/SKILL.md)。外部 AI Agent 应从仓库根目录按完整路径读取。

矿工 CLI 的网络误标已在提交 `2351a032ae3b2c4b068395f96921f1be5c7cd60b` 修复：广播警告根据已验证的 chain_id 动态显示 `BSC MAINNET` / `BSC TESTNET`。CPU/GPU 求解器也按 chain_id 显示网络名称。该显示修复不改变合约规则。

本次文档更新的本地回归：`python -m pytest test/test_miner.py test/test_miner_gpu.py test/test_safe_output_miners_monitor.py test/test_scripts_safe_output.py -q`，结果 **55 passed**，包含主网/测试网广播文案回归。该结果是本地测试，不是新增的链上交易证据。
