# PoolCoin: 按份额比例分配的 PoW 式代币系统

本项目为部署在 BSC 测试网 (chainId: 97) 的 PoW 式代币分发智能合约工程。

---

## 1. 核心架构与分配机制

- **代币总量**: 1,000,000,000 (10 亿) TTK
- **挖矿产出**: 900,000,000 (9 亿, 90%) TTK
- **初始预铸 (占位地址)**:
  - 流动性 (5%): 50,000,000 TTK (占位地址，主网前需注入流动性池/锁仓)
  - 团队 (3%): 30,000,000 TTK (**未实现归属**，当前为占位地址，主网部署前必须替换为经过独立审计的线性归属合约 VestingWallet)
  - 社区 (2%): 20,000,000 TTK (占位地址，主网前配置为社区多签地址)
- **轮次划分**: 每轮 1 小时 (3600 秒)，自 START 起算。
- **每日额度**: 起始 3,000,000 TTK/日，每日衰减系数 0.9967，每轮额度 = 当日额度 / 24。未发额度作废不结转。
- **硬上限封顶**: 采用等比数列闭式计算 cumulativeQuotaBefore(e)，在第 1394 天精确封顶 9 亿，当轮所有人按比例平权缩减，无抢跑优势。
- **份额与权重**:
  - 份额验证: keccak256(subSeed, nonce) <= target
  - 单轮单地址最多计入 15 份 (CAP = 15)
  - 权重计算: w = sqrt(c) * 1e6 * ramp，预热 24 轮权重从 25% 线性增至 100%
  - 差分合并: (w(c + n) - w(c)) * ramp
- **难度调控与防御体系**:
  - 难度带 34–36 位, S_STAR 为软信号; 主要防线是保证金、CAP 与 20% 单地址上限。
  - 单轮限幅: ±25%
  - EMA 系数: 0.5，滞后一轮 (shares = totalShares[n - 2])
- **安全边界与治理权限 (Option B 架构)**:
  - StakeVault: 不可升级，固定 BNB 保证金，提款需 48 轮冷却。
  - MinerV1: Transparent Upgradeable Proxy 架构，逻辑合约禁用初始化器，预留 47 个 storage gap。
  - **提款与 Claim 状态说明**: 当前代码中 claim 与 withdraw 不受暂停影响，但治理方在时间锁延迟后可升级合约。
  - **治理隔离与权限交接**:
    - TimelockController: 由部署脚本部署，proposers 绑定为 Safe 多签地址 (`TIMELOCK_PROPOSER_SAFE`)，executors 设为 `address(0)`（开放任何人无许可执行已过时延的提案），部署完成后部署者原子化撤销 `TIMELOCK_ADMIN_ROLE`（即 `DEFAULT_ADMIN_ROLE`），部署者不再保留管理员角色；
    - Guardian: 独立 Safe 多签地址 (`GUARDIAN_SAFE`)，仅保留零延迟紧急暂停（熔断）权限，无权解暂停、无权改参、无权升级；
    - Deployer EOA: 执行完毕后部署者不再保留管理员角色。

---

## 2. 部署指南与环境配置 (Stage 5 BSC Testnet Deployment Guide)

### 2.1 环境变量配置 (`.env`)

在项目根目录创建 `.env` 文件（**绝不提交至代码库**），填入以下配置：

```shell
# 节点 RPC 配置 (BSC 测试网: chainId 97)
# 默认主 RPC: https://data-seed-prebsc-1-s1.binance.org:8545
# 备用 RPC (以官方文档为准): https://bsc-testnet-rpc.publicnode.com
RPC_URL=https://data-seed-prebsc-1-s1.binance.org:8545

# 部署私钥 (部署者 EOA 私钥，脚本 Deploy.s.sol 严格且唯一读取本变量 PRIVATE_KEY，绝非 DEPLOYER_PRIVATE_KEY；仅通过本机 .env 变量名读取，绝不硬编码，绝不在脚本中打印)
PRIVATE_KEY=0x...

# Keeper 自动化运维机器人私钥 (与部署者 PRIVATE_KEY 严格物理隔离，独立 EOA 地址，需在测试网水龙头领少量 tBNB 作为 gas，仅本机 .env 保存，绝不共用)
KEEPER_PRIVATE_KEY=0x...

# Etherscan API Key (仅保存在本机 .env，绝不进 CI; 用于测试网合约自动化验证)
ETHERSCAN_API_KEY=your_etherscan_api_key_here

# 治理实体 Safe 多签地址配置 (架构选择 B: 必须为已部署且合规的 Safe 智能合约账户)
TIMELOCK_PROPOSER_SAFE=0x4444444444444444444444444444444444444444
GUARDIAN_SAFE=0x5555555555555555555555555555555555555555

# 时间锁最小排期延迟 (测试网默认 600 秒, 主网默认 172800 秒; 披露文档须与此一致，不一致时报错)
TIMELOCK_MIN_DELAY=600
DISCLOSURE_TIMELOCK_DELAY=600

# 预分配代币接收地址 (测试网请换成你控制的地址，切勿直接使用下方示例占位地址)
LIQUIDITY_WALLET=0x1111111111111111111111111111111111111111
TEAM_VESTING_WALLET=0x2222222222222222222222222222222222222222
COMMUNITY_MULTISIG=0x3333333333333333333333333333333333333333

# 关键系统参数 (可选覆盖，默认如下)
# START_TIMESTAMP: 需晚于当前时间至少 1 小时的整点 Unix 时间戳 (必须为 3600 的整倍数，勿写死固定日期)
# 动态整点时间戳计算命令 (Bash 与 PowerShell 各一条):
#   Bash:       date -u -d "$(date -u -d '+2 hour' +'%Y-%m-%d %H:00:00 UTC')" +%s
#   PowerShell: $now = [DateTimeOffset]::UtcNow.AddHours(2); [DateTimeOffset]::new($now.Year, $now.Month, $now.Day, $now.Hour, 0, 0, [TimeSpan]::Zero).ToUnixTimeSeconds()
# 占位说明: 部署前请先运行上方命令获取最新未来整点时间戳并填入 (未配置则脚本自动推算当前时间+2小时后的整点)
# START_TIMESTAMP=<请使用上方命令计算后填入整点时间戳>
DEPOSIT_BNB=10000000000000000       # 0.01 BNB 质押金
MIN_DEPOSIT=1000000000000000        # 0.001 BNB 下限
MAX_DEPOSIT=10000000000000000000    # 10 BNB 上限
MIN_DEPOSIT_INTERVAL_EPOCHS=24      # 两次调整最小间隔 24 轮
MAX_DEPOSIT_MULTIPLIER=2            # 单次最大调幅 2 倍
```

