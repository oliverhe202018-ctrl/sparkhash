# PoolCoin RC2 候选材料安全离线回归报告

> [!IMPORTANT]
> **测试环境与归属边界声明**  
> 1. **回归归属**：本报告结果严格归属于 **“HEAD `f7bcef8ffa3f9da74c9980dccb0244c6698c8709` + 0 已跟踪变更 + 6 项未跟踪候选测试与材料输入哈希”**。因新增测试与 fixture 尚未提交，不能且未归属于裸 HEAD。  
> 2. **性质边界**：本轮为**只读离线回归**，不包含网络调用、不执行公网 RPC、不广播交易、不改动代码，**本报告不是 M1 阶段上线放行批准**。  
> 3. **全量与排除项原则**：本轮在 Python 套件中明确标出 3 个独立基准/压测脚本不纳入单元测试套件收集，因此本报告**明确界定为“确定安全范围内的完整离线回归通过”，而非“无条件全量通过”**。

---

## 1. 环境基线与工具版本

- **主工作区**：`D:\project\poolcoin-rc2`
- **隔离回归目录**：`C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression`
- **Git 分支**：`feature/progress-emission`
- **确认 HEAD**：`f7bcef8ffa3f9da74c9980dccb0244c6698c8709`
- **已跟踪文件变更**：`git diff --stat HEAD` 为空（0 处改动，0 暂存）
- **工具链版本**：
  - **Python**：`3.14.5` (pytest `8.4.2`, pluggy `1.6.0`, web3, pycryptodome)
  - **Foundry**：`forge 1.9.0` (commit `6056c58`, build profile `dist`)
  - **Cast**：`cast 1.9.0` (commit `6056c58`, build profile `dist`)
  - **Solidity 编译器**：`solc 0.8.38` (本地缓存路径 `%APPDATA%\svm\0.8.38\solc-0.8.38`)

### 1.1 Python 解释器版本差异说明
- **文档推荐版本**：`docs/miner-quickstart-testnet.md` §2.1 中建议为 `Python 3.10 ~ 3.12`。
- **本轮实测环境**：本机全局执行环境为 `Python 3.14.5`。
- **版本支持边界**：本轮回归仅证明了当前测试套件在 `Python 3.14.5` 下完全通过（289 项通过）；**本轮测试未覆盖且未证明 Python 3.10、3.11 或 3.12 环境**。不得擅自将验证结论扩大到未经实跑的其他解释器版本。

---

## 2. 候选材料与输入身份 (SHA-256 核验)

所有测试均以以下 6 个候选材料的固定哈希作为输入身份执行：

| 候选文件路径 | 状态 | 预期 SHA-256 | 本轮实测 SHA-256 | 核验结果 |
| :--- | :--- | :--- | :--- | :---: |
| `test/SafeL2GovernanceNegatives.t.sol` | 未跟踪 | `5195C7C685D7EEBDDB81D43F1FE962820AE0976CEBB6A1252A7AE28AC5976484` | `5195C7C685D7EEBDDB81D43F1FE962820AE0976CEBB6A1252A7AE28AC5976484` | **一致** |
| `test/NormalSubmissionCapBoundary.t.sol` | 未跟踪 | `7D044925B0C2DF460504E17FCC6624B68C6F96373DF95C331125FFF4458A6E9C` | `7D044925B0C2DF460504E17FCC6624B68C6F96373DF95C331125FFF4458A6E9C` | **一致** |
| `test/fixtures/SafeL2-v1.3.0-runtime.hex` | 未跟踪 | `3D06C66A744A520CB1A46C2017B13A724B95D163704F1363ADEE08235934F8F6` | `3D06C66A744A520CB1A46C2017B13A724B95D163704F1363ADEE08235934F8F6` | **一致** |
| `test/fixtures/SafeProxy-v1.3.0-runtime.hex` | 未跟踪 | `4C0D9DFD76A6BF1150FE312519F388FBFE69E70130B4D0C4705B44D796313305` | `4C0D9DFD76A6BF1150FE312519F388FBFE69E70130B4D0C4705B44D796313305` | **一致** |
| `docs/rc2-offline-governance-cap-evidence.md` | 未跟踪 | `2FB96EF5763C54A1C1A52901FDACC8FC5B0C06381495D90D45FDB7A6B157AD8B` | `2FB96EF5763C54A1C1A52901FDACC8FC5B0C06381495D90D45FDB7A6B157AD8B` | **一致** |
| `docs/miner-quickstart-testnet.md` | 未跟踪 | `12A0DC30B4F29FD16666B90A8A4F097F4E62D4F92072B2F11E5B628052BA09BC` (修改前) | `6C3BC4FA21439442DA8851E101224AAA735F0E79A9A52CBA3246820CDC9A25FB` (修正后) | **已完成歧义修正** |

