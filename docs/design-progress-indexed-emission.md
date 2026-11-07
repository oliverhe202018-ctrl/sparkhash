# PoolCoin MinerV2 核心设计文档：按进度递减的自适应动态发行方案 (Progress-Indexed Emission)

## 文档状态与变更上下文
- **文档版本**：v1.0 (Proposal & Formal Specification)
- **状态**：待参数定案 (Draft for Governance Decision)
- **基线合约**：`src/MinerV1.sol` (commit `33cbb63`)
- **核心目标**：将原固化绑定的“日历时间衰减”发行方案改造为“按进度递减”的动态发行机制，解决网络冷启动与低算力参与期代币被永久注销、无法按预定总量（9 亿枚）完整发行的核心缺陷。
- **已固化决策**：
  1. 接受发行在时间上的平移/顺延（参与度不足时曲线整体后移，代币总量硬顶 9 亿恒定不破）；
  2. 单地址 20% 上限截断部分，在矿工调用 `claim` 时加回剩余未分配量 $R$；
  3. 首版不做强制领取期限与过期回收。

---

## 1. 现有配额公式的精确定义、数学模型与源码行号

在当前 `src/MinerV1.sol` 实现中，代币发行配额严格受日历时间（`epoch` 与 `day`）的单向驱动，核心公式与常数定义如下：

### 1.1 常数与基础定义
- **`MINING_CAP`** (`src/MinerV1.sol:23`):
  ```solidity
  uint256 public constant MINING_CAP = 900_000_000e18;
  ```
  矿池挖矿发行的全局绝对硬顶，共 9 亿枚代币（$9 \times 10^{26}$ wei）。
- **`D0`** (`src/MinerV1.sol:24`):
  ```solidity
  uint256 public constant D0 = 3_000_000e18; // Day 1 quota
  ```
  创世第 1 天（Day 1）的基准日配额，为 3,000,000 枚。
- **`DECAY`** (`src/MinerV1.sol:25`):
  ```solidity
  uint256 public constant DECAY = 0.9967e18; // Daily decay: 0.9967
  ```
  日衰减系数 $r = 0.9967$（即每天配额衰减为前一天的 99.67%，日衰减率 0.33%）。
- **`_powFixed`** (`src/MinerV1.sol:132-142`):
  18 位定点数快速幂算法，计算 $base^{exp} / 10^{18}$。

### 1.2 周期与日配额模型
- **`dailyQuota(uint256 day)`** (`src/MinerV1.sol:144-147`):
  $$D_d = D_0 \cdot r^{d-1}$$
  第 $d$ 天（1-indexed）的计划日配额。
  ```solidity
  function dailyQuota(uint256 day) public pure override returns (uint256) {
      if (day == 0) return 0;
      uint256 f = _powFixed(DECAY, day - 1);
      return (D0 * f) / 1e18;
  }
  ```
- **`epochQuota(uint256 e)`** (`src/MinerV1.sol:150-152`):
  1 天固定分为 24 个周期（`EPOCH_LEN = 3600` 秒）。周期 $e$ 的计划基准配额为当日日配额的 $1/24$：
  ```solidity
  function epochQuota(uint256 e) public pure override returns (uint256) {
      return dailyQuota(e / 24 + 1) / 24;
  }
  ```

### 1.3 累计配额闭式解与硬顶拦截
- **`cumulativeDailyQuota(uint256 completedDays)`** (`src/MinerV1.sol:162-166`):
  前 $d$ 个完整日的等比数列求和：
  $$\sum_{k=1}^d D_0 \cdot r^{k-1} = D_0 \frac{1 - r^d}{1 - r}$$
  代入 $D_0 = 3,000,000 \times 10^{18}$，$1 - r = 0.0033 \times 10^{18} = 3.3 \times 10^{15}$：
  $$\frac{D_0}{1 - r} = \frac{3 \times 10^{24}}{3.3 \times 10^{15}} = \frac{10^{10}}{11} \times 10^{18}$$
  源码通过定点数放大消除了除法精度损失：
  ```solidity
  function cumulativeDailyQuota(uint256 completedDays) public pure returns (uint256) {
      if (completedDays == 0) return 0;
      uint256 rd = _powFixed(DECAY, completedDays);
      return (10_000_000_000 * (1e18 - rd)) / 11;
  }
  ```
