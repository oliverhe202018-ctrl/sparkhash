# PoolCoin 链上监控指标规格说明书 (Monitoring Specification)

本文档定义 PoolCoin 核心智能合约（`MinerV1`、`StakeVault`、`Token`）的 14 项关键监控指标，涵盖链上数据源、计算方式、更新频率、合约接口（Getter 与事件签名/选择器/Topic0）及异常处置指引。

---

## 1. 监控架构与设计原则

- **只读零副作用原则 (Read-Only Zero Side-Effect)**：监控系统仅使用 `eth_call` 与只读 `eth_getLogs`，严禁持有任何私钥，严禁发送写交易或向外部第三方服务回传未经脱敏的敏感连接信息与密钥。
- **治理分级响应**：
  - **Guardian (紧急守护多签)**：持有直接暂停权（`MinerV1.pauseSubmissions()`、`StakeVault.pause()`），无需经过时间锁延时，用于防范严重漏洞、代币发行失衡或女巫洪水攻击。
  - **Timelock (治理时间锁)**：负责参数调整（如 `setDeposit`、`setSStar`、`setAddrCapBps`、`unpause` 等），主网排期延时待定，按 24 到 48 小时估计。
- **双版本模型兼容 (rc1 vs rc2)**：
  - **rc1 (日历递减旧模型)**：未开辟 `remainingEmission` (Slot 21) 与 `settledEpochQuota` (Slot 22)，仅能校验可用项（总铸造上限、总供应量硬顶与单轮结算配额）。
  - **rc2 (按进度递减模型)**：已上线 `remainingEmission` 动态递减机制与 `settledEpochQuota` 历史固化，支持“已铸造 + 待领取 + 剩余可分配 = 9 亿”全局封闭守恒校验。

---

## 2. 合约核心实体与交互拓扑

| 合约实体 | 标准角色 | 测试网部署代理地址 | 源码路径 | 核心职责 |
| :--- | :--- | :--- | :--- | :--- |
| **Miner (Proxy)** | 业务代理入口 | `0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf` | `src/MinerV1.sol` | PoW 份额验证、权重累加、轮次结算、难度自适应与奖励 Claim |
| **StakeVault** | 矿工质押金库 | `0x40b92431de886Ea46117407c8f7123a31FB3F744` | `src/StakeVault.sol` | 矿工 0.01 BNB 质押管理、资格判定与 48 轮冷却退出 |
| **Token (TTK)** | ERC20 代币 | `0x4ca603774D8D3750481007683ce44C7d4c9b2C8d` | `src/Token.sol` | 10 亿总量硬顶、9 亿挖矿上限铸币，仅限 Miner 代理铸造 |
| **TimelockController** | 治理时间锁 | `0x038f2377A32581Dc3aa8d9f18386619cf72C832e` | OpenZeppelin | 系统最高权限实体，所有参数调整均需排期延迟生效 |
| **Guardian Safe** | 应急守护多签 | `0x8E9D51118C70906dE13E2FED7787B056b9268358` | Gnosis Safe | 突发事故秒级熔断暂停权 |

---

## 3. 14 项监控指标技术规格

### 3.1 每轮活跃地址数 (Active Addresses per Round)
- **链上来源**:
  - 事件: `MinerV1.SharesSubmitted(address indexed u, uint256 indexed epoch, uint256 n, uint256 weightAdded)`
    - Topic0: `0xf50e89c738e371409f94eeeac48fe75568269cfaed112e683bf11d4b4684b214`
    - Topic1: `address u` (32 bytes 补齐)
    - Topic2: `uint256 epoch`
  - Getter (点对点核查): `MinerV1.counted(uint256 epoch, address user) -> uint8`
    - 选择器: `0x834f9a82`
- **计算方式**:
  - 检索目标轮次 $e$ 对应的所有 `SharesSubmitted` 事件日志，提取矿工地址集合 $U_e = \{ u \mid \text{event.epoch} == e \}$，计算集合势 $|U_e|$。
  - 即 $\text{ActiveMiners}(e) = \text{Count}(\text{Unique}(u))$。
- **更新频率**:
  - 轮次进行中：随矿工提交交易实时递增；
  - 轮次结束：该轮时长（3600 秒）耗尽后数值固化。

---

### 3.2 每轮份额数 (Shares per Round)
- **链上来源**:
  - Getter: `MinerV1.totalShares(uint256 epoch) -> uint256`
    - 选择器: `0x13f2dad0`
  - 事件: `MinerV1.SharesSubmitted(address indexed u, uint256 indexed epoch, uint256 n, uint256 weightAdded)`
    - 事件 Data 字段前 32 字节表示该笔交易提交的份额数 `n`。
