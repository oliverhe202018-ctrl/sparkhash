# PoolCoin BSC 测试网 (chainId: 97) rc2 全新部署操作手册 (Deployment Runbook)

- **版本**: `feature/progress-emission` (rc2)
- **目标网络**: BSC Testnet (Chain ID: 97)
- **部署模式**: **全新干净部署 (Fresh Deployment)**（严禁从已有 rc1 代理升级）
- **核心脚本**: `script/Deploy.s.sol` 与 `script/VerifyPostDeploy.s.sol`
- **执行原则**: **只写不执行、只读核对、安全第一**（不得在命令中明文存储私钥与敏感 RPC 地址）

---

## 1. 部署前准备与环境配置

### 1.1 环境变量说明与配置模板 (无敏感泄露)
在独立的安全终端中导出以下环境变量（严禁将私钥与真实 RPC 地址写入 Git 跟踪目录）：

```bash
# ==================== 网络与钱包基础配置 ====================
export CHAIN_ID=97
export RPC_URL="<BSC_TESTNET_RPC_URL>"                     # 填写受信任的 BSC 测试网节点地址
export KEYSTORE_ACCOUNT="<DEPLOYER_KEYSTORE_NAME>"         # 通过 cast wallet 导入的加密账户名
export DEPLOYER_ADDRESS="<DEPLOYER_PUBLIC_ADDRESS>"         # 部署者公钥地址 (需储备 >= 0.05 tBNB)

# ==================== 多签与治理角色地址 ====================
# 要求: 必须为真实合约地址、threshold >= 2、owners >= 3、部署者不得为 owner、二者无重叠签名者
export TIMELOCK_PROPOSER_SAFE="<TIMELOCK_PROPOSER_SAFE_ADDRESS>"
export GUARDIAN_SAFE="<GUARDIAN_SAFE_ADDRESS>"

# ==================== 经济模型与创世分配地址 ====================
export LIQUIDITY_WALLET="<LIQUIDITY_RESERVE_ADDRESS>"       # 预铸 50,000,000 TTK (50%)
export TEAM_VESTING_WALLET="<TEAM_VESTING_ADDRESS>"         # 预铸 30,000,000 TTK (30%)
export COMMUNITY_MULTISIG="<COMMUNITY_MULTISIG_ADDRESS>"     # 预铸 20,000,000 TTK (20%)

# ==================== 治理延时与防呆校验 ====================
export TIMELOCK_MIN_DELAY=600                              # 测试网固定 600 秒 (10 分钟)
export DISCLOSURE_TIMELOCK_DELAY=600                       # 操作员确认输入，必须与 TIMELOCK_MIN_DELAY 严格相等

# ==================== 挖矿启动时间 (START_TIMESTAMP) ====================
# 必须为整点 Unix 时间戳 (能被 3600 整除)，且距当前时间至少 1 小时以后
# 示例: 预定在 UTC 12:00:00 (北京时间 20:00:00) 启动
export START_TIMESTAMP="<ON_THE_HOUR_UNIX_TIMESTAMP>"

# ==================== 质押金库参数 ====================
export DEPOSIT_BNB="10000000000000000"                     # 0.01 tBNB
export MIN_DEPOSIT="1000000000000000"                      # 0.001 tBNB
export MAX_DEPOSIT="10000000000000000000"                  # 10 tBNB
export MIN_DEPOSIT_INTERVAL_EPOCHS=24                      # 24 周期
export MAX_DEPOSIT_MULTIPLIER=2                            # 2 倍
```

### 1.2 源码重映射与子模块依赖约束
为保证多平台构建确定性与字节码哈希 100% 可重现：
1. **禁用自动探测重映射**：`foundry.toml` 中已锁定：
   ```toml
   auto_detect_remappings = false
   ```
2. **固定显式重映射 (`remappings.txt`)**：
   ```text
   @openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/
   @openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/
   forge-std/=lib/forge-std/src/
   openzeppelin-contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/
   openzeppelin-contracts/=lib/openzeppelin-contracts/
   ```
