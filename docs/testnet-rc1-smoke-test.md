> **说明**：本文件是 RC1 的历史记录，地址与 START 已被 RC2 取代，当前有效部署见 docs/deployment-bsc-testnet-rc2.md。

# PoolCoin BSC 测试网 (chainId: 97) 矿工端冒烟测试 SOP

> **文档定位与核心准则**：
> 本文档基于 `mainnet-rc1` 版本代码库与 `script/` 目录下的矿工工具链（`miner_gpu.py`、`claim.py`、`status.py`、`keeper.py`、`submit_share.py`），为开发与运维人员提供在 BSC 测试网（chainId: 97）执行全链路端到端冒烟测试的标准化实操指南。
> 
> **安全红线**：
> 1. **严禁明文私钥**：全流程强制通过 Foundry 加密 Keystore 交互，杜绝在命令行参数、配置文件、日志或历史记录中暴露私钥；
> 2. **环境变量隔离**：RPC 节点连接统一读取环境变量（如 `$env:RPC_URL` 或 `$RPC_URL`），严禁在文档或终端中回显或硬编码真实 RPC 地址；
> 3. **不可读写敏感文件**：严禁读取 `.env` 文件，严禁向任何未经授权的外部服务上报数据。

---

## 1. 核心网络参数与合约地址基线

测试网核心部署数据与合约地址（BSC Testnet, chainId: 97）：

| 实体名称 | 合约类型 | 链上地址 (Checksum) | 说明 |
| :--- | :--- | :--- | :--- |
| **StakeVault** | 质押金库 | `0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7` | 矿工 0.01 tBNB 质押与资格管理，不可升级 |
| **Miner (代理)** | 业务代理入口 | `0x420382a0f874c1F120621f847d81581e8737504e` | TransparentUpgradeableProxy，PoW 交互唯一入口 |
| **MinerV1 (实现)**| 逻辑实现合约 | `0xE8dC8F90F631c23BaA5f81fBF86B79DF11d89EA9` | 业务逻辑核心（升级后实现为 `0xD397...b915`） |
| **Token (TTK)** | 挖矿 ERC20 代币 | `0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29` | 铸币权限仅限 Miner 代理合约 |
| **ProxyAdmin** | 代理升级管理 | `0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185` | 管理员归属 TimelockController |
| **TimelockController**| 治理时间锁 | `0x75F344F2c5a79d945D873Edbdccf67e61e56C9de` | 延时 600 秒 (`minDelay = 600s`)，系统最高权限控制者 |

### 协议经济与时序常数
- **轮次时长 (`EPOCH_LEN`)**: 3600 秒 (1 小时)
- **启动时间戳 (`START`)**: `1793869200` (UTC: `2026-11-05 09:00:00`)
- **质押要求 (`DEPOSIT`)**: `0.01 tBNB` (`10000000000000000 wei`)
- **提款冷静期 (`COOLDOWN_EPOCHS`)**: 48 轮 (48 小时)
- **单轮单地址份额上限 (`CAP`)**: 6 份 (`k \in [0, 5]`)
- **预热爬坡期 (`RAMP_EPOCHS`)**: 24 轮；起始权重比例 `RAMP_START_BPS = 2500` (25%)
- **最低难度目标 (`EASIEST`)**: `1 << 224` (32 位零前缀)
- **最高难度目标 (`HARDEST`)**: `1 << 221` (35 位零前缀)

---

## 2. 步骤 1：专用测试矿工账户的准备 (Foundry Keystore)

为满足“零明文私钥”要求，测试矿工账户必须使用 Foundry 的原生加密 Keystore 进行管理。私钥在本地经 AES-128-CTR 加密存储，仅在发起签名交易时由终端交互式提示输入口令解锁。

### 2.1 创建或导入 Keystore

**方式 A：生成全新测试矿工账户**
```bash
cast wallet new ~/.foundry/keystores/miner-smoke
```
终端将提示设置口令并确认，成功后生成标准加密 JSON 文件。

**方式 B：通过助记词/私钥交互式导入已有测试账户**
```bash
cast wallet import miner-smoke --interactive
```
在交互式输入提示中安全粘贴私钥并设定加密密码，避免留在终端历史记录中。