### 附：`docs/miner-quickstart-testnet.md` 公式修正文本差异说明
在 §11 中原先使用的 `RequestEpoch` 缺乏严谨的变量定义。本轮依规执行单处微调：
- **修改前**：
  ```markdown
  > 3. **冷却期精确数学公式**：  
  >    金库记录 `withdrawReqEpoch = uint64(epoch()) + 1`。  
  >    `withdraw()` 解锁要求：`epoch() >= uint256(s.withdrawReqEpoch) - 1 + 48`，即：
  >    $$\text{Earliest Withdraw Epoch} = \text{RequestEpoch} + 48$$
  >    该周期受链上出块时间戳驱动（48 × 3600 秒），并非本机时钟的绝对 48 小时。
  ```
- **修改后**：
  ```markdown
  > 3. **冷却期精确数学公式**：  
  >    - `requestEpoch` = 申请交易所在轮次 (`epoch()`)；  
  >    - `storedWithdrawReqEpoch` = 金库存储字段，编码为 `requestEpoch + 1`；  
  >    - 解锁轮次公式：  
  >      $$\text{unlockEpoch} = \text{storedWithdrawReqEpoch} - 1 + \text{COOLDOWN\_EPOCHS} = \text{requestEpoch} + \text{COOLDOWN\_EPOCHS}$$  
  >    （其中 $\text{COOLDOWN\_EPOCHS} = 48$）。该周期受链上出块时间戳驱动（48 × 3600 秒），并非本机时钟的绝对 48 小时。
  ```
未改动其他任何章节或业务说明。

---

## 3. 隔离副本同步与安全范围核对

### 3.1 副本同步范围、凭据排除与输入一致性边界
隔离副本采用 `robocopy` 复制至 `C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression`：
- **包含范围**：生产合约源码 `src/`、测试套件 `test/`（含全部未跟踪测试及 4 个 fixture）、测试库 `lib/`、运维与客户端脚本 `script/`、配置 `foundry.toml`、已跟踪文档 `docs/` 等，共计复制 1,842 个有效文件。
- **排除目录规则 (`/XD`)**：`.git`、`.logs`、`broadcast`、`cache`、`out`、`__pycache__`、`.pytest_cache`、`.venv_gpu`。
- **排除文件名匹配规则 (`/XF`)**：`.env`、`.env.*`、`*.key`、`*.pem`、`*.keystore`、`*.pkcs12`、`id_rsa*`、`*secret*`、`*private_key*`。
- **已做的检查与凭据边界**：已对复制日志确认上述匹配规则生效；但文件名过滤模式**不能等同于凭据绝不存在的绝对证明**，未被该模式覆盖的非标准命名文件内部是否包含敏感信息未做全量内容扫描。
- **全输入一致性边界**：本轮对 6 项关键候选文件（2 个新增测试合约、2 个 Safe fixture、补证报告、矿工指引）执行了逐项 SHA-256 强哈希核验比对；但对于其余 1,836 个已复制文件仅依赖 `robocopy` 标准文件流复制，未逐文件建立并比对全量哈希表。因此，除 6 项候选文件外，其余依赖及库文件**尚未取得严格的全输入哈希一致性证明**，此项如实保留为证据边界。

### 3.2 安全性与排除项审查分析
在执行前对所有测试脚本与合约进行了静态安全识别：

1. **Solidity 测试套件**：
   - 检查 `foundry.toml`：未开启 `ffi`（默认 `false`）。
   - 检查 `.t.sol` 源码：未发现 `vm.createFork`、`vm.selectFork`、RPC URL 或私钥调用。
   - 文件权限：仅允许只读访问 `./test/fixtures`。
   - **结论**：全部 22 个 Solidity 测试套件属于纯本地 EVM 离线模拟，确认完全安全，无网络或外部副作用。

2. **Python 测试套件**：
   - **单元测试套件（10 个文件，289 个用例）**：
     - `test_cast_signer.py`：对 `urllib.request.urlopen` 和 `subprocess.run` 实施严格 Mock，严禁真实联网和外部进程执行；环境变量注入检测时自动过滤私钥变量。
     - `test_keeper.py`：使用 `tempfile.TemporaryDirectory` 重置 `KEEPER_STATE_DIR`，完全隔离状态文件，RPC 交互全部 Mock。
     - `test_keeper_process_fault.py`：测试子进程并发与异常退出时，均绑定在独立的临时工作目录；仅对由自身通过 `subprocess.Popen` 生成的子进程对象调用 `proc.kill()`，**绝未扫描系统进程或向真实 Keeper 进程发送信号**。
     - `test_safe_output*.py` / `test_scripts_safe_output.py`：纯文本脱敏与异常格式化逻辑测试。
     - `test_validate_share_batch.py`：纯离线 JSON 批次校验与格式检查。
     - `test_miner.py` / `test_miner_gpu.py`：网络与签名函数全量 Mock，GPU 检测在缺乏硬件时通过 pytest fixture 自动安全 skip。
   - **排除项与理由（3 个脚本）**：
     1. `test/test_exhaustive_check.py`：2^28 空间暴力哈希比对脚本，需 GPU OpenCL 运行时并耗时极长，仅在 `__name__ == '__main__'` 时执行，无 pytest 用例。
     2. `test/test_cpu_statistics.py`：多进程 CPU 挖矿方差统计压测脚本，无单元测试用例。
     3. `test/test_gpu_statistics.py`：GPU 50 轮挖矿方差统计压测脚本，无单元测试用例。
     *以上 3 项作为基准压测工具排除于本次常规回归之外，符合预期。*

