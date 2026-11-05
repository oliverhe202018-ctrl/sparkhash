# PoolCoin 智能合约审计准备交接文档 (Audit Preparation Package)

**工程名称**: PoolCoin Ecosystem  
**目标网络**: BSC Testnet (chainId: 97) / BNB Smart Chain Mainnet  
**编译器版本**: Solidity 0.8.24 (via Solc 0.8.24, optimizer=true, runs=200)  
**依赖框架**: Foundry / OpenZeppelin Contracts v5.7.0 / OpenZeppelin Contracts Upgradeable v5.7.0 (旧版本 v5.0.2, 仅供对比)  
**安全状态**: **静态分析未报告高危项，需独立审计** (已运行 Slither,未报告高危项,需独立审计，不可替代独立第三方安全审计)  
**文档生成时间**: 2026-11-04  

> ⚠️ **核心安全与审计说明：源码已验证 ≠ 已审计 (Source Code Verified ≠ Audited)**。
> 区块链浏览器（如 BscScan / Etherscan）上的“源码验证”（Standard-Json 绿标）仅代表链上部署的字节码与公布的 Solidity 源代码编译匹配，绝对不代表代码逻辑不存在安全漏洞、逻辑缺陷或经济模型风险，不可替代专业安全机构的独立深度审计。

---

## 1. 审计代码范围 (In-Scope Contracts)

| 合约路径 | 代码行数 (LOC) | 架构模式 | 核心功能与职责 |
| :--- | :--- | :--- | :--- |
| `src/Token.sol` | ~55 | 标准不可升级 ERC20 | 10 亿枚硬顶，10% 初始预铸，90% 挖矿铸造权单向绑定至 Miner |
| `src/StakeVault.sol` | ~170 | 不可升级 Staking Vault | 质押 BNB 资金池，48 轮提款冷却，多签熔断与 Timelock 治理隔离 |
| `src/MinerV1.sol` | ~400 | Transparent Upgradeable Proxy | PoW 份额验证、EMA 动态难度调控、等比数列硬上限、单地址 20% 奖励封顶 |
| `src/interfaces/IToken.sol` | ~15 | 接口定义 | Token 核心只读属性与铸币接口 |
| `src/interfaces/IVault.sol` | ~20 | 接口定义 | StakeVault 状态查询与质押状态校验接口 |
| `src/interfaces/IMiner.sol` | ~25 | 接口定义 | Miner 轮次推进、份额提交与奖励领取接口 |

*注：`lib/openzeppelin-contracts` 与 `lib/openzeppelin-contracts-upgradeable` 属于经业界充分实战审计的开源标准库，不纳入定制逻辑审计，但需核验其引入方式与插槽兼容性。*

---

## 2. 核心数学与业务不变量清单 (System Invariants)

以下不变量已全部在 `test/Invariant.t.sol` 及相关单元测试中实现形式化/状态机断言（Fuzz & Invariant Testing）：

### 不变量 1: 挖矿产出总额绝对单调不超发 (Total Mined Bound)
$$\sum_{e=0}^{\infty} \text{minted}(e) \le 900,000,000 \times 10^{18} \text{ TTK}$$
- **验证手段**: 闭式等比数列在第 1394 天精确封顶，第 1394 天当轮未分配额度由最后一轮差额填平，第 1395 天起 `effectiveQuota` 恒为 0；底层 `Token.sol` 设置有不可突破的 `MINING_CAP` require 检查。

### 不变量 2: 质押资金池绝对偿付能力与零挪用 (Vault Solvency & Non-Custodial)
$$\text{address}(\text{vault}).\text{balance} \ge \sum_{u \in \text{Stakers}} \text{stakes}[u].\text{depositAmount}$$
- **验证手段**: 无论系统如何暂停，Vault 没有任何将资金转出给外部管理员的逻辑，`withdraw()` 仅能将对应用户存入的 `depositAmount` 返还给原调用者，确保 100% 准备金偿付。

