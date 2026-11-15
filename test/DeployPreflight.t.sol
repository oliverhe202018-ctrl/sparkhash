// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {DeployScript, ISafe} from "../script/Deploy.s.sol";
import {MockSafe} from "./mocks/MockSafe.sol";

contract NonSafeMock {
    uint256 public dummy = 123;
}

contract DeployPreflightTest is Test {
    DeployScript public deployScript;
    uint256 internal deployerKey = 0xA11CE;
    address internal deployer;

    function setUp() public {
        deployer = vm.addr(deployerKey);
        vm.deal(deployer, 100 ether);
        deployScript = new DeployScript();
        vm.setEnv("PRIVATE_KEY", vm.toString(deployerKey));
        vm.setEnv("TIMELOCK_MIN_DELAY", "172800");
        vm.setEnv("DISCLOSURE_TIMELOCK_DELAY", "172800");
    }

    function test_PreflightPassesWithValidSafes() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));
        vm.setEnv("TIMELOCK_MIN_DELAY", "172800");
        vm.setEnv("DISCLOSURE_TIMELOCK_DELAY", "172800");

        deployScript.run();
    }

    function test_PreflightPassesWithGuardian1Of2() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](2);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        MockSafe safeB = new MockSafe(ownersB, 1); // 1/2 Guardian Safe

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));
        vm.setEnv("TIMELOCK_MIN_DELAY", "172800");
        vm.setEnv("DISCLOSURE_TIMELOCK_DELAY", "172800");

        deployScript.run();
    }

    function test_PreflightFailsIfGuardianThresholdLessThan1() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](2);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        MockSafe safeB = new MockSafe(ownersB, 0); // threshold 0 < 1

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("GUARDIAN_SAFE threshold must be >= 1");
        deployScript.run();
    }

    function test_PreflightFailsIfGuardianOwnersLessThan2() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](1);
        ownersB[0] = address(0x201);
        MockSafe safeB = new MockSafe(ownersB, 1); // 1 owner < 2

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("GUARDIAN_SAFE owners count must be >= 2");
        deployScript.run();
    }

    function test_PreflightFailsIfSafeMissingInterface() public {
        NonSafeMock nonSafe = new NonSafeMock();

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(nonSafe)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("TIMELOCK_PROPOSER_SAFE does not implement getThreshold() - not a valid Safe");
        deployScript.run();
    }

    function test_PreflightFailsIfThresholdLessThan2() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 1); // threshold 1 < 2

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("TIMELOCK_PROPOSER_SAFE threshold must be >= 2");
        deployScript.run();
    }

    function test_PreflightFailsIfOwnersLessThan3() public {
        address[] memory ownersA = new address[](2);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        MockSafe safeA = new MockSafe(ownersA, 2); // 2 owners < 3

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("TIMELOCK_PROPOSER_SAFE owners count must be >= 3");
        deployScript.run();
    }

    function test_PreflightFailsIfOwnersOverlap() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x102); // Overlaps with ownersA[1]
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("TIMELOCK_PROPOSER_SAFE and GUARDIAN_SAFE signers overlap");
        deployScript.run();
    }

    function test_PreflightFailsIfDeployerInOwners() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = deployer; // Contains deployer
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));

        vm.expectRevert("TIMELOCK_PROPOSER_SAFE owner cannot be deployer");
        deployScript.run();
    }

    function test_PreflightFailsIfStartTimestampLessThan1Hour() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));
        deployScript.setStartOverride(block.timestamp + 1800);

        vm.expectRevert("Deploy: START_TIMESTAMP must be at least 1 hour in the future");
        deployScript.run();
    }

    function test_PreflightFailsIfStartTimestampNotHourly() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));
        uint256 notHourly = ((block.timestamp + 7200 + 3599) / 3600) * 3600 + 120;
        deployScript.setStartOverride(notHourly);

        vm.expectRevert("Deploy: START_TIMESTAMP must be an on-the-hour Unix timestamp (multiple of 3600)");
        deployScript.run();
    }
    function test_PreflightFailsIfDisclosureDelayMismatch() public {
        address[] memory ownersA = new address[](3);
        ownersA[0] = address(0x101);
        ownersA[1] = address(0x102);
        ownersA[2] = address(0x103);
        MockSafe safeA = new MockSafe(ownersA, 2);

        address[] memory ownersB = new address[](3);
        ownersB[0] = address(0x201);
        ownersB[1] = address(0x202);
        ownersB[2] = address(0x203);
        MockSafe safeB = new MockSafe(ownersB, 2);

        vm.setEnv("TIMELOCK_PROPOSER_SAFE", vm.toString(address(safeA)));
        vm.setEnv("GUARDIAN_SAFE", vm.toString(address(safeB)));
        deployScript.setDelayOverride(999999);

        vm.expectRevert("Deploy: TIMELOCK_MIN_DELAY does not match operator-confirmed DISCLOSURE_TIMELOCK_DELAY (manual check reminder)");
        deployScript.run();
    }
}