### 2.2 查看矿工公钥地址
```bash
cast wallet address --account miner-smoke
```
记录返回的 0x 地址，记为 `<MINER_ADDRESS>`。

### 2.3 获取测试代币与余额检查
1. 前往 BSC Testnet 官方水龙头或从已有测试资产钱包向 `<MINER_ADDRESS>` 转入至少 **0.05 tBNB**（用于支付 0.01 tBNB 质押金及后续交互 Gas 费）。
2. 查询链上余额：
```bash
cast balance <MINER_ADDRESS> --rpc-url $env:RPC_URL
```
**预期输出**：
`>= 50000000000000000` (`>= 0.05 tBNB`)

---

## 3. 步骤 2：向 StakeVault 质押

### 3.1 质押函数与金额约束
- **目标合约**: `StakeVault` (`0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7`)
- **调用函数**: `deposit() external payable`
- **金额约束**: 必须**严格等于**合约记录的 `DEPOSIT` 值（即 `0.01 ether` = `10^16 wei`）。金额过多或过少均会直接触发 `BadDeposit()` 回滚。

### 3.2 发送质押交易
```bash
cast send 0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7 "deposit()" \
  --value 0.01ether \
  --account miner-smoke \
  --legacy \
  --gas-price 1000000000 \
  --rpc-url $env:RPC_URL
```
*(提示：终端将提示输入 Keystore 解密口令)*

### 3.3 验证质押状态
质押确认后，通过以下命令核验链上质押记录：

**命令 1：查询激活状态与质押存入轮次**
```bash
cast call 0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7 "status(address)(bool,uint256)" <MINER_ADDRESS> --rpc-url $env:RPC_URL
```
**预期输出**：
`true  <depositEpoch>` (例如 `true 14`)

**命令 2：查询质押结构体完整详情**
```bash
cast call 0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7 "stakes(address)(uint64,uint64,bool,uint128)" <MINER_ADDRESS> --rpc-url $env:RPC_URL
```
**预期输出**：
`<depositEpoch>  0  true  10000000000000000`
（分别对应：存入轮次、提款申请轮次为 0、质押状态 true、质押金额 0.01 BNB）

**命令 3：通过 status.py 全景核对**
```bash
python script/status.py <MINER_ADDRESS> --rpc $env:RPC_URL
```
**预期关键输出**：
```text
【1. 质押与基础轮次状态 (Staking & Protocol Status)】
  [*] 质押状态 (active):        Active (已激活生效)
  [*] 质押生效轮 (since):       Epoch <depositEpoch>
  [*] 质押金额 (deposit):       0.0100 BNB (10000000000000000 wei)
  [*] 提款请求状态:             正常质押中 (未发起提款申请)
```

### 3.4 核心协议规则：质押预热期 (Warmup Rule)
> **极重要前置防错规则**：
> 根据 `MinerV1.sol` 源码中 `_getRamp` 函数：
> ```solidity
> (bool active, uint256 since) = vault.status(user);
> if (!active || e < since + 1) revert NoActiveStake();
> ```
> **矿工在质押存入当期（`since`）不可提交任何份额！**
> 必须等待链上时间推进进入下一轮（即 `currentEpoch >= since + 1`），矿工身份才正式具备提交资格。如果在存入当轮直接发起提交，将因 `e < since + 1` 必然触发 `NoActiveStake()` 错误回滚。

---

## 4. 步骤 3：提交份额前提与算力耗时估算

### 4.1 提交份额的 5 大链上前置条件
在发起 PoW 份额计算与提交前，链上必须同时满足以下 5 项断言：
1. **矿工质押合格**: `active == true` 且当前轮次 `currentEpoch >= since + 1`；
2. **协议同步就绪**: `lastSynced == currentEpoch`。若 `lastSynced < currentEpoch`，必须先由 Keeper 或手动调用 `sync(steps)` 补齐；
3. **当轮种子已初始化**: `epochSeed[currentEpoch] != 0`。若为 `0x0`，需调用 `initEpochSeed(currentEpoch, 24)` 生成；
4. **难度目标已确立**: 读取 `target[currentEpoch]`，初始默认值为 `EASIEST = 1 << 224`；
5. **未达到单地址配额**: 检查 `usedMask[currentEpoch][miner]` 与 `counted`，确保当前轮已提交份额 `< CAP (6)`，且本次选择的索引 `k \in [0, 5]` 尚未被置位。