### 不变量 3: 提款与历史收益领取的永不可暂停性 (Unpausable Core Rights)
$$\forall \text{ state } (\text{paused} \in \{\text{true}, \text{false}\}), \quad \text{canCall}(\text{withdraw}) \equiv \text{true} \land \text{canCall}(\text{claim}) \equiv \text{true}$$
- **验证手段**: `pause()` 与 `pauseSubmissions()` 仅阻断新质押存入和新 PoW 份额提交，`withdraw()` 与 `claim()` 代码中无任何 `paused` 修饰符，杜绝管理层恶意锁死资产。

### 不变量 4: 历史已结束轮次奖励绝对不可追溯篡改 (Reward Snapshot Immutability)
$$\forall e < \text{currentEpoch}, \quad \text{claimReward}(e, u) \text{ is invariant under } \frac{\partial \text{S\_STAR}}{\partial t} \land \frac{\partial \text{ADDR\_CAP\_BPS}}{\partial t}$$
- **验证手段**: 治理参数在每轮**首次被 `sync()` 推进至该轮时固化**至 `epochSStar[n]` 与 `epochAddrCapBps[n]` 映射中。后续 Admin 的任何修改仅影响未来的未快照轮次，历史收益在数学上永远确定。

### 不变量 5: 单地址单轮奖励 20% 硬封顶 (Single-Address Anti-Monopoly Cap)
$$\forall e, \forall u, \quad \text{reward}(e, u) \le \frac{\text{effectiveQuota}(e) \times \text{epochAddrCapBps}[e]}{10000}$$
- **验证手段**: 在 `claim(e)` 计算中，`capReward = (eq * capBps) / 10000` 作为上限截断，不论算力多大均不可能突破该上限。

### 不变量 6: 单轮次份额索引唯一性与无重放 (Share Nonce Uniqueness)
$$\forall e, \forall u, \quad \text{sharesCounted}(e, u) \le 15 \land \forall k_1, k_2, \quad (k_1 = k_2 \implies \text{revert})$$
- **验证手段**: 合约使用 `uint16 usedMask` 按位记录 $k \in [0, 14]$，一次提交中相同 $k$ 或跨交易重放相同 $k$ 均会触发位运算冲突回滚。

---

## 3. 已知技术权衡与妥协清单 (Known Trade-offs & Compromises)

1. **`sync()` 循环补齐与 Gas 预算限制**:
   - **设计**: 若链上长期处于低活跃状态导致多个轮次未被调用，`sync()` 需循环推进状态。
   - **妥协**: 为防止单次交易消耗超过区块 Gas 上限（BSC 主网区块 Gas 上限 140M 标注为未验证，以实时链上网络参数为准），设置 `maxSteps = 48`。若滞后超过 48 轮，需多笔交易分步追平。
2. **动态难度调控的 1 轮延迟 (Lag by 1 Epoch)**:
   - **设计**: EMA 难度更新使用 `totalShares[n-2]` 而非 `n-1`。
   - **权衡**: 牺牲了一轮的即时响应性，以彻底杜绝矿工在当轮提交尾声利用算力突变自反馈操控下轮难度的作弊可能。在 10 倍极端算力冲击下需要约 11 轮完全收敛。
3. **单地址 CAP 截断导致难度对触顶矿工脱敏**:
   - **实测结果**: 当大矿工算力远超 15 份/轮时，难度的上升在 3600 秒内无法削减其提交数量（仍然是 15 份），因此难度调整主要影响全网未触顶的散户边缘矿工。系统对女巫攻击的实质防御完全依赖质押金沉淀与冷冻时间。
4. **18 位定点数快速幂的微小舍入误差**:
   - **实测结果**: `_powFixed` 在计算 1394 天等比数列衰减时，由于底层整除截断，理论解析解与链上计算累计产生 $2.1 \times 10^{-8}$ TTK 的误差（相当于 0.000000021 个代币，小于 1 个 Gwei 粒度）。该误差在第 1394 天由硬顶差额结算函数精确消化，不破坏 9 亿枚封顶。

---

## 4. 测试套件与覆盖率说明 (Test Suites & Verification Matrix)

全套测试由 Foundry 原生执行，包含 14 个测试套件，82 个独立用例 (OpenZeppelin v5.7.0 升级后全量 PASS；旧版本阶段为 13 套件 72 用例，仅供对比)：

