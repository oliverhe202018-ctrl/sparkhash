# RC2 上线前离线补证

基线为 feature/progress-emission，HEAD f7bcef8ffa3f9da74c9980dccb0244c6698c8709。开始时无已跟踪变更。新增两个测试、两个只含公开 runtime 的 fixture 和本报告。原有未跟踪文件保持原位，未暂存。

## 新增证据

### Safe L2 v1.3.0

从 BSC testnet chainId 97 固定区块 142479802、块哈希 0x0fce88fdc0b16c5155ae87119ef840a414e96e19fef650843be1f576ac8d375d 读取公开代码。测试在读取 fixture 后重新计算 Keccak-256 并严格断言。

- Safe L2 singleton 0xe2cf742b554f466d5e7a37c371fd47c786d2fbc0，runtime hash 0x21842597390c4c6e3c1239e434a682b054bd9548eee5e9b1d6a4482731023c0f，匹配 [Safe 官方发布记录](https://github.com/safe-global/safe-deployments/blob/main/src/assets/v1.3.0/gnosis_safe_l2.json)。
- Safe proxy runtime 取自 0xE5fE521AfD41f1aF00bB623CE5511a8412a68b47，hash 0xb89c1b3bdf2cf8827818646bce9a8f6e372885f8c55e5c07acbd307cb133b000。
- 本地测试用 vm.etch 加载两个 runtime，只写代理构造器本应设置的 singleton 指针槽，再通过真实 setup 初始化 2/3。使用三个合成地址及 vm.prank 调用真实 approveHash，签名数据为按 owner 升序拼接的类型 1 记录。全程无私钥或 ECDSA 签名。执行者不是 owner，因此不能借助 Safe 的发送者隐式批准分支。使用生产依赖中的 TimelockController 和本地计数目标。
- 单一签名记录触发 GS020，两个记录中只有一份已批准触发 GS025。错误 hash 和旧 nonce 批准亦触发 GS025。外层回退保持 Safe nonce、操作状态、timestamp 和目标计数，且无执行事件。
- 取消后状态 Unset、timestamp=0。Done 重复执行保持 timestamp=1 和目标计数 1。Waiting 提前执行保持 timestamp=1600、目标计数 0。三者直接调用均断言带操作 ID 和 Ready 位图的 Timelock 错误。
- Waiting 经 Safe 执行时，零 safeTxGas/零 gasPrice 导致 GS013 外层回退，nonce 保持 1。显式 safeTxGas=200000 且零 gasPrice 时，Safe 返回 false、nonce 1→2，发出 ExecutionFailure，数据中的 hash 匹配、payment=0。Timelock 和目标状态均不变。正向铺垫断言 Safe ExecutionSuccess、Timelock 排期/取消/执行事件及相关参数。
- 范围为固定 runtime 下的离线批准和执行语义。生产 Safe owner 密钥、生产配置和 ECDSA 路径未复测。未启用模块、guard 或 fallback，未依赖链上状态分叉。

### 20% 正常提交路径

本地部署生产 Token、Vault、Miner 实现和透明代理，通过真实 deposit、initEpochSeed、sync、submitShares、claim 生成状态。vm.deal 只给合成账户提供押金，vm.warp 控制 epoch，vm.prank 指定调用者。为把 PoW 搜索限制在离线测试可行规模，仅将 epoch 25 的 target 设置为 1<<248。nonce 仍按生产哈希验证公式搜索，通过真实 submitShares 验证。未写入 userWeight、totalWeight、usedMask、counted 或结算状态。

满 ramp 账户的 6、5、1 份权重分别为 24494890000、22360670000、10000000000。固定观测账户为六份，三种总权重均高于 W_min=100000000000，领取分母均为总权重。

- 下：五个六份账户加一个一份账户，总权重 132474450000，观测账户 rawReward 小于 cap。
- 等：五个六份账户，总权重 122474450000，精确比例为 1/5，向下取整后的 rawReward 与 cap 也相等。
- 上：四个六份账户加一个五份账户，总权重 120340230000，观测账户 rawReward 大于 cap。

三点均断言提交计数与位图、总权重、epoch 快照、实际扣池、结算额度、每笔 RewardClaimed 事件、取整和 overflow 回流、totalMined、premint 与总量关系、剩余发行量、dust 和精确守恒。重复领取回退且余额池与铸币数保持。此测试不验证原生产难度下的求解性能。既有注入权重边界、端到端单侧截断与稀疏权重守恒继续由回归提供。

## 实际测试

在仅复制 src、test、script、lib、foundry.toml、remappings.txt 的临时副本中，用 Solc 0.8.38 执行。未复制环境文件，测试无 fork、广播或 FFI。

~~~powershell
forge.exe test --offline --match-contract '^(SafeL2GovernanceNegativesTest|NormalSubmissionCapBoundaryTest)$'
forge.exe test --offline --match-contract '^(GovernancePermissionsTest|AllocationCapsTest|CapAndOverflowConservationTest|ProxyUpgradeTest|WithdrawalTest|TokenTest)$'
~~~

第一条为新增定向测试，Safe 7 项、正常提交 3 项，10 通过、0 失败、0 跳过。第二条为相关回归，GovernancePermissions 14、AllocationCaps 5、CapAndOverflowConservation 7、ProxyUpgrade 3、Withdrawal 4、Token 4，37 通过、0 失败、0 跳过。未跑全量。

## 已有证据与 Keeper 观测准备

只盘点已跟踪的 Epoch 39 报告、较早测试网报告、监控规范以及 Keeper/监控源码。未查现有 Keeper 或监控窗口、真实状态/审计文件、进程命令行及历史终端。可用启动记录是 Epoch 39 报告的单次 --once，账户与目前已知 Keeper 不同。源码具备 START/CONFIG/STATUS 日志，不能由此推定当前进程的实际启动参数或覆盖时段。

- UTC / 区块：Keeper 文本时间用本机 localtime，未附 UTC 偏移。监控脚本输出 latest_block，但无统一采样 UTC。Epoch 39 报告的两笔成功交易区块 141841554 和 141841716，本轮只读查得分别为 2026-11-10 02:59:40 UTC 和 03:00:53 UTC。相距 73 秒，仅为两个上链证据点。
- epoch、lastSynced、seed：Keeper STATUS 和 monitor 输出具备这些字段或可由输出关联。Epoch 39 记录从 lastSynced 23、当前 seed 未准备，到同步至 39、种子已初始化。
- 钱包 latest/pending nonce、待确认交易：历史报告有结束时两 nonce 均为 12、首笔无回执及同 nonce 替换成功。监控脚本未提供钱包级的持续 nonce/待确认队列采样。
- gas：历史报告给出两笔已确认交易的 gas used 和替换报价。Keeper 具备费用/交易诊断，但没有本轮可用的连续费用轨迹。余额：现有查阅材料未提供连续 Keeper 钱包余额。
- 告警：Keeper 有 ALERT 输出，monitor 有分级告警摘要。未看到当前 Keeper 连续运行期的告警存档或明确的告警缺失记录。早期报告约九小时未运行是停机事件，不能作 RC2 连续观测证据。

最小采集方案是由现有只读观测链路定时记录 UTC、链 ID、固定区块号/哈希、epoch、lastSynced、seedReady、公开钱包 latest/pending nonce 和余额。对已知交易 hash 附交易身份、receipt 状态、gasUsed、effectiveGasPrice 及费用，保留错误、告警和采样间隙。至少跨多个 epoch，记录起止 UTC、采样间隔和覆盖率。此次未启动采集。

## 发现的问题与剩余证据

- 未发现需要修改生产代码的回归。Keeper 本机时间不带时区，现有证据不足以验收无人值守。最小补证是现有只读观测链路提供显式 UTC 和上述连续样本。
- Safe 负例验证的是真实 runtime 的本地调用语义。真实链签名流程、链上治理负例和 Keeper 进程故障均未执行。
- Git 已跟踪 diff --stat 为空。新增文件保持未跟踪。原有未跟踪条目未删除、未暂存。