- **`cumulativeQuotaBefore(uint256 e)`** (`src/MinerV1.sol:169-174`):
  周期 $e$ 之前历史上所有周期的累计计划配额（闭式解析值）：
  $$\text{cumulativeQuotaBefore}(e) = \text{cumulativeDailyQuota}(\lfloor e / 24 \rfloor) + (e \bmod 24) \cdot \text{epochQuota}(e)$$
- **`effectiveQuota(uint256 e)`** (`src/MinerV1.sol:177-183`):
  日历时间强截断：
  ```solidity
  function effectiveQuota(uint256 e) public pure override returns (uint256) {
      uint256 cb = cumulativeQuotaBefore(e);
      if (cb >= MINING_CAP) return 0;
      uint256 remaining = MINING_CAP - cb;
      uint256 eq = epochQuota(e);
      return remaining < eq ? remaining : eq;
  }
  ```

### 1.4 现有机制缺陷与相关测试剖析
在测试 `test/HardCap.t.sol` 中：
- `test_Day1394CappingExactSum`:
  - 第 1394 天开始时（`epoch = 1393 * 24 = 33432`），累计计划配额达 899,976,059 TTK；
  - 第 1394 天运行完 24 个 epoch 后，累计值恰好精确达到 900,000,000 TTK；
  - **第 1395 天第 0 周期起（`e >= 1394 * 24 = 33456`），`effectiveQuota(e)` 永久归零**。

**核心结构性问题**：
现有公式计算的是“**日历应发配额**”，而非“**实际已铸造量**”。
在网络初期或波动期，当矿工权重 $tw < wMin$ 或触发单地址 20% 硬顶时，未分配部分沉淀记入 `unallocatedGap[e]`（四分桶 $B_3$ 与 $B_4$），但在底层经济学上**这些代币实质上被永久焚毁**。即使全网参与率仅 10%，到第 1395 天挖矿依然强制终结，最终流通盘仅发出了 9,000 万枚（10%），剩余 8.1 亿枚代币永远无法进入生态，严重违背经济模型规划。

---

## 2. “按进度递减” (Progress-Indexed) 方案深度设计

### 2.1 核心状态转移方程
为了实现“参与不足则时间顺延、参与充足则平滑释放、总量恒等于 9 亿”，我们将每周期释放配额改为与当前**尚未分配的剩余池** $R_e$ 成正比：

$$q_e = \lfloor \lambda \cdot R_e \rfloor$$

其中：
- $R_0 = \text{MINING\_CAP} = 900,000,000 \times 10^{18}$ wei；
- $\lambda \in (0, 1)$ 为无量纲的**单周期衰减比例系数**；
- $R_e$ 为截至第 $e$ 周期开始结算时，矿池中仍未分配、可供后续发行的代币存量。

### 2.2 衰减参数 $\lambda$ 的严格数学推导与三档取值
根据设计要求：在**矿工参与充足**（即 $tw \ge wMin$ 且无地址截断，$\alpha_e = 1$）的标准基准下，希望前 1394 天（即 $N = 1394 \times 24 = 33,456$ 个周期）累计发出的代币比例达到 $p$。

1. **推导过程**：
   在充足参与时，每周期剩余量递推：
   $$R_{e+1} = R_e - q_e = R_e(1 - \lambda)$$
   经过 $N$ 个周期后，剩余量为：
   $$R_N = R_0 \cdot (1 - \lambda)^N$$
   截至第 $N$ 周期已发行的代币占总量的比例 $p$ 为：
   $$p = \frac{R_0 - R_N}{R_0} = 1 - (1 - \lambda)^N$$
   解此关于 $\lambda$ 的高次方程：
   $$(1 - \lambda)^N = 1 - p \implies 1 - \lambda = (1 - p)^{1/N} \implies \lambda = 1 - (1 - p)^{1/N}$$

2. **三档 $p$ 取值下的 $\lambda$ 精确计算结果**（基于 $N = 33,456$）：