3. **子模块规则**：禁止使用 `--recursive` 递归拉取外部未知依赖，仅使用本地已锁定的 `lib/openzeppelin-contracts` 与 `lib/forge-std`。

---

## 2. 部署脚本准确调用命令与部署明细

### 2.1 Keystore 导入命令 (一次性离线导入)
```bash
# 将部署者私钥安全导入本地加密 Keystore，避免在命令行历史中泄露明文
cast wallet import $KEYSTORE_ACCOUNT --interactive
```

### 2.2 部署广播调用命令
采用 `--account` 调取加密 Keystore，指定 BSC 测试网 Chain ID `97`，固定 Gas 价格为 `1.0 gwei`（`1000000000 wei`，彻底避开测试网 1 wei 估算异常），采用 `--legacy` 兼容 BSC 测试网类型交易，并附加 `--start-fresh` 强制清空历史未完成广播：

```bash
forge script script/Deploy.s.sol:DeployScript \
  --rpc-url $RPC_URL \
  --account $KEYSTORE_ACCOUNT \
  --sender $DEPLOYER_ADDRESS \
  --chain-id 97 \
  --broadcast \
  --slow \
  -vvv \
  --legacy \
  --with-gas-price 1000000000 \
  --start-fresh
```

### 2.3 部署执行顺序与构造参数明细 (共 10 笔连续交易)

| 序号 | 交易类别 | 涉及合约 / 动作 | 构造 / 初始化参数 | 说明 |
| :---: | :--- | :--- | :--- | :--- |
| **0** | `CREATE` | **TimelockController** | `minDelay`: 600 秒<br>`proposers`: `[TIMELOCK_PROPOSER_SAFE]`<br>`executors`: `[address(0)]`<br>`admin`: `DEPLOYER_ADDRESS` | 治理时间锁控制器，部署者暂留临时 Admin |
| **1** | `CREATE` | **Token (TTK)** | `liquidity`: `LIQUIDITY_WALLET`<br>`teamVesting`: `TEAM_VESTING_WALLET`<br>`community`: `COMMUNITY_MULTISIG` | ERC20 代币合约，创世铸造 1 亿枚分配给三方金库 |
| **2** | `CREATE` | **StakeVault** | `depositBnb`: 0.01 ether<br>`start`: `START_TIMESTAMP`<br>`admin`: `DEPLOYER_ADDRESS`<br>`guardian`: `GUARDIAN_SAFE`<br>`minDeposit`: 0.001 ether<br>`maxDeposit`: 10 ether<br>`minDepositIntervalEpochs`: 24<br>`maxDepositMultiplier`: 2 | 矿工质押金库，部署者暂留临时 Admin |
| **3** | `CREATE` | **MinerV1 (实现)** | *无入参构造函数* | PoW 核心逻辑实现合约，构造函数中自动锁定初始化 |
| **4** | `CREATE` | **TransparentUpgradeableProxy** | `_logic`: `MinerV1(实现)` 地址<br>`initialOwner`: `DEPLOYER_ADDRESS`<br>`_data`: `MinerV1.initialize.selector` 编码包 | 业务代理合约。自动部署 OpenZeppelin `ProxyAdmin` |
| **5** | `CALL` | **Token.setMiner** | `_miner`: `MinerProxy` 地址 | 将代币铸币权限绑定到 Miner 业务代理合约 |
| **6** | `CALL` | **ProxyAdmin.transferOwnership** | `newOwner`: `TimelockController` 地址 | 将代理升级管理员所有权移交 Timelock 时间锁 |
| **7** | `CALL` | **MinerV1.transferAdmin** | `newAdmin`: `TimelockController` 地址 | 将挖矿系统管理权移交 Timelock 时间锁 |
| **8** | `CALL` | **StakeVault.transferAdmin** | `newAdmin`: `TimelockController` 地址 | 将质押金库管理权移交 Timelock 时间锁 |
| **9** | `CALL` | **Timelock.renounceRole** | `role`: `DEFAULT_ADMIN_ROLE`<br>`account`: `DEPLOYER_ADDRESS` | **终极脱敏**：部署者放弃治理时间锁管理员角色 |