### 2.2 本地部署演练 (Dry-Run 仿真)

```shell
# 本地沙箱无广播模拟演练 (验证全流程 gas、Safe 接口校验与逻辑)
forge script script/Deploy.s.sol
```

- **Gas 实测值与差异说明**:
  - 本地无广播演练测得总 Gas 消耗为 **7,420,265**（实测值，包含核心合约部署、权限原子交接与链上前置/后置完整校验）；
  - 各合约独立部署 Gas 实测消耗（来自 `forge test --gas-report`）：
    - `TimelockController`: 1,605,592 (实测)
    - `Token`: 767,965 (实测)
    - `StakeVault`: 1,005,383 (实测)
    - `MinerV1` 实现合约: 1,791,275 (实测)
    - `TransparentUpgradeableProxy`: 949,736 (实测)
  - 历史上不同演练阶段出现的微弱 Gas 差异（如 6.68M vs 7.41M）**原因未验证**，未实测项（如真实测试网多笔交易打包开销与单笔工厂部署开销）均保持**估算**。

### 2.3 部署前严格核验清单 (Pre-Deployment Checklist)

在发起实际部署交易前，必须对以下 14 个硬性检查点逐一确认：

- [ ] **1. START 时间戳严格一致**: `StakeVault` 与 `MinerV1` 的 `START` 必须传入完全相同的未来时间戳，确保质押与挖矿轮次起点一致。
- [ ] **2. DEPOSIT 质押金参数有效**: 默认 0.01 BNB，且必须满足 `MIN_DEPOSIT <= DEPOSIT <= MAX_DEPOSIT`。
- [ ] **3. 难度带与软信号设定**: 难度带 34–36 位, S_STAR 为软信号; 主要防线是保证金、CAP 与 20% 单地址上限。
- [ ] **4. ADDR_CAP_BPS 限额设定**: 默认 2000 (20%)，处于 [500, 5000] 安全区间内。
- [ ] **5. 时间锁时延人工核对提醒**: 部署前须人工核对 `TIMELOCK_MIN_DELAY`（测试网默认 600 秒，主网默认 172800 秒）与 `docs/DISCLOSURE.md` 声明值一致。脚本通过环境变量 `DISCLOSURE_TIMELOCK_DELAY` 作为操作员二次确认输入（不一致直接报错）。
- [ ] **6. Safe 合约存在性与接口校验**: 在链上核验 `TIMELOCK_PROPOSER_SAFE` 与 `GUARDIAN_SAFE` 均具有字节码 (`code.length > 0`)，且必须正确实现 `ISafe` 接口（`getOwners()` 与 `getThreshold()`），若接口不存在则直接报错拒绝。
- [ ] **7. Safe 签名人数量与门槛要求**:
  - Proposer Safe 与 Guardian Safe 的 `threshold` 均必须 $\ge 2$；
  - Proposer Safe 与 Guardian Safe 的 `owners` 签名人数量均必须 $\ge 3$。
- [ ] **8. 签名人集合物理隔离与非部署者断言**:
  - `TIMELOCK_PROPOSER_SAFE` 与 `GUARDIAN_SAFE` 的 `owners` 集合交集必须为空（零重叠）；
  - 两个 Safe 的所有 `owners` 中均绝对不能包含 `deployer` EOA 地址；
  - 部署完成后部署者不再保留管理员角色。
- [ ] **9. Token.setMiner 一次性绑定**: 验证 `Token.miner()` 已指向 `MinerV1` 透明代理合约地址，且该函数已被永久关闭。
- [ ] **10. 初始暂停状态核实**: 验证 `MinerV1.paused() == false` 且 `StakeVault.paused() == false`，系统初始状态正常开放。
- [ ] **11. Keeper 专属地址与 Gas 储备核验**:
  - 拥有完全独立的 Keeper EOA 地址，专门用于轮次自动推进（`advanceEpoch`）等常态化运行；
  - 该 Keeper 地址已在 BSC 测试网水龙头（Faucet）领取少量测试币（tBNB）作为执行 Gas 储备；
  - 严禁让部署者账户兼任 Keeper，实现运维职责与权限分离。
