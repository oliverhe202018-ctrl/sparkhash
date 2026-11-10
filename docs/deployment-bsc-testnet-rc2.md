# PoolCoin BSC 测试网 (chainId: 97) RC2 部署记录

## 1. 部署基本信息与合约地址

- **网络**: BSC Testnet (chainId 97)
- **挖矿起始时间 START**: `1794135600` (UTC: `2026-11-08 11:00:00 UTC`, 北京时间: `2026-11-08 19:00:00 UTC+8`)
- **部署基准提交**: `d34f1b80dcb8577b786badc4150f9bc771d5cdb5`
- **与当前 HEAD 关系**:
  - `git merge-base --is-ancestor d34f1b80dcb8577b786badc4150f9bc771d5cdb5 HEAD` 退出码为 0，确认部署基准提交是当前 HEAD 的直系祖先；
  - `git diff --stat d34f1b80dcb8577b786badc4150f9bc771d5cdb5 HEAD -- src` 结果为 0 差异（0 文件变动，0 行增减），证明链上部署的智能合约源码与当前 HEAD 中 `src/` 下合约代码完全一致。

### 核心合约与管理组件地址表

| 组件名称 | 合约类型 | 链上地址 (Checksum) | 说明 |
| :--- | :--- | :--- | :--- |
| **TimelockController** | 治理时间锁 | `0x038f2377A32581Dc3aa8d9f18386619cf72C832e` | 延迟 600 秒 (`minDelay = 600s`)，系统最高权限控制者 |
| **Token (TTK)** | ERC20 代币 | `0x4ca603774D8D3750481007683ce44C7d4c9b2C8d` | 本次未在链上核对 |
| **StakeVault** | 质押金库 | `0x40b92431de886Ea46117407c8f7123a31FB3F744` | 本次未在链上核对 |
| **MinerV1 实现** | 逻辑实现 | `0x16cB6B01330dAdf5aA71165b686F16D375eE026F` | 进度索引发行逻辑实现合约 |
| **Miner (代理)** | 业务代理 | `0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf` | TransparentUpgradeableProxy，对外业务唯一入口 |
| **ProxyAdmin** | 升级管理 | `0xac6be887a777a11326e2b31efbd63e071c460c01` | 代理管理员合约，所有权已移交 Timelock |

### 多签与部署实体

| 实体角色 | 链上地址 (Checksum) | 说明 |
| :--- | :--- | :--- |
| **Deployer** | `0x2C1460A56a1B9dFA7EAa252f46092962B2E47BD3` | 部署发起者地址 |
| **Proposer Safe** | `0xE5fE521AfD41f1aF00bB623CE5511a8412a68b47` | 治理提案多签 Safe |
| **Guardian Safe** | `0x8E9D51118C70906dE13E2FED7787B056b9268358` | 本次未在链上核对 |

---

## 2. 合约验证与发行设计

### 2.1 Sourcify 验证状态
- 部署时记录为除 ProxyAdmin 创建交易外均为 `exact_match`（部署当时记录，本次未重新验证）。

### 2.2 发行机制设计
- **发行机制**: 进度索引发行 (Progress-Indexed Emission)。
- **核心衰减系数**: `LAMBDA_WAD = 137639085754731`。
- **单地址单轮奖励上限**: 单地址单轮奖励上限为该轮配额的 20%(epochAddrCapBps=2000), 超出部分回到剩余发行池。
- **设计规范引用**: 详见设计文档 [docs/design-progress-indexed-emission.md](design-progress-indexed-emission.md)。

---

## 3. 验收与实盘演练记录

- **测试网端到端验收演练 (Epoch 22)**: 详见 [docs/rc2-testnet-rehearsal-epoch22.md](rc2-testnet-rehearsal-epoch22.md)。
- **测试网 Keeper 实盘验收记录 (Epoch 39)**: 详见 [docs/rc2-keeper-live-run-epoch39.md](rc2-keeper-live-run-epoch39.md)。

---

## 4. 2026-11-10 链上核对

- 代理的实现地址等于上表 MinerV1 实现；
- 代理的管理员等于上表 ProxyAdmin；
- ProxyAdmin 的所有者是 Timelock；
- Timelock 最小延迟为 600 秒；
- Proposer Safe 持有 Timelock 的提案角色(PROPOSER_ROLE)；
- Miner 代理的 START 为 1794135600。

注：本次未核对 Token 铸币权限、StakeVault 管理员与 Guardian 暂停权。

---

## 5. 安全边界声明

本文档仅记录 BSC 测试网 RC2 部署公开地址与参数，不包含任何密码、私钥、Keystore 内容或本机用户路径。