- **计算方式**:
  - 直接调用合约 Getter: `totalShares(e)`；
  - 或对当轮所有 `SharesSubmitted` 事件中的 `n` 求和: $\text{TotalShares}(e) = \sum_{i} n_i$。
- **更新频率**:
  - 实时更新，轮次结束后完全固定。

---

### 3.3 总权重 (Total Weight)
- **链上来源**:
  - Getter: `MinerV1.totalWeight(uint256 epoch) -> uint256`
    - 选择器: `0x72e78e1c`
  - 事件: `MinerV1.EpochSettled(uint256 indexed epoch, uint256 effectiveQuota, uint256 totalWeight, uint256 wMin, uint256 unallocatedDilution)`
    - Topic0: `0xdd61069809ef8d608890acc089aa62bd79a1b97490fdb3b8c9c5b96caeeb45c5`
    - Data 第 64~128 字节解析为 `totalWeight`。
- **计算方式**:
  - 直接读取 `totalWeight(e)`，单位为 $10^{10}$（基于 $\sqrt{c} \times 10^6 \times \text{rampBps}$，其中基准 $\text{WSCALE} = 10^{10}$）。
- **更新频率**:
  - 轮次进行中随矿工调用 `submitShares` 动态累加，轮次结算（`sync`）后冻结。

---

### 3.4 wMin (最小权重门槛)
- **链上来源**:
  - Getter: `MinerV1.wMin(uint256 e) -> uint256`
    - 选择器: `0xcf9eb447`
  - 事件: `EpochSettled` 中第 3 个 Data 字段 `wMin`。
- **计算方式**:
  - 合约纯函数逻辑:
    $$d = \lfloor e / 24 \rfloor$$
    $$\text{wMin}(e) = \begin{cases} 10 \times 10^{10} = 10^{11} & d < 30 \text{ (前 30 天)} \\ 50 \times 10^{10} = 5 \times 10^{11} & 30 \le d < 90 \text{ (30~90 天)} \\ 100 \times 10^{10} = 10^{12} & d \ge 90 \text{ (90 天以后)} \end{cases}$$
- **更新频率**:
  - 由轮次编号严格确定，每 24 轮（1 天）前移，关键阶梯变化位于第 720 轮与第 2160 轮。

---

### 3.5 分配比例 tw/wMin (Allocation Ratio)
- **链上来源**:
  - 结合 `MinerV1.totalWeight(e)` 与 `MinerV1.wMin(e)`。
- **计算方式**:
  $$\text{AllocationRatio}(e) = \frac{\text{totalWeight}(e)}{\text{wMin}(e)}$$
  - 若 $\text{totalWeight}(e) \ge \text{wMin}(e)$，分配比例达到 100%（全额分配当轮配额）；
  - 若 $\text{totalWeight}(e) < \text{wMin}(e)$，分配比例低于 100%，实际可供全体矿工瓜分的配额按此比例折算。
- **更新频率**:
  - 轮次结算时确定（每小时结算一次）。

---

### 3.6 未分配额度占比 (Unallocated Quota Ratio)
- **链上来源**:
  - Getter: `MinerV1.unallocatedGap(uint256 epoch) -> uint256`
    - 选择器: `0xca6875f7`
  - Getter (rc2): `MinerV1.settledEpochQuota(uint256 epoch) -> uint256`
    - 选择器: `0x67ef6de5`
  - Getter (兼容): `MinerV1.effectiveQuota(uint256 epoch) -> uint256`
    - 选择器: `0xca97a081`
  - 事件: `EpochSettled` 中参数 `unallocatedDilution` (B3) 与 `effectiveQuota`。
- **计算方式**:
  - 对于已结算轮次：
    $$\text{UnallocatedRatio}(e) = \frac{\text{unallocatedGap}(e)}{\text{effectiveQuota}(e)}$$
  - 合约结算公式中：若 $\text{totalWeight}(e) < \text{wMin}(e)$，则 $B_3 = q_e - \frac{q_e \times \text{totalWeight}(e)}{\text{wMin}(e)}$，占比即为 $1 - \frac{\text{totalWeight}(e)}{\text{wMin}(e)}$。
- **更新频率**:
  - 轮次结算（`sync`）执行时写入状态，永久不变。

---

