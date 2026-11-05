// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract MinerV1Test is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public alice = address(0x10);
    address public bob = address(0x20);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

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

        vm.deal(alice, 10 ether);
        vm.deal(bob, 10 ether);
    }

    function test_InitializeConstraints() public {
        MinerV1 impl2 = new MinerV1();
        bytes memory badInit = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(token),
            address(vault),
            admin,
            guardian,
            start + 1
        );
        vm.expectRevert(MinerV1.MismatchStart.selector);
        new TransparentUpgradeableProxy(address(impl2), admin, badInit);

        // Implementation cannot be initialized directly
        MinerV1 directImpl = new MinerV1();
        vm.expectRevert();
        directImpl.initialize(address(token), address(vault), admin, guardian, start);
    }

    function test_ClosedFormQuotaCurve() public view {
        assertEq(miner.dailyQuota(1), 3_000_000e18);
        assertEq(miner.epochQuota(0), 125_000e18);
        assertEq(miner.effectiveQuota(0), 125_000e18);

        // Day 1394 capping test:
        // Day 1394 start epoch = 1393 * 24 = 33432
        uint256 day1394Start = 1393 * 24;
        uint256 day1394Sum = 0;
        for (uint256 i = 0; i < 24; i++) {
            uint256 eff = miner.effectiveQuota(day1394Start + i);
            day1394Sum += eff;
        }

        // Expected Day 1394 remaining quota is approx 6079.967e18
        assertApproxEqAbs(day1394Sum, 6079967100000000000000, 1e18);

        // Sum of all mining quota up to end of day 1394 is exactly 900M
        uint256 totalBefore1394 = miner.cumulativeQuotaBefore(day1394Start);
        assertEq(totalBefore1394 + day1394Sum, 900_000_000e18);

        // Epochs in Day 1395 and beyond have strictly 0 quota
        assertEq(miner.effectiveQuota(1394 * 24), 0);
        assertEq(miner.effectiveQuota(1400 * 24), 0);
    }

    function test_SeedInitialization() public {
        vm.warp(start + 1);
        // Epoch 0 seed
        miner.initEpochSeed(0);
        uint256 seed0 = miner.epochSeed(0);
        assertTrue(seed0 != 0);

        // Cannot re-init
        vm.expectRevert(MinerV1.SeedAlreadyInitialized.selector);
        miner.initEpochSeed(0);

        // Future epoch reverts
        vm.expectRevert(MinerV1.FutureEpoch.selector);
        miner.initEpochSeed(1);

        // Warp to epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        uint256 seed1 = miner.epochSeed(1);
        assertTrue(seed1 != 0);
        assertTrue(seed1 != seed0);
    }

    function test_SyncCatchup() public {
        vm.warp(start + 1);
        miner.initEpochSeed(0);

        // Warp 10 epochs forward
        vm.warp(start + 10 * 3600);
        assertEq(miner.lastSynced(), 0);

        // Sync with maxSteps = 4
        miner.sync(4);
        assertEq(miner.lastSynced(), 4);

        // Sync remaining
        miner.sync(10);
        assertEq(miner.lastSynced(), 10);
        assertTrue(miner.target(10) > 0);
    }

    function setTargetForTest(uint256 e, uint256 t) internal {
        bytes32 slot = keccak256(abi.encode(e, uint256(10)));
        vm.store(address(miner), slot, bytes32(t));
    }
    function test_SubmitSharesAndWarmup() public {
        vm.warp(start + 1); // Epoch 0
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        miner.initEpochSeed(0);

        // Alice cannot submit in epoch 0 (since == 0, requires e >= since + 1)
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);

        vm.prank(alice);
        vm.expectRevert(MinerV1.NoActiveStake.selector);
        miner.submitShares(ks, nonces);

        // Move to Epoch 1 (first eligible epoch for Alice)
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1); // Ensure target(1) is synced

        // Find a valid nonce for Alice at k=0
        bytes32 es = bytes32(miner.epochSeed(1));
        bytes32 sub = keccak256(abi.encode(es, alice, uint256(0), address(miner), block.chainid));
        uint256 validNonce = 0;
        // Set 8-bit fast difficulty target (248 bits) for rapid test execution
        uint256 fastTarget = uint256(1) << 248;
        setTargetForTest(1, fastTarget);
        uint256 t = miner.target(1);
        assertEq(t, fastTarget);

        while (uint256(keccak256(abi.encode(sub, validNonce))) > t) {
            validNonce++;
        }
        nonces[0] = validNonce;

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // Weight verification: at age = 0 (e == since + 1), ramp is 2500 bps (25%)
        // w(1) = 1e6. add = 1e6 * 2500 = 2500000000 = 2.5e9
        assertEq(miner.userWeight(1, alice), 2_500_000_000);
        assertEq(miner.totalShares(1), 1);

        // Re-submitting same k reverts
        vm.prank(alice);
        vm.expectRevert(MinerV1.DuplicateShareIndex.selector);
        miner.submitShares(ks, nonces);
    }

    function test_ClaimAndCap() public {
        vm.warp(start + 1); // Epoch 0
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Warp to Epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        // Set 8-bit fast difficulty target (248 bits) for rapid test execution
        uint256 fastTarget = uint256(1) << 248;
        setTargetForTest(1, fastTarget);

        bytes32 es = bytes32(miner.epochSeed(1));
        bytes32 sub = keccak256(abi.encode(es, alice, uint256(0), address(miner), block.chainid));
        uint256 validNonce = 0;
        while (uint256(keccak256(abi.encode(sub, validNonce))) > miner.target(1)) {
            validNonce++;
        }
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = validNonce;

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // Cannot claim before epoch 1 finishes
        vm.prank(alice);
        vm.expectRevert(MinerV1.EpochNotEnded.selector);
        miner.claim(1);

        // Warp to Epoch 2 (Epoch 1 ended)
        vm.warp(start + 2 * 3600);

        uint256 balBefore = token.balanceOf(alice);
        vm.prank(alice);
        miner.claim(1);
        uint256 reward = token.balanceOf(alice) - balBefore;

        // Alice is only miner, so raw reward is epochQuota * userWeight / wMin.
        // But capped at ADDR_CAP_BPS = 20% of epochQuota.
        uint256 eq = miner.epochQuota(1);
        uint256 maxReward = (eq * 2000) / 10000;
        assertTrue(reward <= maxReward);

        // Duplicate claim reverts
        vm.prank(alice);
        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        miner.claim(1);
    }

    function test_AdminPauseAndParams() public {
        vm.prank(alice);
        vm.expectRevert(MinerV1.OnlyAdminOrGuardian.selector);
        miner.pauseSubmissions();

        vm.prank(admin);
        miner.pauseSubmissions();
        assertTrue(miner.paused());

        // Submitting while paused reverts
        vm.warp(start + 3600);
        uint8[] memory ks = new uint8[](1);
        uint256[] memory nonces = new uint256[](1);
        vm.prank(alice);
        vm.expectRevert(MinerV1.SubmissionsPausedError.selector);
        miner.submitShares(ks, nonces);

        // Parameter bounds
        vm.prank(admin);
        vm.expectRevert(MinerV1.ParameterOutOfBounds.selector);
        miner.setSStar(500); // Below 1,000

        vm.prank(admin);
        miner.setSStar(60_000);
        assertEq(miner.S_STAR(), 60_000);
    }
}
