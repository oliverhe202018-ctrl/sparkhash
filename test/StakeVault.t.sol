// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {StakeVault} from "../src/StakeVault.sol";

contract StakeVaultTest is Test {
    StakeVault public vault;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public alice = address(0x1);
    address public bob = address(0x2);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public constant MIN_DEP = 0.001 ether;
    uint256 public constant MAX_DEP = 10 ether;
    uint256 public constant MIN_INTERVAL = 24; // 24 epochs
    uint256 public constant MAX_MULT = 2;     // 2x max adjustment

    uint256 public start;

    function setUp() public {
        start = block.timestamp + 100;
        vault = new StakeVault(
            DEPOSIT,
            start,
            admin,
            guardian,
            MIN_DEP,
            MAX_DEP,
            MIN_INTERVAL,
            MAX_MULT
        );

        vm.deal(alice, 10 ether);
        vm.deal(bob, 10 ether);
    }

    function test_DepositBeforeStartReverts() public {
        vm.prank(alice);
        vm.expectRevert(StakeVault.NotStarted.selector);
        vault.deposit{value: DEPOSIT}();
    }

    function test_DepositSuccess() public {
        vm.warp(start + 1);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        (bool active, uint256 since) = vault.status(alice);
        assertTrue(active);
        assertEq(since, 0); // epoch 0
    }

    function test_DepositWrongAmountReverts() public {
        vm.warp(start + 1);
        vm.prank(alice);
        vm.expectRevert(StakeVault.BadDeposit.selector);
        vault.deposit{value: 0.02 ether}();
    }

    function test_DuplicateDepositReverts() public {
        vm.warp(start + 1);
        vm.startPrank(alice);
        vault.deposit{value: DEPOSIT}();

        vm.expectRevert(StakeVault.BadDeposit.selector);
        vault.deposit{value: DEPOSIT}();
        vm.stopPrank();
    }

    function test_RequestWithdrawAndCooldown() public {
        vm.warp(start + 1);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Warp to epoch 5
        vm.warp(start + 5 * 3600);
        vm.prank(alice);
        vault.requestWithdraw();

        (bool active,) = vault.status(alice);
        assertFalse(active); // No longer active stake after request

        // Attempt withdraw before 48 epochs cooldown
        vm.warp(start + 52 * 3600); // 47 epochs later
        vm.prank(alice);
        vm.expectRevert(StakeVault.CooldownActive.selector);
        vault.withdraw();

        // Warp past 48 epochs cooldown (epoch 5 + 48 = 53)
        vm.warp(start + 53 * 3600);
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        vault.withdraw();
        assertEq(alice.balance, balBefore + DEPOSIT);

        // State is completely cleaned up
        (bool activeAfter,) = vault.status(alice);
        assertFalse(activeAfter);
    }

    function test_PauseOnlyStopsDeposit_WithdrawStillWorks() public {
        vm.warp(start + 1);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Guardian pauses in emergency
        vm.prank(guardian);
        vault.pause();
        assertTrue(vault.paused());

        // Bob cannot deposit while paused
        vm.prank(bob);
        vm.expectRevert(StakeVault.VaultIsPaused.selector);
        vault.deposit{value: DEPOSIT}();

        // Alice requests withdraw during pause
        vm.warp(start + 10 * 3600);
        vm.prank(alice);
        vault.requestWithdraw();

        // Alice withdraws after cooldown even while still paused
        vm.warp(start + 58 * 3600);
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        vault.withdraw();
        assertEq(alice.balance, balBefore + DEPOSIT);
    }

    function test_SetDepositAdminOnlyAndBounds() public {
        // Non-admin (Alice) reverts
        vm.prank(alice);
        vm.expectRevert(StakeVault.OnlyAdmin.selector);
        vault.setDeposit(0.02 ether);

        // Guardian cannot set deposit
        vm.prank(guardian);
        vm.expectRevert(StakeVault.OnlyAdmin.selector);
        vault.setDeposit(0.02 ether);

        // Advance to epoch 24 so interval requirement is satisfied
        vm.warp(start + 24 * 3600);

        // Under lower bound (0.001 ether) reverts ParameterOutOfBounds
        vm.prank(admin);
        vm.expectRevert(StakeVault.ParameterOutOfBounds.selector);
        vault.setDeposit(0.0009 ether);

        // Over upper bound (10 ether) reverts ParameterOutOfBounds
        vm.prank(admin);
        vm.expectRevert(StakeVault.ParameterOutOfBounds.selector);
        vault.setDeposit(10.0001 ether);
    }

    function test_SetDepositMultiplierBounds() public {
        // Must wait MIN_INTERVAL (24 epochs) for first change
        vm.warp(start + 24 * 3600);

        // Initial DEPOSIT is 0.01 ether
        // 1. More than 2x increase: 0.020000000000000001 ether -> reverts
        vm.prank(admin);
        vm.expectRevert(StakeVault.DepositAdjustmentExceedsLimit.selector);
        vault.setDeposit(0.02 ether + 1 wei);

        // 2. Exact 2x increase: 0.02 ether -> succeeds!
        vm.prank(admin);
        vault.setDeposit(0.02 ether);
        assertEq(vault.DEPOSIT(), 0.02 ether);
        assertEq(vault.lastDepositUpdateEpoch(), 24);

        // Advance another 24 epochs (to epoch 48)
        vm.warp(start + 48 * 3600);

        // 3. More than 2x drop: from 0.02 ether to 0.009999999999999999 ether (< half) -> reverts
        vm.prank(admin);
        vm.expectRevert(StakeVault.DepositAdjustmentExceedsLimit.selector);
        vault.setDeposit(0.01 ether - 1 wei);

        // 4. Exact 2x drop (down to half): 0.01 ether -> succeeds!
        vm.prank(admin);
        vault.setDeposit(0.01 ether);
        assertEq(vault.DEPOSIT(), 0.01 ether);
        assertEq(vault.lastDepositUpdateEpoch(), 48);
    }

    function test_SetDepositIntervalEnforcement() public {
        vm.warp(start + 24 * 3600); // Epoch 24
        vm.prank(admin);
        vault.setDeposit(0.015 ether);

        // Attempt second change before 24 epochs have elapsed (e.g. at epoch 24 + 23 = 47)
        vm.warp(start + 47 * 3600);
        vm.prank(admin);
        vm.expectRevert(StakeVault.DepositIntervalNotElapsed.selector);
        vault.setDeposit(0.02 ether);

        // At epoch 48 (exactly 24 epochs elapsed) -> succeeds!
        vm.warp(start + 48 * 3600);
        vm.prank(admin);
        vault.setDeposit(0.02 ether);
        assertEq(vault.DEPOSIT(), 0.02 ether);
    }

    function test_DynamicDepositExactRefund() public {
        vm.warp(start + 1);
        // Alice deposits at old deposit (0.01 ether)
        vm.prank(alice);
        vault.deposit{value: 0.01 ether}();

        // Warp to epoch 24 to allow setDeposit
        vm.warp(start + 24 * 3600);

        // Admin updates deposit to 0.02 ether (within 2x multiplier)
        vm.prank(admin);
        vault.setDeposit(0.02 ether);

        // Bob tries old deposit (0.01 ether) -> fails
        vm.prank(bob);
        vm.expectRevert(StakeVault.BadDeposit.selector);
        vault.deposit{value: 0.01 ether}();

        // Bob deposits at new deposit (0.02 ether) -> succeeds
        vm.prank(bob);
        vault.deposit{value: 0.02 ether}();

        // Alice requests withdraw at epoch 25
        vm.warp(start + 25 * 3600);
        vm.prank(alice);
        vault.requestWithdraw();

        // Bob requests withdraw at epoch 26
        vm.warp(start + 26 * 3600);
        vm.prank(bob);
        vault.requestWithdraw();

        // Advance past cooldown (epoch 25 + 48 = 73 for Alice, 26 + 48 = 74 for Bob)
        vm.warp(start + 75 * 3600);

        uint256 aliceBalBefore = alice.balance;
        vm.prank(alice);
        vault.withdraw();
        // Alice receives exactly 0.01 ether
        assertEq(alice.balance - aliceBalBefore, 0.01 ether, "Alice must receive exactly original 0.01 ether");

        uint256 bobBalBefore = bob.balance;
        vm.prank(bob);
        vault.withdraw();
        // Bob receives exactly 0.02 ether
        assertEq(bob.balance - bobBalBefore, 0.02 ether, "Bob must receive exactly original 0.02 ether");
    }
}
