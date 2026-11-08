# PoolCoin 测试网 MinerV1 升级演练准备材料

> **重要安全提示**：本材料仅用于离线升级演练与治理数据核验，严禁直接对生产或公共测试网发送未经多签委员会核准的真实交易。演练全过程遵循无状态只读原则。

---

## 1. 演练网络环境与关键核心地址

- **网络名称**：BSC Testnet (ChainId: `97`)
- **Gas 费率基准**：最低允许 0.1 Gwei，演练建议使用 1.0 Gwei 确保快速落块
- **当前活跃轮次**：持续推进中（起始轮次 0，种子已初始化轮次 0~12，13 至执行时当前活跃轮次 `epoch()` 待回填补齐）
- **核心合约与治理实体**：
  | 实体名称 | 角色类型 | 合约地址 | 权限与说明 |
  | :--- | :--- | :--- | :--- |
  | **Miner (Proxy)** | 业务代理入口 | `0x420382a0f874c1F120621f847d81581e8737504e` | TransparentUpgradeableProxy，对外业务统一入口 |
  | **ProxyAdmin** | 代理升级管理员 | `0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185` | 代理管理员合约，所有权已移交 TimelockController |
  | **TimelockController** | 治理时间锁 | `0x75F344F2c5a79d945D873Edbdccf67e61e56C9de` | 延时 600 秒 (`minDelay = 600s`)，系统最高权限控制者 |
  | **Proposer Safe** | 提案与取消多签 | `0xE5fE521AfD41f1aF00bB623CE5511a8412a68b47` | 持有 Timelock `PROPOSER_ROLE` 与 `CANCELLER_ROLE` |
  | **Guardian Safe** | 紧急守护多签 | `0x8E9D51118C70906dE13E2FED7787B056b9268358` | 持有业务暂停权，**默认无 Timelock 角色** |
  | **Token (POW)** | 挖矿结算代币 | `0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29` | ERC20 挖矿代币，铸币权限仅限 Miner 代理 |
  | **StakeVault** | 矿工质押金库 | `0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7` | 矿工 0.01 BNB 质押与 48h 退出时间锁 |

---

## 2. 步骤 1：部署新 MinerV1 实现合约 (`forge create`)

在本地编译通过（`solc = 0.8.38`, `evm_version = "cancun"`, `optimizer_runs = 200`）后，通过以下命令部署新的实现逻辑合约：

- **版本对照标签**：`mainnet-rc1`
- **MinerV1 部署字节码哈希 (`keccak256(forge inspect MinerV1 bytecode)`)**：
  `0x6ad3685b67013d79515b6df928cf96cf99d2654e58ab9e23f79991fb28a7f15c`
  *(旧记录值 `0x984ee72eb6fc6e7d72200ba36fa87fd179314151207c5d574e8305e4a22e1ee0` 已作废，原因是旧构建依赖本机嵌套子模块状态；当前为固定 5 条 remappings 且配置 `auto_detect_remappings = false` 后的确定性记录值)*
- **MinerV1 运行时字节码哈希 (`keccak256(forge inspect MinerV1 deployedBytecode)`)**：
  `0x7badee20c11424e9bfd9b1b7ef45ddf1642bc3174d782f07df6435becc7c649c`
  *(旧记录值 `0xc6a177f9165d920b784f0b895ab9b9e8850a874c4ab55fec509376b7c3b9a1c9` 已同步作废，原因同样为旧构建依赖本机嵌套子模块状态)*
- **校验依据**：演练部署前应比对本地编译字节码 keccak256（包含部署字节码与运行时字节码）必须与上述固定 5 条 remappings 后的新记录哈希 100% 吻合，确保链上落合约与经过审计测试的代码完全一致。

```bash
# 环境变量配置
RPC_URL="https://data-seed-prebsc-1-s1.binance.org:8545/"
# 部署 MinerV1 实现 (无构造参数，执行 _disableInitializers；使用安全 Keystore 账户)
forge create src/MinerV1.sol:MinerV1   --rpc-url "$RPC_URL"   --account <keystore 名称>   --legacy   --gas-price 1000000000
```