---

## 4. 离线回归执行记录与异常排查说明

### 4.1 新增 10 项定向测试执行历史（含 Shell 语法异常排查）

在首轮执行针对新增 10 项测试时，前 3 次调用出现了命令退出码 1：

#### 失败调用 1
- **命令**：`Set-Location C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression; forge test --offline --match-contract "NormalSubmissionCapBoundaryTest|SafeL2GovernanceNegativesTest" -vv`
- **退出码**：`1`
- **原始错误**：
  ```text
  'SafeL2GovernanceNegativesTest' is not recognized as an internal or external command, operable program or batch file.
  ```

#### 失败调用 2
- **命令**：`Set-Location C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression; forge test --offline --match-contract 'NormalSubmissionCapBoundaryTest|SafeL2GovernanceNegativesTest' -vv`
- **退出码**：`1`
- **原始错误**：同上。

#### 失败调用 3
- **命令**：`powershell -Command "Set-Location 'C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression'; forge test --offline --match-contract 'NormalSubmissionCapBoundaryTest|SafeL2GovernanceNegativesTest' -vv"`
- **退出码**：`1`
- **原始错误**：同上。

#### 根因分析与成功方案
- **性质界定**：以上 3 次退出码 1 的错误提示均指向命令调用链中的引用与转义（escaping / quoting）问题，报错主体为找不到命令 `'SafeL2GovernanceNegativesTest'`，而非 Solidity 合约执行或测试断言失败。本报告不对调用链底层（cmd.exe / PowerShell 包装层）对引号内管道符的具体处理机制做已证实断言，仅记录该错误发生在包含 `|` 的 match 参数调用中。
- **最终成功命令**：避免使用管道符 `|`，拆分为两个连续合约匹配命令：
  ```powershell
  powershell -Command "Set-Location 'C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression'; forge test --offline --match-contract SafeL2GovernanceNegativesTest; forge test --offline --match-contract NormalSubmissionCapBoundaryTest"
  ```
- **成功执行结果**：
  - `SafeL2GovernanceNegativesTest`：**7 passed, 0 failed, 0 skipped** (耗时 4.86ms)
  - `NormalSubmissionCapBoundaryTest`：**3 passed, 0 failed, 0 skipped** (耗时 12.03ms)
  - **小计**：**10 / 10 PASS (100% 通过)**。

---

### 4.2 完整离线 Solidity 测试套件（包含新增 10 项）

- **执行目录**：`C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression`
- **执行命令**：`powershell -Command "Set-Location 'C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression'; forge test --offline"`
- **套件覆盖数量**：22 个测试套件，共计 130 项测试（**明确：130 项中已完整包含上述新增 10 项测试**）
- **测试结果**：**130 passed, 0 failed, 0 skipped (全部通过)**
- **执行耗时**：170.78ms (CPU time: 686.87ms)
- **套件清单**：
  1. `SybilSimulationTest`: 2 passed
  2. `TokenTest`: 4 passed
  3. `BaselineUpgradeTest`: 1 passed
  4. `Benchmark34BitTest`: 5 passed
  5. `DifficultyTest`: 4 passed
  6. `ProxyUpgradeTest`: 3 passed
  7. `WithdrawalTest`: 4 passed
  8. `MinerV1Test`: 7 passed
  9. `StakeVaultTest`: 10 passed
  10. `EpochSeedBackfillTest`: 9 passed
  11. `ProgressEmissionTest`: 4 passed
  12. `GovernancePermissionsTest`: 14 passed
  13. `HardCapTest`: 4 passed
  14. `DeployPreflightTest`: 9 passed
  15. `SafeL2GovernanceNegativesTest` (新增): 7 passed
  16. `CapAndOverflowConservationTest`: 7 passed
  17. `RewardBucketingInvariantTest`: 2 passed
  18. `MinerShareVerificationTest`: 12 passed
  19. `InvariantTest`: 3 passed (含 64 runs / 2048 calls 不变量断言)
  20. `AllocationCapsTest`: 5 passed
  21. `ReplayAndBoundariesTest`: 11 passed
  22. `NormalSubmissionCapBoundaryTest` (新增): 3 passed

