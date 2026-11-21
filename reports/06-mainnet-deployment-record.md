# SparkHash（SPKH）BSC 主网部署备案

- 网络：BNB Smart Chain 主网（BSC，Chain ID `56`）
- 合约部署区块范围：`134711138` 至 `134711154`（含首尾）
- 记录来源：[`broadcast/Deploy.s.sol/56/run-latest.json`](../broadcast/Deploy.s.sol/56/run-latest.json)
- 源文件 SHA-256：`ae45a84a21a3512c72278d5cb8256bd6a3dd62113b4b83a6eb0b39ebd23e9538`

## 合约地址及部署区块

- **Token（SPKH）**：`0xcce53217a4f2850c536d751cfb676da26bd372dc`
  - 部署区块 `134711142`（`0x8078766`）
  - 部署交易 `0x23a699cc26a7adf648b82cd6d861ef3c23915db25b5daa6dca201827de5ee04c`
- **TeamVestingWallet**：`0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e`
  - 部署区块 `134711142`（`0x8078766`）
  - 所属部署交易 `0x23a699cc26a7adf648b82cd6d861ef3c23915db25b5daa6dca201827de5ee04c`（由 Token 创建）
- **StakeVault**：`0x45eb4e8a7a3070c8160e9a91fae002934d296591`
  - 部署区块 `134711146`（`0x807876a`）
  - 部署交易 `0x8eddd1a29bf03c880193614f653ab58fcfc728159493822a597d505689336bce`
- **Miner Implementation（MinerV1）**：`0xc74756ad1ca5ea01f46b81cc86f6068e76467457`
  - 部署区块 `134711150`（`0x807876e`）
  - 部署交易 `0x592cf74e008fec6e37a447338ecf9085e61e9529e1a06d97ad655ebb3e42ab38`
- **Miner Proxy（TransparentUpgradeableProxy）**：`0x614b5efd21cfa99cb893abf2fb02d791b3aea843`
  - 部署区块 `134711154`（`0x8078772`）
  - 部署交易 `0x68821777f99fcc4dcb300a596979d9595378efddbdb56a93d753efb491fac6c8`
- **ProxyAdmin**：`0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f`
  - 部署区块 `134711154`（`0x8078772`）
  - 所属部署交易 `0x68821777f99fcc4dcb300a596979d9595378efddbdb56a93d753efb491fac6c8`（由 Miner Proxy 创建）
- **TimelockController**：`0x565f007310ee7d28aa526a7a94df7b95d554be00`
  - 部署区块 `134711138`（`0x8078762`）
  - 部署交易 `0xa1baca79cc5e8612382cf8432028ec95eb877ed61316a196f6e4839471e968de`

## 取证依据

按 `transactions[].hash` 与 `receipts[].transactionHash` 匹配交易和回执。5 笔顶层 `CREATE` 交易的回执状态均为 `0x1`，回执中的 `contractAddress` 均与交易记录一致。部署区块取自对应回执的 `blockNumber`，同时列出十进制和原始十六进制值。

TeamVestingWallet 与 ProxyAdmin 的地址分别取自 Token 与 Miner Proxy 创建交易的 `additionalContracts[].address`，并与同一回执中的日志发出地址核对。两者的部署区块与所属创建交易相同。以上核对基于本地广播记录。