- [ ] **12. 密钥物理隔离与环境变量统一 (`PRIVATE_KEY` / `KEEPER_PRIVATE_KEY`)**:
  - 部署私钥统一且严格使用 `PRIVATE_KEY` 环境变量（禁止使用 `DEPLOYER_PRIVATE_KEY`，与 `Deploy.s.sol` 脚本实际读取名称完全统一）；
  - Keeper 私钥统一配置在 `KEEPER_PRIVATE_KEY` 环境变量中；
  - 部署私钥与 Keeper 私钥必须为两个完全不同的独立 EOA，且私钥只保存在本地 `.env` 文件中，绝对禁止共用同一私钥，严禁提交到 Git。
- [ ] **13. Git 安全提交流程与密钥审查**:
  - 部署前必须先执行 `git status` 审查工作区所有待提交与未跟踪文件，绝对确认没有任何 `.env`、密钥文件（如 `*.key`、`*.pem`、`*.p12`）或敏感临时凭证；
  - 严禁盲目执行 `git add .`，必须显式指定文件/目录添加暂存（如 `git add src/ script/ test/ foundry.toml ...`）；
  - 提交并固定当前 Git Commit Hash (`git rev-parse HEAD`) 作为部署验证基线。
- [ ] **14. 依赖版本确切锁定 (`git submodule status`)**:
  - 验证能否完全复现取决于 OpenZeppelin 等核心依赖的确切版本；
  - 执行 `git submodule status` 获取确切 Commit SHA 与版本 Tag，并将其完整记录保存至部署档案中。

### 2.4 部署后链上验收清单 (Post-Deployment Acceptance Checklist)

实际广播部署到测试网后，须使用以下命令进行逐项验收核对（所有命令中的 RPC 一律使用 `$RPC_URL`）：

#### 1. 合约源码验证与 API V2 规范说明 (待测试网实测)

> ⚠️ **核心安全与审计说明：源码已验证 ≠ 已审计 (Source Code Verified ≠ Audited)**。区块链浏览器上的“源码验证”（Standard-Json 绿标）仅代表链上部署的字节码与公布的 Solidity 源代码编译匹配，绝对不代表代码逻辑不存在安全漏洞、逻辑缺陷或经济模型风险，不可替代专业安全机构的独立深度审计。

> **API 变动、端点实测与首选路径决策说明 (待测试网实测)**:
> 1. BscScan 旧 API (`https://api-testnet.bscscan.com/api`) 已停用，Etherscan V1 API 已于 2025-08-15 正式下线；现统一采用 **Etherscan API V2**，端点为 `https://api.etherscan.io/v2/api?chainid=97`；
> 2. **实测最小请求结果与首选路径决策**：
>    - 最小探测命令：`curl -s "https://api.etherscan.io/v2/api?chainid=97&module=contract&action=verifycontract"`
>    - 实测响应：`{"status":"0","message":"NOTOK","result":"Free API access is not supported for this chain. Please upgrade your api plan for full chain coverage. https://etherscan.io/apis"}`
>    - **决策**：实测表明 Etherscan V2 对 BSC 测试网 (`chainid=97`) 不开放免费 API 访问。**因此，将【浏览器手动上传标准 JSON (Standard-Json-Input)】作为验证的“首选路径(待测试网实测)”（零费用，待实测）**；`forge verify-contract` 命令行自动化验证作为“持付费 API 时的可选路径”；
> 3. **API 密钥安全要求**：API 密钥 (`ETHERSCAN_API_KEY`) **仅保存在本机 `.env` 中，绝不提交至 Git，绝不进入 CI 工作流**；
> 4. 所有验证端点与状态均标注为“待测试网实测”。

##### 1.0 部署前记录与基线锁定 (Pre-Deployment Baseline Records)
在执行任何广播部署与合约验证前，必须严格完成以下前置记录与基线固化，并核实工具链配置：

1. **工作区安全审查与 Git 提交基线锁定 (Git Commit Hash)**:
   - **审查待提交清单（严禁盲目 `git add .`）**:
     - 虽然 `.gitignore` 已配置覆盖 `.env` 与 `broadcast/`，但 `git add .` 存在极大误操作风险（易因未识别后缀或新临时文件造成凭据泄漏）；
     - 必须先执行 `git status` 逐项查看待提交文件清单，确认没有任何密钥、私钥文件（`*.key`、`*.pem`、`*.p12`）、`.env` 或本地临时日志；
     - 严禁使用 `git add .`，必须采用显式路径方式添加代码与配置变更：
       ```shell
       git status
       # 显式添加代码、脚本、配置与测试，禁止盲目 git add .
       git add src/ script/ test/ docs/ foundry.toml foundry.lock .env.example README.md
       git commit -m "chore: lock baseline for stage 5 testnet deployment"
       ```
   - **记录 Git 提交哈希**:
     - 部署前必须固定代码基线，执行以下命令记录当前工程的 Git 提交哈希：
       ```shell
       git rev-parse HEAD
       ```
2. **记录与锁定依赖版本 (`git submodule status`)**:
   - 智能合约字节码能否在区块链浏览器上成功复现验证，完全取决于 OpenZeppelin 等核心依赖的确切版本与 Commit SHA；
   - 必须执行 `git submodule status`，并将其实际输出妥善保存记录至部署档案中：
     ```shell
     git submodule status
     # 实测依赖版本锁定基线输出：
     # +f3dae6e6ee381f25eb6a246f7da9b85c91a68219 lib/forge-std (v1.17.0)
     # +dbb6104ce834628e473d2173bbc9d47f81a9eec3 lib/openzeppelin-contracts (v5.0.0-12-gdbb6104c / tag: v5.0.2)
     # +723f8cab09cdae1aca9ec9cc1cfa040c2d4b06c1 lib/openzeppelin-contracts-upgradeable (v5.0.2)
     ```