---

### 4.3 完整离线 Python 测试套件与失败尝试记录

#### 4.3.1 `test_gpu_statistics.py` 的独立调用失败尝试
在全量收集前，曾对 `test/test_gpu_statistics.py` 进行过独立调用尝试：
- **执行命令**：
  ```powershell
  powershell -Command "Set-Location 'C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression'; python -m pytest test/test_gpu_statistics.py"
  ```
- **退出码**：`1`
- **原始输出**：
  ```text
  ============================= test session starts =============================
  platform win32 -- Python 3.14.5, pytest-8.4.2, pluggy-1.6.0
  rootdir: C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression
  plugins: anyio-4.14.2, platformdirs-4.13.5, asyncio-0.24.0
  asyncio: mode=Mode.STRICT, default_loop_scope=None
  collected 0 items

  ============================ no tests ran in 0.15s ============================
  ```
- **性质界定**：
  1. `test_gpu_statistics.py` 内部仅定义了 `run_gpu_statistical_test(...)` 函数及 `if __name__ == '__main__':` 运行块，**未定义任何以 `test_` 开头的测试函数或测试类**。
  2. 按 pytest 标准规范，未收集到测试时默认退出码通常为 5（`ExitCode.NO_TESTS_COLLECTED`），但本轮执行实际记录的退出码为 1。二者存在不一致，具体是原生 pytest 在特定环境/插件下的行为、还是外部执行包装层返回的退出码，在此未做深入调查和确认。本报告严格保留历史报告的退出码 1 与原始输出，不擅自修改历史记录，亦不重新运行测试。
  3. **事实明确**：该文件属于基准压测脚本，不属于常规单元测试；该次退出码 1 属于测试收集为空，不是断言报错。本轮未为了消除该记录而修改脚本，如实记录此失败尝试。

#### 4.3.2 单元测试全量执行结果
- **执行目录**：`C:\Users\a2515\AppData\Local\Temp\poolcoin-rc2-isolated-regression`
- **执行命令**：`python -m pytest test/ -q --disable-warnings`
- **测试收集**：289 项测试
- **测试结果**：**289 passed, 0 failed, 0 skipped (全部通过)**
- **执行耗时**：28.41s
- **模块分布**：
  1. `test_cast_signer.py`: 48 passed
  2. `test_deployment_consistency.py`: 7 passed
  3. `test_keeper.py`: 120 passed
  4. `test_keeper_process_fault.py`: 18 passed
  5. `test_miner.py`: 14 passed
  6. `test_miner_gpu.py`: 27 passed
  7. `test_safe_output.py`: 14 passed
  8. `test_safe_output_miners_monitor.py`: 9 passed
  9. `test_scripts_safe_output.py`: 6 passed
  10. `test_validate_share_batch.py`: 26 passed

---

## 5. 综合技术结论与验收建议

### 5.1 是否可以进入“一条主路径的干净环境验收”
**技术建议：可以进入。**  
理由：
1. 本轮候选的 10 项新增定向测试（Safe 负例与 20% 正常份额边界）在离线 EVM 环境中已完整跑通，断言逻辑与边界条件与设计严格相符。
2. 既有 20 个 Solidity 测试套件及 10 个 Python 模块（289 个单元用例）在此基线下未发生任何回退。
3. `docs/miner-quickstart-testnet.md` 提款公式歧义已修正，主路径（Windows PowerShell + Foundry Keystore）操作步骤与参数已具备闭环验证文档。

### 5.2 阻断该验收的问题清单与操作准则
- **无代码缺陷阻断**。
- **无测试失败阻断**。
- **操作准则与核账边界**：
  1. **禁止改变链上参数**：不采纳“临时降低共享测试网难度”或“跨 epoch 延长同一 share 求解”等建议；矿工客户端直接读取链上实时 `target(epoch)`，结合测试机现场硬件实际算力选择单一主路径（CPU 或 GPU），本轮及后续验收均不在链上改变任何参数。
  2. **提款核账边界**：对于最终提款 (`withdraw()`)，**不作“钱包余额必然净增 0.01 tBNB”的绝对断言**；提款交易本身需扣除链上 Gas 费，实际核账必须将**保证金本金返还 (0.01 tBNB) 与提款交易产生的 Gas 手续费分开核算**。
  3. **时序与状态前置**：
     - 确认链上当前 `epoch > depositEpoch` 方可提交 share；
     - 确认当轮 `epochSeed != 0x0` 方可启动求解；
     - 退出申请后需等待 48 轮链上冷却期满方可调用 `withdraw()`。
