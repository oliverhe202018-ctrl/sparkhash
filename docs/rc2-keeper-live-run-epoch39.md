# RC2 测试网 keeper 实盘验收记录(2026-11-10)

## 1. 范围
- 链:BSC testnet(chain id 97)。
- Miner 代理:0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf。
- 签名账户:Foundry keystore miner-smoke(公开地址 0x2e9e19f7F1D3a2b01ba5BbA7c37E58398568d82c)。
- 运行方式:keeper 单次巡检(--once),使用 --no-env 与显式 RPC。

## 2. 运行前状态
- 当前 epoch 39,lastSynced 23,epoch 39 种子未初始化。
- 待初始化种子:epoch 23 到 39,共 17 个。

## 3. 链上动作
| 动作 | nonce | 交易哈希 | 结果 |
|---|---|---|---|
| initEpochSeed(39, 24) | 10 | 0x0bde6b2e4876e6a64d3e4baa7cfe2453e05a104c28187f82c1a4d2c04d67774e | 区块 141841554 确认,gas 453004 |
| sync(16) 首次广播 | 11 | 0x7652b2959df6a89bdbb76006b0262428456169d7e0906f16d4e8188b88cda54c | 60 秒内未确认,随后被替换,链上无回执 |
| sync(16) 同 nonce 加价替换 | 11 | 0x543e658543006d74ae939f8a367b72ed7d61591ffe19786585ff07a267600f3f | gas 价 0.1826 gwei,区块 141841716 确认,gas 1999618 |

- 一笔 initEpochSeed 就初始化了 17 个 epoch;旧的逐 epoch 方式需要 17 笔。
- 超时后只提高了 gas 价格,nonce、目标合约、calldata 均未变。

## 4. 运行后独立核对
- lastSynced 为 39;epoch 39 种子已初始化。
- remainingEmission 保持 899972128170384997645791874,与运行前一致。
- epoch 23 至 38 均为空轮:每轮 settledEpochQuota 为 123871340926111379439856,unallocatedGap 等于整轮配额,totalWeight 为 0。
- 被替换交易无回执,替换交易回执成功。
- 签名账户 latest nonce 与 pending nonce 均为 12,无遗留待处理交易。

## 5. 已知边界
- 只覆盖单个测试账户和测试网。
- 尚未在真实网络中触发"结果不确定"的人工核对锁。
- 不代表主网、多账户并发或安全审计通过。