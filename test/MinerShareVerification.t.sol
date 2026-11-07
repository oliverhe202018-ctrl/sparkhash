// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {console} from "forge-std/console.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

/// @title MinerShareVerificationTest
/// @notice Comprehensive verification tests for PoW share validation in MinerV1.
/// Covers:
/// 1. Contract acceptance of valid program-generated shares.
/// 2. Negative cases: altered nonce, wrong epoch, wrong miner address, duplicate k, no stake, warmup violation.
/// 3. Testing method for lowering difficulty without modifying contract source code (vm.store).
contract MinerShareVerificationTest is Test {
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
    address public charlie = address(0x30);
    address public eve = address(0x999);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

    function setUp() public {
        // Enforce BSC Testnet chainId
        vm.chainId(97);

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
        vm.deal(charlie, 10 ether);

        // Epoch 0: Alice and Bob stake in StakeVault
        vm.warp(start + 1);
        miner.initEpochSeed(0);

        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        vm.prank(bob);
        vault.deposit{value: DEPOSIT}();

        // Warp to Epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);
    }

    /// @dev Demonstrates how to calculate the inner sub hash matching MinerV1._verifyShare:
    /// bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(this), block.chainid));
    function _computeSub(bytes32 es, address user, uint8 k) internal view returns (bytes32) {
        return keccak256(abi.encode(es, user, uint256(k), address(miner), block.chainid));
    }

    /// @dev Demonstrates how to mine a nonce matching MinerV1._verifyShare:
    /// uint256(keccak256(abi.encode(sub, nonce))) <= target_
    function _mineValidNonce(bytes32 sub, uint256 target) internal pure returns (uint256) {
        uint256 nonce = 0;
        while (uint256(keccak256(abi.encode(sub, nonce))) > target) {
            nonce++;
        }
        return nonce;
    }

    /// @notice Demonstrates lowering difficulty in test WITHOUT modifying contract source code.
    /// In MinerV1: mapping(uint256 => uint256) public target is at storage slot 10.
    /// Using vm.store, we adjust target[epoch] to a lower difficulty (e.g. 16 bits = 1 << 240).
    function test_LowerDifficultyMethodWithoutSourceChange() public {
        uint256 epochNum = 1;
        uint256 lowDifficultyTarget = uint256(1) << 240; // 16-bit difficulty

        // Calculate slot for target[1]: keccak256(abi.encode(epochNum, uint256(10)))
        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(lowDifficultyTarget));

        assertEq(miner.target(epochNum), lowDifficultyTarget, "Target should be updated to low difficulty in storage");
    }

    /// @notice Contract accepts a valid share output by program at reduced test difficulty.
    function test_AcceptsValidShare_LowerDifficulty() public {
        uint256 epochNum = 1;
        uint256 target16 = uint256(1) << 240; // 16-bit difficulty

        // Lower difficulty via vm.store without modifying source
        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);
        bytes32 sub = _computeSub(es, alice, 0);
        uint256 validNonce = _mineValidNonce(sub, target16);

        // Verify hash mathematically
        bytes32 shareHash = keccak256(abi.encode(sub, validNonce));
        assertTrue(uint256(shareHash) <= target16, "Mined share hash must satisfy target");

        // Submit to contract
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = validNonce;

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // Assert state updates
        assertEq(miner.usedMask(epochNum, alice), uint16(1) << 0, "usedMask should mark k=0");
        assertEq(miner.counted(epochNum, alice), 1, "counted shares should be 1");
        assertEq(miner.totalShares(epochNum), 1, "totalShares should be 1");
        assertTrue(miner.userWeight(epochNum, alice) > 0, "userWeight should be positive");
    }

    /// @notice Contract accepts valid batch submission of multiple shares (k=0, 1, 2).
    function test_AcceptsValidBatchShares() public {
        uint256 epochNum = 1;
        uint256 target16 = uint256(1) << 240;

        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);

        uint8[] memory ks = new uint8[](3);
        uint256[] memory nonces = new uint256[](3);
        for (uint8 i = 0; i < 3; i++) {
            ks[i] = i;
            bytes32 sub = _computeSub(es, alice, i);
            nonces[i] = _mineValidNonce(sub, target16);
        }

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        assertEq(miner.usedMask(epochNum, alice), (1 << 0) | (1 << 1) | (1 << 2));
        assertEq(miner.counted(epochNum, alice), 3);
        assertEq(miner.totalShares(epochNum), 3);
    }

    /// @notice Rejection Test 1: Modifying nonce by even 1 bit MUST cause InvalidProof revert.
    function test_RevertIfNonceAlteredByOneBit() public {
        uint256 epochNum = 1;
        uint256 target16 = uint256(1) << 240;

        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);
        bytes32 sub = _computeSub(es, alice, 0);
        uint256 validNonce = _mineValidNonce(sub, target16);

        // Flip 1 bit in nonce
        uint256 badNonce = validNonce ^ 1;

        // If by astronomical chance badNonce also satisfies target, flip another bit
        if (uint256(keccak256(abi.encode(sub, badNonce))) <= target16) {
            badNonce ^= 0x100;
        }

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = badNonce;

        vm.prank(alice);
        vm.expectRevert(MinerV1.InvalidProof.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 2: Share mined for Epoch 1 MUST revert when submitted in Epoch 2.
    function test_RevertIfWrongEpoch() public {
        uint256 epoch1 = 1;
        uint256 target16 = uint256(1) << 240;

        bytes32 targetSlot = keccak256(abi.encode(epoch1, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        bytes32 es1 = bytes32(miner.epochSeed(epoch1));
        bytes32 sub1 = _computeSub(es1, alice, 0);
        uint256 validNonceEpoch1 = _mineValidNonce(sub1, target16);

        // Advance to epoch 2
        vm.warp(start + 7200);
        miner.initEpochSeed(2);
        miner.sync(1);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = validNonceEpoch1;

        // Submitting epoch 1 share in epoch 2 fails because epochSeed[2] != epochSeed[1]
        vm.prank(alice);
        vm.expectRevert(MinerV1.InvalidProof.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 3: Bob cannot steal Alice's share (msg.sender must match user in hash).
    function test_RevertIfWrongMinerAddress() public {
        uint256 epochNum = 1;
        uint256 target16 = uint256(1) << 240;

        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        // Mine share for Alice
        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);
        bytes32 subAlice = _computeSub(es, alice, 0);
        uint256 validNonceAlice = _mineValidNonce(subAlice, target16);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = validNonceAlice;

        // Bob tries to submit Alice's nonce
        vm.prank(bob);
        vm.expectRevert(MinerV1.InvalidProof.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 4: Reusing the same share index k in the same epoch MUST revert.
    function test_RevertIfDuplicateShareIndex() public {
        uint256 epochNum = 1;
        uint256 target16 = uint256(1) << 240;

        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, bytes32(target16));

        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);
        bytes32 sub = _computeSub(es, alice, 0);
        uint256 validNonce = _mineValidNonce(sub, target16);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = validNonce;

        // 1st submission succeeds
        vm.prank(alice);
        miner.submitShares(ks, nonces);

        // 2nd submission with same k=0 reverts with DuplicateShareIndex
        vm.prank(alice);
        vm.expectRevert(MinerV1.DuplicateShareIndex.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 5: k >= 6 (CAP) MUST revert with InvalidShareIndex.
    function test_RevertIfShareIndexGe6() public {
        uint8[] memory ks = new uint8[](1);
        ks[0] = 6; // CAP is 6, valid indices are 0..5
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 12345;

        vm.prank(alice);
        vm.expectRevert(MinerV1.InvalidShareIndex.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 6: An address without active stake MUST revert with NoActiveStake.
    function test_RevertIfNoActiveStake() public {
        uint256 epochNum = 1;
        bytes32 es = bytes32(miner.epochSeed(epochNum));
        console.log("miner address:", address(miner));
        console.logBytes32(es);
        bytes32 subEve = _computeSub(es, eve, 0);
        uint256 nonce = _mineValidNonce(subEve, uint256(1) << 240);

        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = nonce;

        // Eve never deposited to StakeVault
        vm.prank(eve);
        vm.expectRevert(MinerV1.NoActiveStake.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 7: Warmup violation (submitting in same epoch as deposit) MUST revert.
    function test_RevertIfWarmupNotElapsed() public {
        // Charlie deposits in epoch 1
        vm.prank(charlie);
        vault.deposit{value: DEPOSIT}();

        // Charlie tries to submit in epoch 1 (contract requires epoch >= since + 1, so epoch 2+)
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 12345;

        vm.prank(charlie);
        vm.expectRevert(MinerV1.NoActiveStake.selector);
        miner.submitShares(ks, nonces);
    }

    /// @notice Rejection Test 8: Batch length mismatch MUST revert with BadBatchLength.
    function test_RevertIfBadBatchLength() public {
        uint8[] memory ks = new uint8[](2);
        ks[0] = 0;
        ks[1] = 1;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 12345;

        vm.prank(alice);
        vm.expectRevert(MinerV1.BadBatchLength.selector);
        miner.submitShares(ks, nonces);
    }
    /// @notice Confirms contract accepts the exact share JSON file produced by miner.py
    function test_AcceptsProgramMinedShareFromFile() public {
        uint256 epochNum = 1;
        string memory json = vm.readFile("test/fixtures/test_share_program.json");
        address minerAddr = vm.parseJsonAddress(json, ".miner");
        uint8 k = uint8(vm.parseJsonUint(json, ".k"));
        uint256 nonce = vm.parseJsonUint(json, ".nonceDecimal");
        bytes32 targetBytes = vm.parseJsonBytes32(json, ".target");

        // Set target to match the share's difficulty via vm.store without modifying contract source
        bytes32 targetSlot = keccak256(abi.encode(epochNum, uint256(10)));
        vm.store(address(miner), targetSlot, targetBytes);

        uint8[] memory ks = new uint8[](1);
        ks[0] = k;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = nonce;

        vm.prank(minerAddr);
        miner.submitShares(ks, nonces);

        assertEq(miner.usedMask(epochNum, minerAddr), uint16(1) << k);
        assertEq(miner.counted(epochNum, minerAddr), 1);
        assertEq(miner.totalShares(epochNum), 1);
        assertTrue(miner.userWeight(epochNum, minerAddr) > 0);
    }
}