3. **记录 Foundry 工具链版本 (`forge --version`)**:
   - 运行 `foundryup` 确保本地 Foundry CLI 为最新发布版本，规避已知 API V2 请求构造兼容性缺陷；
   - 记录当前本地实测工具链版本：
     ```shell
     forge --version
     # 实测环境版本记录：
     # forge Version: 1.9.0
     # Commit SHA: 6056c5845d34205a83cf7108042adb137325c9df
     # Build Timestamp: 2026-11-02T20:11:29.162616900Z (1793650289)
     # Build Profile: dist
     ```
4. **记录 `foundry.toml` 编译器设置 (Compiler Settings)**:
   - `solc = "0.8.24"` (对应区块链浏览器编译器版本 `v0.8.24+commit.e11b9ed9`)
   - `optimizer = true`
   - `optimizer_runs = 200`
   - `evm_version = "cancun"` (Cancun EVM 版本)
   - `via_ir = false`
   - `bytecode_hash = "ipfs"`
5. **⚠️ 核心铁律：验证前不得修改源码/配置**:
   - **严重警告**：自发起广播部署交易起，至区块链浏览器上所有合约源码验证通过并取得绿标止，**绝对禁止对任何 Solidity 源码文件、注释内容、空格换行、引用库或 `foundry.toml` 编译配置进行任何修改**！
   - **底层机理**：Solidity 编译器会将全部源代码 AST、代码注释与编译配置参数哈希化为 CBOR Metadata 并附着于部署字节码末尾。即便仅变动一个字符或增减一行空注释，都会导致本地重新编译出的字节码 Metadata Hash 发生漂移，进而导致区块链浏览器对比标准 JSON 输入时报字节码哈希不匹配错误而验证失败！
6. **用最小请求实测 Etherscan V2 支持情况**:
   - 发起前置最小探测调用：
     ```shell
     curl -s "https://api.etherscan.io/v2/api?chainid=97&module=contract&action=verifycontract"
     ```
   - **实测结果**：返回 `Free API access is not supported for this chain. Please upgrade your api plan for full chain coverage.`；
   - **路径确认**：因免费 API 不支持 chainid=97，**将手动标准 JSON 作为首选路径(待测试网实测)**，避免因自动化验证报错阻断验收流程；`forge verify-contract` 命令行作为**持付费 API 时的可选路径**。

##### 1.1 推荐验证流程与机制说明
- **推荐流程**：**优先走浏览器手动标准 JSON 首选路径(待测试网实测)；若持有付费 API 计划，也可选走命令行自动化验证（先执行 `forge script --broadcast` 部署，再逐个执行 `forge verify-contract` 携带 `--compiler-version 0.8.24` 与 `--watch`，构造参数直接从 `broadcast/` 运行记录中提取），若命令行验证失败则无缝切回手动标准 JSON 首选路径**。
- **为何不推荐在 broadcast 时自动携带 `--verify`**：
  - 在部署脚本命令中绑定 `--verify` 会导致广播与批量验证强耦合；若测试网节点确认延迟或 API 限流，批量验证中断难以恢复；
  - 推荐解耦：先完成链上广播落块，再使用带 `--watch` 的独立命令逐个合约轮询验证状态，清晰可控；
- **构造参数提取方式**：
  - 广播执行完成后，Foundry 会在 `broadcast/Deploy.s.sol/97/run-latest.json` 中保存每笔部署交易的完整凭证；
  - 直接在 `run-latest.json` 中找到对应合约部署交易的 `arguments`（已由 Foundry 生成的 ABI 编码十六进制串），无需人工重新拼接计算。

##### 1.2 手写构造参数与源码逐一核对表
若需手动指定构造参数，参数顺序必须与源码构造函数严格一致（Timelock 构造参数使用 `$TIMELOCK_MIN_DELAY`）：

| 合约名称 | 源码构造函数参数签名 (Source Constructor) | 部署脚本传参顺序 (Deploy Call) | 实测构造参数 ABI 编码命令 (`cast abi-encode`) |
| :--- | :--- | :--- | :--- |
| **TimelockController** | `constructor(uint256 minDelay, address[] memory proposers, address[] memory executors, address admin)` | `(minDelay, proposers, executors, admin)` | `cast abi-encode "constructor(uint256,address[],address[],address)" $TIMELOCK_MIN_DELAY "[$TIMELOCK_PROPOSER_SAFE]" "[0x0000000000000000000000000000000000000000]" $DEPLOYER` |
| **Token** | `constructor(address liquidityWallet, address teamVestingWallet, address communityMultisig)` | `(liquidityWallet, teamVestingWallet, communityMultisig)` | `cast abi-encode "constructor(address,address,address)" $LIQUIDITY_WALLET $TEAM_VESTING_WALLET $COMMUNITY_MULTISIG` |
| **StakeVault** | `constructor(uint256 d, uint256 start, address _admin, address _guardian, uint256 _minDeposit, uint256 _maxDeposit, uint256 _minDepositIntervalEpochs, uint256 _maxDepositMultiplier)` | `(depositBnb, start, deployer, guardianSafe, minDeposit, maxDeposit, minDepositIntervalEpochs, maxDepositMultiplier)` | `cast abi-encode "constructor(uint256,uint256,address,address,uint256,uint256,uint256,uint256)" $DEPOSIT_BNB $START_TIMESTAMP $DEPLOYER $GUARDIAN_SAFE $MIN_DEPOSIT $MAX_DEPOSIT $MIN_DEPOSIT_INTERVAL_EPOCHS $MAX_DEPOSIT_MULTIPLIER` |
| **MinerV1 (实现)** | `constructor() { _disableInitializers(); }` (无参数) | 无参数 | *(无构造参数，直接验证)* |
| **TransparentUpgradeableProxy** | `constructor(address _logic, address initialOwner, bytes memory _data)` | `(minerImpl, deployer, initData)` | `cast abi-encode "constructor(address,address,bytes)" $MINER_IMPL_ADDRESS $DEPLOYER $(cast calldata "initialize(address,address,address,address,uint256)" $TOKEN_ADDRESS $VAULT_ADDRESS $DEPLOYER $GUARDIAN_SAFE $START_TIMESTAMP)` |

