# PoolCoin rc2 (feature/progress-emission) 相对 mainnet-rc1 变更审查报告

- **审查对象**：`feature/progress-emission` 分支相对 `mainnet-rc1` 标签的代码与架构变更
- **审查日期**：2026-11-08
- **审查基准**：Git 提交 `mainnet-rc1..HEAD` (包含 4234f32、a5ddc8c、3b99095 等核心提交)
- **审查结论**：**审查通过** (全套 111 个 Foundry 单元/模糊/不变量测试及 31 个 Python 测试 100% 通过；存储槽布局与守恒性数学模型无误；确认只支持全新部署)

---

## 1. sync 与 claim 的完整 Diff 与逐行变量读写审计

### 1.1 sync(uint256) 与 claim(uint256) 源码 Diff (mainnet-rc1 vs HEAD)

```diff
@@ -277,23 +271,30 @@ contract MinerV1 is Initializable, ReentrancyGuard, IMiner {
         while (n < stop) {
             // Settle completed epoch (n)
             uint256 settledEpoch = n;
-            uint256 effQ = effectiveQuota(settledEpoch);
+            uint256 q_e = (remainingEmission * LAMBDA_WAD) / 1e18;
+            settledEpochQuota[settledEpoch] = q_e;
+
             uint256 tw = totalWeight[settledEpoch];
             uint256 wm = wMin(settledEpoch);
+            uint256 allocatable = 0;
             uint256 b3 = 0;
-            if (effQ > 0) {
-                if (tw < wm) {
-                    uint256 allocatable = (effQ * tw) / wm;
-                    b3 = effQ - allocatable;
+            if (q_e > 0) {
+                if (tw >= wm) {
+                    allocatable = q_e;
+                } else {
+                    allocatable = (q_e * tw) / wm;
+                    b3 = q_e - allocatable;
                 }
             }
+            remainingEmission -= allocatable;
             unallocatedGap[settledEpoch] = b3;
-            emit EpochSettled(settledEpoch, effQ, tw, wm, b3);
+            emit EpochSettled(settledEpoch, q_e, tw, wm, b3);
 
             n++;
             uint256 shares = (n >= 2) ? totalShares[n - 2] : S_STAR;
             ema = (ema + shares) / 2;
             uint256 t = target[n - 1];
+            if (t > EASIEST) t = EASIEST;
             uint256 nt = (ema == 0) ? (t * 125 / 100) : (t * S_STAR / ema);
             if (nt > t * 125 / 100) nt = t * 125 / 100;
             if (nt < t * 100 / 125) nt = t * 100 / 125;
@@ -376,13 +377,17 @@ contract MinerV1 is Initializable, ReentrancyGuard, IMiner {
     /// @dev Uses epochAddrCapBps snapshot to ensure changes to ADDR_CAP_BPS never alter past ended rewards.
     function claim(uint256 e) external override nonReentrant {
         if (e >= epoch()) revert EpochNotEnded();
+        if (lastSynced <= e) {
+            sync(48);
+            if (lastSynced <= e) revert SyncingRequired();
+        }
         if (claimed[e][msg.sender]) revert AlreadyClaimed();
         claimed[e][msg.sender] = true;
 
         uint256 uw = userWeight[e][msg.sender];
         if (uw == 0) revert ZeroWeight();
 
-        uint256 effQ = effectiveQuota(e);
+        uint256 effQ = settledEpochQuota[e];
         if (effQ == 0) {
             emit RewardClaimed(msg.sender, e, 0, 0);
             return;
@@ -403,6 +408,7 @@ contract MinerV1 is Initializable, ReentrancyGuard, IMiner {
         if (amt > maxAmt) {
             cappedOverflow = amt - maxAmt;
             amt = maxAmt;
+            remainingEmission += cappedOverflow;
         }
 
         emit RewardClaimed(msg.sender, e, amt, cappedOverflow);
```

### 1.2 sync(uint256 maxSteps) 变量读写逐行说明

