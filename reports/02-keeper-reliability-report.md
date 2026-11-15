# Keeper 可靠性排查与加固报告

## 1. 运行态进程排查（双 PID 现象核实）

### 1.1 进程与父子层级分析
此前在系统进程列表中观察到两个包含 `script.keeper` 关键字的 Python 进程：
- **PID 43196**:
  - 路径：`C:\Users\a2515\AppData\Local\Microsoft\WindowsApps\python.exe`
  - 父进程 PID：`19700` (Windows Terminal / PowerShell 启动宿主)
  - 启动时间：`2026/11/14 12:36:51` (北京时间)
- **PID 27032**:
  - 路径：`C:\Users\a2515\AppData\Local\Python\pythoncore-3.14-64\python.exe`
  - 父进程 PID：`43196`
  - 启动时间：`2026/11/14 12:36:51` (北京时间)

### 1.2 结论
**两者并非两套相互竞争或冲突的独立 Keeper，而是一套单一的父子进程实例**。
在 Windows 平台中，`WindowsApps\python.exe` 是 Windows 应用别名启动器（Execution Alias Shim），它接收命令参数后派生实际的 Python 运行时子进程 `pythoncore-3.14-64\python.exe`。
因此，真实占用 CPU、网络和状态锁的执行体为 **PID 27032**。

---

## 2. 状态锁与并发保护审计

### 2.1 状态锁实现
Keeper 使用 `script/keeper_state.py` 中的 `KeeperStateManager` 实现文件互斥锁：
- **锁文件路径**：
  `C:\Users\a2515\.poolcoin\keeper_state\keeper_97_sender_0xCA3D9fA3_618b024b29674380.lock`
- **锁粒度**：以 `(chainId, senderAddress)` 为命名空间（域名绑定），而非粗暴的全局锁。这允许同一机器上不同 Keeper 地址或不同链并发运行，但严格阻止同一签名地址双开。
- **系统调用机制**：
  - Windows: 使用 `msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)` 实现内核级非阻塞文件互斥；
  - POSIX: 使用 `fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)`。
  - 进程崩溃时，操作系统内核会自动释放文件句柄并解除文件锁，无脏死锁遗留风险。

### 2.2 锁持有现状核实
经只读检测，当前锁文件被 **PID 27032** 正常持有，文件大小为 0 字节，状态正常。
禁止人为删除该锁文件，否则会破坏正在运行的后台守护进程并发边界。

---

## 3. 审计日志与历史停机根因分析

### 3.1 审计日志解析
分析 `C:\Users\a2515\.poolcoin\keeper_state\keeper_97_0x565D06e8_0xCA3D9fA3_7f8953a5488c924e_audit.jsonl`：
- 文件包含 **73 条** 完整审计条目。
- **近期运行状态**：自北京时间 2026-11-14 12:37:12 起至 2026-11-15 01:00:15（覆盖 Epoch 143 至 Epoch 150），每个整点准时触发 `sync(1)` 交易，所有交易均成功广播并取得有效区块回执（例如最新的 block `142719480`），燃气消耗稳定在约 27,000 gas，无任何 revert 或超时。

### 3.2 历史停机记录与根因定性
- **审计记录断层**：
  在 2026-11-14 00:00:35 UTC（北京时间 08:00:35）确认完成 `sync(1)`（对应 Epoch 133）后，审计日志出现长达 **4.5 小时的空白**，直至 2026-11-14 04:37:12 UTC（北京时间 12:37:12）由当前进程重新拉起。
- **根本原因定性**：**未知 (Unknown / Unrecorded)**。
  由于历史进程未将异常堆栈重定向到持久化日志文件（仅标准输出输出），无法断定是由于终端意外关闭、Windows 宿主休眠重启，还是由于只读 RPC 偶发异常导致程序退出。

---

## 4. 可靠性脆弱点与代码加固