| 目标释放比例 $p$ | 剩余比例 $1-p$ | 浮点解析值 $\lambda$ | Solidity 定点数表示 (Wad, $10^{18}$) | 创世首周期配额 $q_0$ (TTK) | 创世首日配额 $24 \cdot q_0$ (TTK) |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **98.0%** | 2.0% | $1.169235502499 \times 10^{-4}$ | `116923550249948` | 105,231.20 | 2,525,548.69 |
| **99.0% (推荐)** | 1.0% | $1.376390857547 \times 10^{-4}$ | `137639085754704` | 123,875.18 | 2,973,004.25 |
| **99.9%** | 0.1% | $2.064515242749 \times 10^{-4}$ | `206451524274898` | 185,806.37 | 4,459,352.92 |

> **关键发现与对照**：
> 在 $p = 99\%$ 档位下，算得的创世首日配额为 **2,973,004 TTK**，创世首周期配额为 **123,875 TTK**。这与现有 MinerV1 的基准常数 $D_0 = 3,000,000$ TTK（首周期 125,000 TTK）的**相对偏差仅为 0.90%**！这证明 $p=99\%$ 能够实现与原经济学预期的无缝衔接。

### 2.3 状态变量布局与 `__gap` 空间占用
为了保持与 OpenZeppelin 透明可升级代理（UUPS / Transparent Proxy）的绝对兼容，新增变量从现有的 Slot 21 `__gap[46]` 中依序扣除 2 个槽位：

```solidity
// ==================== 状态变量声明 (Slot 21 - Slot 22) ====================

/// @notice Slot 21: 剩余可供分配的挖矿总量 R (初始 900_000_000e18)
uint256 public remainingEmission;

/// @notice Slot 22: 已结算周期的固化基准配额映射 (epoch => settledQuota)
mapping(uint256 => uint256) public settledEpochQuota;

/// @dev Reserved storage space for proxy upgrades (46 slots reduced to 44)
uint256[44] private __gap;
```

**存储槽布局验证表**：

| Slot 编号 | 变量名 | 类型 | 字节偏移 | 说明 |
| :---: | :---: | :---: | :---: | :---: |
| 19 | `guardian` | `address` | 0..19 | 现有紧急守护者地址 |
| 20 | `unallocatedGap` | `mapping(uint256=>uint256)` | 0..31 | 现有稀释缺口映射 |
| **21** | **`remainingEmission`** | **`uint256`** | **0..31** | **【新增】当前剩余可分配量 $R$** |
| **22** | **`settledEpochQuota`** | **`mapping(uint256=>uint256)`** | **0..31** | **【新增】历史周期固化基准配额 $q_e$** |
| **23..66** | **`__gap`** | **`uint256[44]`** | **0..31** | **【缩减】升级保留槽位 (44 槽)** |
| 108904... | Initializable | struct | - | OZ 代理初始化命名空间槽 |

**结论**：Slot 21 至 Slot 66 总计仍占据 46 个完整 32 字节槽位，升级前后存储空间占用严格不变，杜绝了变量错位风险。

### 2.4 `sync()` 状态转移与 $R$ 的扣减逻辑
在每个周期结束由 `sync(maxSteps)` 执行状态结算时，按以下确定性顺序更新：

```solidity
// 在 MinerV2.sync() 的内部循环中：
uint256 settledEpoch = n;
uint256 r = remainingEmission;

// 1. 基于当前剩余量 r 计算本周期的基准配额 q_e
uint256 q_e = (r * LAMBDA_WAD) / 1e18;

// 2. 固化写入本周期配额，供后续 claim 幂等读取
settledEpochQuota[settledEpoch] = q_e;

// 3. 计算本周期算力参与率与可分配额
uint256 tw = totalWeight[settledEpoch];
uint256 wm = wMin(settledEpoch);
uint256 allocatable = 0;
uint256 b3 = 0;

if (q_e > 0) {
    if (tw >= wm) {
        allocatable = q_e;
    } else {
        allocatable = (q_e * tw) / wm;
        b3 = q_e - allocatable; // 未达到 wMin 导致的未分配缺口
    }
}

// 4. 仅从剩余量 R 中扣除实际可被分配领取的数额 allocatable
// 未达到 wMin 的部分 b3 天然留在 R 中，自动结转进入后续轮次
remainingEmission = r - allocatable;

// 5. 记录稀释日志并发出事件
unallocatedGap[settledEpoch] = b3;
emit EpochSettled(settledEpoch, q_e, tw, wm, b3);
```

### 2.5 `claim()` 读取固化配额与 Gas 增量对比
矿工提取奖励时，直接读取该周期在 `sync` 时已经冻结的数值：