---

## 3. 治理权限移交与多签角色拓扑

部署完成后，链上权限严格满足 Option B 治理隔离拓扑：

```text
                  +--------------------------------+
                  |    TimelockController          |
                  |  (minDelay = 600s, chain 97)   |
                  +---------------+----------------+
                                  |
         +------------------------+------------------------+
         | 所有权 (admin)                                   | 所有权 (owner)
         v                                                 v
+------------------+                              +-------------------+
|  MinerV1 (Proxy) |                              |    ProxyAdmin     |
|  StakeVault      |                              | (控制实现升级)    |
+--------+---------+                              +-------------------+
         |
         | 紧急暂停权 (guardian)
         v
+------------------+
|  GUARDIAN_SAFE   | (只具备 instant pause 权，无 Proposer / Canceller / 升级权)
+------------------+
```

### 各实体权限核定表
1. **TimelockController**：
   - 唯一的 `ProxyAdmin` 所有者（升级 MinerV1 实现必须经过 10 分钟时间锁排期）；
   - 唯一的 `MinerV1` 管理员（修改 `S_STAR`、`ADDR_CAP_BPS` 必须经过 10 分钟时间锁排期）；
   - 唯一的 `StakeVault` 管理员（修改质押金额必须经过 10 分钟时间锁排期）。
2. **TIMELOCK_PROPOSER_SAFE**：
   - 拥有 TimelockController 的 `PROPOSER_ROLE`（排期提案发起权）；
   - 拥有 TimelockController 的 `CANCELLER_ROLE`（恶意提案否决与撤销权）。
3. **GUARDIAN_SAFE**：
   - 注册为 `MinerV1.guardian`（可随时紧急单向调用 `pauseSubmissions()` 暂停挖矿提交）；
   - 注册为 `StakeVault.guardian`（可随时紧急单向调用 `pause()` 暂停质押充值）；
   - **绝对不具备**时间锁的提案、否决或升级权限（实现权限彻底隔离）。
4. **DEPLOYER (部署者 EOA)**：
   - 权限归零：无任何 Admin、Proposer、Guardian 角色，无法调用任何特权函数。

---

## 4. Sourcify 源码验证命令

BSC 测试网支持通过 Sourcify 进行开源验证。部署完成后，分别执行以下命令对 6 个核心合约进行验证：

```bash
# 1. 验证 TimelockController
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <TIMELOCK_ADDRESS> \
  lib/openzeppelin-contracts/contracts/governance/TimelockController.sol:TimelockController

# 2. 验证 Token (TTK)
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <TOKEN_ADDRESS> \
  src/Token.sol:Token

# 3. 验证 StakeVault
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <VAULT_ADDRESS> \
  src/StakeVault.sol:StakeVault

# 4. 验证 MinerV1 (逻辑实现合约)
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <MINER_IMPL_ADDRESS> \
  src/MinerV1.sol:MinerV1

# 5. 验证 TransparentUpgradeableProxy (代理合约)
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <MINER_PROXY_ADDRESS> \
  lib/openzeppelin-contracts/contracts/proxy/transparent/TransparentUpgradeableProxy.sol:TransparentUpgradeableProxy

# 6. 验证 ProxyAdmin
forge verify-contract \
  --chain-id 97 \
  --verifier sourcify \
  <PROXY_ADMIN_ADDRESS> \
  lib/openzeppelin-contracts/contracts/proxy/transparent/ProxyAdmin.sol:ProxyAdmin
```

---

## 5. 部署后核对清单 (Post-Deployment Checklist)

部署完成后，在正式向矿工开放前，必须执行以下只读核对。