| 源码行 / 表达式 | 操作类型 | 涉及变量与存储槽位 | 机制与安全说明 |
| :--- | :---: | :--- | :--- |
| `uint256 q_e = (remainingEmission * LAMBDA_WAD) / 1e18;` | **SLOAD** (读)<br>**CONST** (读) | `remainingEmission` (Slot 21)<br>`LAMBDA_WAD` (常量) | 基于当前剩余可分配矿池存量 $R$，计算本周期的基准配额 $q_e$。向下取整，无任何溢出风险 ($R \le 9 \times 10^{26}$, $\lambda < 2^{47}$)。 |
| `settledEpochQuota[settledEpoch] = q_e;` | **SSTORE** (写) | `settledEpochQuota` (Slot 22 Mapping) | 将本轮算出的基准配额固化写入存储。为后续所有矿工的 `claim(settledEpoch)` 提供幂等、确定性的历史数据源。 |
| `uint256 tw = totalWeight[settledEpoch];` | **SLOAD** (读) | `totalWeight` (Slot 12 Mapping) | 读取该已结束轮次矿工提交的有效总权重 $tw$。 |
| `uint256 wm = wMin(settledEpoch);` | **PURE** (计算) | 内存局部计算 | 依据 `START`、轮次编号及阶梯衰减算法纯计算当期门槛权重 $W_{\min}$，无状态读取。 |
| `allocatable = q_e;` / `(q_e * tw) / wm;` | **PURE** (计算) | 内存局部变量 | 依据算力参与率判定本轮实际可被矿工领取的最大代币上限。若 $tw < wm$，未达标比例对应的配额沉淀在矿池中。 |
| `remainingEmission -= allocatable;` | **SSTORE** (写) | `remainingEmission` (Slot 21) | **核心状态转移**：仅扣除本轮实际分配出去的配额。未达标差额 $b_3$ 天然保留在 $R$ 中顺延给未来轮次。 |
| `unallocatedGap[settledEpoch] = b3;` | **SSTORE** (写) | `unallocatedGap` (Slot 20 Mapping) | 记录因算力未达 $W_{\min}$ 产生的稀释记账（供前端与审计溯源）。 |
| `emit EpochSettled(settledEpoch, q_e, tw, wm, b3);` | **LOG** (事件) | EVM 日志 | 记录结算详情，参数变更为 $(q_e, tw, wm, b3)$。 |
| `if (t > EASIEST) t = EASIEST;` | **MEMORY** (读写) | 局部变量 `t` | 难度调整保护，确保基准难度不会超过最易门槛 `EASIEST` ($1 \ll 224$)。 |
| `lastSynced = n;` | **SSTORE** (写) | `lastSynced` (Slot 7) | 更新全局已同步进度标尺。 |

### 1.3 claim(uint256 e) 变量读写逐行说明

| 源码行 / 表达式 | 操作类型 | 涉及变量与存储槽位 | 机制与安全说明 |
| :--- | :---: | :--- | :--- |
| `if (lastSynced <= e) { sync(48); ... }` | **SLOAD** (读)<br>**INTERNAL** (调用) | `lastSynced` (Slot 7)<br>`sync(48)` 内部调用 | **防未结先领保护**：若所领轮次尚未经 `sync` 结算，自动触发最多 48 步的连续结算。若结算后仍未能覆盖 $e$，回滚抛出 `SyncingRequired()`。 |
| `claimed[e][msg.sender] = true;` | **SSTORE** (写) | `claimed` (Slot 16 Mapping) | **CEI 模式先置检查与修改**：严格在任何代币铸造前将领取标记置为 `true`，配合 `nonReentrant` 彻底阻断重入。 |
| `uint256 effQ = settledEpochQuota[e];` | **SLOAD** (读) | `settledEpochQuota` (Slot 22 Mapping) | 直接读取由 `sync` 固化的历史配额，替代了原有的纯函数动态计算，保证了同一轮次内所有矿工计算基准绝对一致。 |
| `uint256 maxAmt = (effQ * capBps) / 10000;` | **SLOAD** (读) | `epochAddrCapBps` (Slot 18 Mapping) | 读取该轮历史快照的单地址上限比例（默认 20%）。 |
| `remainingEmission += cappedOverflow;` | **SSTORE** (写) | `remainingEmission` (Slot 21) | **20% 截断回流机制**：当矿工触发单地址上限时，溢出无法领取的份额实时加回全局未分配池 $R$，重新释放给全网未来矿工。 |
| `token.mint(msg.sender, amt);` | **CALL** (外部调用) | `token` (Slot 0) | **唯一外部调用**：在所有本地状态变量更新完成后，最后调用 ERC20 代币合约铸造并分发代币。 |

---