```solidity
function claim(uint256 e) external override nonReentrant {
    if (e >= epoch()) revert EpochNotEnded();
    if (claimed[e][msg.sender]) revert AlreadyClaimed();
    claimed[e][msg.sender] = true;

    uint256 uw = userWeight[e][msg.sender];
    if (uw == 0) revert ZeroWeight();

    // 1. 直接读取已固化的配额 (SLOAD)
    uint256 effQ = settledEpochQuota[e];
    if (effQ == 0) {
        emit RewardClaimed(msg.sender, e, 0, 0);
        return;
    }

    // 2. 计算应领代币
    uint256 tw = totalWeight[e];
    uint256 wm = wMin(e);
    uint256 denom = tw > wm ? tw : wm;
    uint256 amt = (effQ * uw) / denom;

    // 3. 单地址 20% 硬顶截断判断
    uint256 capBps = epochAddrCapBps[e];
    if (capBps == 0) capBps = ADDR_CAP_BPS;
    uint256 maxAmt = (effQ * capBps) / 10000;
    uint256 cappedOverflow = 0;

    if (amt > maxAmt) {
        cappedOverflow = amt - maxAmt;
        amt = maxAmt;
        
        // 【已确认决策 2】：在 claim 发生截断时，将溢出不可领取的份额实时加回 R
        remainingEmission += cappedOverflow;
    }

    emit RewardClaimed(msg.sender, e, amt, cappedOverflow);
    if (amt > 0) {
        token.mint(msg.sender, amt);
    }
}
```

#### Gas 增量精确对比表

| 操作函数 | 现有方案 (MinerV1) | 新方案 (MinerV2) | Gas 差异项 | 净 Gas 变动 |
| :---: | :---: | :---: | :---: | :---: |
| **`sync` (单周期结算)** | `effectiveQuota` 纯计算 (~1,800 gas) | 计算 `q_e` (~100 gas) + 写 `settledEpochQuota` (22,100 gas) + 更新 `remainingEmission` (2,900 gas) | +1 次冷写 Mapping，+1 次热写 Storage | **增加约 23,200 gas** |
| **`claim` (普通矿工, 无溢出)** | 调用 `effectiveQuota` 纯计算 (~1,800 gas) | SLOAD 读取 `settledEpochQuota` (2,100 gas) | 纯计算替换为热/冷读 | **基本持平 ($\pm 300$ gas)** |
| **`claim` (巨鲸矿工, 触顶溢出)** | 仅记录本地变量发出事件 (~1,800 gas) | SLOAD (2,100 gas) + 累加写 `remainingEmission` (2,900/5,000 gas) | +1 次 Storage 累加修改 | **增加约 5,000 gas** |

**评估结论**：
`sync` 产生的额外 Gas 由 Keeper 或提交交易的矿工平摊（BSC 上 23,000 gas 成本不足 0.0001 美元），而 99% 的普通矿工在 `claim` 时几乎零成本变化。

---

## 3. 与 $wMin$ 的交互以及 20% 截断加回 $R$（已确认决策落地）

### 3.1 参与度与 $wMin$ 的交互机制
设当前周期总权重为 $tw = \text{totalWeight}[e]$，门槛阈值为 $wm = wMin(e)$。
实际分配比例为：
$$\alpha_e = \min\left(\frac{tw}{wm}, 1\right)$$

- **情况 1：全网算力充足 ($tw \ge wm$)**
  - 分配比例 $\alpha_e = 1$；
  - 本期全额可分配量 $\text{allocatable} = q_e$；
  - 剩余池扣除量：$R \leftarrow R - q_e$；
  - 稀释缺口 $b_3 = 0$。
- **情况 2：全网算力不足 ($tw < wm$)**
  - 分配比例 $\alpha_e = tw / wm < 1$；
  - 本期实际可分配量 $\text{allocatable} = q_e \cdot \frac{tw}{wm}$；
  - 剩余池扣除量：$R \leftarrow R - \text{allocatable}$；
  - 留存量：$(1 - \alpha_e) \cdot q_e$ **自动驻留在剩余池 $R$ 中**，成为后续周期的计算基数。

### 3.2 20% 单地址截断部分的取舍与“加回 $R$”机制
根据项目要求，对单地址 20% 上限截断产生的溢出部分做如下明确取舍：

