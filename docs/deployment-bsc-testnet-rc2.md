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
| **Token (TTK)** | ERC20 代币 | `0x4ca603774D8D3750481007683ce44C7d4c9b2C8d` | 非代理 Token，元数据为 TESTTOKEN/TTK/18，铸币调用方已固定为 Miner 代理地址 |
| **StakeVault** | 质押金库 | `0x40b92431de886Ea46117407c8f7123a31FB3F744` | 质押金库，admin 为 Timelock，guardian 为 Guardian Safe |
| **MinerV1 实现** | 逻辑实现 | `0x16cB6B01330dAdf5aA71165b686F16D375eE026F` | 进度索引发行逻辑实现合约 |
| **Miner (代理)** | 业务代理 | `0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf` | TransparentUpgradeableProxy，对外业务唯一入口；代理升级管理员为 ProxyAdmin，Miner 业务 admin() 为 Timelock，guardian() 为 Guardian Safe |
| **ProxyAdmin** | 升级管理 | `0xac6be887a777a11326e2b31efbd63e071c460c01` | 代理管理员合约，所有权已移交 Timelock |

### 多签与部署实体

| 实体角色 | 链上地址 (Checksum) | 说明 |
| :--- | :--- | :--- |
| **Deployer** | `0x2C1460A56a1B9dFA7EAa252f46092962B2E47BD3` | 部署发起者地址 |
| **Proposer Safe** | `0xE5fE521AfD41f1aF00bB623CE5511a8412a68b47` | 治理提案多签 Safe |
| **Guardian Safe** | `0x8E9D51118C70906dE13E2FED7787B056b9268358` | 安全多签 Safe，持有 Miner 与 StakeVault 的单向紧急暂停权 |

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
- Proposer Safe 持有 Timelock 的提案角色 (PROPOSER_ROLE)；
- Miner 代理的 START 为 1794135600；
- **Token 铸币权限与 setMiner 语义**：在区块 141877607 / 141880596 读得 Token 的 `miner()` 为 `0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf`（Miner 代理）。根据 `src/Token.sol:46-52`，`setMiner` 包含 `if (miner != address(0)) revert MinerAlreadySet();` 检查，仅限 deployer 执行一次；因链上当前读数非零，该方法已被永久锁定，deployer 及任何人均无法再次修改。结合 `src/Token.sol:56-63` 仅限 `miner` 调用的校验，Token 铸币权限已固定为 Miner 代理地址。系统架构说明：此处固定的是可调用 mint 的 Miner 代理地址，并不代表 Miner 实现逻辑不可升级；Miner 代理实现升级由 ProxyAdmin 及其所有者 Timelock 治理控制；此外 Token 合约自身的 `MINING_CAP`（挖矿总量硬顶 9 亿枚，非单轮上限）与 `MAX_SUPPLY`（总量硬顶 10 亿枚）检查仍独立执行；
- **StakeVault 与 Miner 代理的 admin() 归属**：在区块 141878674 核验，StakeVault 与 Miner 代理的 `admin()` 均为 Timelock (`0x038f2377A32581Dc3aa8d9f18386619cf72C832e`)；
- **Guardian 暂停权与权限分离**：在区块 141878674 核验，StakeVault 与 Miner 代理的 `guardian()` 均为 Guardian Safe (`0x8E9D51118C70906dE13E2FED7787B056b9268358`)。源码证据表明 Guardian 仅具备单向紧急暂停权（`src/MinerV1.sol:422-426` 的 `pauseSubmissions()` 与 `src/StakeVault.sol:150-155` 的 `pause()`），只有 `admin`（Timelock）可解除暂停（`src/MinerV1.sol:428-432` 的 `unpause()` 与 `src/StakeVault.sol:157-162` 的 `unpause()`）；当前两合约的 `paused()` 均为 `false`；
- **Token 元数据与主网规划**：在区块 141877607 核验，Token 元数据链上读数确定为 `TESTTOKEN` / `TTK` / `18`。源码为非代理 Token，未提供名称、符号或精度修改接口，现有 RC2 实例不能通过合约接口改名，部署后永久不可变更；因此若主网启用新品牌候选（如候选中文名“星豆”、候选英文名“Starbean”，此名称仅为品牌候选，尚未最终定名），必须在主网部署新 Token 合约。

注：本次已完成 Token 铸币权限、StakeVault 管理员与 Guardian 暂停权核对。创建交易原始 initcode 反解及第三方浏览器源码认证页面仍待核对。

---

## 5. 安全边界声明

本文档仅记录 BSC 测试网 RC2 部署公开地址与参数，不包含任何密码、私钥、Keystore 内容或本机用户路径。