## 2. remainingEmission 初始赋值与代理升级安全性评估

### 2.1 remainingEmission 初始化核实
在 `src/MinerV1.sol` 的 `initialize()` 函数中：
```solidity
function initialize(
    address _token,
    address _vault,
    address _admin,
    address _guardian,
    uint256 _start
) external initializer {
    // ... 前置参数校验 ...
    S_STAR = 50_000;
    ADDR_CAP_BPS = 2000; // 20%
    target[0] = EASIEST;
    ema = 50_000;
    lastSynced = 0;

    epochSStar[0] = 50_000;
    epochAddrCapBps[0] = 2000;
    remainingEmission = MINING_CAP; // 精确初始化为 900,000,000e18
}
```
已核实：`remainingEmission` 在全新部署执行 `initialize()` 时被精确赋予初值 `MINING_CAP`（9 亿枚代币，即 $900,000,000 \times 10^{18}$ wei）。

### 2.2 从 rc1 代理升级不安全的原因剖析
**结论**：`feature/progress-emission` (rc2) **绝对不能直接作为 rc1 代理合约的升级实现 (Upgrade Implementation)**，只支持全套全新部署 (Fresh Deployment)。
原因如下：
1. **存储槽未初始化 (Zero-value in Slot 21)**：
   在 rc1 合约中，Slot 21 属于保留的 `uint256[46] private __gap`。在代理合约的链上底层存储中，Slot 21 的值全部为 `0x0`。
2. **初始化锁定 (Initializer Lock)**：
   `MinerV1.initialize()` 带有 OpenZeppelin `initializer` 修饰器。rc1 代理在首次部署时已经完成了 `initialize()`，代理存储中的 `_initialized` 标志位已置为 1。因此升级后**无法再次调用 `initialize()`**，会直接因 `InvalidInitialization()` 回滚。
3. **缺乏 Reinitializer 升级入口**：
   当前 `MinerV1.sol` 中并没有编写 `initializeV2()` 或任何带有 `reinitializer(2)` 修饰器的二次初始化函数，也没有提供任何针对 `remainingEmission` 的管理权限 setter（如 `setRemainingEmission`）。
4. **配额归零灾难 (Fatal Zero-Quota)**：
   若强行执行代理升级，代理合约的 `remainingEmission` 将恒为 0。随后执行 `sync` 时：
   $$q_e = \lfloor (0 \times \lambda) / 10^{18} \rfloor = 0$$
   所有后续轮次的固化配额 `settledEpochQuota` 均为 0，整个矿池代币发行机制永久瘫痪。
5. **历史已结轮次读取为 0**：
   rc1 运行期间已通过 `sync` 结算的轮次，由于当时代码未写入 `settledEpochQuota[e]`，升级后矿工调用 `claim(e)` 读取到的配额全部为 0，导致矿工无法领取升级前应得的历史奖励。

### 2.3 代码保护评估与建议
- **当前状态评估**：
  由于本项目已经定案对测试网和主网均执行全新部署（不保留旧链状态），因此在代码层面未额外增加 `reinitializer` 函数是合理的，彻底杜绝了因二次初始化入参不当引入的治理攻击面。
- **防止误用代理升级的防护建议**：
  为防止后续操作员误操作将该实现合约配置到已有代理上，可在合约中增加升级防护断言或明确文档约束：
  1. 若未来存在升级需求，必须引入 `initializeV2(uint256 historicalMinted)`，且限定 `reinitializer(2)`；
  2. 在 `sync()` 入口处可增加 `if (remainingEmission == 0 && lastSynced == 0) revert UninitializedEmission();`，以便在被错误升级时第一时间显式报错阻断，而非默默将配额结算为 0。

---

## 3. claim 时序依赖性与配额影响量化

### 3.1 时序依赖机制的数学根源
在 rc2 中，`sync()` 结算轮次时根据当前 `remainingEmission` 扣除 `allocatable`。
- **无截断情形**：若所有矿工权重均未触发单地址 20% 上限（$uw_i / tw \le 0.2$），则无论矿工何时调用 `claim`，`remainingEmission` 均不会发生变化，**对后续轮次配额完全零影响**。
- **触发 20% 截断情形**：若某矿工权重过高导致被截断（$amt > maxAmt$），其溢出的 $cappedOverflow$ 会在调用 `claim()` 时加回 `remainingEmission`。
  - **早领 (Early Claim)**：矿工在下一轮 `sync` 之前执行 `claim`，溢出量 $cappedOverflow$ 提前回归 $R$；
  - **晚领 (Late Claim)**：矿工延迟数轮甚至数月才执行 `claim`，溢出量在延迟期间暂未回归 $R$。