### 3.7 难度位数与 ema 相对 S_STAR (Difficulty Bits & EMA Ratio)
- **链上来源**:
  - Getter: `MinerV1.target(uint256 epoch) -> uint256` (选择器 `0x9811c7c1`)
  - Getter: `MinerV1.ema() -> uint256` (选择器 `0xd7247ec8`)
  - Getter: `MinerV1.S_STAR() -> uint256` (选择器 `0x567154bd`)
  - Getter: `MinerV1.epochSStar(uint256 epoch) -> uint256` (选择器 `0x0f4b65b3`)
  - 事件: `MinerV1.EpochSynced(uint256 indexed epoch, uint256 target, uint256 ema)`
    - Topic0: `0xc7457f76c4fde7f674686f6aa699ce92c9830d6cd0f54b406f4249acb3aa5e41`
- **计算方式**:
  1. **难度前导零位数 (Difficulty Bits)**:
     $$\text{DifficultyBits}(e) = 256 - \log_2(\text{target}(e))$$
     - 最易难度：$\text{EASIEST} = 1 \ll 224 \implies 256 - 224 = 32.0$ 位。
     - 最难难度：$\text{HARDEST} = 1 \ll 221 \implies 256 - 221 = 35.0$ 位。
  2. **EMA 相对期望比例**:
     $$\text{EMA\_Ratio} = \frac{\text{ema}}{\text{S\_STAR}}$$
     - 比值 $= 1.0$：实际算力与目标完全匹配；
     - 比值 $> 1.0$：算力超出预期，下一轮难度将调难（`target` 下调，前导零位数增加）；
     - 比值 $< 1.0$：算力低于预期，下一轮难度将调易（`target` 上调，最多到 32 位）。
- **更新频率**:
  - 每轮结算（`sync`）时执行指数平滑衰减并写入新周期的 `target`。

---

### 3.8 满 CAP 地址占比 (Full CAP Address Ratio)
- **链上来源**:
  - Getter: `MinerV1.counted(uint256 epoch, address miner) -> uint8`
  - 常量: `MinerV1.CAP() = 6`
  - 事件: 遍历当轮 `SharesSubmitted` 累计得到各活跃地址在该轮的最终 `counted`。
- **计算方式**:
  - 在当轮所有活跃地址 $U_e$ 中，筛选 `counted == 6` 的满配地址集合 $U_{\text{full}}$：
    $$\text{FullCapRatio}(e) = \frac{|U_{\text{full}}|}{|U_e|} = \frac{\text{Count}(\{u \in U_e \mid \text{counted}[e][u] = 6\})}{|U_e|}$$
  - 若 $|U_e| = 0$，指标记为 0% 或 N/A。
- **更新频率**:
  - 轮次进行中动态统计，轮次结束时最终确定。

---

### 3.9 单地址与前 10 地址权重占比 (Top 1 & Top 10 Weight Share)
- **链上来源**:
  - Getter: `MinerV1.userWeight(uint256 epoch, address u) -> uint256` (选择器 `0xc3448b7e`)
  - Getter: `MinerV1.totalWeight(uint256 epoch) -> uint256`
  - Getter: `MinerV1.ADDR_CAP_BPS()` (选择器 `0xbc9c68ae`，默认 2000 = 20%)
- **计算方式**:
  - 对当轮所有活跃矿工权重降序排列：$w_{(1)} \ge w_{(2)} \ge \dots \ge w_{(k)}$。
  1. **单地址最大占比 (Top 1)**:
     $$\text{Top1Ratio}(e) = \frac{w_{(1)}}{\text{totalWeight}(e)}$$
  2. **前 10 地址总占比 (Top 10)**:
     $$\text{Top10Ratio}(e) = \frac{\sum_{i=1}^{\min(10, k)} w_{(i)}}{\text{totalWeight}(e)}$$
- **业务意义**:
  - 单地址奖励硬顶为 `ADDR_CAP_BPS`（20%），超出部分被截断重入剩余池。若 Top 1 权重 $> 20\%$，意味着触发大户惩罚与截断机制；若前 10 地址占比 $> 80\%$，表明网络存在严重的寡头化甚至 Sybil 风险。
- **更新频率**:
  - 轮次进行中实时统计，轮次结算后固化。

---

### 3.10 新增质押地址数及出资来源聚类 (New Stakers & Funding Source Clustering)
- **链上来源**:
  - 合约: `StakeVault` (`0x40b92431de886Ea46117407c8f7123a31FB3F744`)
  - 事件: `StakeVault.Deposited(address indexed u, uint256 epoch, uint256 amount)`
    - Topic0: `0x73a19dd210f1a7f902193214c0ee91dd35ee5b4d920cba8d519eca65a7b488ca`
  - 交易调用源: `eth_getTransactionByHash` 获取交易 `from` 字段或资金前序转账源。