1. **方案对比与取舍**：
   - **方案甲（永久沉淀/放弃）**：截断溢出部分计入 $b_4$，永远不再释放。
     - *弊端*：一旦有巨鲸矿工集中提供算力，实际流通盘将永远低于 9 亿，违背代币经济学初衷。
   - **方案乙（在 claim 时加回 $R$，已确认采纳）**：在每笔 claim 交易发生截断时，将 $cappedOverflow$ 加回全局状态 `remainingEmission`。
     - *优势*：既在当期严格制约了单一地址的单轮暴利垄断（强制截断），又确保了被截断的代币资产不被销毁，自动回归矿池反哺全网未来的矿工。

2. **全周期代币资金守恒定理 (Conservation Invariant)**：
   在任意区块时间 $t$，全网资产严格满足：
   $$\text{TotalMinted} + \text{PendingClaimableReserved} + \text{remainingEmission} \equiv \text{MINING\_CAP} = 900,000,000 \times 10^{18}$$
   - 在 `sync` 扣除时，预留了最大的可能被领取的 $\text{allocatable}$（记入未领预留）；
   - 矿工实际 claim 时，铸造代币 $\text{amt}$，将溢出未铸造的 $cappedOverflow$ 加回 `remainingEmission`；
   - 整个过程既无中生有，亦无代币凭空湮灭。

3. **领取期限与过期处理（已确认决策 3 落地）**：
   首版**不设置任何 claim 期限，亦不执行过期没收**。矿工的应领份额永久具备链上提款权（Pull-payment），确保协议的免信任与绝对抗审查性。

---

## 4. 离线仿真实验与全景对比分析

为了全面评估新方案的动态特性，我们在 `docs/simulate_progress_emission.py` 中构建了覆盖 10 年（87,600 个小时周期）的高精度离线仿真引擎。

### 4.1 仿真测试用例设计
- **参与率场景**：
  1. **100% 满额参与**：全网 $tw \ge wm$ 且无截断，考察基准理论曲线；
  2. **50% 参与率**：全网 $tw / wm = 0.5$（或参与天数折半）；
  3. **10% 低谷参与**：模拟极端熊市或网络早期阶段；
  4. **波动参与率**：每周期在 $[0.1, 1.0]$ 之间随机均匀波动（平均参与率约 55%）。
- **对比模型**：
  - **Calendar**：现有 MinerV1 日历衰减模型（1394 天强行关闸）；
  - **Progress (p=98%)**：$\lambda = 1.1692 \times 10^{-4}$；
  - **Progress (p=99%)**：$\lambda = 1.3764 \times 10^{-4}$；
  - **Progress (p=99.9%)**：$\lambda = 2.0645 \times 10^{-4}$。

### 4.2 累计发行量占 9 亿总量的百分比比对表

| 模型架构 | 算力参与场景 | 第 1 年 (Y1) | 第 2 年 (Y2) | 第 3 年 (Y3) | 第 5 年 (Y5) | 第 10 年 (Y10) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **现有日历模型 (Calendar)** | 100% 满额 | 70.78% | 91.96% | 98.30% | 100.00% | 100.00% |
| | 50% 参与 | 35.39% | 45.98% | 49.15% | **50.00% (停滞)** | **50.00% (永久损失)** |
| | 10% 极低 | 7.08% | 9.20% | 9.83% | **10.00% (停滞)** | **10.00% (永久损失)** |
| | 0.1~1.0 波动 | 39.03% | 50.68% | 54.16% | **55.09% (停滞)** | **55.09% (永久损失)** |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **按进度递减 (p=98%)** | 100% 满额 | 64.10% | 87.11% | 95.37% | 99.40% | 100.00% |
| | 50% 参与 | 40.08% | 64.09% | 78.49% | 92.27% | 99.40% |
| | 10% 极低 | 9.74% | 18.52% | 26.46% | 40.08% | 64.09% |
| | 0.1~1.0 波动 | 43.12% | 67.64% | 81.54% | 94.02% | 99.64% |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **按进度递减 (p=99%, 推荐)** | **100% 满额** | **70.05%** | **91.03%** | **97.31%** | **99.76%** | **100.00%** |
| | **50% 参与** | **45.28%** | **70.05%** | **83.61%** | **95.09%** | **99.76%** |
| | **10% 极低** | **11.36%** | **21.43%** | **30.35%** | **45.28%** | **70.05%** |
| | **0.1~1.0 波动** | **48.53%** | **73.50%** | **86.32%** | **96.37%** | **99.87%** |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **按进度递减 (p=99.9%)** | 100% 满额 | 83.61% | 97.31% | 99.56% | 99.99% | 100.00% |
| | 50% 参与 | 59.52% | 83.61% | 93.37% | 98.91% | 99.99% |
| | 10% 极低 | 16.54% | 30.35% | 41.87% | 59.52% | 83.61% |
| | 0.1~1.0 波动 | 63.08% | 86.36% | 94.94% | 99.31% | 100.00% |

