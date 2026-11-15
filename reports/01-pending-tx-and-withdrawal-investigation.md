# 质押退出请求上链验收与提款闭环调查报告

## 勘误与证据冲突排查（Errata & Evidence Reconciliation）

> [!WARNING]
> **前期分析时间戳抄录错误修正**：
> 在前次报告中，曾出现以下证据矛盾：
> - 记录区块时间戳为 `1794676513`（对应北京时间 2026-11-15 01:15:13）；
> - 若以此计算：$(1794676513 - 1794135600) / 3600 = 150.25$，属于 Epoch 150；
> - 这与链上事件 Data `0x97`（十进制 151）及 `withdrawReqEpoch = 152` 存在冲突。

### 1. 根本原因核实与多节点交叉验证
通过标准 JSON-RPC 重新调用 `eth_getBlockByHash`、`eth_getBlockByNumber` 并引入第二独立公开节点（`https://data-seed-prebsc-1-s1.bnbchain.org:8545`）进行只读交叉比对，查明根因：
- **真实链上区块时间戳十六进制**：`0x6af8a721`
- **十进制数值**：**`1794680609`**
- **对应 UTC 时间**：`2026-11-14 18:23:29 UTC`
- **对应北京时间**：**`2026-11-15 02:23:29 BJT`**
- **抄录偏差来源**：此前临时核算脚本中误将十六进制第 5 位 nibble 的 `a`（对应二进制 `1010`）错录为 `9`（对应二进制 `1001`），即错写为 `0x6af89721`（`1794676513`，北京时间 01:15:13），产生恰好 $0x1000 = 4096$ 秒的偏差。
- **冲突化解证明**：
  使用真实区块时间戳 `1794680609` 代入合约轮次公式：
  $$\text{epoch} = \lfloor \frac{1794680609 - 1794135600}{3600} \rfloor = \lfloor \frac{545009}{3600} \rfloor = 151$$
  - 与链上事件 `WithdrawRequested` 记录的 `0x97` (151) **100% 精确相符**；
  - 与同一区块下 `StakeVault.epoch()` 返回的 `0x97` (151) **100% 精确相符**；
  - 与同一区块下 `MinerV1.epoch()` 返回的 `0x97` (151) **100% 精确相符**；
  - 与 `stakes(miner)` 写入的 `withdrawReqEpoch = 152` ($151 + 1$) **100% 精确相符**；
  - 亦完全解释了为何用户在 01:19、01:21 观察时交易仍在 txpool 中未打包——因为替换交易是在 **02:23:29 BJT** 随新区块正式打包入链。

