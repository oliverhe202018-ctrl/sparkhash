# 代码补丁总结与用户操作批准清单

## 1. 代码补丁与测试变更汇总

本次工程任务在 `D:\project\poolcoin-rc2`（分支 `feature/progress-emission`，基线 Commit `9e81ade`）中完成了精准的最小加固补丁，所有改动均经过本地测试套件严格验证，未引入任何破坏性变更。

### 1.1 修改的现有文件
1. **`script/cast_signer.py`**：
   - 新增 `validate_keystore_path(keystore_path)` 验证 keystore 文件有效性并注册敏感脱敏值；
   - 增强 `get_keystore_address(...)` 支持 `--keystore` 模式；
   - 增强 `cast_send(...)` 支持 `--keystore`，在未提供密码文件时强制要求 `sys.stdin.isatty()`，并注入敏感路径脱敏保护。
2. **`script/claim.py`**：
   - CLI 参数新增 `--keystore`；
   - 增加 `--keystore` 与 `--account`、环境变量私钥的互斥校验；
   - 增加非交互式 TTY 运行环境检测，遇到非 TTY 时即刻退出并打印结构化替代命令；
   - 增加广播前明细摘要与显式 `yes` 确认提示。
3. **`script/submit_share.py`**：
   - 与 `claim.py` 一致，增加 `--keystore` 参数支持、互斥性检查、TTY 熔断与预发送交易摘要确认。
4. **`script/keeper.py`**：
   - 构造函数初始化 `self.read_max_retries = 3`；
   - `get_epoch_state(...)` 增加对只读 RPC 偶发异常（HTTP 429/502/503、超时、连接重置）的指数退避重试保护（1.0s, 2.0s, 4.0s），防止无人值守守护进程因一次网络抖动直接退出。

### 1.2 新增的测试文件
1. **`test/test_interactive_keystore.py`**：
   - 包含 21 个自动化测试用例，覆盖 keystore 路径校验、互斥参数检测、TTY 终端检测、矿工地址校验、用户取消零广播保证、超时与未确定哈希处理。
2. **`test/test_keeper_hardening.py`**：
   - 包含 3 个自动化测试用例，验证 Keeper 只读重试机制、瞬时故障恢复以及耗尽重试后的安全异常抛出。

### 1.3 整体测试验证结果
- **Python 测试 (pytest)**：
  - `test/test_interactive_keystore.py`: 21 passed
  - `test/test_cast_signer.py`: 49 passed
  - `test/test_keeper.py` & `test/test_keeper_process_fault.py`: 141 passed
  - `test/test_keeper_hardening.py`: 3 passed
  - **总计 214 项 Python 测试全部通过 (0 失败)**。
- **Solidity 测试 (forge test)**：
  - 22 个测试套件，共计 **130 项智能合约测试全部通过 (0 失败)**。

---

## 2. 需用户决策与明确批准的事项清单 (Action Items & Decisions)

依据“不可违反的安全边界”规范：未获得用户明确批准前，不得擅自广播交易、杀死守护进程或进行 git 提交与发布。以下为需要用户决定的具体动作：

### 事项一：待处理质押解锁交易（Nonce 5）的处理决策
- **背景**：测试矿工此前发送的 `requestWithdraw()` 交易（txHash `0x125203a8...`）因 gasPrice 仅为 1 wei 滞留在公共节点交易池中。
- **请用户二选一**：
  - **决策 A（推荐，若今晚无需推进测试网提款）**：保持原状，暂不干预。待处理交易留在交易池中，不消耗任何资金，后续随节点自动过期或自发打包。
  - **决策 B（若希望立即在测试网开始 48 小时冷却期）**：批准并在用户本机的独立交互终端（PowerShell 或 CMD）中执行加价替换命令。
    - **执行命令（请在真实终端手输密码）**：
      ```bash
      cast send 0x40b92431de886Ea46117407c8f7123a31FB3F744 "requestWithdraw()" \
        --keystore "C:\Users\a2515\.poolcoin-local-credentials\m1-fresh-20261113-170715-404\16ccf93b-1141-41ed-a6ab-cd09d9fffbcf" \
        --rpc-url https://bsc-testnet-rpc.publicnode.com \
        --chain 97 \
        --nonce 5 \
        --gas-limit 150000 \
        --gas-price 3000000000 \
        --priority-gas-price 1000000000 \
        --value 0
      ```

### 事项二：代码提交与分支管理批准
- **现状**：上述 4 个修改文件与 2 个新测试文件目前处于未暂存（unstaged）状态，没有自动执行 git add/commit。
- **请求批准**：是否允许将本次交互式 Keystore 支持与 Keeper 只读重试加固代码暂存并提交到本地分支 `feature/progress-emission`？（不执行 push）。

### 事项三：主网部署阻断项确认与排期
- **阻断项确认**：
  1. Token 名称与符号修改：从 `TESTTOKEN`/`TTK` 变更为正式名称；
  2. 团队代币线性归属合约：部署标准化 `VestingWallet`；
  3. 主网多签 Safe：各签名人备妥主网硬件钱包并完成 Safe 部署。
- **请求确认**：是否认可将上述三项列为主网发布的前提门禁条件（Gate Conditions）？
