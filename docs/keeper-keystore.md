# Keeper Foundry Keystore 签名模式使用与安全规范

本文档说明 PoolCoin Keeper 运维脚本支持 Foundry Keystore 签名的使用方法、安全边界与运维注意事项。

## 1. 架构与安全边界

### 1.1 Python 端与 cast 的职责划分
- **Python 绝不读取密码**：Keeper 脚本自身在任何阶段均**不打开、不读取** --password-file 指向的文件内容，亦不提供任何明文 --password 参数，避免在 Python 内存堆栈或日志中暴露密码。
- **Foundry cast 解密流程**：在实盘发送及地址推导阶段，Keeper 仅通过参数列表调用 Foundry cast 命令行工具（不使用 shell=True），由 cast 本身读取指定密码文件并完成本地 keystore（默认位于 ~/.foundry/keystores）的密码学解密与交易签名。
- **禁止私钥导出**：Keeper 绝不调用 cast wallet decrypt，亦不导出任何私钥明文到标准输出、日志或临时文件中。

### 1.2 密码文件属于高敏感材料
- --password-file 存放了解密 keystore 所需的口令，其安全性与私钥等价。
- 运维人员必须自行妥善保管密码文件，建议设置严格的文件系统权限（如 Linux 环境 chmod 600 <password_file>，Windows 环境限制仅当前用户 ACL 访问）。
- 严禁将密码文件放入版本控制库、公开目录、网络挂载卷或共享临时路径中。

### 1.3 dry-run 行为与边界说明
- --dry-run 模式用于本地策略模拟与交易构造验证，**不验证真实 keystore 身份**。
- dry-run 模式下**不调用** cast wallet address、cast send、cast wallet decrypt 或任何签名/解密命令。
- dry-run 模式下**不要求、不检查、不打开** --password-file 文件是否存在。
- 如需在 dry-run 下模拟特定调用者地址以进行 nonce 查询，应显式传入 --keeper-address <SIMULATED_ADDRESS>；未提供模拟地址时，Keeper 明确记录签名者处于未验证状态，绝不虚构已通过验证的签名身份。

### 1.4 子进程环境变量隔离
为防止当前 shell 或系统环境继承的环境变量无意中改变签名身份，Keeper 在启动 cast 子进程时自动清洗环境，过滤掉以下 36 个潜在冲突变量：
- Foundry / Cast 原生钱包变量：ETH_FROM, ETH_KEYSTORE, ETH_KEYSTORE_ACCOUNT, ETH_PASSWORD, CAST_ASYNC, CAST_ACCOUNT, CAST_PASSWORD, CAST_PASSWORD_FILE, CAST_PRIVATE_KEY, CAST_MNEMONIC, FOUNDRY_KEYSTORE, FOUNDRY_ACCOUNT, FOUNDRY_PASSWORD, FOUNDRY_PASSWORD_FILE, FOUNDRY_PRIVATE_KEY, FOUNDRY_MNEMONIC, FOUNDRY_ETH_FROM
- 私钥与助记词变量：PRIVATE_KEY, KEEPER_PRIVATE_KEY, ETH_PRIVATE_KEY, MNEMONIC, ETH_MNEMONIC
- 远端签名与硬件钱包变量：AWS_KMS_KEY_ID, GCP_PROJECT_ID, GCP_LOCATION, GCP_KEY_RING, GCP_KEY_NAME, GCP_KEY_VERSION, TURNKEY_API_PRIVATE_KEY, TURNKEY_ORGANIZATION_ID, TURNKEY_ADDRESS, TEMPO_ACCESS_KEY, TEMPO_ROOT_ACCOUNT, TEMPO_SPONSOR_URL, TREZOR_DERIVATION_PATH, LEDGER_DERIVATION_PATH
同时，Keeper 完整保留 PATH, SYSTEMROOT, 代理变量等基础设施配置，确保 RPC 网络与命令调用正常运行。

## 2. 运行示例（占位符说明）

> **安全提示**：以下示例全部采用占位符，RPC 配置显式传参并配合 --no-env。文档中不包含任何真实密码文件路径或真实密码。

### 2.1 dry-run 模拟演练模式
dry-run 模式无需提供密码文件，亦不会触发真实账户访问：
```bash
python script/keeper.py \
  --no-env \
  --rpc-url <RPC_ENDPOINT_URL> \
  --miner-address <MINER_CONTRACT_ADDRESS> \
  --account <KEYSTORE_ACCOUNT_NAME> \
  --keeper-address <EXPECTED_KEEPER_ADDRESS> \
  --dry-run \
  --once
```