> **参数说明**：
> 1. `--account <keystore 名称>`：使用 Foundry 加密 Keystore 账户签名交互，严禁在命令行或环境变量中使用 `--private-key`，杜绝私钥明文泄漏风险；
> 2. `--legacy`：强制采用 Legacy 交易类型（Type 0），规避 BSC 节点客户端对 EIP-1559 Type 2 交易在 Gas Price 校验上的偶发不兼容问题；
> 3. `--gas-price 1000000000`（1.0 Gwei）：BSC 测试网底层最低要求 0.1 Gwei（`100000000` wei），1.0 Gwei 可确保交易在 1~2 个出块周期内快速确认；
> 4. 部署成功后，提取终端返回的 `Deployed to: 0x...` 地址，记为 `NEW_IMPL_ADDRESS`。

---

## 3. 步骤 2：生成 Proposer Safe 的 Timelock 调度与执行调用数据

### 3.1 调用链路与参数定义

1. **最终执行目标 (Target)**：ProxyAdmin 合约 `0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185`；
2. **执行转账值 (Value)**：`0`；
3. **底层升级 Payload (`data`)**：
   - 函数签名：`upgradeAndCall(address,address,bytes)`（函数选择器：`0x9623609d`）；
   - 参数 1：代理地址 `0x420382a0f874c1F120621f847d81581e8737504e`；
   - 参数 2：新实现合约地址 `NEW_IMPL_ADDRESS`；
   - 参数 3：升级调用附加数据 `0x`（空字节流，保持无再初始化调用）；
4. **前序依赖 (Predecessor)**：`bytes32(0)`（`0x0000000000000000000000000000000000000000000000000000000000000000`）；
5. **固定盐值 (Salt)**：`0x0000000000000000000000000000000000000000000000000000000000000001`（`bytes32(uint256(1))`）；
6. **延时要求 (Delay)**：`600` 秒（严格等于 Timelock 的 `minDelay`）。

---

### 3.2 离线编码与 OperationId 计算示例 (基于占位地址 `0x1111111111111111111111111111111111111111`)

以演练基准新实现地址 `0x1111111111111111111111111111111111111111` 为例，生成的各级 Calldata 与 OperationId 如下：

#### A. ProxyAdmin.upgradeAndCall 底层 Payload
```text
0x9623609d000000000000000000000000420382a0f874c1f120621f847d81581e8737504e000000000000000000000000111111111111111111111111111111111111111100000000000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000000000000000000000
```

#### B. 计算出的唯一 Operation ID
$$	ext{operationId} = 	ext{keccak256}(	ext{abi.encode}(	ext{target}, 	ext{value}, 	ext{payload}, 	ext{predecessor}, 	ext{salt}))$$
```text
0x36a804add3121874a343ecc05dc95e3f769462adad6d34c5d2a427e4818437f8
```

#### C. Proposer Safe 提交至 TimelockController 的 schedule Calldata
- 函数签名：`schedule(address,uint256,bytes,bytes32,bytes32,uint256)`（选择器：`0x01d5062a`）
```text
0x01d5062a000000000000000000000000d8ab4f2b12a37806bfc6433c9de0838b6e3cf185000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000c000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000000000000000000000000000000000025800000000000000000000000000000000000000000000000000000000000000849623609d000000000000000000000000420382a0f874c1f120621f847d81581e8737504e00000000000000000000000011111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
```

#### D. Proposer Safe 600秒后提交至 TimelockController 的 execute Calldata
- 函数签名：`execute(address,uint256,bytes,bytes32,bytes32)`（选择器：`0x134008d3`）
```text
0x134008d3000000000000000000000000d8ab4f2b12a37806bfc6433c9de0838b6e3cf185000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000a00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000849623609d000000000000000000000000420382a0f874c1f120621f847d81581e8737504e00000000000000000000000011111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
```

---

### 3.3 动态生成脚本 (针对真实部署地址)