### 4.2 真实难度下的哈希空间与期望运算量
- **难度位宽**: 在最低难度 `EASIEST = 1 << 224` 下，有效哈希高位必须具备至少 $256 - 224 = 32$ 个前导零位。
- **期望哈希尝试次数**:
  $$E[N] = 2^{32} = 4,294,967,296 \approx 42.95 \text{ 亿次}$$

### 4.3 CPU 路径实测算力与耗时测算
基于 `script/miner.py` 在现代 x86-64 CPU 环境的实测数据：
- **单核 Python 算力**: 约 $100 \text{ kH/s}$ ($10^5 \text{ H/s}$)。
  $$\text{期望耗时} = \frac{4.295 \times 10^9}{10^5} \approx 42,950 \text{ 秒} \approx 11.93 \text{ 小时}$$
- **多核多进程 (5~10 核心 + PyCryptodome CFFI)**: 实测吞吐约 $4.5 \sim 4.8 \text{ MH/s}$。
  $$\text{期望耗时} = \frac{4.295 \times 10^9}{4.6 \times 10^6} \approx 933.7 \text{ 秒} \approx 15.56 \text{ 分钟}$$
- **若难度上调为 34 位 ($2^{34} \approx 171.8 \text{ 亿次}$)**:
  $$\text{多核耗时} = \frac{1.718 \times 10^{10}}{4.6 \times 10^6} \approx 3,735 \text{ 秒} \approx 62.25 \text{ 分钟}$$

### 4.4 泊松分布与轮次窗口瓶颈分析
1. **单轮时长限制**: 协议单轮时长为固定 3600 秒 (60 分钟)。
2. **长尾方差风险**: PoW 求解服从指数分布，方差极大。多核 CPU 即使均值 15.6 分钟，仍有约 $e^{-3600/933.7} \approx 2.1\%$ 的概率在整整 1 小时内一无所获；
3. **打满 CAP=6 瓶颈**: 若测试目标需要验证满额提交（`CAP = 6` 份），多核 CPU 理论期望需要 $6 \times 15.56 \approx 93.4 \text{ 分钟} > 60 \text{ 分钟}$，在单轮内使用 CPU 几乎无法完成 6 份份额的开采。

### 4.5 GPU 路径实测算力与优势对比
基于 `script/miner_gpu.py` 在消费级主流显卡（NVIDIA RTX 4060 Ti, OpenCL 批处理 $16\text{M}$ 并发）的实测数据：
- **实测平均算力**: **$1.20 \sim 1.35 \text{ GH/s}$** ($1,200 \sim 1,350 \text{ MH/s}$)，速度是 10 核多进程 CPU 的约 **280 倍**；
- **32 位难度单份期望耗时**:
  $$\text{GPU 耗时} = \frac{4.295 \times 10^9}{1.25 \times 10^9} \approx 3.44 \text{ 秒} / \text{份}$$
- **34 位难度单份期望耗时**: 仅约 $12 \sim 14$ 秒 / 份；
- **打满 CAP=6 总耗时**: 32 位难度下仅需约 $20 \sim 30$ 秒，34 位难度下仅需约 $1.5$ 分钟。

### 4.6 结论：是否需要 GPU
- **极简单份验证**: 若仅需提交 1 份份额且具备 8 核心以上的高性能 CPU，多进程 CPU 可以在约 15~30 分钟内完成一次单点冒烟测试；
- **生产级/高频/完备冒烟测试**: **必须配置并使用 GPU (`script/miner_gpu.py`)**。由于测试网难度可能随全网算力上调，且单轮严格限制在 1 小时以内，GPU 是唯一能够消除寻优随机性滞后、在 1~2 分钟内稳定产出有效份额的高可靠手段。

