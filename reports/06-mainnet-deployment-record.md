# SparkHash（SPKH）BSC 主网部署备案

- 网络：BNB Smart Chain 主网（BSC，Chain ID `56`）
- 合约部署区块范围：`134711138` 至 `134711154`（含首尾）
- 公开证据为下列 5 笔 BSC 主网部署交易的链上回执。每笔交易的哈希链接均指向 BscScan。

## 合约地址及部署区块

- **Token（SPKH）**：`0xcce53217a4f2850c536d751cfb676da26bd372dc`
  - 部署区块 `134711142`（`0x8078766`）
  - 部署交易 [`0x23a699cc26a7adf648b82cd6d861ef3c23915db25b5daa6dca201827de5ee04c`](https://bscscan.com/tx/0x23a699cc26a7adf648b82cd6d861ef3c23915db25b5daa6dca201827de5ee04c)
- **TeamVestingWallet**：`0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e`
  - 部署区块 `134711142`（`0x8078766`）
  - 所属部署交易 `0x23a699cc26a7adf648b82cd6d861ef3c23915db25b5daa6dca201827de5ee04c`（由 Token 创建）
- **StakeVault**：`0x45eb4e8a7a3070c8160e9a91fae002934d296591`
  - 部署区块 `134711146`（`0x807876a`）
  - 部署交易 [`0x8eddd1a29bf03c880193614f653ab58fcfc728159493822a597d505689336bce`](https://bscscan.com/tx/0x8eddd1a29bf03c880193614f653ab58fcfc728159493822a597d505689336bce)
- **Miner Implementation（MinerV1）**：`0xc74756ad1ca5ea01f46b81cc86f6068e76467457`
  - 部署区块 `134711150`（`0x807876e`）
  - 部署交易 [`0x592cf74e008fec6e37a447338ecf9085e61e9529e1a06d97ad655ebb3e42ab38`](https://bscscan.com/tx/0x592cf74e008fec6e37a447338ecf9085e61e9529e1a06d97ad655ebb3e42ab38)
- **Miner Proxy（TransparentUpgradeableProxy）**：`0x614b5efd21cfa99cb893abf2fb02d791b3aea843`
  - 部署区块 `134711154`（`0x8078772`）
  - 部署交易 [`0x68821777f99fcc4dcb300a596979d9595378efddbdb56a93d753efb491fac6c8`](https://bscscan.com/tx/0x68821777f99fcc4dcb300a596979d9595378efddbdb56a93d753efb491fac6c8)
- **ProxyAdmin**：`0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f`
  - 部署区块 `134711154`（`0x8078772`）
  - 所属部署交易 `0x68821777f99fcc4dcb300a596979d9595378efddbdb56a93d753efb491fac6c8`（由 Miner Proxy 创建）
- **TimelockController**：`0x565f007310ee7d28aa526a7a94df7b95d554be00`
  - 部署区块 `134711138`（`0x8078762`）
  - 部署交易 [`0xa1baca79cc5e8612382cf8432028ec95eb877ed61316a196f6e4839471e968de`](https://bscscan.com/tx/0xa1baca79cc5e8612382cf8432028ec95eb877ed61316a196f6e4839471e968de)

## 取证依据

在 BSC 主网浏览器打开交易链接，或通过主网 RPC 的 `eth_getTransactionReceipt` 按哈希查询。5 笔顶层 `CREATE` 交易的 `status` 均为 `0x1`。用 `transactionHash`、`blockNumber` 和 `contractAddress` 核对上列交易哈希、区块号和顶层合约地址。

TeamVestingWallet 与 ProxyAdmin 分别由 Token 和 Miner Proxy 在各自的部署交易中创建。可在对应交易的内部交易记录中查看创建动作，并在同一回执的 `logs[].address` 中核对这两个子合约地址。两者的部署区块与所属交易相同。