### 3.2 量化分析与最坏情形推导
设某轮次产生的截断溢出量为 $O$。
在紧邻的下一轮结算时，配额差异为：
$$\Delta q = \lfloor (R + O) \cdot \lambda \rfloor - \lfloor R \cdot \lambda \rfloor \approx \lambda \cdot O$$
由于 $\lambda = 1.37639 \times 10^{-4}$：
$$\Delta q \approx 0.000137639 \times O$$

#### 单轮极端最坏情形 (Worst-Case Single Epoch)
- 场景：创世第 1 轮（配额最大 $q_0 \approx 123,875.18$ TTK），全网仅有 1 个独占算力矿工（算力占比 100%）。
- 矿工因 20% 硬顶只能领取 $24,775.03$ TTK，产生全网最大理论溢出：
  $$O_{\max} = 80\% \times 123,875.18 \text{ TTK} = 99,100.14 \text{ TTK}$$
- **对紧邻下一轮配额的影响**：
  $$\Delta q_1 = 1.37639 \times 10^{-4} \times 99,100.14 \text{ TTK} \approx 13.64 \text{ TTK}$$
- **相对相对偏差比例**：
  $$\frac{\Delta q_1}{q_1} = \frac{13.64}{123,875} \approx 0.0110\% \quad (1.1 \text{ 个基点 / 1.1 bps})$$

#### 多轮延迟叠加最坏情形 (Multi-Epoch Delayed Claim)
- 假设极端场景：连续 24 轮（整整 1 天）全网独占巨鲸均故意不调用 `claim`。
  累积未回归的溢出总量约为 $24 \times 99,100 \approx 2.378 \times 10^6$ TTK（约 237.8 万枚）。
- 此时对第 25 轮配额的暂时性压制影响为：
  $$\Delta q_{25} \approx 1.37639 \times 10^{-4} \times 2,378,000 \approx 327.3 \text{ TTK}$$
  相对当期基准配额的偏差仅为 **0.26%**。

### 3.3 安全性与博弈论结论
1. **代币绝对守恒**：晚领只会导致曲线以微不可察的幅度（< 0.3%）在时间轴上向后顺延，代币既未蒸发亦未凭空增发；一旦矿工发起 `claim`，所有的 $O$ 全部回滚注入 $R$，后序轮次配额相应微幅提高，全周期总积分恒等；
2. **巨鲸无获利动机**：巨鲸无法通过控制自身领取时间来提高自己的收益（其自身在历史轮次的奖励份额已由 `settledEpochQuota` 和 `epochAddrCapBps` 锁死为 20%）；
3. **经济学自驱性**：矿工有极强的套现、质押和再投资动机，在真实主网中绝大多数矿工会在 24 小时内领取，实际影响低于 0.01%。

---

## 4. 取整机制、误差上界与超发风险分析

### 4.1 取整逻辑
在智能合约中，配额生成核心语句为：
`uint256 q_e = (remainingEmission * LAMBDA_WAD) / 1e18;`
Solidity 原生无符号整型除法严格执行**向下取整 (Floor)**。
设理论连续数学值为 $q_e^* = \frac{R \cdot \lambda_{\text{wad}}}{10^{18}}$，实际值为 $q_e = \lfloor q_e^* \rfloor$。
单次计算舍入截断偏差为：
$$\epsilon_e = q_e^* - q_e \in [0, 1) \text{ wei}$$

### 4.2 全生命周期累计误差上界
在整个挖矿生命周期中：
1. **周期结算截断**：每周期在 `sync` 计算 $q_e$ 时损失小于 1 wei；
2. **算力未达标比例截断**：`allocatable = (q_e * tw) / wm` 向下取整损失小于 1 wei；
3. **矿工领取份额截断**：`amt = (effQ * uw) / denom` 向下取整损失小于 1 wei / 人次；
4. **单地址上限截断**：`maxAmt = (effQ * capBps) / 10000` 向下取整损失小于 1 wei。