##### 1.3 命令行自动化验证 (持付费 API 时的可选路径，待测试网实测)
> 注：所有命令均携带 `--compiler-version 0.8.24` 与 `--watch`。构造参数可直接使用下方的 `cast abi-encode`，也可直接从 `broadcast/Deploy.s.sol/97/run-latest.json` 中提取。API 密钥仅在本机 `.env`，不进 CI。若 API 返回 `Free API access is not supported` 或验证失败，立即切换至下方 1.4 手动标准 JSON 首选路径。

```shell
# 1. 验证 TimelockController (时延参数使用 $TIMELOCK_MIN_DELAY)
forge verify-contract $TIMELOCK_ADDRESS \
  lib/openzeppelin-contracts/contracts/governance/TimelockController.sol:TimelockController \
  --chain 97 \
  --compiler-version 0.8.24 \
  --watch \
  --constructor-args $(cast abi-encode "constructor(uint256,address[],address[],address)" $TIMELOCK_MIN_DELAY "[$TIMELOCK_PROPOSER_SAFE]" "[0x0000000000000000000000000000000000000000]" $DEPLOYER) \
  --verifier etherscan \
  --verifier-url "https://api.etherscan.io/v2/api?chainid=97" \
  --etherscan-api-key $ETHERSCAN_API_KEY

# 2. 验证 Token
forge verify-contract $TOKEN_ADDRESS \
  src/Token.sol:Token \
  --chain 97 \
  --compiler-version 0.8.24 \
  --watch \
  --constructor-args $(cast abi-encode "constructor(address,address,address)" $LIQUIDITY_WALLET $TEAM_VESTING_WALLET $COMMUNITY_MULTISIG) \
  --verifier etherscan \
  --verifier-url "https://api.etherscan.io/v2/api?chainid=97" \
  --etherscan-api-key $ETHERSCAN_API_KEY

# 3. 验证 StakeVault (8 个参数，严格对应源码)
forge verify-contract $VAULT_ADDRESS \
  src/StakeVault.sol:StakeVault \
  --chain 97 \
  --compiler-version 0.8.24 \
  --watch \
  --constructor-args $(cast abi-encode "constructor(uint256,uint256,address,address,uint256,uint256,uint256,uint256)" $DEPOSIT_BNB $START_TIMESTAMP $DEPLOYER $GUARDIAN_SAFE $MIN_DEPOSIT $MAX_DEPOSIT $MIN_DEPOSIT_INTERVAL_EPOCHS $MAX_DEPOSIT_MULTIPLIER) \
  --verifier etherscan \
  --verifier-url "https://api.etherscan.io/v2/api?chainid=97" \
  --etherscan-api-key $ETHERSCAN_API_KEY

# 4. 验证 MinerV1 实现合约 (无构造参数)
forge verify-contract $MINER_IMPL_ADDRESS \
  src/MinerV1.sol:MinerV1 \
  --chain 97 \
  --compiler-version 0.8.24 \
  --watch \
  --verifier etherscan \
  --verifier-url "https://api.etherscan.io/v2/api?chainid=97" \
  --etherscan-api-key $ETHERSCAN_API_KEY

# 5. 验证 TransparentUpgradeableProxy 代理合约
forge verify-contract $MINER_PROXY_ADDRESS \
  lib/openzeppelin-contracts/contracts/proxy/transparent/TransparentUpgradeableProxy.sol:TransparentUpgradeableProxy \
  --chain 97 \
  --compiler-version 0.8.24 \
  --watch \
  --constructor-args $(cast abi-encode "constructor(address,address,bytes)" $MINER_IMPL_ADDRESS $DEPLOYER $(cast calldata "initialize(address,address,address,address,uint256)" $TOKEN_ADDRESS $VAULT_ADDRESS $DEPLOYER $GUARDIAN_SAFE $START_TIMESTAMP)) \
  --verifier etherscan \
  --verifier-url "https://api.etherscan.io/v2/api?chainid=97" \
  --etherscan-api-key $ETHERSCAN_API_KEY
```

##### 1.4 手动标准 JSON 验证 SOP (首选路径，待测试网实测)
鉴于实测 Etherscan V2 对 BSC 测试网免费 API 权限限制，浏览器手动标准 JSON 验证为**首选路径(待测试网实测)（零费用，待实测）**。在执行以下导出前，请确保已遵守“**验证前不得修改源码/配置**”铁律。

###### 1.4.1 各合约标准 JSON 导出与浏览器填写项 SOP