```text
Ran 14 test suites: 82 tests passed, 0 failed, 0 skipped (82 total tests)
```

### 套件分布与审计关注点映射：
- `test/GovernancePermissions.t.sol` (13 tests):
  - 重点验证 Guardian 多签与 Timelock 的权限完全隔离；
  - 验证 Guardian 仅有暂停权，绝无参数修改、合约升级或解除暂停权限；
  - 验证 Timelock 延迟排期不影响 Guardian 的紧急熔断能力；
  - 验证 TimelockController 角色分配：Proposer Safe 同时拥有 `PROPOSER_ROLE` 与 `CANCELLER_ROLE`（OpenZeppelin v5 默认机制）；Guardian Safe 在 Timelock 上无任何角色（无 PROPOSER、无 CANCELLER、无 EXECUTOR、无 DEFAULT_ADMIN），见用例 `test_CancellerRoleAssignedToProposerNotGuardian`。
- `test/StakeVault.t.sol` (10 tests):
  - 验证 `setDeposit` 严格受限于 `MIN_DEPOSIT`、`MAX_DEPOSIT`、`MAX_DEPOSIT_MULTIPLIER` (2倍) 与 `MIN_DEPOSIT_INTERVAL_EPOCHS` (24轮)；
  - 验证在不同质押费率变动下，用户的提取本金始终与存入时的记录严格按 1:1 精确退还。
- `test/AllocationCaps.t.sol` (5 tests):
  - 验证单地址 20% 奖励上限在单人垄断与多人竞争下的截断行为；
  - 验证参数修改对已结束轮次收益的快照锁定机制；
  - 验证未同步轮次在首次 sync 时的快照抓取时点。
- `test/ProxyUpgrade.t.sol` (3 tests):
  - 验证逻辑实现合约禁用初始化器；
  - 验证 ProxyAdmin 所有权与升级流程；
  - 验证通过 `__gap` 保留槽位在新增状态变量（如 guardian）时的存储布局完全平移一致性。
- `test/ReplayAndBoundaries.t.sol` (10 tests):
  - 验证跨交易、跨批次的 $k$ 索引位图重放防御；
  - 验证轮次边界 timestamp 的准入与拒绝；
  - 验证 `msg.sender` 切换导致 PoW 种子失效的密码学绑定。
- `test/Invariant.t.sol` (3 tests):
  - Invariant Testing: 状态机持续调用 2048 次，0 revert，严格保证代币不超发与 Vault 账面绝对偿付能力；
  - Fuzz Testing: 256 组随机数验证平方根定点数精度。
- `test/HardCap.t.sol`, `test/Difficulty.t.sol`, `test/Withdrawal.t.sol`, `test/Token.t.sol`, `test/Benchmark34Bit.t.sol`, `test/MinerV1.t.sol`, `test/SybilSimulation.t.sol`.

---

## 5. 待验证与未验证项清单 (Unverified Live Network Assumptions)

在进入 BSC 测试网演练与后续主网部署前，以下 4 个底层机制由于涉及链上真实执行环境与网络拓扑，属于**未在本地完全形式化验证项**，需在本次测试网演练中专项重点观测：

1. **`blockhash` 在真实测试网的行为与深度衰减 (Blockhash Real Behaviour)**:
   - **机制**: 轮次种子通过 `blockhash(prevBlock)` 抓取。
   - **未验证风险**: EVM 原生规则规定 `blockhash` 仅能访问最近 256 个区块；若发生极端网络中断、RPC 故障或 Keeper 停机导致 `initEpochSeed` 滞后超过 256 块，`blockhash` 将恒定返回 `bytes32(0)`。此外，测试网若发生深层区块重组 (reorg)，已固化种子的合法性与矿工提交的微弱回滚概率需实测监测。
2. **Keeper 时延与交易上链可靠性 (Keeper Execution Latency)**:
   - **机制**: 轮次推进与种子初始化完全依赖去中心化 Keeper 定时调用。
   - **未验证风险**: 真实测试网存在 Gas 波动、交易池堆积（Mempool congestion）以及公共 RPC 限流。Keeper 发起交易到最终落块若存在 1~3 分钟的不可控时延，将直接缩短该轮矿工的有效解算窗口。该时延对算力分布不同的矿工是否造成实际不公需长周期运行采集。