若部署出实际地址 `$NEW_IMPL`，可通过以下 `cast` 单行命令动态生成准确调用数据：

```bash
# 1. 生成底层 upgradeAndCall payload
PAYLOAD=$(cast calldata "upgradeAndCall(address,address,bytes)" 0x420382a0f874c1F120621f847d81581e8737504e $NEW_IMPL 0x)

# 2. 生成 schedule calldata
cast calldata "schedule(address,uint256,bytes,bytes32,bytes32,uint256)"   0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185   0   $PAYLOAD   0x0000000000000000000000000000000000000000000000000000000000000000   0x0000000000000000000000000000000000000000000000000000000000000001   600

# 3. 计算 operationId
cast call 0x75F344F2c5a79d945D873Edbdccf67e61e56C9de   "hashOperation(address,uint256,bytes,bytes32,bytes32)(bytes32)"   0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185 0 $PAYLOAD   0x0000000000000000000000000000000000000000000000000000000000000000   0x0000000000000000000000000000000000000000000000000000000000000001   --rpc-url "$RPC_URL"

# 4. 生成 execute calldata
cast calldata "execute(address,uint256,bytes,bytes32,bytes32)"   0xd8ab4f2b12a37806bfc6433c9de0838b6e3cf185   0   $PAYLOAD   0x0000000000000000000000000000000000000000000000000000000000000000   0x0000000000000000000000000000000000000000000000000000000000000001
```

---

## 4. 步骤 3：生成取消操作调用数据 (`Timelock.cancel`) 与权限审计

### 4.1 cancel 调用数据生成

针对上述 Operation ID（`0x36a804add3121874a343ecc05dc95e3f769462adad6d34c5d2a427e4818437f8`）：
- **目标合约**：TimelockController `0x75F344F2c5a79d945D873Edbdccf67e61e56C9de`；
- **函数签名**：`cancel(bytes32)`（选择器：`0xc4d252f5`）；
- **完整 Calldata**：
```text
0xc4d252f536a804add3121874a343ecc05dc95e3f769462adad6d34c5d2a427e4818437f8
```

### 4.2 核心权限与治理审计说明 (至关重要)

根据 `testnet-run-report.md` 的链上权限核验事实：
1. **多签角色分配**：
   - `Proposer Safe` (`0xE5fE...8b47`) 拥有 `PROPOSER_ROLE` 与 `CANCELLER_ROLE`；
   - `Guardian Safe` (`0x8E9D...8358`) 仅被赋予业务代理合约 `MinerV1` 与 `StakeVault` 的紧急暂停权 (`pause()`)，**在 TimelockController 上并未配置任何权限（无 CANCELLER_ROLE）**。
2. **演练执行要求**：
   - 若由 Guardian Safe 直接向 TimelockController 提交上述 `cancel` 交易，**将直接触发 `AccessControlUnauthorizedAccount` revert 失败**；
   - **正确的演练取消路径**：
     - **方案 A（常规推荐）**：由持有 `CANCELLER_ROLE` 的 **Proposer Safe** 发起取消交易；
     - **方案 B（治理扩展）**：若需演练 Guardian Safe 取消紧急提案，多签管理员必须在升级演练前向 Timelock 提交授权提案，为 Guardian Safe 授予 `CANCELLER_ROLE`（`0xfd643c72710c63c0180259aba6b2d05451e3591a24e58b62239378085726f783`）。

---

## 5. 步骤 4：升级后链上验证清单 (Post-Upgrade Verification Checklist)

升级完成落块后，必须严格执行以下三阶段无状态检查：

### 5.1 存储槽与状态快照一致性核验 (Storage Slots Verification)

利用 `cast storage` 与 `cast call` 验证代理合约存储未发生任何错位或覆写：