### 4.3 绝对发行数量统计表 (单位：百万 TTK / Million)

| 模型架构 (参与场景) | 第 1 年 | 第 2 年 | 第 3 年 | 第 5 年 | 第 10 年 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Calendar (100% 满额)** | 637.05 M | 827.68 M | 884.73 M | 900.00 M | 900.00 M |
| **Calendar (50% 参与)** | 318.52 M | 413.84 M | 442.37 M | 450.00 M | 450.00 M |
| **Calendar (波动参与)** | 351.24 M | 456.13 M | 487.44 M | 495.84 M | 495.84 M |
| **Progress p=99% (100% 满额)** | **630.49 M** | **819.30 M** | **875.83 M** | **897.83 M** | **899.99 M** |
| **Progress p=99% (50% 参与)** | **407.49 M** | **630.48 M** | **752.51 M** | **855.83 M** | **897.83 M** |
| **Progress p=99% (波动参与)** | **436.80 M** | **661.53 M** | **776.86 M** | **867.31 M** | **898.82 M** |

### 4.4 核心经济学结论
1. **彻底消除“代币永久失踪”漏洞**：日历模型下，50% 参与率导致第 5 年起代币释放永远锁定在 4.5 亿枚；而在按进度递减模型下，50% 参与率仅使得释放曲线在时间轴上顺延（第 5 年达 8.55 亿枚，第 10 年达 8.98 亿枚），最终完美完成 9 亿枚的既定发行目标。
2. **充足参与下的高度吻合**：在 $p=99\%$ 档位下，100% 参与率下的发行轨迹（前三年分别达到 70.05%、91.03%、97.31%）与日历模型（70.78%、91.96%、98.30%）几乎重叠，保证了项目前期的流通盘市值管理与通胀预期完全稳定。

---

## 5. 攻击面、安全性与系统升级兼容性深度评估

### 5.1 攻击面剖析

#### A. “空档期后突击”攻击防范（配额绝无超额风险）
- **潜在担忧**：若全网出现连续 100 个周期的无人参与空档期，攻击者在第 101 周期突然提交大量算力，是否能“合并一次性领走”前 100 个周期的所有配额？
- **安全证明**：
  **绝对不可能**。
  1. 结算逻辑基于逐周期顺序结算（`sync` 内部 `while(n < stop) n++`），每个历史空档周期 $n$ 独立结算。
  2. 在空档周期，`totalWeight[n] == 0`，计算得 `allocatable = 0`，该周期的固化配额 `settledEpochQuota[n]` 虽然记录为 $q_n$，但该周期没有矿工提交份额，且 `submitShares` 只能针对当前正在进行的 `epoch()` 提交，**绝对无法跨周期回溯补交**。
  3. 第 101 周期的配额严格按照当时的 $q_{101} = \lfloor \lambda \cdot R_{101} \rfloor$ 计算。因为历史空档期未消耗 $R$，所以 $R_{101} = R_0$，该周期的配额上限依然只有 $12.38$ 万枚（占当时 $R$ 的 $0.01376\%$），绝不可能产生“积攒 100 倍配额”的瞬间暴增倾销。

#### B. $R$ 状态变量被操纵的可能
- **安全证明**：
  在整个智能合约中，仅有两个逻辑分支能够修改 `remainingEmission`：
  1. `sync()`：只能递减 $R$（`r -= allocatable`），扣减量取决于经 PoW 验证的合法权重 $tw$；
  2. `claim()`：只有在矿工自身权重触发单地址 20% 硬顶时，将其实际被截断的 $cappedOverflow$ 加回 $R$。
     - 防重入：由 `claimed[e][msg.sender] = true` 与 `nonReentrant` 双重防范，不可重复 claim；
     - 守恒性：加回的量严格小于等于 `sync` 当初为该矿工预留扣除的理论额度，不存在虚增 $R$ 的漏洞。