3. **验证者对种子的潜在影响 (Validator Influence on Blockhash)**:
   - **机制**: PoSA 验证节点轮流打包区块，区块哈希作为伪随机种子源。
   - **未验证风险**: 测试网验证节点集规模小、出块节点集中。理论上若单轮矿池累计额度极高，出块验证节点在博弈论视角下存在通过放弃出块或微调交易打包顺序轻微影响 `blockhash` 尾部比特的理论动机。该博弈在实际网络中是否存在观测迹象需结合链上数据分析。
4. **BSC 出块时间已多次变化 (BSC Block Time Variance)**:
   - **机制**: 轮次时长 `EPOCH_LEN = 3600` 秒由物理时钟（以太坊/BSC 标准 unix timestamp）度量。
   - **未验证风险**: BSC 网络在历史上历经多次硬分叉与出块时间调整（如 3 秒向更低延迟演进，或节点拥堵时的出块不均）。因此，任何将“轮次”与“区块高度”进行等价折算的链下脚本与文档均属经验估计，**必须以实时链上数据为准**；合约内已强制全生命周期使用 `block.timestamp`，但链下监控与 Keeper 调度间隔需依据实时出块动态调优。
5. **BSC 主网区块 Gas 上限 140M (未验证)**:
   - **机制**: 文档与链下估算中提及的 BSC 主网区块 Gas 上限 140M 属于未在当前环境独立验证的假设参数。
   - **未验证风险**: BSC 网络历次硬分叉及各网络环境参数可能演变，严禁作为硬编码或绝对保证依据，必须以实时链上网络节点参数为准。

---

## 6. 算力假设与数据说明 (Benchmarking Notes)

- **GPU 算力速度**: 所有 GPU 算力及产出估算（如 RTX 4090 @ 20 GH/s、Tier 1 1.7 GH/s 等）均标记为**假设**，仅作为压力测试模型输入。
- **CPU 算力数据**: 所有 10 核 CPU 算力及耗时数据均标记为**外推**（由单线程本地实测均值 1.424 MH/s 乘以核数推导，未计入内存总线争用开销；34 位下 [3.38%, 13.75%](均值 5.06%),均为 10 核外推值）。
---

## 7. 部署后治理漂移风险与链下监控方案 (Post-Deployment Governance Drift & Monitoring)

### 7.1 风险机理与链上局限性 (Mechanism & Limitations)
- **Safe 签名人动态可变性**: 根据 Gnosis / Safe 合约的原生标准架构，Safe 在部署后其签名人列表（`owners`）与阈值（`threshold`）允许由 Safe 自身通过多签交易在链上随时调用内置管理接口（`swapOwner`、`addOwnerWithThreshold`、`removeOwner`、`changeThreshold` 等）进行在线变更。
- **底层协议无法链上强阻止**: `PoolCoin` 协议核心合约（`TimelockController`、`StakeVault`、`MinerV1`）只能在部署执行瞬间或特定交互时通过接口读取外部 Safe 的状态。由于 EVM 合约之间无法建立反向被动监听 hook，底层协议无法在链上硬性拦截或阻断部署后 Safe 自发执行的签名人变更。
- **三大治理漂移威胁 (Governance Drift Threats)**:
  1. **签名人重叠漂移**: 部署后若多签持有者发起 Safe 提案，将 `TIMELOCK_PROPOSER_SAFE` 与 `GUARDIAN_SAFE` 的签名人名单变更为部分或全部重叠，原先设计的“提议权与紧急暂停权相互独立与物理制衡”的安全防线将被击穿；
  2. **部署者回权漂移**: 部署后若签名人通过提案重新将已被完全剥离管理权限的 `deployer` EOA 地址添加为任意 Safe 的签名人，部署时建立的“去中心化交接”事实将被推翻；
  3. **门槛降级漂移**: 若 Safe 内部阈值被变更为 1，多签机制即刻退化为单私钥单点控制风险。
