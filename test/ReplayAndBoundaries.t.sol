// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract ReplayAndBoundariesTest is Test {
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

    event RewardClaimed(
        address indexed miner,
        uint256 indexed epoch,
        uint256 claimedAmount,
        uint256 cappedOverflow
    );

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

        // Deposit in epoch 0
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();
        vm.prank(bob);
        vault.deposit{value: DEPOSIT}();

        // Advance to epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        // Set fast target (8-bit)
        bytes32 slot = keccak256(abi.encode(uint256(1), uint256(10)));
        vm.store(address(miner), slot, bytes32(uint256(1) << 248));
    }

    function _mineNonce(address user, uint8 k) internal view returns (uint256) {
        bytes32 es = bytes32(miner.epochSeed(1));
        bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(miner), block.chainid));
        uint256 nonce = 0;
        uint256 t = miner.target(1);
        while (uint256(keccak256(abi.encode(sub, nonce))) > t) {
            nonce++;
        }
        return nonce;
    }

    function test_ReplayDuplicateKInSameBatchReverts() public {
        uint8[] memory ks = new uint8[](2);
        ks[0] = 3;
        ks[1] = 3; // duplicate k
        uint256[] memory nonces = new uint256[](2);
        nonces[0] = _mineNonce(alice, 3);
        nonces[1] = nonces[0];

        vm.prank(alice);
        vm.expectRevert(MinerV1.DuplicateShareIndex.selector);
        miner.submitShares(ks, nonces);
    }

    function test_ReplayDuplicateKAcrossTransactionsReverts() public {
        uint8[] memory ks1 = new uint8[](1);
        ks1[0] = 5;
        uint256[] memory nonces1 = new uint256[](1);
        nonces1[0] = _mineNonce(alice, 5);

        vm.prank(alice);
        miner.submitShares(ks1, nonces1);

        // Re-submit same k in second tx
        vm.prank(alice);
        vm.expectRevert(MinerV1.DuplicateShareIndex.selector);
        miner.submitShares(ks1, nonces1);
    }

    function test_RevertIfKGe6() public {
        uint8[] memory ks = new uint8[](1);
        ks[0] = 6; // k >= CAP (6)
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 123;

        vm.prank(alice);
        vm.expectRevert(MinerV1.InvalidShareIndex.selector);
        miner.submitShares(ks, nonces);
    }

    function test_ChangeMsgSenderInvalidatesNonce() public {
        uint256 aliceNonce = _mineNonce(alice, 0);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = aliceNonce;

        vm.prank(bob);
        vm.expectRevert(MinerV1.InvalidProof.selector);
        miner.submitShares(ks, nonces);
    }

    function test_EpochBoundarySubmissionsAndClaim() public {
        vm.warp(start + 3600);
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = _mineNonce(alice, 0);

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // Claim during ongoing epoch reverts
        vm.prank(alice);
        vm.expectRevert(MinerV1.EpochNotEnded.selector);
        miner.claim(1);

        // Advance to exact end of epoch 1 (start + 7200)
        vm.warp(start + 7200);
        vm.prank(alice);
        miner.claim(1);
        assertTrue(token.balanceOf(alice) > 0);
    }

    function test_SubmitAtExactEpochBoundaries() public {
        vm.warp(start + 3600);
        uint8[] memory ks1 = new uint8[](1);
        ks1[0] = 0;
        uint256[] memory nonces1 = new uint256[](1);
        nonces1[0] = _mineNonce(alice, 0);

        vm.prank(alice);
        miner.submitShares(ks1, nonces1);

        vm.warp(start + 7199);
        uint8[] memory ks2 = new uint8[](1);
        ks2[0] = 1;
        uint256[] memory nonces2 = new uint256[](1);
        nonces2[0] = _mineNonce(alice, 1);

        vm.prank(alice);
        miner.submitShares(ks2, nonces2);

        assertEq(miner.counted(1, alice), 2);
    }

    function test_ClaimWhenWeightBelowWMinHandCalc() public {
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = _mineNonce(alice, 0);

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        vm.warp(start + 7200);

        // Hand calculation:
        // Day 1 (epoch 1), dailyQuota = 3,000,000e18, epochQuota = 125,000e18
        // Alice c = 1, age = 0 (since=0, e=1), ramp = 2500 bps = 25%
        // w(1) = 1e6, userWeight = 1e6 * 2500 = 2.5e9
        // totalWeight = 2.5e9
        // wMin = 10 * WSCALE = 10 * 1e10 = 10^11
        // totalWeight (2.5e9) < wMin (1e11), denom = wMin = 1e11
        // rawReward = (125,000e18 * 2.5e9) / 1e11 = 3,125e18
        // 20% cap = 125,000e18 * 20% = 25,000e18
        // rawReward (3,125e18) <= cap (25,000e18), reward = 3,125e18
        uint256 balBefore = token.balanceOf(alice);
        vm.prank(alice);
        miner.claim(1);
        uint256 reward = token.balanceOf(alice) - balBefore;

        uint256 expectedReward = (miner.epochQuota(1) * 25) / 1000;
        assertEq(reward, expectedReward);
    }

    function test_ClaimZeroWeightReverts() public {
        vm.warp(start + 7200);
        vm.prank(alice);
        vm.expectRevert(MinerV1.ZeroWeight.selector);
        miner.claim(1);
    }

    function test_DuplicateClaimReverts() public {
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = _mineNonce(alice, 0);

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        vm.warp(start + 7200);
        vm.prank(alice);
        miner.claim(1);

        vm.prank(alice);
        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        miner.claim(1);
    }

    function test_ClaimWhenQuotaExhaustedReturnsZero() public {
        uint256 exhaustedEpoch = 1400 * 24;
        vm.warp(start + exhaustedEpoch * 3600);

        // Avoid looping 33,600 times by setting lastSynced directly in storage (slot 7)
        bytes32 lastSyncedSlot = bytes32(uint256(7));
        vm.store(address(miner), lastSyncedSlot, bytes32(exhaustedEpoch));

        // Under progress emission, quota is exhausted when remainingEmission is 0 (slot 21)
        vm.store(address(miner), bytes32(uint256(21)), bytes32(0));

        // Fast target for exhausted epoch
        bytes32 slot = keccak256(abi.encode(exhaustedEpoch, uint256(10)));
        vm.store(address(miner), slot, bytes32(uint256(1) << 248));

        // Directly store non-zero epoch seed for exhausted epoch
        bytes32 seedSlot = keccak256(abi.encode(exhaustedEpoch, uint256(9)));
        vm.store(address(miner), seedSlot, bytes32(uint256(0x12345678)));

        bytes32 es = bytes32(miner.epochSeed(exhaustedEpoch));
        bytes32 sub = keccak256(abi.encode(es, alice, uint256(0), address(miner), block.chainid));
        uint256 nonce = 0;
        while (uint256(keccak256(abi.encode(sub, nonce))) > uint256(1) << 248) {
            nonce++;
        }

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = nonce;

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        vm.warp(start + (exhaustedEpoch + 1) * 3600);

        uint256 balBefore = token.balanceOf(alice);

        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(alice, exhaustedEpoch, 0, 0);

        vm.prank(alice);
        miner.claim(exhaustedEpoch);
        uint256 claimedAmt = token.balanceOf(alice) - balBefore;

        assertEq(claimedAmt, 0);
    }
}
