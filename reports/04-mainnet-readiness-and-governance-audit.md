# PoolCoin 主网上线前参数、权限与治理审计报告

## 1. 审计状态总表 (Audit Status Matrix)

依据当前代码仓库 (`d:\project\poolcoin-rc2`)、编译产物、测试用例与链上证据进行严格核验：

| 审计项目 | 分类 | 状态 | 涉及文件 / 合约代码位置 | 核心参数 / 规则说明 | 验收与阻塞分析 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ERC-20 基础与总量硬顶** | 合约架构 | **PASS** | `src/Token.sol` L14-L35 | Premint 1亿，挖矿上限9亿，绝对硬顶 10 亿 | 数学不变量及测试验证通过 |
| **Token 代币元数据** | 合约参数 | **BLOCKED** | `src/Token.sol` L33 | 当前代码写死为 `"TESTTOKEN"`, `"TTK"` | **主网阻塞项**：需确定主网正式名称与符号并重新部署 |
| **团队代币线性归属** | 代币经济 | **BLOCKED** | `src/Token.sol` L40 | 注释标明当前为占位地址，未挂接正式归属合约 | **主网阻塞项**：主网上线前须部署标准化 `VestingWallet` |
| **Miner 铸币唯一性** | 权限控制 | **PASS** | `src/Token.sol` L45-L53 | `setMiner` 仅可调用一次，之后锁定永久不可更改 | 100% 单元测试及不可撤销性验证通过 |
| **MinerV1 代理架构** | 合约架构 | **PASS** | `src/MinerV1.sol`, `TransparentUpgradeableProxy` | 逻辑合约与代理合约隔离，实现合约直接初始化被阻断 | 离线存储槽及升级测试通过 |
| **治理双权分离架构** | 治理体系 | **PASS** | `src/MinerV1.sol`, `src/StakeVault.sol` | Timelock 控制参数升级，Guardian 仅可单向紧急暂停 | 14/14 治理权限测试通过 |
| **时间锁延迟配置** | 治理参数 | **PASS** | `script/DeployAll.s.sol` L46 | 最小执行延迟硬编码为 `172800` 秒 (严格 48 小时) | 阻止突击升级或恶意修改参数 |
| **主网多签 Safe 准备** | 基础设施 | **BLOCKED** | 主网环境部署前置条件 | 主网需要 2 套独立 Gnosis Safe（Proposer / Guardian） | **外部阻塞项**：需各签名人生成主网硬件钱包并建立 Safe |
| **挖矿冷却与份额验证** | 协议逻辑 | **PASS** | `src/MinerV1.sol` L160-L240 | 预热 2 Epoch，难度动态自适应，34-64 bit 校验 | 130/130 智能合约测试全数通过 |
| **质押退出与提款防死锁** | 协议逻辑 | **PASS** | `src/StakeVault.sol` L70-L100 | 48 Epoch 冷却期，`withdraw` 永不可暂停且防重入 | 提款闭环规则清晰，不变量测试通过 |
| **Keeper 瞬时容错加固** | 运维组件 | **PASS** | `script/keeper.py` L1220-L1250 | 只读指数退避重试，广播不确定性熔断防重复发送 | 141+3 项进程与容错测试通过 |
| **交互式 Keystore 签名** | 客户端安全 | **PASS** | `script/cast_signer.py`, `claim.py`, `submit_share.py` | 原生 TTY 交互提示，非交互式环境即刻熔断 | 70/70 项安全签名测试通过 |

---

## 2. 合约架构与治理模型深度核查

### 2.1 治理权限与角色拓扑
```text
                           [Proposer Safe] (>=3-of-5 多签)
                                  │ (提议/排队)
                                  ▼
                        [TimelockController] (48小时延迟)
                       (DEFAULT_ADMIN / ProxyAdmin Owner)
                                  │
         ┌────────────────────────┴────────────────────────┐
         │ (延迟期满执行更新)                               │ (延迟期满执行升级)
         ▼                                                 ▼
   [StakeVault]                                        [MinerV1 Proxy]
   - admin: Timelock                                   - admin: Timelock
   - guardian: Guardian Safe                           - guardian: Guardian Safe
   - 可更新 deposit 参数                                - 可更新 sStar, 难度, 费率
   - 暂停后仅 Timelock 可解挂                            - 暂停后仅 Timelock 可解挂
         ▲                                                 ▲
         │ (即时单向暂停)                                   │ (即时单向暂停)
         └────────────────────────┬────────────────────────┘
                                  │
                        [Guardian Safe] (>=2-of-3 多签)
                         (无升级权、无参数权、不可解挂)
```

### 2.2 核心参数设定与不变性验证
1. **时间周期 (Epoch)**：
   - 持续时间：`3600` 秒（严格 1 小时）；
   - 起始时间戳：必须为整点（`startTimestamp % 3600 == 0`）。