1. **Token 合约 (标准不可升级 ERC20)**:
   - **导出 Standard-JSON 命令**:
     ```shell
     forge verify-contract $TOKEN_ADDRESS src/Token.sol:Token --show-standard-json-input > Token-standard-json.json
     ```
   - **浏览器录入与操作项**:
     - 访问页面：`https://testnet.bscscan.com/address/$TOKEN_ADDRESS#code`，点击 **"Verify and Publish"**；
     - **Compiler Type**: 选择 `Solidity (Standard-Json-Input)`；
     - **Compiler Version**: 选择 `v0.8.24+commit.e11b9ed9` (待实测)；
     - **Open Source License Type**: 选择 `MIT License (MIT)`；
     - 点击 **Continue** 进入详情上传页；
     - **Upload standard JSON file**: 上传本地生成的 `Token-standard-json.json`；
     - **Constructor Arguments ABI-Encoded**: 粘贴对应编码参数（从 `broadcast/Deploy.s.sol/97/run-latest.json` 中复制对应交易的 `arguments` 字段，或执行 `cast abi-encode "constructor(address,address,address)" $LIQUIDITY_WALLET $TEAM_VESTING_WALLET $COMMUNITY_MULTISIG` 获取）(待实测)；
     - 点击 **"Verify and Publish"** 提交完成验证。

2. **StakeVault 合约 (不可升级质押金金库)**:
   - **导出 Standard-JSON 命令**:
     ```shell
     forge verify-contract $VAULT_ADDRESS src/StakeVault.sol:StakeVault --show-standard-json-input > StakeVault-standard-json.json
     ```
   - **浏览器录入与操作项**:
     - 访问页面：`https://testnet.bscscan.com/address/$VAULT_ADDRESS#code`，点击 **"Verify and Publish"**；
     - **Compiler Type**: 选择 `Solidity (Standard-Json-Input)`；
     - **Compiler Version**: 选择 `v0.8.24+commit.e11b9ed9` (待实测)；
     - **Open Source License Type**: 选择 `MIT License (MIT)`；
     - 点击 **Continue** 进入详情上传页；
     - **Upload standard JSON file**: 上传本地生成的 `StakeVault-standard-json.json`；
     - **Constructor Arguments ABI-Encoded**: 粘贴对应编码参数（从 `broadcast/Deploy.s.sol/97/run-latest.json` 中复制对应交易的 `arguments` 字段，或执行 `cast abi-encode "constructor(uint256,uint256,address,address,uint256,uint256,uint256,uint256)" $DEPOSIT_BNB $START_TIMESTAMP $DEPLOYER $GUARDIAN_SAFE $MIN_DEPOSIT $MAX_DEPOSIT $MIN_DEPOSIT_INTERVAL_EPOCHS $MAX_DEPOSIT_MULTIPLIER` 获取）(待实测)；
     - 点击 **"Verify and Publish"** 提交完成验证。

3. **MinerV1 实现合约 (Logic Implementation)**:
   - **导出 Standard-JSON 命令**:
     ```shell
     forge verify-contract $MINER_IMPL_ADDRESS src/MinerV1.sol:MinerV1 --show-standard-json-input > MinerV1-standard-json.json
     ```
   - **浏览器录入与操作项**:
     - 访问页面：`https://testnet.bscscan.com/address/$MINER_IMPL_ADDRESS#code`，点击 **"Verify and Publish"**；
     - **Compiler Type**: 选择 `Solidity (Standard-Json-Input)`；
     - **Compiler Version**: 选择 `v0.8.24+commit.e11b9ed9` (待实测)；
     - **Open Source License Type**: 选择 `MIT License (MIT)`；
     - 点击 **Continue** 进入详情上传页；
     - **Upload standard JSON file**: 上传本地生成的 `MinerV1-standard-json.json`；
     - **Constructor Arguments ABI-Encoded**: **保持空白（无构造参数）** (待实测)；
     - 点击 **"Verify and Publish"** 提交完成验证。

4. **TimelockController 合约 (治理时间锁)**:
   - **导出 Standard-JSON 命令**:
     ```shell
     forge verify-contract $TIMELOCK_ADDRESS lib/openzeppelin-contracts/contracts/governance/TimelockController.sol:TimelockController --show-standard-json-input > TimelockController-standard-json.json
     ```
   - **浏览器录入与操作项**:
     - 访问页面：`https://testnet.bscscan.com/address/$TIMELOCK_ADDRESS#code`，点击 **"Verify and Publish"**；
     - **Compiler Type**: 选择 `Solidity (Standard-Json-Input)`；
     - **Compiler Version**: 选择 `v0.8.24+commit.e11b9ed9` (待实测)；
     - **Open Source License Type**: 选择 `MIT License (MIT)`；
     - 点击 **Continue** 进入详情上传页；
     - **Upload standard JSON file**: 上传本地生成的 `TimelockController-standard-json.json`；
     - **Constructor Arguments ABI-Encoded**: 粘贴对应编码参数（从 `broadcast/Deploy.s.sol/97/run-latest.json` 中复制对应交易的 `arguments` 字段，或执行 `cast abi-encode "constructor(uint256,address[],address[],address)" $TIMELOCK_MIN_DELAY "[$TIMELOCK_PROPOSER_SAFE]" "[0x0000000000000000000000000000000000000000]" $DEPLOYER` 获取）(待实测)；
     - 点击 **"Verify and Publish"** 提交完成验证。