### 4.1 发现的致命脆弱点
在源码 `script/keeper.py` 中：
1. `get_epoch_state()` 调用 `eth_call(self.rpc_url, self.miner, SELECTOR_EPOCH)` 等只读 RPC 方法。
2. 原代码未对只读查询设置任何重试逻辑，一旦公共 RPC 节点偶发网络抖动、返回 HTTP 429（限流）或 502/503（网关故障），异常将直接抛出至 `step()`。
3. 在 `step()` 中捕获后返回退出码 `1`：
   ```python
   except Exception as e:
       self.alert("Failed during keeper inspection: " + format_exception_safe(e))
       return 1
   ```
4. `run_forever()` 接收到非 0 退出码后，**立即终止整个主循环并退出进程**！
这意味着：**任何一次瞬间的公共 RPC 只读网络抖动，都会导致长达数周稳定运行的无人值守 Keeper 守护进程永久退出**。

### 4.2 代码加固实施（指数退避只读重试）
对 `script/keeper.py` 实施最小侵入性可靠性加固：
1. 在 `KeeperBot.__init__` 中新增配置项 `self.read_max_retries = 3`；
2. 重构 `get_epoch_state` 方法，增加指数退避保护（默认 3 次尝试，间隔 $1.0\text{s} \times 2^{attempt-1}$）：
   ```python
   def get_epoch_state(self, max_retries: Optional[int] = None, initial_backoff: float = 1.0):
       retries = max_retries if max_retries is not None else getattr(self, "read_max_retries", 3)
       attempt = 0
       while True:
           try:
               res_epoch = eth_call(self.rpc_url, self.miner, SELECTOR_EPOCH)
               cur_epoch = int(res_epoch, 16) if res_epoch else 0
               self.current_epoch = cur_epoch
               res_synced = eth_call(self.rpc_url, self.miner, SELECTOR_LAST_SYNCED)
               last_synced = int(res_synced, 16) if res_synced else 0
               seed = self.get_epoch_seed(cur_epoch)
               return cur_epoch, last_synced, seed
           except Exception as e:
               attempt += 1
               if attempt >= retries:
                   raise
               backoff = initial_backoff * (2 ** (attempt - 1))
               self.log("WARN", f"Transient failure reading epoch state (attempt {attempt}/{retries}): {format_exception_safe(e)}. Retrying in {backoff:.1f}s...")
               time.sleep(backoff)
   ```
3. 严格边界区分：
   - **只读查询 (Read Queries)**：自动重试，平滑抵抗 RPC 瞬时波动；
   - **交易广播 (Write / Broadcast)**：**严禁静默重发或擅自更换 nonce**。一旦广播结果超时或不确定，强制进入 `PREPARING_TO_SEND` 崩溃恢复 / 人工审核锁定状态（`locked_for_manual_review = True`），确保资金与 nonce 绝对安全。

---

## 5. 测试验证与验收状态

### 5.1 自动化测试覆盖
- `test/test_keeper.py` + `test/test_keeper_process_fault.py`: **141/141 通过**（耗时 23.00s）
- `test/test_keeper_hardening.py`: **3/3 通过**
  - `test_default_read_max_retries`: 验证默认重试参数注入；
  - `test_get_epoch_state_recovers_after_transient_failures`: 故障注入模拟 HTTP 502 及网络中断，验证第 3 次自动恢复并成功读取状态；
  - `test_get_epoch_state_exhausts_retries_and_raises`: 验证持续性 RPC 宕机耗尽重试次数后安全抛出异常。