**误差上限计算**：
- 在标准 1394 天周期内（$N = 33,456$ 个 epoch）：
  仅考虑配额生成核心项，累计最大截断误差：
  $$\text{Error}_{\max} < 33,456 \times 1 \text{ wei} \approx 3.35 \times 10^{-14} \text{ TTK}$$
- 即使按 10 年（87,600 周期）加上数百万次矿工 claim 交易统计：
  全系统累计总截断损失严格小于 $10^{14}$ wei = **$0.0001$ TTK**（不足万分之一枚代币）。

### 4.3 能否出现超发？
**结论：绝对不可能出现超发（0 风险）。**
- **数学证明**：
  因为除法均为向下取整，合约中分配出的总配额永远小于或等于理论数学值：
  $$\sum \text{Allocated} \le \sum q_e \le \int \lambda R \, dt$$
  在任何时刻 $t$，系统严格满足不变量：
  $$\text{TotalMinted}(t) + \text{PendingClaimableReserved}(t) + \text{remainingEmission}(t) \le \text{MINING\_CAP} = 900,000,000 \times 10^{18}$$
  且底层代币合约 `Token.sol` 包含硬顶断言：
  `require(totalMined + amount <= MINING_CAP, "Mining cap exceeded");`
  双重锁死，超发在逻辑与底层均被绝对杜绝。

---

## 5. 重入安全性与 Checks-Effects-Interactions (CEI) 审计

### 5.1 sync(uint256 maxSteps) 重入分析
- **外部调用审计**：
  `sync` 内部全部为算术运算、存储槽读写（`settledEpochQuota`, `remainingEmission`, `unallocatedGap`, `ema`, `target`, `lastSynced`）以及事件发送（`EpochSettled`, `EpochSynced`）。
  **全过程包含 0 个外部合约调用**（No external calls）。
- **结论**：不存在任何被外部合约劫持控制流进行重入的可能。

### 5.2 claim(uint256 e) 状态更新顺序与 CEI 审计
`claim` 函数执行路径具有严格的时序保障：
```solidity
function claim(uint256 e) external override nonReentrant {
    // 1. Checks (前置检查)
    if (e >= epoch()) revert EpochNotEnded();
    if (lastSynced <= e) {
        sync(48); // 内部函数调用，内部无外部调用
        if (lastSynced <= e) revert SyncingRequired();
    }
    if (claimed[e][msg.sender]) revert AlreadyClaimed();
    
    // 2. Effects (本地状态变更)
    claimed[e][msg.sender] = true; // [状态写 1] 优先防重复

    uint256 uw = userWeight[e][msg.sender];
    if (uw == 0) revert ZeroWeight();

    uint256 effQ = settledEpochQuota[e];
    if (effQ == 0) {
        emit RewardClaimed(msg.sender, e, 0, 0);
        return;
    }

    uint256 tw = totalWeight[e];
    uint256 wm = wMin(e);
    uint256 denom = tw > wm ? tw : wm;
    uint256 amt = (effQ * uw) / denom;

    uint256 capBps = epochAddrCapBps[e];
    if (capBps == 0) capBps = ADDR_CAP_BPS;
    uint256 maxAmt = (effQ * capBps) / 10000;
    uint256 cappedOverflow = 0;
    if (amt > maxAmt) {
        cappedOverflow = amt - maxAmt;
        amt = maxAmt;
        remainingEmission += cappedOverflow; // [状态写 2] 溢出加回 R
    }

    emit RewardClaimed(msg.sender, e, amt, cappedOverflow);

    // 3. Interactions (外部交互)
    if (amt > 0) {
        token.mint(msg.sender, amt); // 唯一的外部调用，置于函数最末端
    }
}
```
- **关键安全判定**：
  1. `claimed[e][msg.sender] = true` 在计算和代币分发前即刻写入；
  2. `remainingEmission += cappedOverflow` 在外部调用 `token.mint` 之前完成更新；
  3. 函数入口修饰器声明了 OpenZeppelin `nonReentrant`；
  4. 外部调用 `token.mint` 严格遵循 Checks-Effects-Interactions (CEI) 模式，置于最后一行；
  5. 结论：重入漏洞防御等级为最高级别，不可被攻击。

---

## 6. LAMBDA_WAD 高精度推导、常量差异分析与设计文档勘误