### 2.2 实盘 Keystore 运行模式
实盘运行必须同时指定 --account 与 --password-file，以避免后台服务因等待终端交互输入而发生死锁挂起：
```bash
python script/keeper.py \
  --no-env \
  --rpc-url <RPC_ENDPOINT_URL> \
  --miner-address <MINER_CONTRACT_ADDRESS> \
  --account <KEYSTORE_ACCOUNT_NAME> \
  --password-file <PATH_TO_SECURE_PASSWORD_FILE> \
  --keeper-address <EXPECTED_KEEPER_ADDRESS> \
  --once
```
如需持续守护轮询，可去掉 --once 并设置 --poll-interval <SECONDS>。

### 2.3 异常防御与地址核对
- 实盘启动时，Keeper 首先执行 cast wallet address --account <名称> --password-file <路径> 严格解析公钥地址。
- 若提供了 --keeper-address，Keeper 会将推导出的真实签名地址与预期地址进行校验；若两者不一致，Keeper 立即终止退出，在发起任何链上广播前阻断错误交易。
- 账户名严格限制为合法文件名，禁止包含目录分隔符（如 / 或 \）或路径穿越序列（如 ..）。

## 3. 交易结果不确定性与人工核对规范

### 3.1 结果不确定性判定
在网络丢包、RPC 超时、节点拥堵或进程异常中断等场景下，交易发送结果可能处于无法立即证明的状态：
- **无 Hash 不代表未广播**：cast send 发生 TimeoutExpired 超时、进程返回非零退出码、或输出中缺少有效 32 字节交易 Hash 时，底层 RPC 节点可能已经接收原始交易并向全网广播。因此，缺少有效 Hash 绝不代表交易未发送。
- **Nonce 前进不代表本动作成功**：当捕获 nonce too low 或 already known 报错时，仅表明签名账户的 Nonce 在链上或内存池中已推进，不能作为本次特定业务动作（如 initEpochSeed 或 sync）已成功确认的证据。

### 3.2 保守恢复与自动熔断
- **停止自动发送与进程级锁定**：一旦进入结果不确定状态，Keeper 立即触发人工核对锁定（Manual Review Lock），终止本次发送动作，并阻止当前进程发起任何后续交易广播。
- **禁止静默重试与换号重发**：程序绝不清空 Nonce 后立即换用新 Nonce 重发同一业务动作，主循环亦不会在下一轮轮询中悄悄重试，杜绝并发双花与状态破坏风险。
- **锁定状态不可自动解除**：人工核对锁定为单向熔断状态，不实现未经验证的自动交易搜索与盲目恢复系统。

### 3.3 重启风险与人工核对规范
- **重启程序不是核对证明**：直接重启 Keeper 进程绝不是交易已被正确核实确认的证明。未做链上核对前重启，极易引发 Nonce 冲突与重复交易。
- **人工恢复前四项核对要点**：
  1. **签名者公开地址与目标合约**：核对警报中输出的 Signer Address 与 Target Contract 校验和地址，确保环境与配置完全匹配。
  2. **账户 Nonce 状态**：通过区块链浏览器或独立只读节点分别查询该地址的最新已确认 Nonce (latest) 与包含待确认交易的 Nonce (pending)。
  3. **在途交易与回执核对**：若存在可用的交易 Hash，查询其回执状态（status=1 确认成功，status=0 确认失败）；若无 Hash，检索内存池中该 Nonce 是否存在未打包交易。
  4. **链上业务状态**：直接查询 Miner 合约的公开只读方法（如 epoch()、lastSynced()、epochSeed(e) 等），核实当前轮次的业务动作是否已实际在链上生效。
  只有在人工确认链上交易已最终定论且业务状态清晰后，方可恢复 Keeper 的自动运维流程。

## 4. 验证范围声明

本任务仅验证 Keeper 对 Foundry Keystore 的离线参数解析、身份推导流程、子进程安全隔离及错误脱敏机制，所有自动化测试均使用 mock/fake cast 离线执行。
本文档及测试通过**不代表 Keeper 已经过链上真实环境的端到端认证**。在正式投入生产前，运维人员应在测试网隔离环境中使用测试账户与测试密码文件完成实盘验收。