---

## 5. 步骤 4：提交份额、等待周期结束、sync、claim 完整命令序列

以下按实际业务流展现端到端操作步骤与每一步的预期控制台回执。

### 5.1 第 1 步：全景状态巡检与前置准备
```bash
python script/status.py <MINER_ADDRESS> --rpc $env:RPC_URL
```
**关键检查项**：
- `质押状态 (active)`: 必须为 `Active`；
- 确认 `currentEpoch >= since + 1`；
- `协议同步轮次 (lastSynced)`: 必须等于 `currentEpoch`（若落后需先执行 Keeper 或 `sync`）。

### 5.2 第 2 步：运行 GPU 矿工开采有效份额
```bash
python script/miner_gpu.py --miner <MINER_ADDRESS> --rpc $env:RPC_URL --out share_smoke.json
```
**预期输出**：
```text
=== GPU Device Detection ===
[0] Backend:       PyOpenCL
    Platform:      NVIDIA CUDA (NVIDIA Corporation)
    Device Name:   NVIDIA GeForce RTX ...
=== PoolCoin GPU-Accelerated PoW Miner ===
Target Network:   BSC Testnet (Chain ID 97)
Miner Contract:   0x420382a0f874c1F120621f847d81581e8737504e
Current Epoch:    15
Epoch Seed:       0x8a92...
Target Difficulty: 32 bits
Selected Share k: 0 (usedMask: 0x0000)
Sub Hash:         0x3f1a...
[Progress] Hashes: 16,777,216 | Speed: 1250.40 MH/s ...
[SUCCESS] Share Found on GPU!
Nonce:            184920482 (0xb05ac22)
Share Hash:       0x000000003b12...
Effective Bits:   32 bits (required >= 32)
Elapsed Time:     3.12 s
Share successfully saved to: share_smoke.json
```

### 5.3 第 3 步：提交份额上链 (Submit Shares)

**3.3.1 预检模拟 (Dry Run)**
```bash
python script/submit_share.py --file share_smoke.json --rpc $env:RPC_URL
```
**预期输出**：
`[DRY-RUN] Simulation successful! eth_call succeeded. Transaction is valid.`

**3.3.2 使用 Keystore 广播上链**
从 `share_smoke.json` 提取 `k` 与 `nonceDecimal`，调用 `submitShares`：
```bash
cast send 0x420382a0f874c1F120621f847d81581e8737504e \
  "submitShares(uint8[],uint256[])" "[0]" "[184920482]" \
  --account miner-smoke \
  --legacy \
  --gas-price 1000000000 \
  --rpc-url $env:RPC_URL
```
**预期回执**：
- `status`: `1 (success)`
- `gasUsed`: 约 `171,500`
- `logs`: 抛出 `SharesSubmitted(address indexed u, uint256 indexed epoch, uint256 n, uint256 weightAdded)` 事件，表明份额成功入账并累加权重。

### 5.4 第 4 步：等待轮次结束 (Epoch Rollover)
当前轮次必须走满 3600 秒并进入下一轮次后，本轮收益方可结算。
查询链上当前轮次：
```bash
cast call 0x420382a0f874c1F120621f847d81581e8737504e "epoch()(uint256)" --rpc-url $env:RPC_URL
```
当返回值大于开采轮次（即 `currentEpoch > minedEpoch`）时，进入结算步骤。

### 5.5 第 5 步：跨轮协议结算 (Sync)
在矿工 Claim 之前，必须将协议同步进度推进到开采轮次之后（`lastSynced > minedEpoch`），以便合约锁定历史配额并计算权重分母。

**执行命令**（任何地址均可公开免密调用）：
```bash
cast send 0x420382a0f874c1F120621f847d81581e8737504e "sync(uint256)" 48 \
  --account miner-smoke \
  --legacy \
  --gas-price 1000000000 \
  --rpc-url $env:RPC_URL
```
*(亦可直接启动后台 `python script/keeper.py --rpc $env:RPC_URL` 自动监控同步)*