2. **挖矿与排放参数**：
   - 单 Epoch 目标产出基准：`100` 份份额（`targetPerEpoch`）；
   - 难度调整范围：`dMin = 34` bit, `dMax = 64` bit；
   - 单地址份额上限：`addrCapBps = 2000`（即单个矿工单 Epoch 份额奖励封顶 20%）；
   - 排放曲线：渐进衰减，初始总量 1 亿，挖矿产出上限 9 亿，硬顶 10 亿代币永不超发。
3. **质押 Vault 参数**：
   - 退出冷却期：`COOLDOWN_EPOCHS = 48`（严格 48 小时）；
   - 提款安全性：`withdraw()` 永不包含 `whenNotPaused` 修饰器，即使 Vault 因紧急事故被 Guardian 暂停，用户的提款权永远不受影响，杜绝资金被项目方扣留锁定。

---

## 3. 主网上线阻断性事项 (Critical Blockers) 与解锁条件

根据“不得将未经验证项虚标为可发布主网”的安全原则，当前存在以下明确的主网上线前阻断项：

### 阻断项 1：Token 代币名称与符号硬编码问题
- **现状**：`src/Token.sol` 构造函数中写死为 `ERC20("TESTTOKEN", "TTK")`。
- **解锁条件**：
  1. 团队确定正式的主网代币名称（如 `PoolCoin`）与符号（如 `PCOIN`）；
  2. 在源码中更新或采用构造函数传参模式；
  3. 执行 `forge test` 确保元数据变更未破坏铸币逻辑。

### 阻断项 2：团队代币锁定归属合约 (VestingWallet)
- **现状**：`src/Token.sol` 第 40 行注释明确标明当前分配给团队的 30,000,000 代币接收地址为临时占位地址（`teamVestingWallet`），缺乏链上时间释放约束。
- **解锁条件**：
  1. 部署 OpenZeppelin 官方标准的 `VestingWallet` 合约（设置如 1 年 cliff/悬崖期，3-4 年线性解锁）；
  2. 将该 VestingWallet 合约地址配置为 Token 构造函数的团队代币接收者；
  3. 验证归属合约受益人由多签控制。

### 阻断项 3：主网生产级多签 Safes 建立
- **现状**：测试网 Safe 合约地址（`0xE5fE521...` 和 `0x8E9D511...`）无法直接充当主网 Safe 合约（主网需独立部署 Gnosis Safe）。但多签所有者的个人硬件钱包地址跨链复用本身并无禁忌。
- **解锁条件**：
  1. 设立 **Proposer Safe**：主网部署，需配置核心成员硬件钱包地址及门限（如建议 $\ge 3/5$ 或团队确定门限）；
  2. 设立 **Guardian Safe**：主网部署，需配置安全值班人员硬件钱包地址及门限（如建议 $\ge 2/3$ 或团队确定门限）；
  3. **门禁与策略说明**：当前部署脚本 `script/Deploy.s.sol` 硬性包含 `_validateSafe` 门禁（要求 Proposer owners 与 Guardian owners 零重叠，且 Deployer 不得在 owners 中，对应单测 `test_PreflightFailsIfOwnersOverlap`）。团队需确认是将“两套多签签名人零重叠”保持为硬性部署门禁，还是调整为团队特定的多签策略。

---

## 4. 主网部署与权限交接 Runbook

在完成上述解锁条件后，依序执行标准化主网启动流程：

### 阶段一：前置环境与多签准备
1. 校验 Proposer Safe 与 Guardian Safe 已在主网部署完成；
2. 校验 deployer 账户具备充足主网原生 Gas 代币（BNB）；
3. 校验主网 RPC 节点的高可用性（主备两个独立 RPC 端点）。

### 阶段二：合约部署与初始化
1. 部署正式 `Token` 合约与 `VestingWallet` 合约；
2. 部署 `StakeVault` 合约；
3. 部署 `MinerV1` 逻辑实现合约；
4. 部署 `TransparentUpgradeableProxy` 并调用 `initialize(...)` 完成初始化；
5. 调用 `Token.setMiner(minerProxyAddress)` 绑定唯一铸币权。

### 阶段三：治理时间锁与权限交接
1. 部署 `TimelockController`，设置 `minDelay = 172800` (48h)，指定 Proposer Safe 为 proposer，指定全网（`address(0)`）为 executor；
2. 将 `ProxyAdmin` 的所有权转移给 `TimelockController`；
3. 将 `StakeVault.admin` 转移给 `TimelockController`，`guardian` 设置为 Guardian Safe；
4. 将 `MinerV1.admin` 转移给 `TimelockController`，`guardian` 设置为 Guardian Safe；
5. Deployer 执行 `renounceRole`，完全放弃 Timelock 的管理权限。

### 阶段四：链上全项审计验证 (Preflight Verification)
运行部署后只读校验脚本，断言：
- Deployer 零剩余特权；
- 所有代理指向与所有权指向 100% 归属于 Timelock；
- Guardian 紧急暂停链路单向有效；
- 排放与难度参数与主网设计指标完全一致。