### 5.1 自动化验证脚本执行
项目内置了只读核对脚本 `script/VerifyPostDeploy.s.sol`，可使用无需私钥的纯查询模式直接运行：

```bash
export DEPLOYED_TIMELOCK="<TIMELOCK_ADDRESS>"
export DEPLOYED_TOKEN="<TOKEN_ADDRESS>"
export DEPLOYED_VAULT="<VAULT_ADDRESS>"
export DEPLOYED_PROXY="<MINER_PROXY_ADDRESS>"
export DEPLOYED_MINER_IMPL="<MINER_IMPL_ADDRESS>"

forge script script/VerifyPostDeploy.s.sol:VerifyPostDeploy --rpc-url $RPC_URL -vvv
```
**通过判据**：输出必须全部显示 `[PASS] 1..6` 并打印 `ALL POST-DEPLOYMENT ON-CHAIN ASSERTIONS PASSED`。

### 5.2 关键链上存储槽与状态逐项核对表

```bash
# 1. 核对 ERC-1967 逻辑实现槽 (Implementation Slot)
cast storage $DEPLOYED_PROXY 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc --rpc-url $RPC_URL
# [预期值]: 等于 DEPLOYED_MINER_IMPL (补齐 32 字节 hex)

# 2. 核对 ERC-1967 升级管理槽 (Admin Slot)
cast storage $DEPLOYED_PROXY 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103 --rpc-url $RPC_URL
# [预期值]: 等于 ProxyAdmin 合约地址

# 3. 核对 ProxyAdmin 的所有者 (owner)
cast call <PROXY_ADMIN_ADDRESS> "owner()(address)" --rpc-url $RPC_URL
# [预期值]: 等于 DEPLOYED_TIMELOCK

# 4. 核对实现合约直接初始化锁 (Implementation Init Lock)
# 尝试直接调用逻辑实现合约的 initialize:
cast call $DEPLOYED_MINER_IMPL "initialize(address,address,address,address,uint256)" \
  $DEPLOYED_TOKEN $DEPLOYED_VAULT $DEPLOYER_ADDRESS $GUARDIAN_SAFE $START_TIMESTAMP --rpc-url $RPC_URL
# [预期值]: 必须 REVERT，返回错误 0xf92ee8a9 (InvalidInitialization)

# 5. 核对未分配剩余矿池存量 remainingEmission
cast call $DEPLOYED_PROXY "remainingEmission()(uint256)" --rpc-url $RPC_URL
# [预期值]: 900000000000000000000000000 (精确等于 9 亿枚 TTK, 900M * 1e18)

# 6. 核对创世与经济模型核心参数初值
cast call $DEPLOYED_PROXY "START()(uint256)" --rpc-url $RPC_URL        # 预期等于配置的 START_TIMESTAMP
cast call $DEPLOYED_PROXY "S_STAR()(uint256)" --rpc-url $RPC_URL       # 预期等于 50000
cast call $DEPLOYED_PROXY "ADDR_CAP_BPS()(uint256)" --rpc-url $RPC_URL # 预期等于 2000 (20%)
cast call $DEPLOYED_PROXY "target(uint256)(uint256)" 0 --rpc-url $RPC_URL # 预期等于 EASIEST (2^224 = 2695994666715063979466701508701963067301370074324205423874392753443657449472)
cast call $DEPLOYED_PROXY "ema()(uint256)" --rpc-url $RPC_URL          # 预期等于 50000
cast call $DEPLOYED_PROXY "lastSynced()(uint256)" --rpc-url $RPC_URL   # 预期等于 0
cast call $DEPLOYED_PROXY "paused()(bool)" --rpc-url $RPC_URL          # 预期等于 false

# 7. 核对代币与金库绑定
cast call $DEPLOYED_TOKEN "miner()(address)" --rpc-url $RPC_URL        # 预期等于 DEPLOYED_PROXY
cast call $DEPLOYED_TOKEN "totalSupply()(uint256)" --rpc-url $RPC_URL  # 预期等于 100000000000000000000000000 (1 亿枚)
cast call $DEPLOYED_VAULT "DEPOSIT()(uint256)" --rpc-url $RPC_URL      # 预期等于 10000000000000000 (0.01 tBNB)
```