- **计算方式**:
  1. **24 小时新增质押数**:
     $$\text{NewStakers24h} = \text{Count}(\{ u \mid \text{Deposited in } [\text{block}_{\text{now}} - 28800, \text{block}_{\text{now}}] \})$$
  2. **出资来源聚类 (Clustering by Funder)**:
     - 对这批 `deposit()` 交易追溯其资金归集发起者 $F = \text{tx.from}$（或分析向其分发刚好 0.01 BNB 的中心分发钱包）：
     - 若单个出资地址 $F$ 在 24 小时内资助了 $M \ge 5$ 个不同的新质押地址，则判定该出资来源为一个疑似 Sybil 集群；
     - 聚类聚合度度量：
       $$\text{MaxClusterShare} = \frac{\max_F (\text{Count}(\text{Stakers funded by } F))}{\text{NewStakers24h}}$$
- **更新频率**:
  - 事件驱动，每小时或每天按滑动窗口更新。

---

### 3.11 同步滞后 (Sync Lag)
- **链上来源**:
  - Getter: `MinerV1.epoch() -> uint256` (选择器 `0x900cf0cf`)
  - Getter: `MinerV1.lastSynced() -> uint256` (选择器 `0x93afb4c5`)
- **计算方式**:
  $$\text{SyncLag} = \text{epoch}() - \text{lastSynced}()$$
  - $\text{SyncLag} = 0$：系统处于健康完全同步状态；
  - $\text{SyncLag} \ge 1$：说明轮次推进但尚未执行 `sync()` 结算，此时矿工无法提交当前轮份额（合约强制校验 `lastSynced == e`），且未结算轮次无法 Claim 奖励。
- **更新频率**:
  - 实时（秒级 / 每个区块均可获取）。

---

### 3.12 当轮种子是否就绪 (Current Round Seed Ready Status)
- **链上来源**:
  - Getter: `MinerV1.epochSeed(uint256 epoch) -> uint256`
    - 选择器: `0x4abccd28`
  - 事件: `EpochSeedInitialized(uint256 indexed epoch, uint256 seed)`
    - Topic0: `0x39d7463eafe5f8fd37df0bc31b3418db8f0e961fca9b34bfa2169e2c22544f5e`
- **计算方式**:
  - 读取当前轮 $e = \text{epoch}()$ 的种子：
    $$\text{IsSeedReady} = (\text{epochSeed}(e) \neq 0)$$
  - 若为 `0`，表明种子尚未初始化，任何矿工尝试调用 `submitShares` 均会触发 `SeedNotInitialized()` 报错回滚。
- **更新频率**:
  - 每小时整点由 Keeper 初次触发初始化，随后该轮保持恒定就绪。

---

### 3.13 暂停状态 (Paused State)
- **链上来源**:
  - Getter: `MinerV1.paused() -> bool` (选择器 `0x5c975abb`)
  - Getter: `StakeVault.paused() -> bool` (选择器 `0x5c975abb`)
  - 触发事件:
    - `MinerV1.SubmissionsPaused` (`0x504d002d...`) / `SubmissionsUnpaused` (`0x863c3a02...`)
    - `StakeVault.VaultPaused` (`0xdffada28...`) / `VaultUnpaused` (`0xcf1b4127...`)
- **计算方式**:
  - 布尔状态直接评估：
    - `isMiningPaused`: 阻断 PoW share 提交；
    - `isVaultPaused`: 阻断新质押（但不阻断到期提取 `withdraw()`）。
- **更新频率**:
  - 实时状态，仅在 Guardian 或 Timelock 调用时改变。

---

### 3.14 守恒检查 (Conservation Check)
- **链上来源**:
  - Token 合约: `Token.totalMined() -> uint256` (选择器 `0x5556db65`)
  - Token 合约: `Token.totalSupply() -> uint256` (选择器 `0x18160ddd`)
  - Token 常量: `Token.MINING_CAP() = 900_000_000e18` (选择器 `0xce28c1bf`)
  - Miner 合约 (rc2): `MinerV1.remainingEmission() -> uint256` (选择器 `0xb547df86`)
  - Miner 合约 (rc2): `MinerV1.settledEpochQuota(uint256 epoch) -> uint256`
  - Miner 合约 (状态查询): 待领取代币总和 `PendingClaimable`
