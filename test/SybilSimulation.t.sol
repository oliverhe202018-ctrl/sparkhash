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
        // User has total 15 shares of hashrate in an epoch
        // Case 1: 1 address submits all 15 shares
        uint256 w1 = _w(15); // 3,872,983

        // Case 2: Split into 10 addresses (5 with 2 shares, 5 with 1 share = 15 shares total)
        uint256 w10 = 5 * _w(2) + 5 * _w(1); // 5 * 1,414,213 + 5 * 1,000,000 = 12,071,065

        // Case 3: Split into 15 addresses (each with 1 share = 15 shares total)
        uint256 w15 = 15 * _w(1); // 15,000,000

        assertEq(w1, 3_872_983);
        assertEq(w10, 12_071_065);
        assertEq(w15, 15_000_000);

        // Weight amplification ratio
        // w10 / w1 = 12,071,065 / 3,872,983 = 3.1167x
        // w15 / w1 = 15,000,000 / 3,872,983 = 3.8729x (equals sqrt(15))
        assertTrue(w10 * 1000 / w1 >= 3116, "10-address split must yield ~3.12x weight");
        assertTrue(w15 * 1000 / w1 >= 3872, "15-address split must yield ~3.87x weight");
    }

    function test_SybilVsHonestMinersComparison() public pure {
        // Honest pool: 100 CPU users, each with 2 shares
        uint256 honestTotalWeight = 100 * _w(2); // 100 * 1,414,213 = 141,421,300

        // Attacker option A: 1 Mega-Miner (1 address, capped at 15 shares)
        uint256 attackerWeightA = 1 * _w(15); // 3,872,983
        uint256 poolA = honestTotalWeight + attackerWeightA; // 145,294,283
        uint256 shareBpsA = (attackerWeightA * 10000) / poolA; // ~266 bps = 2.66%

        // Attacker option B: 10 addresses (each 15 shares = 150 shares)
        uint256 attackerWeightB = 10 * _w(15); // 38,729,830
        uint256 poolB = honestTotalWeight + attackerWeightB; // 180,151,130
        uint256 shareBpsB = (attackerWeightB * 10000) / poolB; // ~2149 bps = 21.50%

        // Attacker option C: 100 addresses (each 15 shares = 1500 shares)
        uint256 attackerWeightC = 100 * _w(15); // 387,298,300
        uint256 poolC = honestTotalWeight + attackerWeightC; // 528,719,600
        uint256 shareBpsC = (attackerWeightC * 10000) / poolC; // ~7325 bps = 73.25%

        console.log("1 Single Address (15 shares) Share: %s bps (2.66%)", shareBpsA);
        console.log("10 Sybil Addresses (150 shares) Share: %s bps (21.50%)", shareBpsB);
        console.log("100 Sybil Addresses (1500 shares) Share: %s bps (73.25%)", shareBpsC);

        // Verification against architect baseline:
        // 100 addresses captures ~73.25% of the total reward pool!
        assertEq(shareBpsA, 266);   // 2.66%
        assertEq(shareBpsB, 2149);  // 21.50%
        assertEq(shareBpsC, 7325);  // 73.25% (Matches architect hand-calc benchmark!)
    }
}