5. **TransparentUpgradeableProxy 代理合约 (Miner Proxy)**:
   - **导出 Standard-JSON 命令**:
     ```shell
     forge verify-contract $MINER_PROXY_ADDRESS lib/openzeppelin-contracts/contracts/proxy/transparent/TransparentUpgradeableProxy.sol:TransparentUpgradeableProxy --show-standard-json-input > MinerProxy-standard-json.json
     ```
   - **浏览器录入与操作项**:
     - 访问页面：`https://testnet.bscscan.com/address/$MINER_PROXY_ADDRESS#code`，点击 **"Verify and Publish"**；
     - **Compiler Type**: 选择 `Solidity (Standard-Json-Input)`；
     - **Compiler Version**: 选择 `v0.8.24+commit.e11b9ed9` (待实测)；
     - **Open Source License Type**: 选择 `MIT License (MIT)`；
     - 点击 **Continue** 进入详情上传页；
     - **Upload standard JSON file**: 上传本地生成的 `MinerProxy-standard-json.json`；
     - **Constructor Arguments ABI-Encoded**: 粘贴对应编码参数（从 `broadcast/Deploy.s.sol/97/run-latest.json` 中复制对应交易的 `arguments` 字段，或执行 `cast abi-encode "constructor(address,address,bytes)" $MINER_IMPL_ADDRESS $DEPLOYER $(cast calldata "initialize(address,address,address,address,uint256)" $TOKEN_ADDRESS $VAULT_ADDRESS $DEPLOYER $GUARDIAN_SAFE $START_TIMESTAMP)` 获取）(待实测)；
     - 点击 **"Verify and Publish"** 提交完成验证。

6. **ProxyAdmin 合约 (透明代理专属管理合约，可选)**:
   - *说明*: 由 `TransparentUpgradeableProxy` 部署时在构造函数中自动实例化（所有权已移交至 Timelock）。若需要在浏览器独立认证 ProxyAdmin，执行：
     ```shell
     forge verify-contract $PROXY_ADMIN lib/openzeppelin-contracts/contracts/proxy/transparent/ProxyAdmin.sol:ProxyAdmin --show-standard-json-input > ProxyAdmin-standard-json.json
     ```
   - 构造参数为 `$(cast abi-encode "constructor(address)" $DEPLOYER)` (待实测)。

###### 1.4.2 代理合约与实现合约的关联步骤 (Proxy Linking SOP, 实测后补全, 未实测的标注"待实测")

- **前置条件**: `MinerV1` 实现合约 (`$MINER_IMPL_ADDRESS`) 与 `TransparentUpgradeableProxy` 代理合约 (`$MINER_PROXY_ADDRESS`) 均已在 BscScan 上完成上述 Standard JSON 验证并获得绿标；
- **关联步骤 1 (打开代理合约页面)**:
  - 在浏览器中打开代理合约页面：`https://testnet.bscscan.com/address/$MINER_PROXY_ADDRESS#code` (待实测)；
- **关联步骤 2 (发起代理识别与验证)**:
  - 点击 **"Contract"** 选项卡；
  - 在代码视图右侧工具条中点击 **"More Options"** 下拉菜单，选择 **"Is this a proxy?"**（部分界面显示为 **"Verify as Proxy"**）(待实测)；
- **关联步骤 3 (点击确认检测)**:
  - 弹出 "Proxy Contract Verification" 确认弹窗，点击 **"Verify"**（或 **"Save"**）按钮 (待实测)；
- **关联步骤 4 (自动插槽解析与关联生效)**:
  - BscScan 引擎会自动读取代理合约 ERC-1967 实现插槽（`0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc`），检测其指向的 `$MINER_IMPL_ADDRESS`。因实现合约已预先完成验证，BscScan 会即刻判定关联成功并回显 "The proxy contract has been updated." (待实测)；
- **关联步骤 5 (交互界面生成与权限核验)**:
  - 关联成功后刷新页面，Contract 标签下将自动新增 **"Read as Proxy"** 与 **"Write as Proxy"** 选项卡；
  - 用户与前端可在该页面直接读取和调用 `MinerV1` 的所有业务接口（如 `currentEpoch()`、`totalShares()`、`claim()` 等）(待实测)；
- **实测标记说明**:
  - 本 SOP 关联流程基于 Etherscan/BscScan 官方标准代理识别规范拟定；待测试网实际广播部署后在链上实测跑通并补全细节截图；未实测部分统一标注为“(待实测)”。

#### 2. 代币总量与预铸分配核验
```shell
# 2.1 检查 Token totalSupply() == 100,000,000e18 (1 亿枚初始预铸)
cast call $TOKEN_ADDRESS "totalSupply()(uint256)" --rpc-url $RPC_URL
# 预期输出: 100000000000000000000000000

# 2.2 检查三个预铸地址余额
# 流动性钱包 (50,000,000 TTK, 5%)
cast call $TOKEN_ADDRESS "balanceOf(address)(uint256)" $LIQUIDITY_WALLET --rpc-url $RPC_URL
# 预期输出: 50000000000000000000000000

# 团队归属占位钱包 (30,000,000 TTK, 3%)
cast call $TOKEN_ADDRESS "balanceOf(address)(uint256)" $TEAM_VESTING_WALLET --rpc-url $RPC_URL
# 预期输出: 30000000000000000000000000

# 社区多签钱包 (20,000,000 TTK, 2%)
cast call $TOKEN_ADDRESS "balanceOf(address)(uint256)" $COMMUNITY_MULTISIG --rpc-url $RPC_URL
# 预期输出: 20000000000000000000000000
```