1. **ERC-1967 槽位检查**：
   ```bash
   # 检查逻辑实现槽 (Implementation Slot)
   cast storage 0x420382a0f874c1F120621f847d81581e8737504e      0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc      --rpc-url "$RPC_URL"
   # 预期结果: 严格等于 NEW_IMPL_ADDRESS (去掉前导零)

   # 检查管理员槽 (Admin Slot)
   cast storage 0x420382a0f874c1F120621f847d81581e8737504e      0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103      --rpc-url "$RPC_URL"
   # 预期结果: 0x000000000000000000000000d8ab4f2b12a37806bfc6433c9de0838b6e3cf185 (ProxyAdmin)
   ```

2. **核心业务槽位基线核验**：
   | 槽位 (Slot) | 变量名称 | 预期值 / 检查命令 | 安全含义 |
   | :--- | :--- | :--- | :--- |
   | **Slot 0** | `token` | `0x2Ec162EF329F3C7b202B14F9DAb2877cFC922c29` | 绑定代币合约未被篡改 |
   | **Slot 1** | `vault` | `0x04aDD681D11F4af72D44BD8cCc6Ee5d12801A1a7` | 质押金库未被篡改 |
   | **Slot 2** | `admin` | `0x75F344F2c5a79d945D873Edbdccf67e61e56C9de` | 确保管理员为 Timelock |
   | **Slot 3** | `START` | `1793869200` (测试网启动时间戳，现场由 cast call 读取确认) | 起始轮次时间锚点对齐 |
   | **Slot 4** | `S_STAR` | `30` (或当前设定的治理目标值) | 挖矿难度调节基准 |
   | **Slot 5** | `ADDR_CAP_BPS` | `2000` (20%) | 单地址上限强约束生效 |
   | **Slot 6** | `paused` | `false` (0) | 升级后系统处于正常就绪态 |
   | **Slot 7** | `lastSynced` | 升级前已同步的最新轮次 | 检查追平进度历史无回退 |
   | **Slot 8** | `ema` | 升级前的平滑历史算力值 | EMA 连续性未破坏 |
   | **Slot 19** | `guardian` | `0x8E9D51118C70906dE13E2FED7787B056b9268358` | Guardian Safe 守护权完整 |
   | **Slot 20** | `unallocatedGap` | 查询任意历史轮次应返回 `0` (无历史碰撞) | 新增存储槽未污染已有内存 |

---

### 5.2 历史轮次 Seed 补齐验证 (`initEpochSeed` 补齐 13 到当前轮 `epoch()`)

在测试网运行中，第 0~12 轮已完成初始化，而从第 13 轮至执行时的当前活跃轮次 `epoch()`，由于距当前区块早已超出 256 个块深度，直接取 `blockhash` 会为 0。

1. **触发补齐回填**：
   回填目标必须设定为“到执行时的 `epoch()`”。通过重复调用合约的 `initEpochSeed(epoch(), 24)` 进行分批级联回填（单次最多 24 步，防范 Gas 溢出），重复调用直至返回值等于执行时的当前轮次：
   ```bash
   # 获取执行时的当前活跃轮次
   TARGET_EPOCH=$(cast call 0x420382a0f874c1F120621f847d81581e8737504e "epoch()(uint256)" --rpc-url "$RPC_URL")

   # 重复调用 initEpochSeed(TARGET_EPOCH, 24) 直至返回当前轮 (返回值 lastInit == TARGET_EPOCH)
   cast send 0x420382a0f874c1F120621f847d81581e8737504e      "initEpochSeed(uint256,uint256)(uint256)" $TARGET_EPOCH 24      --rpc-url "$RPC_URL"      --account <keystore 名称>      --legacy      --gas-price 1000000000
   ```
   亦可运行 Keeper 补齐机制，由 Keeper 签名完成循环级联回填：
   ```bash
   python script/miner.py --backfill-seed --start-epoch 13      --end-epoch $(cast call 0x420382a0f874c1F120621f847d81581e8737504e "epoch()(uint256)" --rpc-url "$RPC_URL")      --rpc-url "$RPC_URL" --miner-contract 0x420382a0f874c1F120621f847d81581e8737504e
   ```
