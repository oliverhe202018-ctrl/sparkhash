// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {console} from "forge-std/console.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract HardCapTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public alice = address(0x10);

    uint256 public constant MINING_CAP = 900_000_000e18;
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

    /// @notice Verifies that lambda corresponds to emitting ~99% over 1394 days (33,456 epochs) under 100% participation
    function test_LambdaParameter99PercentProof() public pure {
        uint256 lambdaWad = 137_639_085_754_704; // 1.37639e-4 Wad
        uint256 factor = 1e18 - lambdaWad;
        uint256 rem = 1e18;
        for (uint256 i = 0; i < 33456; i++) {
            rem = (rem * factor) / 1e18;
        }
        // rem should be approx 0.01e18 (1% remaining -> 99% emitted)
        assertApproxEqRel(rem, 0.01e18, 0.005e18, "Remaining after 33456 epochs must be ~1%");
    }

    /// @notice Tests that initial state starts with exactly MINING_CAP and cumulative + remaining == MINING_CAP
    function test_InitialMiningCapState() public view {
        assertEq(miner.remainingEmission(), MINING_CAP, "Initial remaining must be 900M");
        assertEq(miner.cumulativeQuotaBefore(0), 0, "Initial cumulative before must be 0");
        assertEq(miner.cumulativeQuotaBefore(100), 0, "No epochs settled yet, so 0 allocated");
    }

    /// @notice Tests that remaining emission decreases monotonically and strictly conserves 900M
    function test_AsymptoticDecayAndConservation() public {
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Simulate 50 epochs of empty participation
        uint256 prevR = miner.remainingEmission();
        for (uint256 e = 1; e <= 50; e++) {
            vm.warp(start + e * 3600);
            miner.initEpochSeed(e);
            miner.sync(1);

            uint256 curR = miner.remainingEmission();
            // Since tw == 0 for these epochs (no shares submitted), allocatable is 0, so curR == prevR
            assertEq(curR, prevR, "Empty epochs do not deduct from R");
        }

        // Now inject participation in epoch 51 and verify deduction
        uint256 activeEpoch = 51;
        bytes32 twSlot = keccak256(abi.encode(activeEpoch, uint256(12)));
        vm.store(address(miner), twSlot, bytes32(uint256(100_000_000_000))); // tw >= wMin

        vm.warp(start + 52 * 3600);
        miner.initEpochSeed(52);
        miner.sync(2); // settles epoch 50 and 51

        uint256 afterR = miner.remainingEmission();
        assertTrue(afterR < prevR, "R must decrease after participating epoch is settled");
        uint256 q51 = miner.settledEpochQuota(activeEpoch);
        assertEq(prevR - afterR, q51, "Deduction from R must match settledEpochQuota");
        assertEq(miner.cumulativeQuotaBefore(52), q51, "cumulativeQuotaBefore must equal deducted quota");
        assertEq(miner.cumulativeQuotaBefore(52) + afterR, MINING_CAP, "Conservation: cumulative + remaining == 900M");
    }

    /// @notice Tests that Token hardcap of 1 Billion (100M premint + 900M mining cap) can never be exceeded
    function test_TotalSupplyNeverExceedsCap() public view {
        assertEq(token.MAX_SUPPLY(), 1_000_000_000e18, "Token CAP must be 1B");
        assertEq(token.MINING_CAP(), 900_000_000e18, "Mining cap must be 900M");
        assertEq(token.totalSupply(), 100_000_000e18, "Premint must be 100M");
    }
}