#### 3. Token 铸币权限唯一绑定核验
```shell
# Token.miner() 必须非零且已精确绑定至 MinerV1 代理合约 (Token 合约中不存在 minerSet()，统一读取 miner() != address(0))
cast call $TOKEN_ADDRESS "miner()(address)" --rpc-url $RPC_URL
# 预期输出: $MINER_PROXY_ADDRESS
```

#### 4. ProxyAdmin 与代理权限核验
```shell
# 4.1 用 cast admin 从 ERC-1967 admin 存储插槽读取 ProxyAdmin 地址
PROXY_ADMIN=$(cast admin $MINER_PROXY_ADDRESS --rpc-url $RPC_URL)
echo "ProxyAdmin: $PROXY_ADMIN"

# 4.2 校验 ProxyAdmin 治理所有权已转移至 TimelockController
cast call $PROXY_ADMIN "owner()(address)" --rpc-url $RPC_URL
# 预期输出: $TIMELOCK_ADDRESS
```

#### 5. 时间锁与多签治理权限核验
```shell
# 5.1 检查 getMinDelay() == 600 (测试网 10 分钟排期时延)
cast call $TIMELOCK_ADDRESS "getMinDelay()(uint256)" --rpc-url $RPC_URL
# 预期输出: 600

# 5.2 PROPOSER_ROLE "仅有" 校验 (读取 RoleGranted/RoleRevoked 事件日志核对唯一持有人)
# PROPOSER_ROLE 哈希: 0xb09aa5aeb3702cfd50b6b62bc4532604938f21248a27a1d5ca736082b6819cc1
cast logs --address $TIMELOCK_ADDRESS --from-block $DEPLOY_BLOCK \
  "RoleGranted(bytes32,address,address)" --rpc-url $RPC_URL
cast logs --address $TIMELOCK_ADDRESS --from-block $DEPLOY_BLOCK \
  "RoleRevoked(bytes32,address,address)" --rpc-url $RPC_URL
# 净授权账户必须严格仅有 TIMELOCK_PROPOSER_SAFE

# 5.3 EXECUTOR_ROLE 开放核对 (address(0) 具有执行权限)
cast call $TIMELOCK_ADDRESS "hasRole(bytes32,address)(bool)" \
  0xd8aa0f3194971a2a116679f7c2090f6939c8d4e01a2a8d7e41d55e5351469e63 0x0000000000000000000000000000000000000000 \
  --rpc-url $RPC_URL
# 预期输出: true

# 5.4 部署者已放弃 Timelock 管理员角色核对
cast call $TIMELOCK_ADDRESS "hasRole(bytes32,address)(bool)" \
  0x0000000000000000000000000000000000000000000000000000000000000000 $DEPLOYER \
  --rpc-url $RPC_URL
# 预期输出: false
```

#### 6. 实现合约防重入初始化安全检查
```shell
# 实现合约不能被直接初始化 (预期触发 InvalidInitialization 回滚)
cast call $MINER_IMPL_ADDRESS \
  "initialize(address,address,address,address,uint256)" \
  $TOKEN_ADDRESS $VAULT_ADDRESS $DEPLOYER $GUARDIAN_SAFE $START_TIMESTAMP \
  --rpc-url $RPC_URL
# 预期输出: Revert (InvalidInitialization)
```

---

## 3. 治理说明与主网独立性差距

- **测试网控制现状**: 测试网阶段 Safe 签名人均由项目方团队直接控制，用于流程技术仿真，**不构成真正的独立去中心化治理**。
- **主网要求与差距**: 主网正式上线前必须引入互不隶属的独立第三方签名人；当前测试网阶段尚未满足独立治理条件。

---

## 4. 配套运维与监控工具链 (Keeper & Monitor Tooling)

### 4.1 Keeper 自动化运维机器人 (`script/keeper.py`)

用于每轮自动触发种子初始化并监控轮次同步滞后：

```shell
# 1. 安全演练模式 (只读扫描，只打印将发出的交易，不广播)
python script/keeper.py --dry-run --once

# 2. 守护进程运行模式 (根据 .env 自动读取 KEEPER_PRIVATE_KEY)
python script/keeper.py --interval 15
```

### 4.2 链上公平性与基尼系数监控 (`script/monitor.py`)

用于读取链上事件并计算每轮 Top 10 地址集中度与基尼系数，实时验证“防寡头与去中心化”效果：

```shell
# 1. 运行本地模拟诊断 Demo (50/100/200 地址不同分布场景)
python script/monitor.py --demo

# 2. 连接测试网 RPC 扫描实时事件
python script/monitor.py --from-block 0
```

---

## 5. 编译、测试与安全审查命令

```shell
# 编译
forge build

# 全量测试 (14 个套件，81 个用例)
forge test -vvv

# 静态安全检查 (当前状态: 已运行 Slither,未报告高危项,需独立审计)
slither .

# 查看存储布局
forge inspect MinerV1 storage-layout
```

---

## 6. 免责声明与算力假设说明

- **算力假设**: 所有 GPU 算力速度（如 RTX 4090 @ 20 GH/s）均标记为**假设**；所有 10 核 CPU 算力数据均标记为**外推**（34 位下 [3.38%, 13.75%](均值 5.06%),均为 10 核外推值）。
- **合规声明**: 项目方**不声称与任何交易所存在合作或背书**。公开反馈渠道待定（项目方尚未设立公开渠道）。
