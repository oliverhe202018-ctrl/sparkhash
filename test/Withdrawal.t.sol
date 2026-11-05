// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract MaliciousReceiver {
    StakeVault public vault;

    constructor(StakeVault _vault) {
        vault = _vault;
    }

    function attack() external {
        vault.withdraw();
    }

    receive() external payable {
        // Attempt reentrant call to withdraw
        vault.withdraw();
    }
}

contract WithdrawalTest is Test {
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

    function test_CooldownEnforcementBeforeWithdraw() public {
        vm.warp(start + 1); // Epoch 0
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Warp to Epoch 5 and request withdrawal
        vm.warp(start + 5 * 3600);
        vm.prank(alice);
        vault.requestWithdraw();

        // withdrawReqEpoch is set to 5 + 1 = 6.
        // Required epoch to withdraw: >= 6 - 1 + 48 = 53.
        // In Epoch 52, withdraw must revert
        vm.warp(start + 52 * 3600);
        vm.prank(alice);
        vm.expectRevert(StakeVault.CooldownActive.selector);
        vault.withdraw();

        // In Epoch 53, cooldown elapsed, withdraw succeeds
        vm.warp(start + 53 * 3600);
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        vault.withdraw();
        assertEq(alice.balance - balBefore, DEPOSIT);

        // Stake entry deleted
        (bool active, ) = vault.status(alice);
        assertFalse(active);
    }

    function test_CannotSubmitSharesAfterRequestWithdraw() public {
        vm.warp(start + 1); // Epoch 0
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Advance to Epoch 1
        vm.warp(start + 3600);
        miner.initEpochSeed(0);
        miner.initEpochSeed(1);
        miner.sync(1);

        // Fast target
        bytes32 slot = keccak256(abi.encode(uint256(1), uint256(10)));
        vm.store(address(miner), slot, bytes32(uint256(1) << 248));

        // Alice requests withdraw in epoch 1
        vm.prank(alice);
        vault.requestWithdraw();

        // Alice tries to submit shares
        uint8[] memory ks = new uint8[](1);
        ks[0] = 0;
        uint256[] memory nonces = new uint256[](1);
        nonces[0] = 0;

        vm.prank(alice);
        vm.expectRevert(MinerV1.NoActiveStake.selector);
        miner.submitShares(ks, nonces);
    }

    function test_WithdrawalOperationalDuringVaultPause() public {
        vm.warp(start + 1);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Request withdraw at epoch 1
        vm.warp(start + 3600);
        vm.prank(alice);
        vault.requestWithdraw();

        // Wait past 48 epochs cooldown -> epoch 50
        vm.warp(start + 50 * 3600);

        // Admin pauses vault
        vm.prank(admin);
        vault.pause();
        assertTrue(vault.paused());

        // New deposits are blocked
        address charlie = address(0x30);
        vm.deal(charlie, 1 ether);
        vm.prank(charlie);
        vm.expectRevert(StakeVault.VaultIsPaused.selector);
        vault.deposit{value: DEPOSIT}();

        // But Alice's withdrawal MUST succeed (funds never hostage)
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        vault.withdraw();
        assertEq(alice.balance - balBefore, DEPOSIT);
    }

    function test_ReentrancyGuardPreventsRecursiveWithdraw() public {
        vm.warp(start + 1);
        MaliciousReceiver attacker = new MaliciousReceiver(vault);
        vm.deal(address(attacker), 1 ether);

        // Attacker deposits
        vm.prank(address(attacker));
        vault.deposit{value: DEPOSIT}();

        // Request withdraw
        vm.warp(start + 3600);
        vm.prank(address(attacker));
        vault.requestWithdraw();

        // Wait for cooldown
        vm.warp(start + 50 * 3600);

        // Reentrancy attack must revert
        vm.expectRevert();
        attacker.attack();
    }
}