完整原始数据已固化至脱敏凭证文件：[evidence-block-142730498.json](file:///d:/project/poolcoin-rc2/reports/evidence-block-142730498.json)。

---

## 1. 退出请求上链最终验收（已上链确认）

### 1.1 链上固定区块回执核验 (Block 142730498)
- **交易哈希 (`txHash`)**：`0xd873d1c90e63002aed560d5f9ed1d479a951add4cc4ea6805e29cb19df15e1d0`
- **区块高度 (`blockNumber`)**：`142730498` (`0x881e502`)
- **区块哈希 (`blockHash`)**：`0x9eceb309763ebf00c637d2bf85c352b83bfa9b7edca389e4f2109e1fe74a8239`
- **区块时间戳 (`blockTimestamp`)**：`1794680609`（`0x6af8a721`，北京时间 `2026-11-15 02:23:29`）
- **执行状态 (`status`)**：`0x1`（成功）
- **链 ID (`chainId`)**：`97` (`0x61`)
- **发送者 (`from`)**：`0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484`（测试矿工）
- **目标合约 (`to`)**：`0x40b92431de886Ea46117407c8f7123a31FB3F744`（StakeVault）
- **交易 Nonce**：`5`
- **调用函数 (`input`)**：`0xb3423eec`（`requestWithdraw()`，无参调用，严格匹配 `keccak256("requestWithdraw()")[:8]`）
- **转账金额 (`value`)**：`0 wei`
- **Gas Limit / Gas Used**：`150,000` / `28,621`
- **费率参数 (Type-2 EIP-1559)**：
  - `maxFeePerGas`: `3,000,000,000 wei` (3.0 gwei)
  - `maxPriorityFeePerGas`: `1,000,000,000 wei` (1.0 gwei)
  - `effectiveGasPrice`: `1,000,000,000 wei` (1.0 gwei)
- **实际 Gas 支出**：$28,621 \times 1.0\text{ gwei} = 0.000028621\text{ tBNB}$
- **完整事件日志解码**：
  - Topic 0: `0xf7774b688d56120b783560a913ee60792a73dfd511812b7be5eccf10d08c6689`（完整无截断，严格等于 `keccak256("WithdrawRequested(address,uint256)")`）
  - Topic 1: `0x000000000000000000000000d8d630b6172f9ed5db58a17ec9b0512fbd4c9484`（索引参数 `address indexed u`，匹配矿工地址）
  - Data: `0x0000000000000000000000000000000000000000000000000000000000000097`（非索引参数 `uint256 epoch`，值为 **151**）

### 1.2 账户 Nonce 队列核验
- **最新 Nonce (`latest`)**：`6`
- **待处理 Nonce (`pending`)**：`6`
- **结论**：原滞留的 Nonce 5 交易已由该替换交易终结上链，在当前公共节点观察到的该账户待处理交易队列已排空（latest=6, pending=6），无任何待处理交易阻塞。

---

## 2. 链上存储状态固定核查（Block 142730498 实测）

根据 `src/StakeVault.sol` 的真实完整结构定义：
```solidity
struct S {
    uint64 depositEpoch;
    uint64 withdrawReqEpoch;
    bool has;
    uint128 depositAmount;
}
```
标准 ABI 规范中，Getter 函数返回的静态结构体占 4 个 32 字节槽位（共 128 字节，256 位十六进制字符）。
只读调用 `stakes(0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484)` 固定在区块 `142730498`：
- **原始返回十六进制 (128 字节完整输出)**：
  `0x000000000000000000000000000000000000000000000000000000000000007e00000000000000000000000000000000000000000000000000000000000000980000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000000000000000000000002386f26fc10000`
- **四字段逐槽解码**：
  1. 槽 0 (bytes 0..31): `0x...7e` $\rightarrow$ `126`（历史存入轮次不变）
  2. 槽 1 (bytes 32..63): `0x...98` $\rightarrow$ `152`（源码逻辑为 `uint64(epoch()) + 1`，即 $151 + 1 = 152$，已成功写入！）
  3. 槽 2 (bytes 64..95): `0x...01` $\rightarrow$ `true`（质押凭据保持存在，待提款后清除）
  4. 槽 3 (bytes 96..127): `0x...002386f26fc10000` $\rightarrow$ `10,000,000,000,000,000 wei`（精确等于 `0.01 tBNB`，本金金额完整无损）

### 2.2 挖矿资格实时状态 (`status(miner)`)
调用 `StakeVault.status(miner)` 返回：
- `active`: **`false`**
- `since`: `126`
- **安全性说明**：因为 `s.withdrawReqEpoch == 0` 已不成立，矿工的 active 资格被合约即刻注销。若此时矿工尝试向 `MinerV1.submitShares` 提交算力份额，将被合约第 187 行 `require(active, "No active stake")` 直接拦截并 revert。退出请求已生效且杜绝算力作弊。

---

## 3. 最早提款时间精确数学推导与北京时间换算

### 3.1 冷却公式推导
依据 `src/StakeVault.sol` 第 113 行源码：
```solidity
if (epoch() < uint256(s.withdrawReqEpoch) - 1 + COOLDOWN_EPOCHS) revert CooldownActive();
```
- 已知 `s.withdrawReqEpoch = 152`
- 合约常量 `COOLDOWN_EPOCHS = 48`
- 最小解锁轮次条件：
  $$\text{epoch} \ge (152 - 1) + 48 = 151 + 48 = 199$$
- **最早可提款轮次**：**Epoch 199**。

### 3.2 真实北京时间换算（整点量化计算）
合约轮次计算逻辑为：
$$\text{epoch} = \lfloor \frac{\text{block.timestamp} - \text{START}}{\text{EPOCH_LEN}} \rfloor$$
读取链上实测常量：
- `START` = `1794135600`（对应北京时间 2026-11-08 19:00:00）
- `EPOCH_LEN` = `3600`（严格 3600 秒，1 小时整）
- **Epoch 199 起始时间戳**：
  $$\text{Timestamp}_{199} = 1794135600 + 199 \times 3600 = 1794852000$$
- **换算标准时间**：
  - UTC 时间：`2026-11-16 18:00:00 UTC`
  - **北京时间：`2026-11-17 02:00:00 BJT`**（严格整点）

### 3.3 请求时间戳与解锁时间戳等待时长精确对比
- 请求交易打包区块时间戳：`1794680609`（2026-11-15 02:23:29 BJT）
- 解锁整点区块时间戳起点：`1794852000`（2026-11-17 02:00:00 BJT）
- **两时间戳相差净秒数**：
  $$1794852000 - 1794680609 = 171,391 \text{ 秒}$$
  即 **47 小时 36 分钟 31 秒**（约 47.61 小时）。
- **说明**：因为请求发生在 Epoch 151 的第 23 分 29 秒（已过去 1409 秒），而解锁发生在 Epoch 199 的第 0 秒，因此从交易时间戳算起到解锁时间戳实际为 48 小时减去 1409 秒。
  **必须以链上区块时间戳达到 1794852000（即 Epoch 199）且只读模拟 `withdraw()` 成功为最终准入标准，绝不能使用离线固定时长定时广播**。

---

## 4. 提前提款只读模拟测试

使用 `eth_call` 从矿工地址向 `StakeVault` 模拟发起 `withdraw()`（Selector: `0x3ccfd60b`）：
- **模拟调用返回值**：
  `{'code': 3, 'message': 'execution reverted', 'data': '0xaa9a98df'}`
- **错误代码解码**：
  通过计算 `keccak256("CooldownActive()")[:8]`，得出其签名哈希精确为 `aa9a98df`。
- **结论**：链上合约在当前轮次对提前提款行为实施了严格拦截。未向网络广播任何提前提款交易。

---

## 5. 验收状态矩阵与到账核账方案

| 验收项目 | 状态 | 判定依据 |
| :--- | :--- | :--- |
| **退出请求上链** | **PASS** | Block 142730498，状态 1，`withdrawReqEpoch=152` |
| **退出后挖矿失效** | **PASS** | `status(miner).active == false`，份额提交通道关闭 |
| **提前提款拦截** | **PASS** | `eth_call` 模拟实测触发 `CooldownActive()` (0xaa9a98df) |
| **测试网最终提款到账** | **BLOCKED** | 受链上时间硬约束，需等待链上区块时间戳达到 Epoch 199 (1794852000) |

### 5.1 后续到账核账执行方案（届时由用户执行）
当链上最新区块时间戳 $\ge 1794852000$（预计北京时间 2026-11-17 02:00:00）时：
1. **只读预检**：
   查询 `StakeVault.epoch()` 是否已达到 199，并用 `eth_call` 预检 `withdraw()` 是否不再 revert；
2. **提款广播**：
   在交互式终端执行：
   ```bash
   cast send 0x40b92431de886Ea46117407c8f7123a31FB3F744 "withdraw()" \
     --keystore "C:\Users\a2515\.poolcoin-local-credentials\m1-fresh-20261113-170715-404\16ccf93b-1141-41ed-a6ab-cd09d9fffbcf" \
     --rpc-url https://bsc-testnet-rpc.publicnode.com \
     --chain 97 \
     --gas-limit 150000 \
     --gas-price 3000000000 \
     --priority-gas-price 1000000000
   ```
3. **资金与状态核账**：
   - 验证回执 `status == 1`，包含 `Withdrawn(miner, 10000000000000000)` 事件；
   - 验证矿工 BNB 余额净增加：$\Delta \text{Balance} = 0.01\text{ tBNB} - \text{GasUsed} \times \text{GasPrice}$；
   - 验证 `stakes(miner).has == false`，全部质押记录完全释放清空。
