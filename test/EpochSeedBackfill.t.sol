// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract EpochSeedBackfillTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public alice = address(0x10);

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
    }

    /// @notice Requirement 1: Test seed initialization when blockhash returns 0 / hash chain fallback.
    function test_BlockhashZeroFallback() public {
        vm.warp(start + 1);

        // For historical epochs backfilled, bh is explicitly bytes32(0) fallback
        // Let us warp to epoch 2, without initializing epoch 0 and 1
        vm.warp(start + 2 * 3600 + 1);
        assertEq(miner.epoch(), 2);

        // Backfill epoch 0 and 1
        miner.initEpochSeed(1, 10);
        assertTrue(miner.epochSeed(0) != 0);
        assertTrue(miner.epochSeed(1) != 0);

        // Verify epoch 0 uses bytes32(0) as prev and bytes32(0) as bh (since epoch 0 is historical relative to curEpoch=2)
        uint256 expectedSeed0 = uint256(keccak256(abi.encode(bytes32(0), bytes32(0), uint256(0), block.chainid, address(miner))));
        assertEq(miner.epochSeed(0), expectedSeed0);

        // Verify epoch 1 uses epochSeed(0) as prev and bytes32(0) as bh
        uint256 expectedSeed1 = uint256(keccak256(abi.encode(bytes32(expectedSeed0), bytes32(0), uint256(1), block.chainid, address(miner))));
        assertEq(miner.epochSeed(1), expectedSeed1);

        // Now initialize current epoch (epoch 2)
        miner.initEpochSeed(2, 10);
        bytes32 bh2 = blockhash(block.number - 1);
        uint256 expectedSeed2 = uint256(keccak256(abi.encode(bytes32(expectedSeed1), bh2, uint256(2), block.chainid, address(miner))));
        assertEq(miner.epochSeed(2), expectedSeed2);
    }

    /// @notice Requirement 1: Halt 100 epochs, backfill in 5 calls of maxSteps=20, verify hash chain.
    function test_Halt100EpochsBackfillIn5Calls() public {
        vm.warp(start + 1);
        // Initialize epoch 0
        miner.initEpochSeed(0);
        assertTrue(miner.epochSeed(0) != 0);

        // System halted for 100 epochs: warp from epoch 0 to epoch 100
        vm.warp(start + 100 * 3600 + 1);
        assertEq(miner.epoch(), 100);

        // Epochs 1..100 (exactly 100 uninitialized epochs)
        // Call 1: initializes epochs 1..20
        uint256 last1 = miner.initEpochSeed(100, 20);
        assertEq(last1, 20);
        assertTrue(miner.epochSeed(20) != 0);
        assertEq(miner.epochSeed(21), 0);

        // Call 2: initializes epochs 21..40
        uint256 last2 = miner.initEpochSeed(100, 20);
        assertEq(last2, 40);
        assertTrue(miner.epochSeed(40) != 0);
        assertEq(miner.epochSeed(41), 0);

        // Call 3: initializes epochs 41..60
        uint256 last3 = miner.initEpochSeed(100, 20);
        assertEq(last3, 60);
        assertTrue(miner.epochSeed(60) != 0);
        assertEq(miner.epochSeed(61), 0);

        // Call 4: initializes epochs 61..80
        uint256 last4 = miner.initEpochSeed(100, 20);
        assertEq(last4, 80);
        assertTrue(miner.epochSeed(80) != 0);
        assertEq(miner.epochSeed(81), 0);

        // Call 5: initializes epochs 81..100
        uint256 last5 = miner.initEpochSeed(100, 20);
        assertEq(last5, 100);
        assertTrue(miner.epochSeed(100) != 0);

        // Call 6: target already initialized -> returns targetEpoch (no-op, does not revert)
        uint256 last6 = miner.initEpochSeed(100, 20);
        assertEq(last6, 100);

        // Verify all 100 epochs are non-zero and hash chain is consistent
        for (uint256 e = 1; e <= 99; e++) {
            assertTrue(miner.epochSeed(e) != 0, "seed should not be 0");
            uint256 prev = miner.epochSeed(e - 1);
            uint256 expected = uint256(keccak256(abi.encode(bytes32(prev), bytes32(0), e, block.chainid, address(miner))));
            assertEq(miner.epochSeed(e), expected, "historical hash chain mismatch");
        }

        // Epoch 100 (current epoch) incorporates blockhash(block.number - 1)
        bytes32 curBh = blockhash(block.number - 1);
        uint256 expected100 = uint256(keccak256(abi.encode(bytes32(miner.epochSeed(99)), curBh, uint256(100), block.chainid, address(miner))));
        assertEq(miner.epochSeed(100), expected100, "current epoch seed mismatch");
    }

    /// @notice Requirement 1: Submitting shares before backfilling reverts with SeedNotInitialized.
    function test_SubmitSharesBeforeSeedInitReverts() public {
        vm.warp(start + 1); // Epoch 0
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Warp to Epoch 1 (Alice is staked from epoch 0, warmup completed for epoch 1)
        vm.warp(start + 3600 + 1);
        assertEq(miner.epoch(), 1);

        // Do NOT initialize epochSeed(1)
        assertEq(miner.epochSeed(1), 0);

        // Ensure sync passes so we isolate SeedNotInitialized check
        miner.sync(1);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 12345;

        // Submitting shares when epochSeed is 0 must revert with SeedNotInitialized
        vm.prank(alice);
        vm.expectRevert(MinerV1.SeedNotInitialized.selector);
        miner.submitShares(ks, nonces);

        // Now initialize epochSeed(1)
        miner.initEpochSeed(1);
        assertTrue(miner.epochSeed(1) != 0);

        // Now submitShares will pass the seed check (fails later on PoW or succeeds if valid nonce)
        // With an invalid nonce it reverts with InvalidProof, confirming SeedNotInitialized check was passed
        vm.prank(alice);
        vm.expectRevert(MinerV1.InvalidProof.selector);
        miner.submitShares(ks, nonces);

        // Now mine a real valid share with fast difficulty target
        bytes32 targetSlot = keccak256(abi.encode(uint256(1), uint256(10)));
        uint256 fastTarget = uint256(1) << 248;
        vm.store(address(miner), targetSlot, bytes32(fastTarget));

        bytes32 es = bytes32(miner.epochSeed(1));
        bytes32 sub = keccak256(abi.encode(es, alice, uint256(0), address(miner), block.chainid));
        uint256 validNonce = 0;
        while (uint256(keccak256(abi.encode(sub, validNonce))) > fastTarget) {
            validNonce++;
        }
        nonces[0] = validNonce;

        vm.prank(alice);
        miner.submitShares(ks, nonces);
        assertEq(miner.counted(1, alice), 1);
    }

    /// @notice Test wrapper with single parameter defaults to MAX_SEED_STEPS (24)
    function test_SingleParamWrapperDefaultSteps() public {
        vm.warp(start + 50 * 3600);
        // Call wrapper for target 50
        miner.initEpochSeed(50);
        // Should have initialized 0..23 (24 steps)
        assertTrue(miner.epochSeed(23) != 0);
        assertEq(miner.epochSeed(24), 0);
    }

    /// @notice Requirement 1(a): Calling future epoch reverts with FutureEpoch.
    function test_PrefixProperty_FutureEpochReverts() public {
        vm.warp(start + 5 * 3600 + 1);
        assertEq(miner.epoch(), 5);

        vm.expectRevert(MinerV1.FutureEpoch.selector);
        miner.initEpochSeed(6, 10);
    }

    /// @notice Requirement 1(a) & 1(c): maxSteps == 0 reverts with InvalidMaxSteps.
    function test_PrefixProperty_MaxStepsZeroReverts() public {
        vm.warp(start + 5 * 3600 + 1);
        assertEq(miner.epoch(), 5);

        vm.expectRevert(MinerV1.InvalidMaxSteps.selector);
        miner.initEpochSeed(5, 0);
    }

    /// @notice Requirement 1(a) & 1(b): Calling with already initialized epoch is no-op returning targetEpoch.
    function test_PrefixProperty_AlreadyInitializedNoOp() public {
        vm.warp(start + 10 * 3600 + 1);
        miner.initEpochSeed(4, 10);
        for (uint256 e = 0; e <= 4; e++) {
            assertTrue(miner.epochSeed(e) != 0, "seed should be initialized");
        }
        uint256 seed2Before = miner.epochSeed(2);

        uint256 res = miner.initEpochSeed(2, 5);
        assertEq(res, 2);
        assertEq(miner.epochSeed(2), seed2Before);

        res = miner.initEpochSeed(4, 5);
        assertEq(res, 4);

        for (uint256 e = 5; e <= 10; e++) {
            assertEq(miner.epochSeed(e), 0, "future seed must remain 0");
        }
    }

    /// @notice Requirement 1(a): maxSteps greater than gap initializes all remaining epochs up to target.
    function test_PrefixProperty_MaxStepsGreaterThanGap() public {
        vm.warp(start + 8 * 3600 + 1);
        miner.initEpochSeed(2, 10);

        uint256 last = miner.initEpochSeed(8, 50);
        assertEq(last, 8);

        for (uint256 e = 0; e <= 8; e++) {
            assertTrue(miner.epochSeed(e) != 0, "seed should be initialized");
        }
        assertEq(miner.epochSeed(9), 0, "epoch 9 must not be initialized");
    }

    /// @notice Requirement 1(a): Proves initialized epochs ALWAYS form a contiguous prefix [0, ..., K-1].
    function test_PrefixProperty_ContiguousPrefixInvariant() public {
        vm.warp(start + 30 * 3600 + 1);
        assertEq(miner.epoch(), 30);

        uint256[5] memory steps = [uint256(3), uint256(7), uint256(5), uint256(10), uint256(15)];
        uint256 expectedK = 0;

        for (uint256 i = 0; i < steps.length; i++) {
            uint256 s = steps[i];
            uint256 lastInit = miner.initEpochSeed(30, s);
            expectedK = (expectedK + s > 31) ? 31 : expectedK + s;
            assertEq(lastInit, expectedK - 1, "lastInit should match K-1");

            for (uint256 e = 0; e < expectedK; e++) {
                assertTrue(miner.epochSeed(e) != 0, "seed in prefix must be non-zero");
            }
            for (uint256 e = expectedK; e <= 30; e++) {
                assertEq(miner.epochSeed(e), 0, "seed outside prefix must be zero");
            }
        }
    }
}