### 5.2 验收分级记录
| 验收维度 | 状态 | 证明/说明 |
| :--- | :--- | :--- |
| **近期连续运行观察** | **已验收通过** | 审计日志证明整点连续稳定执行（Epoch 143-152 无遗漏） |
| **双 PID 进程拓扑确认** | **已验收通过** | 确认属于 WindowsApps 别名启动器与 Python 解释器父子关系 |
| **状态锁竞争与恢复机制** | **已验收通过** | 离线测试模拟锁持有、竞争、TOCTOU 竞态保护通过 |
| **瞬时网络故障平滑恢复** | **已验收通过** | 新增只读指数退避，单元测试 100% 通过 |
| **新实例启动与正常轮询** | **已验收通过** | 用户 03:42 重启 PID 19048，加载加固补丁并成功轮询 |
| **重启后真实自动广播** | **已验收通过** | 04:00 整点自动成功广播 Tx 80 (`initEpochSeed`) 与 Tx 81 (`sync(1)`) |
| **外部 Supervisor 部署与自动拉起** | **未实机验收** | 独立策略与测试通过 (8/8)，但主机尚未将其注册为系统服务 |

---

## 6. 新实例重启与 04:00 整点真实广播验证证据

### 6.1 进程识别与代码版本确认
- **旧进程注销**：原 PID 43196/27032 已于 2026-11-15 03:42 前由用户手工终止。
- **新进程拓扑**：PID `19048`，父进程 PPID `29380` (`powershell.exe`)。
- **运行解释器**：`C:\Users\a2515\AppData\Local\Python\pythoncore-3.14-64\python.exe`。
- **工作目录**：`D:\project\poolcoin-rc2`。
- **代码版本依据**：加载目标文件 `script/keeper.py` 的 SHA-256 为 `10b1090e8614d399cfd753a30f9a319ddb39d4014206fa429c85300761db19e6`（修改时间 `01:39:01`，对应 pycache 生成时间 `01:39:06`，早于进程启动时间 `03:42:36`），确认加固补丁已完整载入运行内存。
- **唯一实例核查**：全系统遍历确认旧实例已彻底退出，当前全系统仅存在 PID `19048` 唯一有效 Keeper 进程。

### 6.2 04:00 整点（Epoch 153 切换）链上真实自动广播证据
在 2026-11-15 04:00:00 (UTC 20:00:00) 链上出块时间戳跨入 Epoch 153 后，Keeper 实例 PID 19048 自主检测到状态跨度并连续广播两笔交易，全部成功上链：

1. **交易 1：种子初始化 `initEpochSeed(153, 24)`**
   - **TxHash**：`0x1e77134e087e56f9c372361c2efb9b9f1d00e2acd4f504192c9398c0031594de`
   - **区块高度**：`142743386`（时间戳 `1794686426`，即 `04:00:26 BJT`）
   - **Nonce**：`80`（严格承接上一历史 nonce 79）
   - **Gas Used**：`73,652`
   - **状态**：`1` (Success)
   - **链上效果**：合约 `epochSeed(153)` 成功写入 `0xd0ecc96db7c6cd809bfc45e3fae036c59fee841a2fe86094dc2a84eaaa1de05e`。

2. **交易 2：跨轮同步 `sync(1)`**
   - **TxHash**：`0x548e687f399819cb655150a85e877c62f0b2f48ade9fef218f4231cd028c6b90`
   - **区块高度**：`142743399`（时间戳 `1794686432`，即 `04:00:32 BJT`）
   - **Nonce**：`81`
   - **Gas Used**：`166,409`
   - **状态**：`1` (Success)
   - **链上效果**：合约 `lastSynced` 成功由 152 步进至 153，触发释放代币铸造。

3. **状态文件与审计条目**
   - 审计文件 `keeper_97_0x565D06e8_0xCA3D9fA3_7f8953a5488c924e_audit.jsonl` 成功写入 Entry 78 与 Entry 79，动作均为 `CONFIRMED_SUCCESS`。
   - 状态文件持久化更新为 nonce 81，tx `0x548e...`，状态 `CONFIRMED_SUCCESS`。
   - 链上 nonce 状态：`latest = 82`，`pending = 82`。
   - 进程状态：PID 19048 持续稳定存活，内存占用约 77 MB，CPU 占用 0.0%。
