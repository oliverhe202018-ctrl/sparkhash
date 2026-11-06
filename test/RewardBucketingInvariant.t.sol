// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract RewardBucketingInvariantTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

    struct Buckets {
        uint256 B1;
        uint256 B2;
        uint256 B3;
        uint256 B4;
        uint256 dust;
    }

    struct EpochParams {
        uint256 effQ;
        uint256 wm;
        uint256 tw;
        uint256 denom;
        uint256 maxAmt;
        uint256 numMiners;
    }

    function setUp() public {
        start = block.timestamp + 1000;

        token = new Token(liquidity, team, community);
        vault = new StakeVault(DEPOSIT, start, admin, guardian, 0.001 ether, 10 ether, 24, 2);

        MinerV1 impl = new MinerV1();
        bytes memory initData = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(token),
            address(vault),
            admin,
            guardian,
            start
        );
        TransparentUpgradeableProxy proxy = new TransparentUpgradeableProxy(
            address(impl),
            admin,
            initData
        );
        miner = MinerV1(address(proxy));
        token.setMiner(address(miner));
    }

    function _setWeights(uint256 e, address user, uint256 uw) internal {
        bytes32 slotUW = keccak256(abi.encode(user, keccak256(abi.encode(e, uint256(15)))));
        vm.store(address(miner), slotUW, bytes32(uw));
    }

    function _setTotalWeight(uint256 e, uint256 tw) internal {
        bytes32 slotTW = keccak256(abi.encode(e, uint256(12)));
        vm.store(address(miner), slotTW, bytes32(tw));
    }

    function _runSingleEpoch(uint256 e, bytes32 rand) internal {
        EpochParams memory p;
        p.effQ = miner.effectiveQuota(e);
        if (p.effQ == 0) return;
        p.wm = miner.wMin(e);
        p.numMiners = 2 + (uint256(rand) % 7);

        address[] memory miners = new address[](p.numMiners);
        uint256[] memory weights = new uint256[](p.numMiners);
        p.tw = 0;

        bool lowWeight = (uint256(keccak256(abi.encode(rand, "low"))) % 10) < 4;

        for (uint256 i = 0; i < p.numMiners; i++) {
            miners[i] = address(uint160(0x1000 + e * 100 + i));
            uint256 baseW = (uint256(keccak256(abi.encode(rand, i))) % (p.wm / 2)) + 1;
            if (i == 0 && (uint256(keccak256(abi.encode(rand, "whale"))) % 2 == 0)) {
                baseW = p.wm * 2;
            }
            weights[i] = baseW;
            p.tw += baseW;
        }

        if (lowWeight && p.tw >= p.wm) {
            p.tw = 0;
            for (uint256 i = 0; i < p.numMiners; i++) {
                weights[i] = (weights[i] % (p.wm / p.numMiners)) + 1;
                p.tw += weights[i];
            }
        }

        _setTotalWeight(e, p.tw);
        for (uint256 i = 0; i < p.numMiners; i++) {
            _setWeights(e, miners[i], weights[i]);
        }

        vm.warp(start + (e + 1) * 3600 + 1);
        miner.sync(1);

        Buckets memory b;
        b.B3 = miner.unallocatedGap(e);

        uint256 expectedB3 = 0;
        if (p.tw < p.wm) {
            uint256 alloc = (p.effQ * p.tw) / p.wm;
            expectedB3 = p.effQ - alloc;
        }
        assertEq(b.B3, expectedB3, "B3 formula mismatch");

        p.denom = p.tw > p.wm ? p.tw : p.wm;
        uint256 capBps = miner.epochAddrCapBps(e);
        if (capBps == 0) capBps = 2000;
        p.maxAmt = (p.effQ * capBps) / 10000;

        uint256[] memory entitled = new uint256[](p.numMiners);

        for (uint256 i = 0; i < p.numMiners; i++) {
            uint256 rawAmt = (p.effQ * weights[i]) / p.denom;
            if (rawAmt > p.maxAmt) {
                b.B4 += (rawAmt - p.maxAmt);
                entitled[i] = p.maxAmt;
            } else {
                entitled[i] = rawAmt;
            }
        }

        // First wave of claims (even indices)
        for (uint256 i = 0; i < p.numMiners; i++) {
            if (i % 2 == 0) {
                uint256 bal0 = token.balanceOf(miners[i]);
                vm.prank(miners[i]);
                miner.claim(e);
                uint256 amt = token.balanceOf(miners[i]) - bal0;
                assertEq(amt, entitled[i]);
                b.B1 += amt;
            } else {
                b.B2 += entitled[i];
            }
        }

        // Conservation assertions
        assertLe(b.B1, p.effQ, "Minted total must not exceed quota");
        assertLe(b.B1 + b.B2, p.effQ, "Minted + entitled must not exceed quota");

        uint256 sumBuckets = b.B1 + b.B2 + b.B3 + b.B4;
        assertLe(sumBuckets, p.effQ, "Bucket sum cannot exceed quota");
        b.dust = p.effQ - sumBuckets;

        assertLe(b.dust, p.numMiners, "Dust must not exceed number of miners in wei");
        assertEq(b.B1 + b.B2 + b.B3 + b.B4 + b.dust, p.effQ, "Exact 4-bucket conservation holds");

        // Second wave of claims (odd indices)
        for (uint256 i = 0; i < p.numMiners; i++) {
            if (i % 2 != 0) {
                uint256 bal0 = token.balanceOf(miners[i]);
                vm.prank(miners[i]);
                miner.claim(e);
                uint256 amt = token.balanceOf(miners[i]) - bal0;
                assertEq(amt, entitled[i]);
                b.B1 += amt;
                b.B2 -= amt;
            }
        }

        assertEq(b.B2, 0, "All entitled rewards claimed");
        assertEq(b.B1 + b.B3 + b.B4 + b.dust, p.effQ, "Final conservation after all claims");
    }

    /// @notice Invariant test across 100 random epochs
    function test_RewardBucketingConservation100Epochs() public {
        for (uint256 e = 0; e < 100; e++) {
            bytes32 rand = keccak256(abi.encode("seed_epoch_test", e));
            _runSingleEpoch(e, rand);
        }
    }

    event EpochSettled(
        uint256 indexed epoch,
        uint256 effectiveQuota,
        uint256 totalWeight,
        uint256 wMin,
        uint256 unallocatedDilution
    );
    event RewardClaimed(
        address indexed miner,
        uint256 indexed epoch,
        uint256 claimedAmount,
        uint256 cappedOverflow
    );

    /// @notice Verify that sync emits EpochSettled and claim emits RewardClaimed with exact values
    function test_EpochSettledAndRewardClaimedEvents() public {
        uint256 e = 1;
        uint256 effQ = miner.effectiveQuota(e);
        uint256 wm = miner.wMin(e);

        address miner1 = address(0x999);
        address miner2 = address(0x888);

        // miner1: 2000 weight, miner2: 1000 weight -> tw = 3000
        // Set wm higher than tw so dilution gap B3 > 0
        uint256 tw = wm / 2;
        uint256 w1 = tw * 2 / 3;
        uint256 w2 = tw - w1;

        _setWeights(e, miner1, w1);
        _setWeights(e, miner2, w2);
        _setTotalWeight(e, tw);

        // First sync epoch 0
        vm.warp(start + 3600 + 1);
        miner.sync(1);

        // Now advance to epoch 2 so epoch 1 is ended
        vm.warp(start + 2 * 3600 + 1);

        uint256 expectedB3 = effQ - (effQ * tw) / wm;

        // Expect EpochSettled event on sync of epoch 1
        vm.expectEmit(true, false, false, true);
        emit EpochSettled(e, effQ, tw, wm, expectedB3);
        miner.sync(1);

        assertEq(miner.unallocatedGap(e), expectedB3);

        // Expect RewardClaimed on claim
        uint256 denom = wm;
        uint256 raw1 = (effQ * w1) / denom;
        uint256 max1 = (effQ * 2000) / 10000;
        uint256 amt1 = raw1 > max1 ? max1 : raw1;
        uint256 overflow1 = raw1 > max1 ? raw1 - max1 : 0;

        vm.expectEmit(true, true, false, true);
        emit RewardClaimed(miner1, e, amt1, overflow1);
        vm.prank(miner1);
        miner.claim(e);
    }
}