**预期回执**：
- `status`: `1 (success)`
- `gasUsed`: 约 `77,000` / 每跨 1 轮
- `logs`: 依次抛出 `EpochSettled(settledEpoch, q_e, tw, wm, b3)` 与 `EpochSynced(n, nt, ema)` 事件；
- 链上验证：`cast call 0x420382a0f874c1F120621f847d81581e8737504e "lastSynced()(uint256)" --rpc-url $env:RPC_URL` 返回值已超过开采轮次。

### 5.6 第 6 步：领取挖矿奖励 (Claim)

**5.6.1 预检模拟 (Dry Run)**
```bash
python script/claim.py --epoch <minedEpoch> --miner <MINER_ADDRESS> --rpc $env:RPC_URL
```
**预期输出**：
```text
[DRY-RUN] Epoch <minedEpoch> Claim Simulation: SUCCESS
  [*] 矿工本轮可分得奖励: XXX.XXXX TTK
```

**5.6.2 使用 Keystore 广播领取**
```bash
cast send 0x420382a0f874c1F120621f847d81581e8737504e \
  "claim(uint256)" <minedEpoch> \
  --account miner-smoke \
  --legacy \
  --gas-price 1000000000 \
  --rpc-url $env:RPC_URL
```
**预期回执**：
- `status`: `1 (success)`
- `gasUsed`: 约 `90,000 ~ 145,000`
- `logs`: 抛出 `RewardClaimed(address indexed miner, uint256 indexed epoch, uint256 claimedAmount, uint256 cappedOverflow)` 以及 ERC20 `Transfer(address(0), miner, amount)`。

### 5.7 第 7 步：核验矿工代币余额
```bash
cast call 0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29 "balanceOf(address)(uint256)" <MINER_ADDRESS> --rpc-url $env:RPC_URL
```
**预期输出**：
返回大于 0 的 TTK 代币数量（大数形式，18 位精度），证明整条 PoW 挖矿与结算闭环成功！

---

## 6. 失败时的排查表 (Troubleshooting Reference)

在测试网实测中若遇到合约 Revert 回滚，可通过错误 Selector 迅速对齐下表排查定位：

