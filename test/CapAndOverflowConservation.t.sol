// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

/// @title CapAndOverflowConservationTest
/// @notice Rigorous verification of single-address 20% cap, overflow recycling, and conservation laws.
/// @dev Covers:
///      1. 仅调整难度目标的公开流程验证 (Public contract workflow with target adjusted solely for PoW feasibility, rawReward > maxReward).
///      2. Exact deduction of allocatable during epoch settlement (and non-deduction of unallocatedGap).
///      3. Exact single recycling of cappedOverflow back into remainingEmission upon claim.
///      4. Invariant: token.totalMined() + remainingEmission + settledPendingReserved == constant.
///      5. Strict distinction between totalMined and Token totalSupply (accounting for 100M premint).
///      6. Strict distinction among unallocatedGap, cappedOverflow, and integer division rounding dust.
///      7. Zero-dust clean divisible scenario proving exact 0-wei conservation.
///      8. Duplicate claim revert without duplicate minting or duplicate overflow recycling.
///      9. Frozen settledEpochQuota preservation independent of subsequent remainingEmission changes.
///      10. Immutability of epochAddrCapBps snapshot against subsequent ADDR_CAP_BPS governance changes.
contract CapAndOverflowConservationTest is Test {
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
    uint256 public constant PREMINT_TOTAL = 100_000_000e18;
    uint256 public constant MINING_CAP = 900_000_000e18;
    uint256 public start;

    event RewardClaimed(
        address indexed miner,
        uint256 indexed epoch,
        uint256 claimedAmount,
        uint256 cappedOverflow
    );

    event EpochSettled(
        uint256 indexed epoch,
        uint256 effectiveQuota,
        uint256 totalWeight,
        uint256 wMin,
        uint256 unallocatedDilution
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
    }

    function _setFastTarget(uint256 e) internal {
        // Slot 10 in MinerV1: mapping(uint256 => uint256) public target
        // Setting target to 1 << 248 (8-bit difficulty) solely accelerates EVM PoW nonce search
        // without altering any weight calculation, share verification, or claim accounting logic.
        bytes32 slot = keccak256(abi.encode(e, uint256(10)));
        vm.store(address(miner), slot, bytes32(uint256(1) << 248));
    }

    function _mineNonce(uint256 e, address user, uint8 k) internal view returns (uint256) {
        bytes32 es = bytes32(miner.epochSeed(e));
        bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(miner), block.chainid));
        uint256 nonce = 0;
        uint256 t = miner.target(e);
        while (uint256(keccak256(abi.encode(sub, nonce))) > t) {
            nonce++;
        }
        return nonce;
    }

    /// @notice Test 1: 仅调整难度目标的公开流程验证 (Public contract workflow with target adjusted solely for PoW feasibility).
    /// @dev 仅调整难度目标 (target = 1 << 248 via _setFastTarget) 以加速 EVM 测试环境下的 PoW 碰撞，不修改任何其他状态，其余均遵循完整公开合约流程：
    ///      - Demonstrates:
    ///      - Alice deposits in epoch 0.
    ///      - At epoch 25 (warmup age = 24 = RAMP_EPOCHS), Alice reaches full 100% ramp (10000 bps).
    ///      - Alice submits CAP = 6 shares via submitShares, obtaining weight _w(6)*10000 = 24,494,890,000.
    ///      - Since wMin(25) = 10 * WSCALE = 100_000_000_000, Alice's raw reward is 24.49489% of effQ.
    ///      - Because 24.49489% > 20.00000%, rawReward STRICTLY exceeds maxReward.
    ///      - Settled deduction is allocatable.
    ///      - Capped overflow is recycled once.
    ///      - Total mined + remaining emission is strictly conserved.
    ///      - Duplicate claim reverts without double-minting or double-recycling.
    function test_EndToEnd_20PercentCap_TruncationAndConservation() public {
        // 1. Initial State Checks
        assertEq(miner.remainingEmission(), MINING_CAP);
        assertEq(token.totalMined(), 0);
        assertEq(token.totalSupply(), PREMINT_TOTAL);
        assertTrue(token.totalSupply() != token.totalMined(), "Must never confuse totalSupply with totalMined");

        // 2. Deposit in epoch 0
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // 3. Advance to epoch 25: Alice's warmup ramp reaches 100% (age = 25 - (0 + 1) = 24 = RAMP_EPOCHS)
        vm.warp(start + 25 * 3600);
        miner.initEpochSeed(25, 26);
        miner.sync(25);
        assertEq(miner.lastSynced(), 25);

        // Accelerate EVM nonce search by setting 8-bit difficulty target for epoch 25
        _setFastTarget(25);

        // Alice mines and submits CAP = 6 shares
        uint8[] memory ks = new uint8[](6);
        uint256[] memory nonces = new uint256[](6);
        for (uint8 i = 0; i < 6; i++) {
            ks[i] = i;
            nonces[i] = _mineNonce(25, alice, i);
        }

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // Verify Alice's weight matches exact formula: _w(6) * 10000 = 2,449,489 * 10,000 = 24,494,890,000
        uint256 expectedWeight = 24_494_890_000;
        assertEq(miner.userWeight(25, alice), expectedWeight, "Alice weight must be _w(6) * 10000");
        assertEq(miner.totalWeight(25), expectedWeight, "Total weight must equal Alice weight");

        // 4. Advance to epoch 26 to settle epoch 25
        vm.warp(start + 26 * 3600);
        uint256 remBeforeSettle = miner.remainingEmission();
        uint256 minedBeforeSettle = token.totalMined();

        // Sync epoch 25
        miner.sync(1);
        assertEq(miner.lastSynced(), 26);

        uint256 effQ = miner.settledEpochQuota(25);
        uint256 wm = miner.wMin(25);
        assertEq(wm, 100_000_000_000, "wMin at Day 1 (epoch 25) must be 10 * WSCALE");

        // Verification of Settlement Deduction
        // allocatable = (effQ * tw) / wm
        uint256 expectedAllocatable = (effQ * expectedWeight) / wm;
        uint256 expectedUnallocatedGap = effQ - expectedAllocatable;
        assertEq(miner.unallocatedGap(25), expectedUnallocatedGap, "Unallocated gap B3 recorded");
        assertEq(remBeforeSettle - miner.remainingEmission(), expectedAllocatable, "Settlement must deduct EXACTLY allocatable");

        // 5. Pre-Claim Reward and Cap Calculations
        uint256 remBeforeClaim = miner.remainingEmission();
        uint256 rawReward = (effQ * expectedWeight) / wm;
        assertEq(rawReward, expectedAllocatable, "Raw reward matches allocatable for sole miner");

        uint256 capBps = miner.epochAddrCapBps(25);
        assertEq(capBps, 2000, "Default cap is 2000 bps (20%)");
        uint256 maxReward = (effQ * capBps) / 10000;

        // CRITICAL CHECK A: rawReward MUST be strictly greater than maxReward to prove truncation branch coverage!
        assertGt(rawReward, maxReward, "Raw reward (24.49%) must strictly exceed maxReward (20%)");
        uint256 expectedOverflow = rawReward - maxReward;
        assertGt(expectedOverflow, 0, "Capped overflow must be strictly positive");

        // 6. Pre-Claim Conservation Check
        // Total Mined (0) + Remaining Emission + Settled Pending Reserved (rawReward) == Constant
        uint256 settledPendingReserved = rawReward;
        assertEq(
            token.totalMined() + miner.remainingEmission() + settledPendingReserved,
            minedBeforeSettle + remBeforeSettle,
            "Conservation before claim: totalMined + remainingEmission + settledPendingReserved == constant"
        );

        // 7. Verify RewardClaimed Event
        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(alice, 25, maxReward, expectedOverflow);

        // 8. Alice Claims
        assertFalse(miner.claimed(25, alice), "claimed mapping must be false prior to claim");
        vm.prank(alice);
        miner.claim(25);

        // 9. Post-Claim Verification
        assertTrue(miner.claimed(25, alice), "claimed mapping must be true after successful claim");
        // CHECK B: Mint quantity equals maxReward
        assertEq(token.balanceOf(alice), maxReward, "Minted tokens to Alice must equal maxReward");
        assertEq(token.totalMined() - minedBeforeSettle, maxReward, "token.totalMined must increase by maxReward");

        // CHECK C: remainingEmission precisely increased by cappedOverflow
        assertEq(miner.remainingEmission() - remBeforeClaim, expectedOverflow, "remainingEmission must increase by cappedOverflow");

        // CHECK D: Conservation after claim
        // Pending reserved is now 0. Total Mined + Remaining Emission == Constant
        assertEq(
            token.totalMined() + miner.remainingEmission(),
            minedBeforeSettle + remBeforeSettle,
            "Strict conservation holds after claim: totalMined + remainingEmission == initialConstant"
        );

        // 10. CHECK E: Duplicate Claim Reverts and Does Not Double-Mint or Double-Recycle
        uint256 aliceBalAfter = token.balanceOf(alice);
        uint256 totalMinedAfter = token.totalMined();
        uint256 remAfter = miner.remainingEmission();

        vm.prank(alice);
        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        miner.claim(25);

        assertTrue(miner.claimed(25, alice), "claimed mapping must remain true after duplicate claim revert");
        assertEq(token.balanceOf(alice), aliceBalAfter, "Balance unchanged on duplicate claim");
        assertEq(token.totalMined(), totalMinedAfter, "totalMined unchanged on duplicate claim");
        assertEq(miner.remainingEmission(), remAfter, "remainingEmission unchanged on duplicate claim");
    }

    /// @notice Test 2: Clean divisible scenario with zero integer division dust.
    /// @dev Scope of vm.store in this test:
    ///      - Proves: Mathematical exactness of allocatable deduction, 2-miner truncation,
    ///                3-term conservation equation with ZERO rounding dust, and single overflow recycling.
    ///      - Cannot prove: PoW share verification constraints (which are proven in Test 1).
    function test_DivisibleScenario_ExactConservation() public {
        // Advance to epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        uint256 wm = miner.wMin(1); // 100_000_000_000
        assertEq(wm, 100_000_000_000);

        // Construct clean divisible scenario:
        // Alice weight: 60_000_000_000 (60%)
        // Bob weight:   40_000_000_000 (40%)
        // Total weight: 100_000_000_000 == wm (100%), so tw == wm and unallocatedGap == 0!
        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(uint256(60_000_000_000)));

        bytes32 slotBob = keccak256(abi.encode(bob, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotBob, bytes32(uint256(40_000_000_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(100_000_000_000)));

        // Advance to epoch 2 to settle epoch 1
        vm.warp(start + 7200);
        uint256 initialR = miner.remainingEmission();
        uint256 initialM = token.totalMined();

        miner.sync(1);

        uint256 effQ = miner.settledEpochQuota(1);
        // Because tw == wm, allocatable == effQ, and unallocatedGap == 0
        assertEq(miner.unallocatedGap(1), 0, "Unallocated gap must be 0 when tw == wm");
        assertEq(initialR - miner.remainingEmission(), effQ, "Settlement must deduct exactly allocatable (effQ)");

        // Alice: 60% raw reward, capped at 20%, overflow = 40%
        uint256 aliceRaw = (effQ * 60) / 100;
        uint256 aliceMax = (effQ * 20) / 100;
        uint256 aliceOverflow = aliceRaw - aliceMax;

        // Bob: 40% raw reward, capped at 20%, overflow = 20%
        uint256 bobRaw = (effQ * 40) / 100;
        uint256 bobMax = (effQ * 20) / 100;
        uint256 bobOverflow = bobRaw - bobMax;

        // Check 3-Term Conservation before any claims:
        // totalMined + remainingEmission + pendingReserved == initialConstant
        uint256 pendingReserved = aliceRaw + bobRaw;
        assertEq(pendingReserved, effQ, "Pending reserved must equal allocatable");
        assertEq(
            token.totalMined() + miner.remainingEmission() + pendingReserved,
            initialM + initialR,
            "Conservation before claims: totalMined + remainingEmission + pendingReserved == initialConstant"
        );

        // Alice claims
        uint256 rBeforeAliceClaim = miner.remainingEmission();
        vm.prank(alice);
        miner.claim(1);

        assertEq(token.balanceOf(alice), aliceMax, "Alice receives exactly 20% maxReward");
        assertEq(miner.remainingEmission() - rBeforeAliceClaim, aliceOverflow, "Alice overflow added back");

        // Conservation with Alice claimed, Bob still pending:
        uint256 pendingAfterAlice = bobRaw;
        assertEq(
            token.totalMined() + miner.remainingEmission() + pendingAfterAlice,
            initialM + initialR,
            "Conservation after Alice claim: totalMined + remainingEmission + pendingBob == initialConstant"
        );

        // Bob claims
        uint256 rBeforeBobClaim = miner.remainingEmission();
        vm.prank(bob);
        miner.claim(1);

        assertEq(token.balanceOf(bob), bobMax, "Bob receives exactly 20% maxReward");
        assertEq(miner.remainingEmission() - rBeforeBobClaim, bobOverflow, "Bob overflow added back");

        // Final Conservation: All claims complete, pending == 0
        // Total Mined (40% effQ) + Remaining Emission (initialR - 40% effQ) == initialConstant
        assertEq(
            token.totalMined() + miner.remainingEmission(),
            initialM + initialR,
            "Final Conservation: totalMined + remainingEmission == initialConstant (0 wei difference)"
        );

        // Duplicate claims
        vm.prank(alice);
        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        miner.claim(1);

        vm.prank(bob);
        vm.expectRevert(MinerV1.AlreadyClaimed.selector);
        miner.claim(1);

        assertEq(token.totalMined() + miner.remainingEmission(), initialM + initialR);
    }

    struct DilutionVars {
        uint256 r0;
        uint256 m0;
        uint256 effQ;
        uint256 allocatable;
        uint256 unallocatedGap;
        uint256 aliceRaw;
        uint256 aliceMax;
        uint256 aliceOverflow;
        uint256 bobRaw;
        uint256 roundingDust;
    }

    /// @notice Test 3: Distinguish unallocatedGap (dilution), cappedOverflow (truncation), and division dust.
    /// @dev Demonstrates:
    ///      - When tw < wm, unallocatedGap = effQ - allocatable > 0.
    ///      - unallocatedGap is NEVER deducted from remainingEmission.
    ///      - Capped overflow is deducted at settlement (as part of allocatable) and recycled upon claim.
    ///      - Rounding dust = allocatable - sum(rawRewards) is quantified as exactly 1 wei.
    ///      - Note: dust 是不可领取的舍入残余，不是已回流 remainingEmission 的 cappedOverflow。
    ///      - Conservation strictly accounts for every category without double counting.
    function test_Conservation_DistinguishesUnallocatedGapAndRoundingDust() public {
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        uint256 wm = miner.wMin(1); // 100_000_000_000

        // Set up tw < wm:
        // Alice weight: 35_000_000_001 (~35% of wm) -> triggers 20% cap!
        // Bob weight:   15_000_000_001 (~15% of wm) -> below 20% cap
        // Total weight: 50_000_000_002 (~50% of wm)
        // Weight values with +1 wei induce non-zero integer division rounding dust (exactly 1 wei).
        uint256 aliceWeight = 35_000_000_001;
        uint256 bobWeight = 15_000_000_001;
        uint256 totalWeight = 50_000_000_002;

        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(aliceWeight));

        bytes32 slotBob = keccak256(abi.encode(bob, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotBob, bytes32(bobWeight));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(totalWeight));

        vm.warp(start + 7200);

        DilutionVars memory v;
        v.r0 = miner.remainingEmission();
        v.m0 = token.totalMined();

        miner.sync(1);

        v.effQ = miner.settledEpochQuota(1);
        v.allocatable = (v.effQ * totalWeight) / wm;
        v.unallocatedGap = v.effQ - v.allocatable;

        assertEq(miner.unallocatedGap(1), v.unallocatedGap, "Unallocated gap B3 stored");
        // Verify settlement deducted ONLY allocatable, NOT effQ
        assertEq(v.r0 - miner.remainingEmission(), v.allocatable, "Deduction from remainingEmission is allocatable");

        // Alice claim calculations
        v.aliceRaw = (v.effQ * aliceWeight) / wm;
        v.aliceMax = (v.effQ * 2000) / 10000;
        assertGt(v.aliceRaw, v.aliceMax, "Alice rawReward must exceed maxReward");
        v.aliceOverflow = v.aliceRaw - v.aliceMax;

        // Bob claim calculations
        v.bobRaw = (v.effQ * bobWeight) / wm;
        assertTrue(v.bobRaw <= v.aliceMax, "Bob rawReward is within maxReward");

        // Rounding Dust Quantification:
        // dust 是不可领取的舍入残余，不是已回流 remainingEmission 的 cappedOverflow。
        v.roundingDust = v.allocatable - (v.aliceRaw + v.bobRaw);
        assertEq(v.roundingDust, 1, "Rounding dust is exactly 1 wei for this weight configuration");

        // 4-Bucket Decomposition of effQ:
        // effQ == (aliceMint + bobMint) + unallocatedGap + aliceOverflow + roundingDust
        assertEq(
            v.aliceMax + v.bobRaw + v.unallocatedGap + v.aliceOverflow + v.roundingDust,
            v.effQ,
            "Exact 4-bucket partition of effQ"
        );

        // Claims execution
        vm.prank(alice);
        miner.claim(1);

        vm.prank(bob);
        miner.claim(1);

        // Post-claim conservation:
        // totalMined + remainingEmission + roundingDust == m0 + r0
        assertEq(
            token.totalMined() + miner.remainingEmission() + v.roundingDust,
            v.m0 + v.r0,
            "Conservation holds exactly accounting for rounding dust"
        );
    }

    /// @notice Test 4: Verify claim uses frozen settledEpochQuota, not recalculated from current remainingEmission.
    /// @dev Demonstrates:
    ///      - Epoch 1 settles and records settledEpochQuota[1].
    ///      - remainingEmission is drastically altered afterwards.
    ///      - Alice's claim of epoch 1 strictly uses settledEpochQuota[1].
    function test_ClaimUsesSettledEpochQuota_NotRecalculatedFromCurrentR() public {
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(uint256(100_000_000_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(100_000_000_000)));

        // Settle epoch 1
        vm.warp(start + 7200);
        miner.sync(1);

        uint256 frozenQuota = miner.settledEpochQuota(1);
        assertTrue(frozenQuota > 0, "Frozen quota must be positive");

        // Drastically mutate remainingEmission (e.g. simulate cutting it in half to 400M)
        bytes32 remSlot = bytes32(uint256(21));
        vm.store(address(miner), remSlot, bytes32(uint256(400_000_000e18)));
        assertEq(miner.remainingEmission(), 400_000_000e18);

        // Alice claims epoch 1
        uint256 expectedCapFromFrozen = (frozenQuota * 2000) / 10000;
        vm.prank(alice);
        miner.claim(1);

        assertEq(
            token.balanceOf(alice),
            expectedCapFromFrozen,
            "Claim MUST be computed against frozen settledEpochQuota, NOT current remainingEmission"
        );
    }

    /// @notice Test 5: Verify non-zero epochAddrCapBps snapshot is not overwritten by current ADDR_CAP_BPS change.
    /// @dev Demonstrates:
    ///      - Epoch 1 snapshots epochAddrCapBps[1] = 2000 (20%).
    ///      - Governance changes ADDR_CAP_BPS to 1000 (10%).
    ///      - Alice claims epoch 1 and receives 20%, strictly honoring the snapshot.
    function test_SnapshotCapBpsImmutable() public {
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(uint256(100_000_000_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(100_000_000_000)));

        vm.warp(start + 7200);
        miner.sync(1);

        assertEq(miner.epochAddrCapBps(1), 2000, "Epoch 1 snapshot cap is 2000 bps");

        // Admin updates global ADDR_CAP_BPS to 1000 bps (10%)
        vm.prank(admin);
        miner.setAddrCapBps(1000);
        assertEq(miner.ADDR_CAP_BPS(), 1000);

        // Alice claims epoch 1: must receive 20% (not 10%)
        uint256 frozenQuota = miner.settledEpochQuota(1);
        uint256 expectedReward20Percent = (frozenQuota * 2000) / 10000;

        vm.prank(alice);
        miner.claim(1);

        assertEq(
            token.balanceOf(alice),
            expectedReward20Percent,
            "Past epoch claim must use immutable snapshot cap of 20%, not new 10% parameter"
        );
    }

    struct BoundaryVars {
        address carol;
        address dave;
        uint256 effQ;
        uint256 maxReward;
        uint256 aliceRaw;
        uint256 bobRaw;
        uint256 carolRaw;
        uint256 daveRaw;
        uint256 carolOverflow;
        uint256 daveOverflow;
        uint256 initialR;
        uint256 initialM;
        uint256 pendingReserved;
        uint256 rBefore;
    }

    /// @notice Test 6: Boundary testing around exactly 20% cap (below, equal, above).
    /// @dev Demonstrates:
    ///      - Alice at 19.99% (below): cappedOverflow == 0, full raw payout, zero recycled.
    ///      - Bob at 20.00% (equal): cappedOverflow == 0, full raw payout, zero recycled.
    ///      - Carol at 20.01% (above): capped at 20%, cappedOverflow == 0.01%, 0.01% recycled to remainingEmission.
    ///      - Dave at 40.00% (above): capped at 20%, cappedOverflow == 20%, 20% recycled to remainingEmission.
    ///      - Total minted (79.99%) + total recycled (20.01%) == 100% of effQ.
    ///      - Strict 3-term conservation holds at every intermediate step and after all claims.
    function test_TwentyPercentThreshold_ExactBoundary_BelowEqualAbove() public {
        BoundaryVars memory v;
        v.carol = address(0x30);
        v.dave = address(0x40);

        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        assertEq(miner.wMin(1), 100_000_000_000);

        // Set up weights totaling exactly wm (100_000_000_000) so unallocatedGap == 0:
        // Alice: 19_990_000_000 (19.99% - strictly below 20%)
        // Bob:   20_000_000_000 (20.00% - exactly equal 20%)
        // Carol: 20_010_000_000 (20.01% - strictly above 20%)
        // Dave:  40_000_000_000 (40.00% - remainder to reach exactly wm)
        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(uint256(19_990_000_000)));

        bytes32 slotBob = keccak256(abi.encode(bob, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotBob, bytes32(uint256(20_000_000_000)));

        bytes32 slotCarol = keccak256(abi.encode(v.carol, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotCarol, bytes32(uint256(20_010_000_000)));

        bytes32 slotDave = keccak256(abi.encode(v.dave, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotDave, bytes32(uint256(40_000_000_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(100_000_000_000)));

        // Advance to epoch 2 to settle epoch 1
        vm.warp(start + 7200);
        v.initialR = miner.remainingEmission();
        v.initialM = token.totalMined();

        miner.sync(1);

        v.effQ = miner.settledEpochQuota(1);
        assertEq(miner.unallocatedGap(1), 0, "Unallocated gap is 0 when tw == wm");
        assertEq(v.initialR - miner.remainingEmission(), v.effQ, "Settlement must deduct effQ from remainingEmission");

        assertEq(miner.epochAddrCapBps(1), 2000, "Default cap snapshot is 2000 bps (20%)");
        v.maxReward = (v.effQ * 2000) / 10000;

        // Raw rewards
        v.aliceRaw = (v.effQ * 19_990_000_000) / 100_000_000_000;
        v.bobRaw = (v.effQ * 20_000_000_000) / 100_000_000_000;
        v.carolRaw = (v.effQ * 20_010_000_000) / 100_000_000_000;
        v.daveRaw = (v.effQ * 40_000_000_000) / 100_000_000_000;

        assertLt(v.aliceRaw, v.maxReward, "Alice raw is strictly below 20% cap");
        assertEq(v.bobRaw, v.maxReward, "Bob raw is exactly equal to 20% cap");
        assertGt(v.carolRaw, v.maxReward, "Carol raw is strictly above 20% cap");
        assertGt(v.daveRaw, v.maxReward, "Dave raw is strictly above 20% cap");

        v.carolOverflow = v.carolRaw - v.maxReward;
        v.daveOverflow = v.daveRaw - v.maxReward;
        assertEq(v.carolOverflow, (v.effQ * 10_000_000) / 100_000_000_000, "Carol overflow is exactly 0.01% of effQ");
        assertEq(v.daveOverflow, (v.effQ * 20_000_000_000) / 100_000_000_000, "Dave overflow is exactly 20.00% of effQ");

        // Pre-claim conservation: totalMined + remainingEmission + pendingReserved == initialConstant
        v.pendingReserved = v.aliceRaw + v.bobRaw + v.carolRaw + v.daveRaw;
        assertEq(v.pendingReserved, v.effQ, "Pending reserved equals effQ");
        assertEq(
            token.totalMined() + miner.remainingEmission() + v.pendingReserved,
            v.initialM + v.initialR,
            "Pre-claim 3-term conservation holds"
        );

        // 1. Alice claims (below cap): receives aliceRaw, overflow == 0, remainingEmission unchanged
        v.rBefore = miner.remainingEmission();
        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(alice, 1, v.aliceRaw, 0);
        vm.prank(alice);
        miner.claim(1);

        assertEq(token.balanceOf(alice), v.aliceRaw, "Alice receives exactly aliceRaw (19.99%)");
        assertEq(miner.remainingEmission(), v.rBefore, "Remaining emission unchanged when below cap");

        v.pendingReserved -= v.aliceRaw;
        assertEq(
            token.totalMined() + miner.remainingEmission() + v.pendingReserved,
            v.initialM + v.initialR,
            "Conservation after Alice claim holds"
        );

        // 2. Bob claims (exactly at cap): receives maxReward, overflow == 0, remainingEmission unchanged
        v.rBefore = miner.remainingEmission();
        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(bob, 1, v.maxReward, 0);
        vm.prank(bob);
        miner.claim(1);

        assertEq(token.balanceOf(bob), v.maxReward, "Bob receives exactly maxReward (20.00%)");
        assertEq(miner.remainingEmission(), v.rBefore, "Remaining emission unchanged when exactly at cap");

        v.pendingReserved -= v.bobRaw;
        assertEq(
            token.totalMined() + miner.remainingEmission() + v.pendingReserved,
            v.initialM + v.initialR,
            "Conservation after Bob claim holds"
        );

        // 3. Carol claims (above cap): receives maxReward, overflow == carolOverflow, remainingEmission increases
        v.rBefore = miner.remainingEmission();
        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(v.carol, 1, v.maxReward, v.carolOverflow);
        vm.prank(v.carol);
        miner.claim(1);

        assertEq(token.balanceOf(v.carol), v.maxReward, "Carol capped at exactly maxReward (20.00%)");
        assertEq(miner.remainingEmission() - v.rBefore, v.carolOverflow, "Remaining emission increases by carolOverflow");

        v.pendingReserved -= v.carolRaw;
        assertEq(
            token.totalMined() + miner.remainingEmission() + v.pendingReserved,
            v.initialM + v.initialR,
            "Conservation after Carol claim holds"
        );

        // 4. Dave claims (above cap): receives maxReward, overflow == daveOverflow, remainingEmission increases
        v.rBefore = miner.remainingEmission();
        vm.expectEmit(true, true, false, true, address(miner));
        emit RewardClaimed(v.dave, 1, v.maxReward, v.daveOverflow);
        vm.prank(v.dave);
        miner.claim(1);

        assertEq(token.balanceOf(v.dave), v.maxReward, "Dave capped at exactly maxReward (20.00%)");
        assertEq(miner.remainingEmission() - v.rBefore, v.daveOverflow, "Remaining emission increases by daveOverflow");

        v.pendingReserved -= v.daveRaw;
        assertEq(v.pendingReserved, 0, "All pending reserved claims cleared");

        // Final conservation: totalMined + remainingEmission == initialConstant
        assertEq(
            token.totalMined() + miner.remainingEmission(),
            v.initialM + v.initialR,
            "Final strict conservation holds after all claims"
        );

        // Verify minted + recycled == effQ
        uint256 totalMinted = token.balanceOf(alice) + token.balanceOf(bob) + token.balanceOf(v.carol) + token.balanceOf(v.dave);
        uint256 totalRecycled = v.carolOverflow + v.daveOverflow;
        assertEq(totalMinted + totalRecycled, v.effQ, "totalMinted + totalRecycled equals effQ exactly");
    }

    struct MultiCapVars {
        address carol;
        address dave;
        uint256 initialR;
        uint256 initialM;
        uint256 effQ;
        uint256 expectedRaw;
        uint256 expectedMax;
        uint256 expectedOverflow;
        uint256 pendingReserved;
    }

    /// @notice Test 7: Multi-miner simultaneous cap hit verification.
    /// @dev Demonstrates:
    ///      - 4 miners each holding 25% of total weight (25,000,000,000 / 100,000,000,000).
    ///      - Each miner raw reward is 25% of effQ; each is capped at 20% of effQ.
    ///      - Each miner produces 5% cappedOverflow recycled back into remainingEmission.
    ///      - Total minted = 80% of effQ; total recycled = 20% of effQ.
    ///      - 3-term conservation holds after each individual claim and at final state.
    function test_MultiMinersSimultaneousCap_ConservationAndOverflowConsistent() public {
        MultiCapVars memory v;
        v.carol = address(0x30);
        v.dave = address(0x40);

        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        assertEq(miner.wMin(1), 100_000_000_000);

        // 4 miners each with 25% weight: 25_000_000_000
        bytes32 slotAlice = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotAlice, bytes32(uint256(25_000_000_000)));

        bytes32 slotBob = keccak256(abi.encode(bob, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotBob, bytes32(uint256(25_000_000_000)));

        bytes32 slotCarol = keccak256(abi.encode(v.carol, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotCarol, bytes32(uint256(25_000_000_000)));

        bytes32 slotDave = keccak256(abi.encode(v.dave, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotDave, bytes32(uint256(25_000_000_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(100_000_000_000)));

        // Settle epoch 1
        vm.warp(start + 7200);
        v.initialR = miner.remainingEmission();
        v.initialM = token.totalMined();

        miner.sync(1);

        v.effQ = miner.settledEpochQuota(1);
        assertEq(miner.unallocatedGap(1), 0, "Unallocated gap is 0 when tw == wm");
        assertEq(v.initialR - miner.remainingEmission(), v.effQ, "Settlement deducts exactly effQ");

        v.expectedRaw = (v.effQ * 25) / 100;
        v.expectedMax = (v.effQ * 20) / 100;
        v.expectedOverflow = v.expectedRaw - v.expectedMax;
        assertEq(v.expectedOverflow, (v.effQ * 5) / 100, "Each miner generates exactly 5% overflow");

        address[4] memory miners = [alice, bob, v.carol, v.dave];
        v.pendingReserved = v.effQ;

        for (uint256 i = 0; i < 4; i++) {
            address m = miners[i];
            uint256 rBefore = miner.remainingEmission();
            uint256 mBefore = token.totalMined();

            vm.expectEmit(true, true, false, true, address(miner));
            emit RewardClaimed(m, 1, v.expectedMax, v.expectedOverflow);

            vm.prank(m);
            miner.claim(1);

            assertEq(token.balanceOf(m), v.expectedMax, "Miner must receive exactly 20% cap");
            assertEq(token.totalMined() - mBefore, v.expectedMax, "Total mined increments by 20%");
            assertEq(miner.remainingEmission() - rBefore, v.expectedOverflow, "Remaining emission increments by 5% overflow");

            v.pendingReserved -= v.expectedRaw;
            assertEq(
                token.totalMined() + miner.remainingEmission() + v.pendingReserved,
                v.initialM + v.initialR,
                "Conservation holds after each individual claim in multi-miner cap scenario"
            );
        }

        assertEq(v.pendingReserved, 0, "All pending claims cleared");

        // Verify totals across all 4 miners
        uint256 totalMinted = token.balanceOf(alice) + token.balanceOf(bob) + token.balanceOf(v.carol) + token.balanceOf(v.dave);
        assertEq(totalMinted, v.expectedMax * 4, "Total minted is 80% of effQ");
        assertEq(miner.remainingEmission() - (v.initialR - v.effQ), v.expectedOverflow * 4, "Total recycled is 20% of effQ");
        assertEq(totalMinted + (v.expectedOverflow * 4), v.effQ, "80% minted + 20% recycled == 100% effQ");

        // Final conservation: totalMined + remainingEmission == initialConstant
        assertEq(
            token.totalMined() + miner.remainingEmission(),
            v.initialM + v.initialR,
            "Final strict conservation holds after all 4 miners claim"
        );
    }
}