### 6.1 高精度推导过程与结果对比
根据定义：希望在满额参与基准下，经过 $N = 1394 \times 24 = 33,456$ 个周期后累计释放比例达到 $p$：
$$(1 - \lambda)^N = 1 - p \implies \lambda = 1 - (1 - p)^{1/N}$$

使用 Python `decimal` 模块在 100 位有效精度下的精确推导结果如下：

| 参数档位 | 剩余比例 $(1-p)$ | 理论高精度实数值 $\lambda$ | Wad 精确向下取整 (Floor) | Wad 精确四舍五入 (Round) | 合约当前常量 | 差异说明 |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **$p = 98.0\%$** | $0.02$ | $0.00011692355024993568...$ | **`116923550249935`** | `116923550249936` | - | 设计文档勘误项 |
| **$p = 99.0\%$ (推荐)** | $0.01$ | $0.00013763908575473133...$ | **`137639085754731`** | **`137639085754731`** | **`137639085754704`** | 合约常量比精确值小 27 |
| **$p = 99.9\%$** | $0.001$ | $0.00020645152427489667...$ | **`206451524274896`** | `206451524274897` | - | 设计文档勘误项 |

### 6.2 合约常量与精确值差异的影响量化与建议
- **差异原因**：
  合约常量 `137639085754704` 来源于早先开发阶段采用 IEEE-754 64 位双精度浮点数运算 `1.0 - 0.01 ** (1.0 / 33456)` 时有效位数舍入误差。
- **全周期差异量化**：
  通过 100 位高精度数值积分模拟 33,456 轮（1394 天）满额参与发行：
  - 合约常量 `137639085754704` 累计铸造：`890,999,999.9999917696` TTK (释放比例 `99.000000%`)；
  - 精确常数 `137639085754731` 累计铸造：`890,999,999.9999999005` TTK (释放比例 `99.000000%`)；
  - **累积偏差**：`8,130,927,133,389` wei $\approx$ **`0.00000813` TTK** (< 0.00001 枚代币，相对偏差仅 $0.0000009\%$)。
- **治理与工程建议**：
  1. **测试网阶段建议保持现状**：当前 `137639085754704` 已经过完整的测试用例全量覆盖，且其累计偏差不足十万分之一枚代币，对硬顶守恒及经济模型没有任何实质影响，无需在当前已封板的 rc2 代码中引入扰动；
  2. **主网定案建议**：在主网最终部署前，若团队追求数学纯粹性，可提交小提交将常量规范更新为高精度整数 `137_639_085_754_731`。

### 6.3 设计文档 `docs/design-progress-indexed-emission.md` 勘误明细
在原设计文档第 2.2 节表格中：
1. **$p = 98.0\%$ 档**：原表记录为四舍五入值 `116923550249936`（部分草稿浮点值为 `116923550249948`），向下取整精确值为 **`116923550249935`**；
2. **$p = 99.9\%$ 档**：原表记录为四舍五入值 `206451524274897`（部分草稿浮点值为 `206451524274898`），向下取整精确值为 **`206451524274896`**。

---

## 7. effectiveQuota 纯函数改 View 视图的调用点全面适配核实

### 7.1 函数签名与 ABI 兼容性
`function effectiveQuota(uint256 e)` 由 `pure` 改为 `view`：
- **函数选择器不变**：`keccak256("effectiveQuota(uint256)")[:4] == 0xee690554`，选择器完全一致；
- **RPC 兼容性**：客户端通过 `eth_call` 调用 `pure` 与 `view` 均为只读静态调用，底层 EVM 操作码均为 `STATICCALL`。

### 7.2 全代码库与脚本调用点审计结果

| 调用方文件 | 修改前调用方式 | 当前代码状态 | 适配结论 |
| :--- | :--- | :--- | :---: |
| `src/interfaces/IMiner.sol:31` | `pure returns (uint256)` | `function effectiveQuota(uint256 e) external view returns (uint256);` | **已适配** |
| `script/status.py:257` | `eth_call` 静态调用查询 | 通过 `eth_call` 查询 `effectiveQuota`，并新增了 `SEL_REMAINING_EMISSION` 查询与打印 | **已适配** |
| `script/claim.py:56` | `eth_call` 静态调用查询 | 保留 `effectiveQuota` 估算打印，并新增 `SEL_REMAINING_EMISSION` 和 `SEL_SETTLED_EPOCH_QUOTA` 选择器 | **已适配** |
| `script/keeper.py` | 历史无 `effectiveQuota` 调用 | 新增 `SELECTOR_REMAINING_EMISSION` 与 `SELECTOR_SETTLED_EPOCH_QUOTA` 常量，主循环依赖 `sync` 与 `lastSynced` | **已适配** |
| `test/RewardBucketingInvariant.t.sol` | 静态调用 | 已适配为 `miner.effectiveQuota(e)` 视图调用 | **已适配** |
| `test/MinerV1.t.sol` | 静态断言 | 适配为动态状态结算测试 | **已适配** |