| 错误选择器 (Selector) | 错误定义 (Custom Error) | 发生原因 (Root Cause) | 标准处置措施 (Remediation SOP) |
| :--- | :--- | :--- | :--- |
| `0xfb348942` | `NoActiveStake()` | 1. 矿工尚未在 StakeVault 质押；<br>2. 矿工已发起提款申请；<br>3. **最为常见**：处于质押存入当期（`epoch < since + 1`），预热规则尚未生效。 | 1. 检查 `StakeVault.status(miner)`；<br>2. 若已质押，核对当前轮次是否满足 `currentEpoch >= since + 1`，耐心等待进入下一轮即可开始挖矿。 |
| `0x6bf299d6` | `SeedNotInitialized()` | 当前轮次的随机种子尚未生成（`epochSeed[e] == 0`）。在 BSC 测试网无矿工开采时极易发生。 | 启动 `script/keeper.py`，或手动调用 `Miner.initEpochSeed(currentEpoch, 24)` 补齐种子。 |
| `0xb9b4ff3d` | `SyncingRequired()` | 1. 协议同步进度严重落后（超过 `submitShares` 内部单次 48 步的修复能力）；<br>2. 在 `claim(e)` 时 `lastSynced <= e`。 | 手动发起交易连续调用 `Miner.sync(48)` 追赶进度，直至 `lastSynced == currentEpoch`。 |
| `0x09bde339` | `InvalidProof()` | 1. 计算出的份额未满足目标难度（`hash > target`）；<br>2. 构造 `subSeed` 时地址、k、合约地址、chainId 或 epochSeed 不匹配。 | 1. 检查 OpenCL 内核与 CPU 本地校验结果；<br>2. 确保 chainId 严格为 97，合约地址严格为代理合约地址，且 `epochSeed` 未过期。 |
| `0xff59fb44` | `AddressCapExceeded()` | 单地址当轮提交的份额数超过上限（`counted + ks.length > CAP`，即单轮超过 6 份）。 | 检查 `counted[e][miner]`，每个矿工每轮最多计入 6 份份额。已达 6 份请等待下一轮。 |
| `0xfa5605ef` | `DuplicateShareIndex()` | 提交的份额索引 `k` 在当前轮次已被使用过（`usedMask & (1 << k) != 0`）。 | 查询 `usedMask[e][miner]`，在本次提交中选择尚未被置位的 `k \in [0, 5]`。 |
| `0x0a2e50aa` | `InvalidShareIndex()` | 提交的索引 `k >= CAP`（即 $k \ge 6$）。 | 确保所选份额索引严格在 `[0, 5]` 范围内。 |
| `0x756b7857` | `EpochNotEnded()` | 在当前轮次尚未结束时过早尝试调用 `claim(e)` 领取奖励（`e >= currentEpoch`）。 | 必须等待本轮完全结束（`currentEpoch > e`），且执行 `sync` 之后方可领取。 |
| `0x646cf558` | `AlreadyClaimed()` | 目标轮次的挖矿奖励已经被领取过，不可重复 Claim。 | 查询 `claimed[e][miner]`，若已为 true 且 TTK 余额已到账，无需再次领取。 |
| `0x19a2a9bd` | `ZeroWeight()` | 矿工在该轮次中没有有效算力贡献（`userWeight == 0`，未提交过成功份额）。 | 仅针对实际开采并成功上链份额的轮次发起 Claim。 |
| `0x302bec96` | `SubmissionsPausedError()`| 管理员或 Guardian 紧急暂停了份额提交。 | 检查 `Miner.paused()` 状态，需由 Guardian 或 Timelock 调用 `unpauseSubmissions()`。 |
| `0xa2a6e22d` | `BadBatchLength()` | `ks` 数组与 `nonces` 数组长度不一致，或者数组长度为 0 / 大于 6。 | 确保参数数组长度匹配且 $1 \le \text{length} \le 6$。 |
| `0xd4a7a8d8` | `BadDeposit()` (StakeVault)| 1. 质押转入的 BNB 数量不等于 0.01 BNB；<br>2. 该地址已经处于质押状态，重复质押。 | 确保发送 Value 恰为 `0.01 ether`，且该账户此前未曾质押（或已彻底提款）。 |
| `0x4238e8cb` | `CooldownActive()` (StakeVault)| 申请提款后尚未走满 48 轮冷却期（48 小时）即调用 `withdraw()`。 | 等待提款冷却期届满（`epoch >= withdrawReqEpoch - 1 + 48`）后再执行提款。 |

---

## 7. 本次实测使用的全部命令清单

本指南在编写与链上数据核验过程中，实际在执行终端运行并通过的完整只读与环境交互命令如下：

```bash
# 1. 检索与自检命令
node C:\Users\a2515\.codex\tools\vault-save\vault-save.js selfcheck
node C:\Users\a2515\.codex\tools\vault-save\vault-save.js retrieve --project personal-vault --query "testnet smoke test StakeVault miner"

# 2. 本地仓库结构与脚本状态核对
Get-ChildItem -Path .
Get-ChildItem -Path script
Get-ChildItem -Path docs
git status

# 3. 核心合约与链上参数只读查询 (对齐 BSC 测试网状态)
cast call 0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7 "DEPOSIT()(uint256)" --rpc-url http://127.0.0.1:8545
cast call 0x420382a0f874c1F120621f847d81581e8737504e "epoch()(uint256)" --rpc-url http://127.0.0.1:8545
cast call 0x420382a0f874c1F120621f847d81581e8737504e "lastSynced()(uint256)" --rpc-url http://127.0.0.1:8545
cast call 0x420382a0f874c1F120621f847d81581e8737504e "CAP()(uint256)" --rpc-url http://127.0.0.1:8545
cast call 0x420382a0f874c1F120621f847d81581e8737504e "EASIEST()(uint256)" --rpc-url http://127.0.0.1:8545
cast call 0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29 "balanceOf(address)(uint256)" 0x497D9aE41E7a2301DC962F6a3f07450ce102F932 --rpc-url http://127.0.0.1:8545
```
