# SparkHash (SPKH)

SparkHash 是部署在 **BNB Smart Chain 主网（BSC，Chain ID 56）** 的开源工作量证明挖矿协议。矿工质押原生 BNB，用 CPU 或 GPU 在本地寻找有效的 PoW share，再由合约按统一规则验证份额、结算轮次并分配 SPKH。

社区矿工可以通过命令行挖矿，也可以使用 **SparkHash Agent Skill + 币安 Agentic Wallet**。推荐本地 GPU/多核离线求解（不接触私钥，仅产出 k/nonce/calldata），由币安 AI 复算哈希校验，钱包负责 preview 预览和签名，用户逐笔确认。

当前为 **BSC 主网矿工预览版**。项目方尚未提供官方交易池；SPKH 奖励不承诺兑换、回购或收益。2026-11-21 的传统 CLI 与 Agentic Wallet 两条实盘路径、已复核的提交和领奖回执及仍待验证的本金赎回见[实盘验收报告](reports/07-mainnet-miner-preview-verification.md)。[供应与主网预览披露](docs/DISCLOSURE.md)区分初始预分配、已铸造、项目控制、团队归属、矿工已领取与经确认的独立公众流通。

## 快速开始 · Quick Start

- [传统矿工主网接入指南](docs/MAINNET_MINER_GUIDE.md) — 准备 BNB、质押激活、一键启动 CPU/GPU 挖矿、提交份额、领取奖励及赎回。

### AI Agent 入口 · Agent Skill

**从仓库根目录打开 [`skills/sparkhash-agentic-miner/SKILL.md`](skills/sparkhash-agentic-miner/SKILL.md)**。将整个 `skills/sparkhash-agentic-miner/` 目录加载到支持 Skills 的客户端，保留脚本和文档的目录结构，再显式调用 `sparkhash-agentic-miner`。

- **本地只读求解器** [`skills/sparkhash-agentic-miner/solver.py`](skills/sparkhash-agentic-miner/solver.py)
- **币安 Agentic Wallet 自动挖矿教程** [`skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md`](skills/sparkhash-agentic-miner/AGENTIC_WALLET_MINING_GUIDE.md)。这是从仓库根目录读取的完整路径；请勿到根目录或顶层 `docs/` 下查找同名文件。

## 主网合约 · Mainnet Contracts

以下是 BSC 主网的 7 个核心合约。挖矿任务、份额提交和领奖使用 **Miner Proxy**。质押、申请退出和赎回使用 **StakeVault**。

| 合约 | BSC 主网地址 | 源码与链上验证 |
| --- | --- | --- |
| **Token（SPKH）** | `0xcce53217a4f2850c536d751cfb676da26bd372dc` | [BscScan 源码](https://bscscan.com/address/0xcce53217a4f2850c536d751cfb676da26bd372dc#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0xcce53217a4f2850c536d751cfb676da26bd372dc) |
| **Miner Proxy** | `0x614b5efd21cfa99cb893abf2fb02d791b3aea843` | [BscScan 源码](https://bscscan.com/address/0x614b5efd21cfa99cb893abf2fb02d791b3aea843#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0x614b5efd21cfa99cb893abf2fb02d791b3aea843) |
| **StakeVault** | `0x45eb4e8a7a3070c8160e9a91fae002934d296591` | [BscScan 源码](https://bscscan.com/address/0x45eb4e8a7a3070c8160e9a91fae002934d296591#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0x45eb4e8a7a3070c8160e9a91fae002934d296591) |
| **TeamVestingWallet** | `0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e` | [BscScan 源码](https://bscscan.com/address/0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0xf2c7f9a49a516ad1aeeb3782eaf6f7867827094e) |
| MinerV1 Implementation | `0xc74756ad1ca5ea01f46b81cc86f6068e76467457` | [BscScan 源码](https://bscscan.com/address/0xc74756ad1ca5ea01f46b81cc86f6068e76467457#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0xc74756ad1ca5ea01f46b81cc86f6068e76467457) |
| ProxyAdmin | `0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f` | [BscScan 源码](https://bscscan.com/address/0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0x9f129bf3dd1a8c7761a2bf36f639f1db7aa8f07f) |
| TimelockController | `0x565f007310ee7d28aa526a7a94df7b95d554be00` | [BscScan 源码](https://bscscan.com/address/0x565f007310ee7d28aa526a7a94df7b95d554be00#code) · [Sourcify](https://sourcify.dev/server/v2/contract/56/0x565f007310ee7d28aa526a7a94df7b95d554be00) |

上述 7 个合约的创建与运行时字节码均在 Sourcify 显示 `exact_match`。BscScan 链接直达各地址的源码页，绿勾状态以页面显示为准。部署交易和区块见[主网部署记录](reports/06-mainnet-deployment-record.md)。

## 安全与透明 · Security & Transparency

- **链上 PoW。** 每个 Epoch 的种子和难度可查。合约校验当前轮次、矿工地址与 PoW，按链上规则累计份额和发放奖励。
- **供应与团队锁仓。** SPKH 总量上限 10 亿枚，矿工发行上限 9 亿枚，初始铸造 1 亿枚：流动性 Safe 5,000 万、另一 Safe 2,000 万、团队归属合约 3,000 万。团队份额在部署时直接铸入 TeamVestingWallet，设有 180 天 cliff，按 720 天总归属期线性释放。另一 Safe 的实际用途和现实归属待确认；项目 Safe 和实测钱包余额不直接算作独立公众流通。[供应披露及区块快照](docs/DISCLOSURE.md) · [Token 源码](src/Token.sol) · [锁仓源码](src/TeamVestingWallet.sol)
- **时间锁与 Safe 治理。** TimelockController 持有 ProxyAdmin 所有权以及 Miner 和 StakeVault 的管理员权限。治理提案由 Proposer Safe（链上门槛 2/3）发起，时间锁最短延迟为 48 小时。独立 Guardian Safe 的紧急权限限于暂停新质押与新份额提交，当前门槛为 1/2。解除暂停、修改治理参数和升级 Miner 走时间锁。[治理披露](docs/DISCLOSURE.md) · [Proposer Safe](https://bscscan.com/address/0x93C38CCB01e223752FF25c066bAE732c38d2d89d) · [Guardian Safe](https://bscscan.com/address/0x4bfB20aA015707832a5f7000fa272d3547eaD6E3)
- **独立复核。** 源码验证核对公开源码与部署字节码的对应关系。合约行为与治理边界可结合源码、链上角色及[审计准备资料](docs/AUDIT_PREPARATION.md)复核。