- **计算方式**:
  - **rc2 全闭环守恒等式**:
    $$\text{ConservationDelta} = (\text{totalMined} + \text{PendingClaimable} + \text{remainingEmission}) - 900,000,000 \times 10^{18}$$
    - 其中 $\text{PendingClaimable}$ 为所有已结算轮次中未 Claim 的矿工应得代币之和：
      $$\text{PendingClaimable} = \sum_{e < \text{lastSynced}} \sum_{u, \text{claimed}[e][u]=\text{false}} \min\left( \frac{\text{settledEpochQuota}[e] \times \text{userWeight}[e][u]}{\max(\text{totalWeight}[e], \text{wMin}[e])}, \frac{\text{settledEpochQuota}[e] \times \text{capBps}}{10000} \right)$$
    - 允许的最大累积舍入截断偏差：$|\text{ConservationDelta}| \le 87,600 \text{ wei}$。若超标视为守恒失效。
  - **rc1 降级可用项校验 (测试网现行版本)**:
    - 因未开辟 Slot 21，无法读取 `remainingEmission`，降级执行：
      1. 累计铸造约束：$\text{totalMined}() \le 900,000,000 \times 10^{18}$；
      2. 总体流通硬顶：$\text{totalSupply}() \le 1,000,000,000 \times 10^{18}$；
      3. 单轮释放上限：$\sum_{u} \text{claimedAmount}(e) \le \text{effectiveQuota}(e)$。
- **更新频率**:
  - 每轮结算或每次发生 `RewardClaimed` 交易后进行复核。

---

## 4. 核心指标与合约元数据速查表

| 指标编号 | 业务指标名称 | 核心数据源 | 函数 / 事件原型 | 选择器 / Topic0 |
| :--- | :--- | :--- | :--- | :--- |
| **01** | 每轮活跃地址数 | `MinerV1` 事件 | `event SharesSubmitted(address,uint256,uint256,uint256)` | `0xf50e89c738e371409f94eeeac48fe75568269cfaed112e683bf11d4b4684b214` |
| **02** | 每轮份额数 | `MinerV1` Getter | `totalShares(uint256)` | `0x13f2dad0` |
| **03** | 总权重 | `MinerV1` Getter | `totalWeight(uint256)` | `0x72e78e1c` |
| **04** | wMin | `MinerV1` Getter | `wMin(uint256)` | `0xcf9eb447` |
| **05** | 分配比例 tw/wMin | 组合计算 | `totalWeight` / `wMin` | `0x72e78e1c` / `0xcf9eb447` |
| **06** | 未分配额度占比 | `MinerV1` Getter | `unallocatedGap(uint256)` | `0xca6875f7` |
| **07** | 难度与 EMA 偏离 | `MinerV1` Getter | `target(uint256)` / `ema()` / `S_STAR()` | `0x9811c7c1` / `0xd7247ec8` / `0x567154bd` |
| **08** | 满 CAP 地址占比 | 组合计算 | `counted(uint256,address) == 6` | `0x834f9a82` |
| **09** | 权重集中度 (Top1/10)| `MinerV1` Getter | `userWeight(uint256,address)` | `0xc3448b7e` |
| **10** | 新增质押与出资聚类 | `StakeVault` 事件| `event Deposited(address,uint256,uint256)` | `0x73a19dd210f1a7f902193214c0ee91dd35ee5b4d920cba8d519eca65a7b488ca` |
| **11** | 同步滞后 | 组合计算 | `epoch()` - `lastSynced()` | `0x900cf0cf` - `0x93afb4c5` |
| **12** | 当轮种子就绪 | `MinerV1` Getter | `epochSeed(uint256)` | `0x4abccd28` |
| **13** | 合约暂停状态 | 组合 Getter | `MinerV1.paused()` / `StakeVault.paused()` | `0x5c975abb` |
| **14** | 守恒检查 | rc2 / rc1 | `totalMined` + `pending` + `remaining` == 9亿 | `0x5556db65` / `0xb547df86` |


---

## 5. 分级监控告警规则、最小样本保护与治理执行矩阵

### 5.1 最小样本保护机制 (Minimum Sample Size Protection)
为避免在网络启动初期、低谷休眠期或少量测试矿工环境下因统计样本过小引发虚假告警，监控系统强制执行三项最小样本过滤约束：
1. **集中度规则样本保护**：所有涉及单地址权重占比（`top1_weight_share`）、前 10 地址集中度（`top10_weight_share`）与基尼系数规则，**强制要求当前轮活跃地址数 $\ge 20$**。活跃地址不足 20 时，单地址占比高属正常现象，不予触发告警；
2. **EMA 平抑规则样本保护**：涉及 EMA 与 S_STAR 比值失衡的规则（`ACT-05`），**强制要求当前轮份额总数 $\ge 0.05 \times S^* $**（即至少达到目标份额的 5%）。冷启动或突发休眠期因样本极度匮乏导致的比值偏离不予触发；
3. **难度边界规则触发机制**：系统难度自适应规则（`OBS-03`）**仅在难度到达 35 位硬顶 (`db >= 35.0`) 或离开 32 位基准 (`db > 32.01`) 时触发**。在 32 位基准稳态下不予报警，避免无效通知泛滥。