### 5.3 创世种子初始化核对
- **启动前状态**：
  `cast call $DEPLOYED_PROXY "epochSeed(uint256)(uint256)" 0 --rpc-url $RPC_URL` 返回 `0`。
- **启动时触发**：
  当网络时间推进至 `block.timestamp >= START_TIMESTAMP` 时，由 Keeper 机器人或操作员执行首笔种子初始化：
  `cast send $DEPLOYED_PROXY "initEpochSeed(uint256,uint256)" 0 24 --rpc-url $RPC_URL --account $KEYSTORE_ACCOUNT`
- **初始化后状态**：
  再次读取 `epochSeed(0)`，必须返回非零的 256 位确定性伪随机数哈希。

---

## 6. Keeper 机器人启动方法与预期首批交易

### 6.1 预先检查与启动前 Dry-Run 演练
Keeper 机器人运行在 Python 3 环境下。启动前先通过 `--dry-run` 模式进行只读预检，验证 RPC 连通性、合约地址识别以及当前链上轮次状态：

```bash
python script/keeper.py \
  --miner $DEPLOYED_PROXY \
  --rpc-url $RPC_URL \
  --dry-run
```

### 6.2 正式后台守护启动命令
通过环境变量注入 Keeper 专属操作钱包私钥，并设置合理轮询间隔与 Gas 策略：

```bash
# 建议为 Keeper 钱包单独分配轻量地址，预存 0.02 tBNB
export KEEPER_PRIVATE_KEY="<KEEPER_OPERATOR_PRIVATE_KEY>"

python script/keeper.py \
  --miner $DEPLOYED_PROXY \
  --rpc-url $RPC_URL \
  --private-key $KEEPER_PRIVATE_KEY \
  --poll-interval 15 \
  --min-gas-price-gwei 0.1 \
  --gas-multiplier 1.5 \
  --max-seed-steps 24 \
  --timeout 60
```

### 6.3 预期的首批交易行为与 Gas 消耗预估

#### 交易 1：创世种子初始化 (`initEpochSeed(0, 24)`)
- **触发时机**：区块时间推进到 `START_TIMESTAMP` 瞬间，当前轮次进入 Epoch 0。
- **调用函数**：`initEpochSeed(0, 24)`（函数选择器 `0xd9d9c33c`）。
- **预期 Gas 消耗**：约 **`265,000`** gas。
- **费用预估**：在 1.0 gwei 下约为 `0.000265 tBNB`。
- **产生效果**：基于前一区块哈希生成 `epochSeed[0]`，允许矿工正式提交创世 PoW 份额。

#### 交易 2：第 1 周期结算与第 1 轮种子生成 (`sync(1)` + `initEpochSeed(1, 24)`)
- **触发时机**：区块时间推进到 `START_TIMESTAMP + 3600` 秒（Epoch 1 开启）。
- **调用动作**：
  1. `initEpochSeed(1, 24)`：初始化第 1 周期种子（约 200,000 gas）；
  2. `sync(1)`：结算第 0 周期挖矿收益与难度调整（函数选择器 `0xb1357bf9`）。
- **`sync(1)` 预期 Gas 消耗**：约 **`85,000`** gas。
  - 冷写 `settledEpochQuota[0]`：~22,100 gas；
  - 热写更新 `remainingEmission`：~2,900 gas；
  - 难度与 EMA 调整及事件：~50,000 gas。
- **费用预估**：在 1.0 gwei 下约为 `0.000085 tBNB`。
- **产生效果**：固化 Epoch 0 配额，发出 `EpochSettled(0, q_0, tw, wm, b3)` 事件，并将 `lastSynced` 推进至 1。