#### C. 舍入误差与精度损失
- **截断分析**：
  $q_e = (R \cdot \lambda) / 10^{18}$ 采用整型除法向下取整，每周期最大的截断损失小于 1 wei。
  在 10 年 87,600 个周期内，全局累计最大舍入偏差小于 $87,600$ wei（即 $< 8.76 \times 10^{-14}$ 枚代币），对 9 亿枚代币系统而言数学上完全可忽略。

#### D. 数值溢出分析
- $R \le 900,000,000 \times 10^{18} \approx 9 \times 10^{26} < 2^{90}$；
- $\lambda_{\text{wad}} \approx 1.38 \times 10^{14} < 2^{47}$；
- 乘积 $R \cdot \lambda_{\text{wad}} \approx 1.24 \times 10^{41} < 2^{137} \ll 2^{256}$；
- 算术运算在 Solidity 0.8.24+ 原生防溢出保护下运行，数学安全边界充足。

### 5.2 存储布局与代理升级兼容性

1. **零槽位偏移保障**：
   原有 Slot 21 的 `uint256[46] private __gap` 调整为：
   - Slot 21: `uint256 public remainingEmission;`
   - Slot 22: `mapping(uint256 => uint256) public settledEpochQuota;`
   - Slot 23..66: `uint256[44] private __gap;`
   原有 Slot 0~20 的所有核心变量（`token`, `vault`, `admin`, `START`, `lastSynced`, `unallocatedGap` 等）位置**分毫未变**。

2. **升级初始化函数 (Reinitializer)**：
   升级通过 OpenZeppelin 的 `reinitializer(2)` 执行：
   ```solidity
   function initializeV2() external reinitializer(2) {
       // 若是从创世部署即采用 V2，直接赋予 900M
       // 若是由 V1 升级而来，剩余量为 900M 减去历史已固化的总配额
       if (remainingEmission == 0) {
           remainingEmission = MINING_CAP - _calculateHistoricalMintedOrAllocated();
       }
   }
   ```

### 5.3 需修改的函数与测试套件影响

#### 合约修改清单
- `src/MinerV1.sol` (演进至 `MinerV2.sol`):
  - 弃用并移除日历闭式计算函数：`dailyQuota`, `epochQuota`, `cumulativeDailyQuota`, `cumulativeQuotaBefore`, `effectiveQuota`, `_powFixed`；
  - `sync()`：重构配额生成逻辑，固化 `settledEpochQuota[settledEpoch] = q_e` 并扣减 `remainingEmission`；
  - `claim()`：改由读取 `settledEpochQuota[e]`，增加溢出加回 `remainingEmission += cappedOverflow`；
  - 声明 `remainingEmission` 与 `settledEpochQuota`，缩减 `__gap` 为 44 槽。
- `src/interfaces/IMiner.sol`:
  - 补充 `remainingEmission()` 与 `settledEpochQuota(uint256)` 的接口定义。

#### 测试套件调整清单
- **`test/HardCap.t.sol` (核心重构点)**:
  - 原测试依赖“第 1394 天配额归零”的日历硬断言（`assertEq(miner.effectiveQuota(day1394Start + 24), 0)`）。
  - 在新模型下，硬顶测试应转换为**全局守恒性不变量测试 (Global Conservation Invariant)**：
    验证任意多周期、任意随机算力输入下，$\sum \text{minted} + R \equiv \text{MINING\_CAP}$，且无论周期推进到第 1394 天还是第 10,000 天，累计铸造量永不突破 9 亿枚。
- **`test/RewardBucketingInvariant.t.sol`**:
  - 原测试以 `effectiveQuota(e)` 作为周期总流入 $Q$；
  - 需适配为从 `settledEpochQuota(e)` 读取 $Q$，四分桶资金守恒公式 $B_1 + B_2 + B_3 + B_4 = Q$ 保持 100% 成立。
- **`test/MinerV1.t.sol`**:
  - 将原静态配额断言更新为包含状态结算的动态周期测试。