### 5.2 完整告警规则与执行权限全景矩阵

| 规则编号 | 规则名称 | 严重级别 | 触发条件 (含最小样本保护) | 执行者 (Executor) | 是否需要 Timelock | 处置响应动作与工程指引 |
| :--- | :--- | :---: | :--- | :--- | :---: | :--- |
| **EMG-01** | 代币发行守恒破坏 | **紧急 (CRITICAL)** | rc2: 偏差 > 1 TTK；rc1: 超过硬顶 | **Guardian (应急守护多签)** | **否** (秒级) | Guardian 立即调用 `MinerV1.pauseSubmissions()` 与 `StakeVault.pause()` 熔断暂停，阻断代币非预期铸造与资金外流，组织核心审计团队排查 |
| **EMG-02** | 轮次结算严重滞后卡死 | **紧急 (CRITICAL)** | `sync_lag >= 24` 轮 (滞后超 24 小时) | **Guardian / 核心运维小组** | **否** (秒级) | 介入核查 Keeper 节点；必要时手动广播 batch sync 恢复推进，或暂停提交以保护矿工计算收益 |
| **EMG-03** | 极端单地址垄断算力 | **紧急 (CRITICAL)** | 活跃地址 $\ge 20$ 且 `top1_weight_share >= 80%` | **Guardian (应急守护多签)** | **否** (秒级) | 启动紧急研判，确认是否存在 ASIC/特定算力漏洞；评估是否紧急暂停挖矿并由治理排期上调难度基准或提高质押门槛 |
| **ACT-01** | 轮次同步滞后警示 | **行动 (WARNING)** | `2 <= sync_lag < 24` 轮 | **Keeper 自动化服务 / 运维节点** | **否** | 触发自动化 Keeper 备用节点广播 `sync(48)` 进行追赶；若持续 3 轮未恢复，运维人员通过控制台执行补结算 |
| **ACT-02** | 当前轮种子超时未就绪 | **行动 (WARNING)** | 轮次过去 $\ge 5$ 分钟且 `epochSeed == 0` | **Keeper 自动化服务 / 矿工社区** | **否** | 触发备用 Keeper 立即调用 `initEpochSeed(epoch, 24)` 进行种子填补，恢复矿工正常计算与提交环境 |
| **ACT-03** | 单地址权重超过硬顶截断 | **行动 (WARNING)** | 活跃地址 $\ge 20$ 且 `top1_weight_share > 20%` | **治理委员会通报 / 监控自动化记录** | **否** | 确认合约内 `ADDR_CAP_BPS` 已对超出部分自动截断；向社区通报算力分布，密切监控是否存在多地址拆分集群 |
| **ACT-04** | 前 10 地址权重高度集中 | **行动 (WARNING)** | 活跃地址 $\ge 20$ 且 `top10_weight_share >= 70%` | **Timelock 治理排期执行** | **是** (24~48h) | 进入寡头垄断预警状态；治理委员会向社区通报算力分布，排期 Timelock 提案调用 `setDeposit` 上调质押额度增加集群成本 |
| **ACT-05** | EMA 相对 S_STAR 严重失衡 | **行动 (WARNING)** | 份额数 $\ge 5\% S^*$ 且 (`ratio < 0.2` 或 `ratio > 5.0`) | **Timelock 治理排期执行** | **是** (24~48h) | 若持续 3 轮以上无法通过原生 target 动态平抑恢复，治理委员会通过 Timelock 排期调用 `setSStar(newSStar)` 校准目标基准值 |
| **ACT-06** | 出资聚类与同区块批量质押 | **行动 (WARNING)** | 同出资源 $\ge 10$ 或单区块批量质押 $\ge 10$ 或每小时新增 $\ge 20$ | **风控团队标记 / Timelock 排期执行** | **是** (调参时) | 链下风控标记该出资地址与同区块批处理集群，持续跟踪其在后续轮次的提交行为；若该集群集中打满 CAP，排期 Timelock 上调 `setDeposit` 提升女巫成本 |
| **ACT-07** | 活跃地址日增 3 倍 | **行动 (WARNING)** | 24 小时活跃地址环比增长 $\ge 300\%$ ($\\ge 3.0$x) | **风控团队核查 / Timelock 排期执行** | **是** (调参时) | 风控系统启动矿工来源排查，核实为社区自然裂变还是女巫集群批量涌入；若伴随算力寡头垄断，排期 Timelock 提议阶梯式上调质押金 |
| **ACT-08** | 单地址权重占比 15% 连续 6 轮 | **行动 (WARNING)** | 活跃地址 $\ge 20$ 且单地址占比 $\ge 15\%$ 连续持续 6 轮 | **Timelock 治理排期执行** | **是** (24~48h) | 表明存在单一大矿工长期贴近 20% 硬上限垄断；治理委员会发起提案，通过 Timelock 排期调用 `setDeposit` 或增大 S_STAR 刺激算力充分竞争 |
| **ACT-09** | 估值触发 | **行动 (WARNING)** | 二级市场 FDV 达到或突破保本估值阈值 (FDV $\ge 35$ BNB / $24,300) | **Timelock 治理排期执行** | **是** (24~48h) | 触发经济模型防御预案；治理委员会向社区披露女巫保本估值矩阵，排期 Timelock 提案上调质押金 `setDeposit` (如从 0.01 提高到 0.02 或 0.04 BNB) 以压制女巫收益 |
| **OBS-06** | tw/wMin 低于 5% 连续 24 轮 | **观察 (INFO)** | `tw_wmin_ratio < 5.0%` 连续持续 24 轮 | **监控自动化系统 / 运维团队记录 / 社区关注** | **否** | 判定冷启动矿工参与度极度匮乏或发生休眠；记录系统日志，由社区在公开论坛讨论是否在未来升级中调整 wMin 阶段系数或优化早期激励，无需紧急介入 |
| **OBS-01** | 份额数达到 0.3 倍 S_STAR | **观察 (INFO)** | 单轮总份额 $\ge 0.3 \times S^*$ (达到 30%) | **监控自动化系统 / 运维团队记录** | **否** | 记录网络算力健康爬坡里程碑；表明网络正在稳步走出冷启动阶段，自适应调难即将进入活跃区间，无需人工干预 |
| **OBS-02** | 满 CAP 地址比例偏高 | **观察 (INFO)** | 满额 6 份份额矿工比例 $\ge 60\%$ | **系统算法自适应调节 / 运维巡检** | **否** | 表明当前难度对于主流矿工算力较为宽松；系统自适应调难算法将在下一结算周期自动上调难度，持续观察无需人工介入 |
| **OBS-03** | 难度触及 35 位或离开 32 位 | **观察 (INFO)** | `difficulty_bits >= 35.0` 或 `difficulty_bits > 32.01` | **运维记录日志 / 治理长期规划** | **否** | 记录难度截断与偏离事件；若长期卡在 35 位最难边界，评估全网总算力是否已超越原有经济学模型预设上限，并在未来升级中扩展 HARDEST 边界；若离开 32 位，记录算力进入爬升周期 |
| **OBS-04** | 合约处于主动暂停状态 | **观察 (INFO)** | `is_miner_paused == true` 或 `is_vault_paused == true` | **Timelock 解除暂停** | **是** (解暂停) | 核实暂停操作是否为预期的维护或事故处置；若维护完毕需由 Timelock 调用 `unpause()` 进行恢复（Guardian 仅可暂停不可解暂停） |
| **OBS-05** | 算力与分配健康常态 | **观察 (INFO)** | `tw >= wMin` 且 `top1 <= 20%` 且 `top10 <= 50%` | **监控自动化系统** | **否** | 系统处于理想去中心化高活跃度运行状态，自动归档巡检报告与基尼系数记录 |

