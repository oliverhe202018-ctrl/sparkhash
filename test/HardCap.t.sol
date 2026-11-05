// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {console} from "forge-std/console.sol";
import {MinerV1} from "../src/MinerV1.sol";

contract HardCapTest is Test {
    MinerV1 public miner;

    uint256 public constant MINING_CAP = 900_000_000e18;

    function setUp() public {
        miner = new MinerV1();
    }

    function test_Day1394CappingExactSum() public view {
        uint256 day1394Start = 1393 * 24;

        uint256 quotaBefore = miner.cumulativeQuotaBefore(day1394Start);
        assertTrue(quotaBefore < MINING_CAP, "Quota before day 1394 must be < 900M");

        uint256 day1394TotalEffective = 0;
        for (uint256 i = 0; i < 24; i++) {
            uint256 eq = miner.effectiveQuota(day1394Start + i);
            day1394TotalEffective += eq;
        }

        assertEq(quotaBefore + day1394TotalEffective, MINING_CAP, "Cumulative + Day 1394 must equal 900M");

        assertEq(miner.effectiveQuota(day1394Start + 24), 0, "Day 1395 epoch 0 must be 0");
        assertEq(miner.effectiveQuota(day1394Start + 48), 0, "Day 1396 must be 0");
        assertEq(miner.effectiveQuota(50_000), 0, "Far future epoch must be 0");
    }

    function test_ClosedFormVsIterativeAdditionMaxDiscrepancy() public view {
        uint256[] memory checkDays = new uint256[](8);
        checkDays[0] = 1;
        checkDays[1] = 30;
        checkDays[2] = 90;
        checkDays[3] = 180;
        checkDays[4] = 365;
        checkDays[5] = 730;
        checkDays[6] = 1095;
        checkDays[7] = 1394;

        uint256 iterativeSum = 0;
        uint256 nextCheckIdx = 0;
        uint256 maxDiff = 0;

        for (uint256 d = 1; d <= 1394; d++) {
            iterativeSum += miner.dailyQuota(d);

            if (nextCheckIdx < checkDays.length && d == checkDays[nextCheckIdx]) {
                uint256 closedForm = miner.cumulativeDailyQuota(d);
                uint256 diff = closedForm > iterativeSum ? (closedForm - iterativeSum) : (iterativeSum - closedForm);
                if (diff > maxDiff) {
                    maxDiff = diff;
                }
                console.log("Day:", d);
                console.log("Diff (wei):", diff);
                nextCheckIdx++;
            }
        }

        console.log("Max Discrepancy (wei):", maxDiff);
        assertTrue(maxDiff < 1e15, "Max discrepancy must be < 0.001 TTK");
    }

    function test_ParameterConsistencyRatio() public pure {
        uint256 D0 = 3_000_000e18;
        uint256 DECAY = 996700000000000000; // 0.9967e18
        uint256 oneMinusDecay = 1e18 - DECAY; // 0.0033e18 = 3.3e15

        // Check that D0 / (1 - r) == 10_000_000_000e18 / 11
        uint256 analyticalRatio = (D0 * 1e18) / oneMinusDecay;
        uint256 closedFormRatio = uint256(10_000_000_000e18) / 11;

        assertEq(analyticalRatio, closedFormRatio, 'Ratio 10_000_000_000/11 must match D0 / (1 - DECAY)');
    }
}
