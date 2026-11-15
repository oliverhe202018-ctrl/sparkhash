# Python --send 交互式 Keystore 签名机制实施报告

## 1. 架构目标与安全边界

为杜绝明文密码泄露、环境变量污染以及非交互式环境下进程意外挂起的问题，在 `script/cast_signer.py`、`script/claim.py` 和 `script/submit_share.py` 中实现了全新、高安全等级的 `--keystore` 签名路径。

### 1.1 核心设计规范
1. **密码绝不经由 Python 内存**：
   交互模式下，Python 子进程绝不自行读取或代理用户输入的密码，亦不把密码拼接入命令行参数；而是通过原生 `cast send` 进程在宿主 TTY 终端中直接提示用户输入密码。
2. **非交互式环境即时熔断，严禁进程假死**：
   在后台服务、无终端管道（pipe）、CI/CD 或无交互 TTY 环境下（`sys.stdin.isatty() is False`），程序严禁挂起等待输入。程序会即刻安全终止，并输出结构完整、参数已预先计算校验完毕的单行原生 `cast send` 命令供用户在独立安全终端中执行。
3. **互斥性全面约束**：
   - `--account` 与 `--keystore` 严格互斥；
   - 外部传入的私钥环境变量（`PRIVATE_KEY`, `MINER_PRIVATE_KEY`）与 `--keystore` / `--account` 互斥；
   - `--keystore` 与 `--password-file` 搭配使用时自动切换为免交互凭证模式，但未提供 `--password-file` 时必须依赖交互式 TTY。
4. **广播前显式确认与零广播保证**：
   在真实网络广播前，必须完整展示目标合约、调用函数、金额、Nonce、Gas Limit、EIP-1559 费率和最大可能费用支出。用户必须明确键入 `yes` 方可广播；任何其他输入即刻终止并保证**零网络发送**。
5. **不确定性熔断保护**：
   若广播超时或未能解析出有效交易哈希，标记为 `UNCERTAIN` 状态，严禁换 Nonce 或自动重试。

---

## 2. 核心模块实现细节

### 2.1 `script/cast_signer.py` 增强
- **`validate_keystore_path(keystore_path)`**：
  严格校验 keystore 文件存在性、是否为普通文件（拒绝传入目录）、去除首尾空格，并在异常处理时将真实路径脱敏登记到敏感值列表中。
- **`get_keystore_address(account, password_file, timeout, keystore)`**：
  扩展地址导出接口，支持通过 `--keystore <path>` 执行 `cast wallet address`；
  在未提供 `password_file` 时校验 `sys.stdin.isatty()`，确保交互终端可用。
- **`cast_send(...)`**：
  支持参数 `keystore: str = None`；
  在构建执行命令时，根据互斥逻辑选择构造 `--account <name>` 或 `--keystore <path>`；
  在隔离环境变量（`get_isolated_cast_env()`）中剥除全部以 `ETH_`、`FOUNDRY_`、`CAST_`、`PRIVATE_KEY` 等开头的敏感变量，确保命令执行不受环境隐式污染。

### 2.2 `script/claim.py` 与 `script/submit_share.py` 增强
- 新增 `--keystore` 命令行选项；
- 参数互斥与校验：
  ```python
  if args.keystore and args.account:
      print("错误: --keystore 与 --account 互斥，不能同时提供。", file=sys.stderr)
      sys.exit(1)
  ```
- 签名者地址核验：导出 keystore 内含地址，并与 `--miner` 地址做 checksum 强制比对，防止使用错误签名者。
- 交互终端前置检测与熔断指引：
  ```python
  if args.send and not args.password_file:
      if not sys.stdin.isatty():
          print("错误: 交互式 keystore 签名需要交互式终端 (TTY)。当前环境不支持交互式输入密码。", file=sys.stderr)
          print(f"请在您的交互终端中直接运行以下完整命令:\n{fallback_cmd}", file=sys.stderr)
          sys.exit(1)
  ```
- 交易摘要与前置确认提示：
  ```text
  ================ 待发送交易摘要 (PRE-SEND SUMMARY) ================
  目标合约: 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf
  调用函数: claim(142)
  发送签名: 0xD8D630b6172F9ed5dB58a17Ec9B0512FBD4c9484
  分配Nonce: 5
  Gas Limit: 120000
  Max Fee: 3.0 gwei (3000000000 wei)
  Priority Fee: 1.0 gwei (1000000000 wei)
  预估最大费用: 0.000360 BNB
  ==================================================================
  请输入 'yes' 确认广播该交易 (其他任何输入将安全取消): 
  ```

---

## 3. 测试套件与回归验证

编写了完备的自动化测试套件 `test/test_interactive_keystore.py`，并结合已有的 `test/test_cast_signer.py` 完成了双重验证。

### 3.1 覆盖的测试向量
1. **Keystore 路径校验**：
   - 正常 JSON 路径验证；
   - 空路径、None、全空格输入拒绝；
   - 目标为目录时报错拒绝；
   - 目标不存在时报错，且错误信息中路径被安全脱敏（sanitized）。
2. **互斥性验证**：
   - 同时指定 `--account` 与 `--keystore` 立即报错退出；
   - 同时指定 `--keystore` 与 `PRIVATE_KEY` / `MINER_PRIVATE_KEY` 环境变量立即报错退出。
3. **TTY 终端交互感知**：
   - 当 `sys.stdin.isatty() == False` 且未提供 `password-file` 时，立即报错并拒绝进入密码等待，打印出包含全部必要参数的替代命令；
   - 当 `sys.stdin.isatty() == True` 时，正常触发原生交互式广播逻辑。
4. **签名者身份核验**：
   - Keystore 导出地址与 `--miner` 地址不一致时，在任何外部调用发生前拒绝执行。
5. **用户取消零发送验证**：
   - 模拟用户在确认提示时输入 `no` 或直接按回车，程序退出，断言底层 `subprocess.run` 广播调用次数为精确的 `0` 次。
6. **不确定性与异常处理**：
   - 子进程超时抛出 `CastSendUncertainError`，打印安全告警；
   - 回显输出缺少有效 `0x[0-9a-fA-F]{64}` 哈希时，抛出 `CastSendUncertainError`。

### 3.2 测试执行结果
- `test/test_interactive_keystore.py`: **21 passed**
- `test/test_cast_signer.py`: **49 passed**
- 合计 **70 项专业测试 100% 通过**，无一失败。
