> **说明**：本文件是 RC1 的历史记录，地址与 START 已被 RC2 取代，当前有效部署见 docs/deployment-bsc-testnet-rc2.md。

# PoolCoin BSC 测试网 (chainId: 97) 首次部署记录

## 1. 部署基本信息与合约地址

- **网络**: BSC Testnet (chainId 97)
- **部署时间**: 2026-11-05
- **广播记录文件**: `broadcast/Deploy.s.sol/97/run-latest.json`
- **挖矿起始时间 START**: `1793869200` (UTC: `2026-11-05 09:00:00`, 北京时间: `2026-11-05 17:00:00`)

### 核心合约与管理组件地址表

| 组件名称 | 合约类型 | 链上地址 (Checksum) | 说明 |
| :--- | :--- | :--- | :--- |
| **TimelockController** | 治理时间锁 | `0x75F344F2c5a79d945D873Edbdccf67e61e56C9de` | 最终系统最高权限控制者，延时 600s |
| **Token (TTK)** | ERC20 代币 | `0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29` | 铸币权限已移交 Miner 代理合约 |
| **StakeVault** | 质押金库 | `0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7` | 质押管理合约，Admin 已设为 Timelock |
| **MinerV1 (实现)** | 逻辑实现 | `0xE8dC8F90F631c23BaA5f81fBF86B79DF11d89EA9` | 逻辑实现合约，已锁定初始化防直接攻击 |
| **Miner (代理)** | 业务代理 | `0x420382a0f874c1F120621f847d81581e8737504e` | TransparentUpgradeableProxy，对外业务唯一入口 |
| **ProxyAdmin** | 升级管理 | `0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185` | 代理管理员合约，所有权已移交 Timelock |

---

## 2. 部署执行交易明细 (共 10 笔)

以下 10 笔交易完整记录于 `broadcast/Deploy.s.sol/97/run-latest.json`：

| 序号 | 交易类别 | 涉及合约 / 动作 | 交易哈希 (TxHash) | 区块高度 | 消耗 Gas (GasUsed) | Gas 单价 (wei) | 交易费用 (BNB) |
| :---: | :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| 0 | CREATE | TimelockController | `0x6df2a23d8edee92e47cb7f09a1fdc262adf8ca48cc3cfc23ad2a3425cd426a6e` | 140915116 | 1,630,581 | 1,000,000,000 (1.00 gwei) | 0.00163058 BNB |
| 1 | CREATE | Token | `0x56d25d4a412af806f786166618dfc9940ff1367840f8fa787a0270c6b0732987` | 140915123 | 768,865 | 1,000,000,000 (1.00 gwei) | 0.00076887 BNB |
| 2 | CREATE | StakeVault | `0x6ab7d4c76c0e4aeba522ab3b1f5359dbd44ccccef9c73124793dbb7189db991f` | 140915129 | 1,027,829 | 1,000,000,000 (1.00 gwei) | 0.00102783 BNB |
| 3 | CREATE | MinerV1 (实现) | `0x229fa212c61396f65bf0aefe5dd0b76d74fb54029cd4856ae0ef2ea7976487d1` | 140915136 | 1,795,332 | 1,000,000,000 (1.00 gwei) | 0.00179533 BNB |
| 4 | CREATE | TransparentUpgradeableProxy | `0x27d925df845a78e33a883b0ce00cf5bc8331c9e04bb7deacc3d1c052a3e43b4f` | 140915144 | 909,760 | 1,000,000,000 (1.00 gwei) | 0.00090976 BNB |
| 5 | CALL | Token.setMiner(proxy) | `0xad4921e9ca7d646c8821eb26c24490182dd7635495b1116604e4429f860f890f` | 140915151 | 45,274 | 1,000,000,000 (1.00 gwei) | 0.00004527 BNB |
| 6 | CALL | ProxyAdmin.transferOwnership | `0x6288f39d5787d03c525d2cb8e76815f3c31a621a938cae00449bd631c05cbf52` | 140915160 | 28,621 | 1,000,000,000 (1.00 gwei) | 0.00002862 BNB |
| 7 | CALL | Miner.transferAdmin(timelock) | `0x12f5435b789d407e9ca0068d4d96057ac9e4a8c070ace8e971a28ec718c32a36` | 140915166 | 33,518 | 1,000,000,000 (1.00 gwei) | 0.00003352 BNB |
| 8 | CALL | StakeVault.transferAdmin(timelock) | `0x839d37921d4027128587fedea2e52b09fa88657be9ce9fb1943af52dac41d3ef` | 140915173 | 28,588 | 1,000,000,000 (1.00 gwei) | 0.00002859 BNB |
| 9 | CALL | Timelock.renounceRole(admin) | `0x55ec0db89875b81b162b4ff26f286e86dbe3773c413ac7b7d6b826162715781a` | 140915181 | 24,623 | 1,000,000,000 (1.00 gwei) | 0.00002462 BNB |

---

## 3. 总花费统计

- **总 Gas 消耗**: `6,292,991` 单位 Gas
- **交易 Gas 单价**: `1,000,000,000 wei` (固定 1.00 gwei)
- **部署总成本**: `6,292,991,000,000,000 wei` = **`0.006292991 BNB`** (约 0.006293 BNB)

---

## 4. 部署执行命令

第二次成功部署时使用的 Forge 命令如下（包含 `--with-gas-price 1000000000` 规避 1 wei 价格估算问题，并采用 `--start-fresh` 强制清空旧广播重置 nonce）：

```bash
forge script script/Deploy.s.sol:DeployScript --rpc-url $env:RPC_URL --broadcast --slow -vvv --legacy --with-gas-price 1000000000 --start-fresh
```

---

## 5. 首次尝试异常与归档说明

- **初次尝试异常记录**: 在首次执行部署广播时，由于 Forge 自动估算 Gas 价格策略异常，估算出了 `1 wei` 的极端价格。该价格远低于 BSC 测试网矿工接单阈值（当前测试网 `eth_gasPrice` 约为 `0.1 gwei` = `100,000,000 wei`），导致广播交易一直卡在内存池中未出块、没有任何交易实际上链。
- **历史归档**: 初次未上链尝试已安全归档为 broadcast 目录下的历史记录：`broadcast/Deploy.s.sol/97/run-latest.1793862617212.json`。