---

## 6. 质押额阶梯机制 (Staking Deposit Ladder)

### 6.1 合约参数与调价硬约束
依据 `StakeVault.sol` 核心规约（第 138 至 147 行 `setDeposit` 逻辑）：
1. **单次调整限幅 (`MAX_DEPOSIT_MULTIPLIER = 2`)**：单次调整幅度不得超过当前值的 2 倍，且下调不得低于当前值的 1/2（即 `newDeposit <= old * 2` 且 `newDeposit * 2 >= old`）；
2. **调价冷却周期 (`MIN_DEPOSIT_INTERVAL_EPOCHS = 24`)**：两次执行 `setDeposit` 的区块轮次间隔必须满足 `curEpoch >= lastDepositUpdateEpoch + 24`（即至少相隔 24 轮 / 24 小时）；
3. **全局上下限硬约束**：`MIN_DEPOSIT = 0.001 BNB`，`MAX_DEPOSIT = 10 BNB`。

### 6.2 四步平滑提额阶梯规划 (0.01 BNB 至 0.10 BNB)
当链上监控系统触发女巫集群泛滥警示（如 `ACT-06` 出资聚类、`ACT-07` 活跃地址日增 3 倍、`ACT-09` 估值突破保本阈值），治理委员会启动四步阶梯式提额程序，将质押金从首版的 0.01 BNB 逐步提高至 0.10 BNB：

