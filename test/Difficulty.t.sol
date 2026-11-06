// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract DifficultyTest is Test {
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

    uint256 public constant EASIEST = uint256(1) << 224;
    uint256 public constant HARDEST = uint256(1) << 221;

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

    function _setTotalShares(uint256 epoch, uint256 shares) internal {
        bytes32 slot = keccak256(abi.encode(epoch, uint256(11)));
        vm.store(address(miner), slot, bytes32(shares));
    }

    function test_DifficultyBoundsClampedAtEasiestAndHardest() public {
        // Zero shares repeatedly should never exceed EASIEST (32 bits)
        vm.warp(start + 20 * 3600);
        miner.sync(20);

        for (uint256 i = 0; i <= 20; i++) {
            uint256 t = miner.target(i);
            assertTrue(t <= EASIEST, "Target exceeded EASIEST bound");
            assertTrue(t >= HARDEST, "Target fell below HARDEST bound");
        }
        assertEq(miner.target(20), EASIEST);

        // Extreme massive shares should never fall below HARDEST (35 bits)
        for (uint256 i = 19; i <= 35; i++) {
            _setTotalShares(i, 10_000_000);
        }

        vm.warp(start + 35 * 3600);
        miner.sync(15);

        uint256 t35 = miner.target(35);
        assertEq(t35, HARDEST, "Target should clamp at HARDEST");
    }

    function test_MaxAdjustment25PercentLimit() public {
        uint256 mid = (EASIEST + HARDEST) / 2;
        bytes32 slot10 = keccak256(abi.encode(uint256(10), uint256(10)));
        vm.store(address(miner), slot10, bytes32(mid));

        bytes32 lastSyncedSlot = bytes32(uint256(7));
        vm.store(address(miner), lastSyncedSlot, bytes32(uint256(10)));

        vm.warp(start + 11 * 3600);
        miner.sync(1);

        uint256 t11 = miner.target(11);
        assertEq(t11, mid * 125 / 100);

        _setTotalShares(10, 10_000_000);
        vm.warp(start + 12 * 3600);
        miner.sync(1);

        uint256 t12 = miner.target(12);
        assertEq(t12, t11 * 100 / 125);
    }

    function test_EMASmoothingAndLagByOneEpoch() public {
        _setTotalShares(0, 100_000);

        vm.warp(start + 2 * 3600);
        miner.sync(2);

        // For n=1: shares = S_STAR = 50_000. ema = (50000 + 50000)/2 = 50000
        // For n=2: shares = totalShares[0] = 100_000. ema = (50000 + 100000)/2 = 75000
        assertEq(miner.ema(), 75000);
    }

    function test_Convergence11EpochsUnder10xHashrateSurge() public {
        // Exact reproduction of the architect's 10x hashrate surge scenario:
        // Baseline: S_STAR = 50,000. Target starts at EASIEST = 1 << 224.
        // Epoch 0: normal (50,000 shares)
        // Epoch 1: normal (50,000 shares)
        // From Epoch 2 onward: 10x hashrate surge (500,000 shares in each epoch)
        _setTotalShares(0, 50_000);
        _setTotalShares(1, 50_000);
        for (uint256 e = 2; e <= 15; e++) {
            _setTotalShares(e, 500_000); // 10x surge
        }

        for (uint256 e = 1; e <= 15; e++) {
            vm.warp(start + e * 3600);
            miner.sync(1);
        }

        assertEq(miner.target(0), EASIEST);
        assertEq(miner.target(1), EASIEST);
        assertEq(miner.target(2), EASIEST);
        assertEq(miner.target(3), EASIEST);

        // Epoch 4: first epoch using totalShares[2] = 500,000! Drops by factor 100/125 = 0.8
        uint256 t3 = miner.target(3);
        uint256 t4 = miner.target(4);
        assertEq(t4, t3 * 100 / 125);

        // Subsequent epochs each drop by factor 0.8
        uint256 t5 = miner.target(5);
        assertEq(t5, t4 * 100 / 125);

        uint256 t6 = miner.target(6);
        assertEq(t6, t5 * 100 / 125);

        uint256 t7 = miner.target(7);
        assertEq(t7, t6 * 100 / 125);

        uint256 t8 = miner.target(8);
        assertEq(t8, t7 * 100 / 125);

        uint256 t9 = miner.target(9);
        assertEq(t9, t8 * 100 / 125);

        uint256 t10 = miner.target(10);
        assertEq(t10, t9 * 100 / 125);

        uint256 t11 = miner.target(11);
        assertEq(t11, t10 * 100 / 125);

        uint256 t12 = miner.target(12);
        assertEq(t12, t11 * 100 / 125);

        // Epoch 13: 10th drop of 0.8 would reach 0.1074 * EASIEST, so it clamps to HARDEST (0.125 * EASIEST)!
        // Across 14 total rounds (Epoch 0 through 13), convergence to HARDEST is complete.
        uint256 t13 = miner.target(13);
        assertEq(t13, HARDEST, "Epoch 13 (14th round) must hit HARDEST");

        // Epoch 14..15: remains locked at HARDEST
        assertEq(miner.target(14), HARDEST);
        assertEq(miner.target(15), HARDEST);
    }
}