---

## 6. 待决策参数与推荐值 (Pending Decisions & Recommendations)

根据已收敛决策，目前仅保留以下两项待最终裁定：

### 决策项 1：衰减参数 $\lambda$ 的档位选择（三选一）

- **档位 A：$p = 98.0\%$**
  - 参数值：$\lambda = 1.169235502499 \times 10^{-4}$ (Wad: `116923550249948`)
  - 创世首日配额：2,525,548 TTK
  - 特点：前期释放最为温和，前 3 年（充足参与）释放 85.83%，有利于压低项目早期的二级市场抛压，但初期对高成本矿工的吸引力相对偏弱。
- **档位 B：$p = 99.0\%$ （强烈推荐 ★★★★★）**
  - 参数值：$\lambda = 1.376390857547 \times 10^{-4}$ (Wad: `137639085754704`)
  - 创世首日配额：2,973,004 TTK
  - **推荐理由**：
    1. **历史预期平滑度最高**：首日 297.3 万枚与原版 MinerV1 的 300 万枚仅差 0.9%，首周期 123,875 枚与原版 125,000 枚近乎无感切换，社区与外部做市预期完全一致；
    2. **兼顾早期激励与中期长尾**：满额参与下第 1 年释放 70.05%、第 2 年 91.03%、第 3 年 97.31%，节奏极为健康。
- **档位 C：$p = 99.9\%$**
  - 参数值：$\lambda = 2.064515242749 \times 10^{-4}$ (Wad: `206451524274898`)
  - 创世首日配额：4,459,353 TTK
  - 特点：前期释放极为激进，第 1 年即释放 83.61%，适合强力打响冷启动、快速分发筹码的激进策略，但后期衰减过快。

---

### 决策项 2：尾部发行的处理方案（三选一）

当网络运行 5~10 年后，$R$ 会降至几十万枚甚至更小，$q_e = \lfloor \lambda \cdot R \rfloor$ 会逐步缩小至几十枚甚至几枚代币，面临尾部微量发行的处理选择：

- **方案 A：纯渐近衰减直至尘埃归零 (Asymptotic Tail Decay，推荐 ★★★★★)**
  - **规则**：不增加任何额外逻辑，始终执行 $q_e = \lfloor \lambda \cdot R \rfloor$。当 $R$ 衰减到几十 wei、导致算出的 $q_e = 0$ 时，发行自然停机。
  - **优点**：**极致简洁与确定性**。智能合约无需增加额外的阈值判断或清尾分支，没有任何边界突变风险与黑客攻击面。
  - **缺点**：最后万分之一（约 9 万枚）代币将在极长的时间跨度内呈尘埃状微量释放。
- **方案 B：尾部最小保底配额清尾 (Min Quota Floor + Final Sweep)**
  - **规则**：设定最小保底周期配额 $q_{\min}$（例如 $10 \times 10^{18}$ wei，即每周期 10 枚）。当 $\lfloor \lambda \cdot R \rfloor < q_{\min}$ 时，若 $R \ge q_{\min}$ 则按 $q_{\min}$ 发行；若最后 $R < q_{\min}$，则将剩余的所有 $R$ 作为最后一个周期的全部配额，随后 $R=0$ 终结。
  - **优点**：能在有限周期内干脆利落地将 9 亿枚代币全部挖净，尾部周期依然保持最低限度的出块奖励。
  - **缺点**：增加了合约逻辑判断分支与状态阈值配置。
- **方案 C：尾部治理清扫划转 (Governance Sweep to Vault)**
  - **规则**：设定硬性时间或存量阈值（例如 5 年后且 $R < 0.1\%$ 即 90 万枚时），允许 Timelock 治理将剩余不可逆的零头 $R$ 一次性划入 `StakeVault` 质押分红金库。
  - **优点**：干净彻底结束 PoW 发行时代，平滑转入全质押收益治理时代。
  - **缺点**：引入了治理主动介入代币划转的特权，削弱了纯粹算法发行的公信力。

**裁定建议**：
首版建议采纳**方案 A**（纯渐近衰减），以最小化合约复杂度和代码风险；若团队对最后万分之几的尘埃释放体验敏感，可选用**方案 B** 并设定 $q_{\min} = 10\text{e}18$。
