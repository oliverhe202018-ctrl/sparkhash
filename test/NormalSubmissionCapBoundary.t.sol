// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract NormalSubmissionCapBoundaryTest is Test {
    uint256 private constant EPOCH = 25;
    uint256 private constant DEPOSIT = 0.01 ether;
    uint256 private constant WEIGHT_SIX = 24494890000;
    uint256 private constant WEIGHT_FIVE = 22360670000;
    uint256 private constant WEIGHT_ONE = 10000000000;
    uint256 private constant TEST_TARGET = 1 << 248;
    uint256 private constant TARGET_MAPPING_SLOT = 10;

    Token private token;
    MinerV1 private miner;
    uint256 private start;
    address[6] private miners;

    event RewardClaimed(address indexed miner, uint256 indexed epoch, uint256 claimedAmount, uint256 cappedOverflow);

    function setUp() public {
        start = block.timestamp + 1000;
        token = new Token(makeAddr("liquidity"), makeAddr("team"), makeAddr("community"));
        StakeVault vault = new StakeVault(DEPOSIT, start, address(this), address(this), 0.001 ether, 10 ether, 24, 2);
        MinerV1 implementation = new MinerV1();
        miner = MinerV1(address(new TransparentUpgradeableProxy(
            address(implementation), address(this),
            abi.encodeCall(MinerV1.initialize, (address(token), address(vault), address(this), address(this), start))
        )));
        token.setMiner(address(miner));
        vm.warp(start);
        for (uint256 i; i < miners.length; i++) {
            address user = address(uint160(0x1000 + i));
            miners[i] = user;
            vm.deal(user, DEPOSIT);
            vm.prank(user);
            vault.deposit{value: DEPOSIT}();
        }
        vm.warp(start + EPOCH * miner.EPOCH_LEN());
        miner.sync(48);
        miner.initEpochSeed(EPOCH, 24);
        miner.initEpochSeed(EPOCH, 24);
        assertNotEq(miner.epochSeed(EPOCH), 0);
        // Only loosen the PoW search target. All share, weight and settlement state is created by public calls.
        vm.store(address(miner), keccak256(abi.encode(EPOCH, TARGET_MAPPING_SLOT)), bytes32(TEST_TARGET));
        assertEq(miner.target(EPOCH), TEST_TARGET);
    }

    function test_NormalSharesBelowTwentyPercent() public {
        _exercise(6, true, -1);
    }

    function test_NormalSharesExactlyTwentyPercent() public {
        _exercise(6, false, 0);
    }

    function test_NormalSharesAboveTwentyPercent() public {
        _exercise(5, false, 1);
    }

    function _submit(address user, uint8 count, uint256 expectedWeight) private {
        uint8[] memory indices = new uint8[](count);
        uint256[] memory nonces = new uint256[](count);
        for (uint8 i; i < count; i++) {
            indices[i] = i;
            bytes32 sub = keccak256(abi.encode(bytes32(miner.epochSeed(EPOCH)), user, uint256(i), address(miner), block.chainid));
            while (uint256(keccak256(abi.encode(sub, nonces[i]))) > TEST_TARGET) nonces[i]++;
        }
        vm.prank(user);
        miner.submitShares(indices, nonces);
        assertEq(miner.counted(EPOCH, user), count);
        assertEq(miner.usedMask(EPOCH, user), (1 << count) - 1);
        assertEq(miner.userWeight(EPOCH, user), expectedWeight);
    }

    function _exercise(uint8 fifthCount, bool extraMiner, int8 relation) private {
        for (uint256 i; i < 4; i++) _submit(miners[i], 6, WEIGHT_SIX);
        uint256 fifthWeight = fifthCount == 6 ? WEIGHT_SIX : WEIGHT_FIVE;
        _submit(miners[4], fifthCount, fifthWeight);
        if (extraMiner) _submit(miners[5], 1, WEIGHT_ONE);

        uint256 total = 4 * WEIGHT_SIX + fifthWeight + (extraMiner ? WEIGHT_ONE : 0);
        assertEq(miner.totalWeight(EPOCH), total);
        assertEq(miner.totalShares(EPOCH), 24 + uint256(fifthCount) + (extraMiner ? 1 : 0));
        assertGt(total, miner.wMin(EPOCH));
        assertEq(miner.epochAddrCapBps(EPOCH), 2000);

        uint256 beforeSettlement = miner.remainingEmission();
        vm.warp(start + (EPOCH + 1) * miner.EPOCH_LEN());
        miner.sync(1);
        assertEq(miner.lastSynced(), EPOCH + 1);
        uint256 quota = beforeSettlement * miner.LAMBDA_WAD() / 1e18;
        assertEq(miner.settledEpochQuota(EPOCH), quota);
        assertEq(miner.unallocatedGap(EPOCH), 0);
        assertEq(miner.remainingEmission(), beforeSettlement - quota);

        uint256 cap = quota * 2000 / 10000;
        uint256 focalRaw = quota * WEIGHT_SIX / total;
        if (relation < 0) assertLt(focalRaw, cap);
        else if (relation == 0) assertEq(focalRaw, cap);
        else assertGt(focalRaw, cap);
        _claimAndReconcile(quota, cap, total, beforeSettlement, extraMiner ? 6 : 5);
    }

    function _claimAndReconcile(uint256 quota, uint256 cap, uint256 total, uint256 initialEmission, uint256 participants)
        private
    {
        uint256 rawTotal;
        uint256 minted;
        uint256 recycled;
        for (uint256 i; i < participants; i++) {
            address user = miners[i];
            uint256 raw = quota * miner.userWeight(EPOCH, user) / total;
            uint256 amount = raw > cap ? cap : raw;
            uint256 overflow = raw - amount;
            uint256 remainingBeforeClaim = miner.remainingEmission();
            vm.expectEmit(true, true, false, true, address(miner));
            emit RewardClaimed(user, EPOCH, amount, overflow);
            vm.prank(user);
            miner.claim(EPOCH);
            assertEq(token.balanceOf(user), amount);
            assertEq(miner.remainingEmission(), remainingBeforeClaim + overflow);
            rawTotal += raw;
            minted += amount;
            recycled += overflow;
        }

        uint256 dust = quota - rawTotal;
        assertLt(dust, participants);
        assertEq(minted + recycled + dust, quota);
        assertEq(token.totalMined(), minted);
        assertEq(token.totalSupply(), token.PREMINT_TOTAL() + minted);
        assertEq(miner.remainingEmission(), initialEmission - quota + recycled);
        assertEq(token.totalMined() + miner.remainingEmission() + dust, initialEmission);
        assertEq(miner.settledEpochQuota(EPOCH), quota);

        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        vm.prank(miners[0]);
        miner.claim(EPOCH);
        assertEq(token.totalMined(), minted);
        assertEq(miner.remainingEmission(), initialEmission - quota + recycled);
    }
}