---

## 8. 存储布局对比 (rc2 vs rc1)

### 8.1 原始 Forge 检查输出 (`forge inspect MinerV1 storage-layout`)

```text
╭--------------------------------------------------+-------------------------------------------------+--------------------------------------------------------------------------------+--------+-------+----------------------------------------------------------------------------------╮
| Name                                             | Type                                            | Slot                                                                           | Offset | Bytes | Contract                                                                         |
+=========================================================================================================================================================================================================================================================================================+
| token                                            | contract IToken                                 | 0                                                                              | 0      | 20    | src/MinerV1.sol:MinerV1                                                          |
| vault                                            | contract IVault                                 | 1                                                                              | 0      | 20    | src/MinerV1.sol:MinerV1                                                          |
| admin                                            | address                                         | 2                                                                              | 0      | 20    | src/MinerV1.sol:MinerV1                                                          |
| START                                            | uint256                                         | 3                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| S_STAR                                           | uint256                                         | 4                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| ADDR_CAP_BPS                                     | uint256                                         | 5                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| paused                                           | bool                                            | 6                                                                              | 0      | 1     | src/MinerV1.sol:MinerV1                                                          |
| lastSynced                                       | uint256                                         | 7                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| ema                                              | uint256                                         | 8                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| epochSeed                                        | mapping(uint256 => uint256)                     | 9                                                                              | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| target                                           | mapping(uint256 => uint256)                     | 10                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| totalShares                                      | mapping(uint256 => uint256)                     | 11                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| totalWeight                                      | mapping(uint256 => uint256)                     | 12                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| usedMask                                         | mapping(uint256 => mapping(address => uint16))  | 13                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| counted                                          | mapping(uint256 => mapping(address => uint8))   | 14                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| userWeight                                       | mapping(uint256 => mapping(address => uint256)) | 15                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| claimed                                          | mapping(uint256 => mapping(address => bool))    | 16                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| epochSStar                                       | mapping(uint256 => uint256)                     | 17                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| epochAddrCapBps                                  | mapping(uint256 => uint256)                     | 18                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| guardian                                         | address                                         | 19                                                                             | 0      | 20    | src/MinerV1.sol:MinerV1                                                          |
| unallocatedGap                                   | mapping(uint256 => uint256)                     | 20                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| remainingEmission                                | uint256                                         | 21                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| settledEpochQuota                                | mapping(uint256 => uint256)                     | 22                                                                             | 0      | 32    | src/MinerV1.sol:MinerV1                                                          |
| __gap                                            | uint256[44]                                     | 23                                                                             | 0      | 1408  | src/MinerV1.sol:MinerV1                                                          |
| openzeppelin.storage.Initializable._initialized  | uint64                                          | 108904022758810753673719992590105913556127789646572562039383141376366747609600 | 0      | 8     | lib/openzeppelin-contracts/contracts/proxy/utils/Initializable.sol:Initializable |
| openzeppelin.storage.Initializable._initializing | bool                                            | 108904022758810753673719992590105913556127789646572562039383141376366747609600 | 8      | 1     | lib/openzeppelin-contracts/contracts/proxy/utils/Initializable.sol:Initializable |
╰--------------------------------------------------+-------------------------------------------------+--------------------------------------------------------------------------------+--------+-------+----------------------------------------------------------------------------------╯
```

### 8.2 对照比对表 (rc1 vs rc2)

