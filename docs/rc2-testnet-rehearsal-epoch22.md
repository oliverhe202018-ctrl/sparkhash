# RC2 测试网端到端验收结果 (Epoch 22)

## 1. 部署地址与参数
- Timelock: 0x038f2377A32581Dc3aa8d9f18386619cf72C832e
- Token: 0x4ca603774D8D3750481007683ce44C7d4c9b2C8d
- StakeVault: 0x40b92431de886Ea46117407c8f7123a31FB3F744
- MinerV1 实现: 0x16cB6B01330dAdf5aA71165b686F16D375eE026F
- Miner 代理: 0x565D06e8d0313E2f8b5aa4b19DE569ca2200C4cf
- ProxyAdmin: 0xac6be887a777a11326e2b31efbd63e071c460c01
- START: 1794135600
- 部署基准提交: d34f1b80dcb8577b786badc4150f9bc771d5cdb5
- Timelock 延迟: 600 秒;链:BSC testnet(chain id 97)

## 2. 测试矿工
- miner-smoke 公开地址: 0x2e9e19f7F1D3a2b01ba5BbA7c37E58398568d82c

## 3. Epoch 22 流程与交易哈希
- 同步: 0x3e50361af07a6e6b502d072f5aef31b04be5ca8bfe048cfd750f7418ab10851f (推进同步,结算 epoch 21(空轮),区块: 141700741)
- 种子初始化: 0x9a63b43959eda7fe9abfac0551ae9f754d7a3619193f186ffa397fbfa1426730
- 提交: 0x7970e373371cabc8606820059c7822055c760eb8450dc9afc7b66a7def03145b
- 结算: 0x8d7c52d6a72966b1ccdb236befdfa51535fecc20491c1e3fcbe7012f44ed4d2b
- 领取: 0xbec84320515a57b6d96b91c35adb8f7d56ff0d829d29ceaddcd8c9dfd764c982

## 4. 数值核算 (原始整数)
- 配额: 123874750927604533540632
- 结算未分配: 96377938847202452024641
- 实际铸币: 24774950185520906708126
- 截断溢出: 2721861894881174807865
- 配额分解差额: 0
- 结算后剩余池: 899969406308490116470984009
- 领取后剩余池: 899972128170384997645791874

## 5. 历史对比结论
- 结算扣池等于可分配量，领取时溢出精确加回 remainingEmission，两步合计净扣池等于铸币。

## 6. 已知边界
- 本记录只覆盖单个测试地址；
- 不代表多地址、主网、keeper 自动化实盘或安全审计通过。

## 7. 操作经验
- 交易签名全部由人工 keystore 完成；
- 一次提交因等待超过轮次结束而被前置检查拦截，已在新一轮重新生成，没有广播过期证明。

## 8. epoch 21 空轮与全局守恒核对
- 区块 141700740 与 141700741 的 remainingEmission 均为 899996903120570518552500000,结算未扣池。
- epoch 21 的 totalWeight 与 totalShares 均为 0; settledEpochQuota[21] 与 unallocatedGap[21] 均为 123874750927604533540632。
- 全局守恒:MINING_CAP 减 remainingEmission 等于 totalSupply 减 1 亿预置量,等于矿工余额 27871829615002354208126。
- 当前 totalSupply:100027871829615002354208126。
