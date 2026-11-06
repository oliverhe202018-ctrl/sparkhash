// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {console} from "forge-std/console.sol";
import {MinerV1} from "../src/MinerV1.sol";

contract SybilSimulationTest is Test {
    MinerV1 public miner;

    function setUp() public {
        miner = new MinerV1();
    }

    function _w(uint256 c) internal pure returns (uint256) {
        if (c == 0) return 0;
        uint256 x = c * 1e12;
        uint256 z = (x + 1) / 2;
        uint256 y = x;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
        return y;
    }

    function test_ConstantHashrateSybilSplittingGain() public pure {
        // User has total 6 shares of hashrate in an epoch (CAP = 6)
        // Case 1: 1 address submits all 6 shares
        uint256 w1 = _w(6); // 2,449,489

        // Case 2: Split into 3 addresses (each with 2 shares = 6 shares total)
        uint256 w3 = 3 * _w(2); // 3 * 1,414,213 = 4,242,639

        // Case 3: Split into 6 addresses (each with 1 share = 6 shares total)
        uint256 w6 = 6 * _w(1); // 6,000,000

        assertEq(w1, 2_449_489);
        assertEq(w3, 4_242_639);
        assertEq(w6, 6_000_000);

        // Weight amplification ratio
        // w3 / w1 = 4,242,639 / 2_449_489 = 1.7320x (equals sqrt(3))
        // w6 / w1 = 6,000,000 / 2,449_489 = 2.4494x (equals sqrt(6))
        // Note: With CAP reduced from 15 to 6, max sybil amplification drops from 3.87x to 2.45x!
        assertTrue(w3 * 1000 / w1 >= 1732, "3-address split must yield ~1.73x weight");
        assertTrue(w6 * 1000 / w1 >= 2449, "6-address split must yield ~2.45x weight");
    }

    function test_SybilVsHonestMinersComparison() public pure {
        // Honest pool: 100 CPU users, each with 2 shares
        uint256 honestTotalWeight = 100 * _w(2); // 100 * 1,414,213 = 141,421,300

        // Attacker option A: 1 Single Address (capped at CAP = 6 shares)
        uint256 attackerWeightA = 1 * _w(6); // 2,449,489
        uint256 poolA = honestTotalWeight + attackerWeightA; // 143,870,789
        uint256 shareBpsA = (attackerWeightA * 10000) / poolA; // ~170 bps = 1.70%

        // Attacker option B: 10 addresses (each 6 shares = 60 shares)
        uint256 attackerWeightB = 10 * _w(6); // 24,494,890
        uint256 poolB = honestTotalWeight + attackerWeightB; // 165,916,190
        uint256 shareBpsB = (attackerWeightB * 10000) / poolB; // ~1476 bps = 14.76%

        // Attacker option C: 100 addresses (each 6 shares = 600 shares)
        uint256 attackerWeightC = 100 * _w(6); // 244,948,900
        uint256 poolC = honestTotalWeight + attackerWeightC; // 386,370,200
        uint256 shareBpsC = (attackerWeightC * 10000) / poolC; // ~6339 bps = 63.39%

        console.log("1 Single Address (6 shares) Share: %s bps (1.70%)", shareBpsA);
        console.log("10 Sybil Addresses (60 shares) Share: %s bps (14.76%)", shareBpsB);
        console.log("100 Sybil Addresses (600 shares) Share: %s bps (63.39%)", shareBpsC);

        assertEq(shareBpsA, 170);   // 1.70%
        assertEq(shareBpsB, 1476);  // 14.76%
        assertEq(shareBpsC, 6339);  // 63.39% (Significantly reduced from 73.25% under CAP=15!)
    }
}