2. **种子非零与事件校验**：
   - 遍历查询：`cast call 0x420382a0f874c1F120621f847d81581e8737504e "epochSeed(uint256)(uint256)" <e> --rpc-url "$RPC_URL"`
   - 验证：从第 13 轮到执行时的当前轮次 `epoch()`，每一轮的返回值必须严格大于 0；
   - 事件日志捕获：验证链上记录的 `EpochSeedInitialized(uint256 indexed epoch, uint256 seed)` 事件：
     - **Topic 0**：`0x39d7463eafe5f8fd37df0bc31b3418db8f0e961fca9b34bfa2169e2c22544f5e`（由 `cast sig-event "EpochSeedInitialized(uint256,uint256)"` 核对确认）
     - **Topic 1 (epoch)**：`13` 至执行时的当前轮次 `epoch()`。

---

### 5.3 核心业务事件检查 (`sync` 与 `claim`)

在新实现激活且历史种子补齐后，执行一轮模拟挖矿与结算验证。当前 `src/MinerV1.sol` 合约实际发出的事件体系如下：

1. **`sync(epoch)` 轮次追平事件验证**：
   - 触发公共追平：`cast send 0x420382a0f874c1F120621f847d81581e8737504e "sync(uint256)" <targetEpoch> --rpc-url "$RPC_URL" --account <keystore 名称> --legacy --gas-price 1000000000`
   - 检查触发的 `EpochSynced` 事件：
     - **Topic 0**：`0xc7457f76c4fde7f674686f6aa699ce92c9830d6cd0f54b406f4249acb3aa5e41`（由 `cast sig-event "EpochSynced(uint256,uint256,uint256)"` 核对确认）
     - **Topic 1 (epoch)**：目标轮次
     - **Data 解码**：`target` 必须严格位于 `[HARDEST, EASIEST]`（`[1<<221, 1<<224]`）区间内，`ema` 按照 0.85/0.15 比例平滑更新。
   - 伴随检查 `EpochSettled` 事件：`src/MinerV1.sol` 内部对已结轮次还会发出 `EpochSettled(uint256 indexed epoch, uint256 effectiveQuota, uint256 totalWeight, uint256 wMin, uint256 unallocatedDilution)`，沉淀结算数据。

2. **`claim(epoch)` 领奖与铸币事件验证**：
   - 对持有有效份额并已结算的轮次发起 `claim`：
     `cast send 0x420382a0f874c1F120621f847d81581e8737504e "claim(uint256)" <e> --rpc-url "$RPC_URL" --account <keystore 名称> --legacy --gas-price 1000000000`
   - **当前合约实际发出事件确认**（根据 `src/MinerV1.sol` 与 `src/interfaces/IMiner.sol`）：
     - **旧版冗余 `Claimed` 事件已彻底移除**，由四参数结构化 `RewardClaimed` 事件统一承接：
       - **`RewardClaimed` 事件**（结构化领奖与溢出截断事件）：
          - **Topic 0**：`0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a`（由 `cast sig-event "RewardClaimed(address,uint256,uint256,uint256)"` 核对确认）
          - **Topic 1 (miner)**：矿工地址 (`address indexed`)
          - **Topic 2 (epoch)**：结算轮次 (`uint256 indexed`)
          - **Data (32 bytes × 2 = 64 bytes)**：
            - `claimedAmount`：实际领取数额（严格受 20% 单地址硬顶截断约束）
            - `cappedOverflow`：因超出 20% 硬顶而被截断的溢出数额（沉淀记入 `unallocatedGap`）
     - 当 `effectiveQuota == 0` 时，仅触发 `RewardClaimed(msg.sender, e, 0, 0)` 并直接返回。
   - 伴随检查代币转账：当 `claimedAmount > 0` 时，代币合约触发 `Transfer(address(0), miner, amount)` 确认 ERC20 Mint 成功；当 `claimedAmount == 0` 时不触发代币铸造；
   - 伴随检查稀释记账：读取 `unallocatedGap(epoch)`，确认超出 20% 上限的被截断数额与因未满员导致的配额差额被精准沉淀记账。

---
*材料准备完毕，演练阶段请严格按照多签排期与链上公示时限执行。*