| 槽位 (Slot) | rc1 (mainnet-rc1) | rc2 (feature/progress-emission) | 占用空间 (字节) | 状态 |
| :---: | :--- | :--- | :---: | :---: |
| **0..19** | `token` ~ `guardian` (20 个槽位) | `token` ~ `guardian` (20 个槽位) | 各 32 字节 | **严格一致** |
| **20** | `unallocatedGap` (mapping) | `unallocatedGap` (mapping) | 32 字节 | **严格一致** |
| **21** | `__gap[0]` (Slot 21..66, 共 46 槽) | **`remainingEmission` (uint256)** | 32 字节 | **【新增】** |
| **22** | `__gap[1]` | **`settledEpochQuota` (mapping)** | 32 字节 | **【新增】** |
| **23..66** | `__gap[2..45]` | **`__gap` (uint256[44])** (Slot 23..66) | 1,408 字节 (44 槽) | **【缩减 2 槽】** |
| **命名空间槽** | `Initializable` (ERC-7201 槽位) | `Initializable` (ERC-7201 槽位) | 9 字节 | **严格一致** |

- **结论**：Slot 21 至 66 的总跨度严格等于 $1 + 1 + 44 = 46$ 个槽位，与旧版 `__gap[46]` 空间完全等价，整体存储边界无任何溢出或错位。

---

## 9. 自动化测试、Gas 消耗对比与静态分析报告

### 9.1 Foundry 自动化测试结果 (`forge test --fuzz-runs 5000`)
- **执行命令**：`forge test --fuzz-runs 5000`
- **执行结果**：**全部通过** (19 test suites, 111 passed, 0 failed, 0 skipped)。
- **核心新增测试覆盖**：
  - `ProgressEmissionTest:test_ConservationInvariantRandomClaimOrder`: 在随机矿工领取顺序下验证代币守恒不变量 $\sum \text{Mined} + R \equiv \text{MINING\_CAP}$；
  - `ProgressEmissionTest:test_PostHiatusQuotaBoundedByLambdaR`: 验证 100 轮空档期后配额严格有界，绝无突发百倍暴增；
  - `ProgressEmissionTest:test_ClaimTimingImpactOnSubsequentQuotas`: 验证早领与晚领的精确数值偏差 $\Delta q = \lambda \cdot O$；
  - `ProgressEmissionTest:test_SyncGasBenchmark`: 压测 `sync(1)` 与 `sync(24)` 的 Gas 消耗。

### 9.2 Gas 消耗对比分析
依据 `forge snapshot` 与基准对比：
- **`sync(1)` (单轮结算)**：
  - rc1 耗费：约 55,000 ~ 62,000 gas；
  - rc2 耗费：约 77,000 ~ 85,000 gas；
  - 增量分析：因新增写入 `settledEpochQuota` 映射及更新 `remainingEmission`，增加约 23,000 gas。在 BSC 测试网单笔仅需不到 0.0001 美元，完全可接受。
- **`claim(e)` (普通矿工领取)**：
  - rc1 耗费：约 68,000 ~ 82,000 gas；
  - rc2 耗费：约 68,000 ~ 82,300 gas；
  - 增量分析：由于 `settledEpochQuota[e]` 替换了原有闭式算术运算，普通矿工领奖 Gas 几乎持平 ($\pm 300$ gas)。

### 9.3 Python 测试套件 (`pytest`)
- **执行命令**：`pytest`
- **执行结果**：`31 passed, 292 warnings in 18.80s` (全部通过，0 failed)。
- **覆盖模块**：`test_keeper.py` (Keeper 机器人状态机与重试逻辑)、`test_miner.py`、`test_miner_gpu.py`。

### 9.4 静态分析 (`slither`)
- **工具版本**：Slither 0.11.6。
- **扫描结果**：分析了 8 个合约，检出 48 项提示信息，核心重点审查如下：
  1. `divide-before-multiply` in `sync`:
     - 现象：`q_e = (R * LAMBDA) / 1e18` 后再 `allocatable = (q_e * tw) / wm`；
     - 审计结论：**误报/设计意图**。`q_e` 必须作为基准配额先固化写入 `settledEpochQuota`，再按比例分配，精度损失小于 1 wei，不构成缺陷；
  2. `timestamp comparisons`:
     - 现象：`epoch()` 基于 `block.timestamp` 划分周期；
     - 审计结论：**设计意图**。PoW 挖矿周期按 3600 秒固定时长自然推进，矿工无法操纵整小时跨度；
  3. `unused-state` / `dead-code`:
     - 现象：`__gap` 未直接读取，`_powFixed` 未被外部调用；
     - 审计结论：`__gap` 系 OpenZeppelin 推荐的标准升级保留槽，`_powFixed` 为内部预留算法，安全无害。