- **时间锁权限与角色设计 (OpenZeppelin v5 规范)**:
  - **Proposer Safe 具备取消权**: `TIMELOCK_PROPOSER_SAFE` 同时拥有 `PROPOSER_ROLE` 与 `CANCELLER_ROLE`（OpenZeppelin v5 `TimelockController` 构造函数默认行为）。提案方在延迟公示期内若发现排期交易参数有误，可通过多签直接取消提案；
  - **Guardian Safe 零 Timelock 角色**: `GUARDIAN_SAFE` 在 `TimelockController` 上没有任何角色（无 PROPOSER、无 CANCELLER、无 EXECUTOR、无 ADMIN）。Guardian 的权限严格限于业务合约中的紧急暂停（`pauseSubmissions` / `pause`），杜绝其干预时间锁排期与治理生命周期。

### 7.2 链下自动化监控与告警方案 (Off-Chain Monitoring & Alerting Specification)
鉴于链上无法阻止事后签名人变更，项目必须建立永续运行的链下守护监控程序（集成至 `script/monitor.py` 或独立监控服务），运行策略如下：

1. **周期性只读轮询**: 监控服务每隔固定周期（测试网建议 15 分钟，主网建议 5 分钟）向 RPC 发送只读 `eth_call` 查询：
   - 调用 `TIMELOCK_PROPOSER_SAFE.getOwners()` 与 `getThreshold()`；
   - 调用 `GUARDIAN_SAFE.getOwners()` 与 `getThreshold()`；
   - 调用 `TIMELOCK_PROPOSER_SAFE.isOwner(deployer)` 与 `GUARDIAN_SAFE.isOwner(deployer)`。
2. **硬性监控断言与告警规则**:
   - **规则 A (零重叠断言)**: `set(proposer_owners).intersection(guardian_owners) == empty`。一旦检测到两个 Safe 的签名人存在任何交集，立即触发 **P0 严重级治理隔离失效告警**；
   - **规则 B (非部署者断言)**: `deployer not in proposer_owners and deployer not in guardian_owners`。一旦检测到包含部署者 EOA，立即触发 **P0 严重级特权回流告警**；
   - **规则 C (门槛与基数断言)**: 校验 `proposer_threshold >= 2`、`guardian_threshold >= 2`、`len(proposer_owners) >= 3`、`len(guardian_owners) >= 3`。任一条件不满足立即触发 **P1 级多签门槛降级告警**。
3. **告警分发机制**:
   - 一旦触发告警，通过 Telegram Bot、Discord Webhook 及 PagerDuty 实时推送，并在公开的社区运行看板上同步公示，确保任何治理漂移行为在公开网络中立即可见。

---

## 8. 主网前 Safe 加固检查技术评估 (Pre-Mainnet Safe Hardening Assessment)

*说明：本章节为进入主网部署前的技术可行性与安全价值评估，不涉及当前测试网合约的代码变更与逻辑实现。*

### 8.1 已知 Safe 版本比对评估 (Safe Canonical Version Verification)
- **检查对象**: 治理多签合约底层的 Singleton / MasterCopy 实现地址与版本。
- **技术可行性**:
  - Safe 官方合约提供标准只读视图函数 `VERSION()`，返回语义化版本字符串（如 `"1.3.0"` 或 `"1.4.1"`）；
  - 代理合约底层的 Singleton 地址可通过读取实现插槽或直接调用 `masterCopy()` 获取（具体使用方式以所用 Safe 版本的官方文档为准）；
  - Safe 官方在 GitHub 仓库发布了确定性的各链 Canonical 部署地址与 Extcodehash 白名单。
- **安全价值**:
  - 防范部署者或第三方误用或恶意部署私有修改版 Safe（例如带有后门函数、绕过签名的虚假多签实现）；
  - 确保主网治理完全运行在 Safe 官方发布的 Singleton 地址和长周期实战检验的官方 Safe 核心之上。
- **评估结论与建议**:
  - 主网部署前应收集官方在 BSC 主网部署的 Singleton 地址白名单，在主网部署预检脚本中硬编码比对 `VERSION()` 及 `masterCopy()` 地址与代码哈希；
  - **白名单必须对应实际使用的 Safe 服务**：白名单必须对应实际使用的 Safe 服务（BNB Safe 为分叉，可能有不同 Singleton），主网前先确定 Safe 服务再制定白名单；
  - 该项加固具有高安全防护收益且不增加运行时 Gas，推荐主网部署前实施。