| 调整阶段 | 质押额度 (DEPOSIT) | 相对上一步增幅 | 合约合规校验 | 距离上一步生效的最短轮次间隔 |
| :---: | :---: | :---: | :---: | :---: |
| **初始基准** | **0.01 BNB** | - | 部署默认值 (`MIN_DEPOSIT <= 0.01 <= MAX_DEPOSIT`) | 初始状态 |
| **第 1 步** | **0.02 BNB** | **2.00x** | 满足 `<= old * 2` (0.02 <= 0.02) | 需满足 `epoch >= lastDepositUpdateEpoch + 24` |
| **第 2 步** | **0.04 BNB** | **2.00x** | 满足 `<= old * 2` (0.04 <= 0.04) | 需在第 1 步执行后至少经过 **24 轮** (24 小时) |
| **第 3 步** | **0.08 BNB** | **2.00x** | 满足 `<= old * 2` (0.08 <= 0.08) | 需在第 2 步执行后至少经过 **24 轮** (24 小时) |
| **第 4 步** | **0.10 BNB** | **1.25x** | 满足 `<= old * 2` (0.10 <= 0.16) 且 `<= 10 BNB` | 需在第 3 步执行后至少经过 **24 轮** (24 小时) |

### 6.3 治理流水线预先排期与灵活撤回机制 (Pipelined Governance Schedule & Cancel)
- **提案预先流水线排期 (Pipelined Proposals)**：
  Timelock 控制器支持多提案并行排期。为了应对突发剧烈的女巫攻击，治理委员会无需在每步生效后再耗时发起新提案，可在同一决策时刻 $T_{\\text{decision}}$ 将四步阶梯提案**预先批量排期**入时间锁，各提案预设执行时间戳（ETA）分别为：
  - Proposal 1 (`setDeposit(0.02 BNB)`): $\\text{ETA}_1 = T_{\\text{decision}} + T_{\\text{delay}}$
  - Proposal 2 (`setDeposit(0.04 BNB)`): $\\text{ETA}_2 = T_{\\text{decision}} + T_{\\text{delay}} + 24\\text{ 轮}$
  - Proposal 3 (`setDeposit(0.08 BNB)`): $\\text{ETA}_3 = T_{\\text{decision}} + T_{\\text{delay}} + 48\\text{ 轮}$
  - Proposal 4 (`setDeposit(0.10 BNB)`): $\\text{ETA}_4 = T_{\\text{decision}} + T_{\\text{delay}} + 72\\text{ 轮}$
- **条件解除则 Cancel (Graceful Circuit Break)**：
  在流水线排期推进过程中，若链上监控指标显示女巫威胁已解除（例如：二级市场币价回落脱离保本盈利区、恶意出资集群停机撤退、活跃地址回归自然分布），拥有 `CANCELLER_ROLE` 的治理多签（`TIMELOCK_PROPOSER_SAFE`）应立即调用 TimelockController 的 `cancel(id)`，取消后续尚未执行的提额提案。这既保障了极端情况下的敏捷防御，又避免了防卫过当对普通散户矿工造成过高的资金门槛阻碍。

### 6.4 从决策到生效的最短时间计算公式
由于所有 `setDeposit` 必须由 Admin（TimelockController）调用，且每一步必须等待前一步在链上成功执行并过去 24 轮（24 小时），从治理发起排期决策到第四步（0.10 BNB）最终生效的最短时间公式为：

$$T_{\\min} = \\text{Timelock 延迟} + 72\\text{ 轮 (即 } 3 \\times 24\\text{ 小时)}$$

**耗时估算矩阵**：
1. **若主网 Timelock 延迟为 24 小时**：
   $$T_{\\min} = 24\\text{ 小时} + 72\\text{ 小时} = 96\\text{ 小时 (整 4 天)}$$
2. **若主网 Timelock 延迟为 48 小时**：
   $$T_{\\min} = 48\\text{ 小时} + 72\\text{ 小时} = 120\\text{ 小时 (整 5 天)}$$