### 8.2 Safe 模块未启用检查评估 (Safe Modules Disabled / Zero-Module Policy)
- **检查对象**: Safe 合约是否启用了任何扩展模块（Modules）。
- **技术可行性**:
  - Safe 原生提供了 `getModulesPaginated(address start, uint256 pageSize)` 接口（具体使用方式以所用 Safe 版本的官方文档为准）；
  - 传入哨兵地址 `0x0000000000000000000000000000000000000001` 查询，若返回数组长度为 0，即证明未启用任何扩展模块。
- **安全价值**:
  - **模块特权绕过风险**: Safe 的 Module 具备最高执行权限，任何已启用的模块均可调用 `execTransactionFromModule(...)` 直接代表 Safe 执行任意交易，**完全无需收集任何签名人的多签签名，直接绕过 threshold 限制**；
  - **后门防范**: 若恶意部署者在初始化 Safe 时预先挂载了受其单方控制的自定义模块，即便多签门槛设置为 2/3，该模块持有者依然拥有对 Safe 的绝对控制权。
- **评估结论与建议**:
  - 对于 PoolCoin 的 `TIMELOCK_PROPOSER_SAFE` 与 `GUARDIAN_SAFE` 这种纯粹用于时间锁提案与紧急暂停的治理实体，**必须严格执行零模块策略（Zero-Module Policy）**；
  - 主网部署前置检查中必须调用 `getModulesPaginated`，确认模块列表为空；若非空，一律拒绝部署。


---

## 9. 存储布局基线与依赖升级兼容性比对 (Storage Layout & Upgrade Compatibility)

### 9.1 OpenZeppelin v5.7.0 升级与基线比对结果
- **核心基线对比**:
  - `MinerV1` 合约的基础状态变量槽位（Slot 0 至 Slot 20 包括 `__gap[47]`）在 OpenZeppelin 从 `v5.0.2` 升级至 `v5.7.0` 后**保持 100% 绝对一致，零错位、零偏移**；
  - `Initializable` 的 ERC-7201 存储命名空间槽位（`0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00`）完全一致；
  - `ReentrancyGuard` 差异说明：
    - 在 OpenZeppelin `v5.0.2` 中，`ReentrancyGuardUpgradeable` 声明了结构体状态变量，被 Solc 静态导出在 slot `0x9b779b17422d0df92223018b32b4d1fa46e071723d6817e2486d003becc55f00`；
    - 在 OpenZeppelin `v5.7.0` 中，官方按设计移除了 `ReentrancyGuardUpgradeable`，统一推荐由 `@openzeppelin/contracts/utils/ReentrancyGuard.sol` 替代（标注为 `@custom:stateless`），运行时通过 `StorageSlot.getUint256Slot` 动态访问常量哈希槽位 `0x9b779b17422d0df92223018b32b4d1fa46e071723d6817e2486d003becc55f00`；
    - **结论**：运行时访问的物理 EVM 槽位数值与 v5.0.2 完全同一，但由于 v5.7.0 不再声明 Solidity 状态变量，Solc AST 存储布局中不再单独列出该条目；所有合约插槽与升级兼容性测试 (`test_StorageLayoutConsistencyAcrossUpgrade`) 均 100% 通过。
- **基线文件 SHA-256 哈希**:
  - `storage-layout.baseline.json`:
    - 当前升级基线 (v5.7.0): `035cb90b1ffbe49847d3691a84ead630ce0aca88a2857c26704d6faa00bee2b5`
    - 历史旧基线 (v5.0.2, 仅供对比): `777546b0564f4e17b9e4b7ecf69130c3a22a635e2e086579e2db4c12d4b8da2c`
  - `storage-layout.baseline.txt`:
    - 当前升级基线 (v5.7.0): `a4686c571b7aae66dd498e7c62a4051f7845dde4c971add72dbeb18ed17c374a`
    - 历史旧基线 (v5.0.2, 仅供对比): `1132bf40ab5c71898244851a02a83532220698aea12c9cefef4c26d3f57a1976`
