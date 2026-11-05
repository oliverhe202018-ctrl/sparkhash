// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy, ITransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";
import {ProxyAdmin} from "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

contract GovernancePermissionsTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;
    ProxyAdmin public proxyAdmin;
    TimelockController public timelock;

    address public deployer = address(0xDE0);
    address public guardian = address(0x60A8D); // Multi-sig guardian
    address public timelockProposer = address(0x713E);
    address public alice = address(0x10);

    uint256 public constant TIMELOCK_DELAY = 2 days; // 172,800 seconds
    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

    function setUp() public {
        start = block.timestamp + 1000;

        // Deploy TimelockController
        address[] memory proposers = new address[](1);
        proposers[0] = timelockProposer;
        address[] memory executors = new address[](1);
        executors[0] = timelockProposer; // or address(0) for open execution after delay

        vm.startPrank(deployer);
        timelock = new TimelockController(TIMELOCK_DELAY, proposers, executors, address(0));

        token = new Token(address(0x2), address(0x3), address(0x4));

        vault = new StakeVault(
            DEPOSIT,
            start,
            address(timelock),
            guardian,
            0.001 ether,
            10 ether,
            24,
            2
        );

        MinerV1 impl = new MinerV1();
        bytes memory initData = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(token),
            address(vault),
            address(timelock),
            guardian,
            start
        );

        TransparentUpgradeableProxy proxy = new TransparentUpgradeableProxy(
            address(impl),
            deployer, // temporary ProxyAdmin owner
            initData
        );
        miner = MinerV1(address(proxy));
        token.setMiner(address(miner));

        // Transfer ProxyAdmin ownership to Timelock
        bytes32 adminSlot = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;
        address proxyAdminAddr = address(uint160(uint256(vm.load(address(proxy), adminSlot))));
        proxyAdmin = ProxyAdmin(proxyAdminAddr);
        proxyAdmin.transferOwnership(address(timelock));

        vm.stopPrank();

        // Deal ETH for tests
        vm.deal(alice, 10 ether);
    }

    // --- Guardian Emergency Pause Tests ---

    function test_GuardianCanPauseSubmissionsInstantly() public {
        assertFalse(miner.paused());

        // Guardian pauses submissions with ZERO delay
        vm.prank(guardian);
        miner.pauseSubmissions();

        assertTrue(miner.paused(), "Submissions must be paused immediately by guardian");
    }

    function test_GuardianCanPauseVaultInstantly() public {
        assertFalse(vault.paused());

        // Guardian pauses vault deposits with ZERO delay
        vm.prank(guardian);
        vault.pause();

        assertTrue(vault.paused(), "Vault must be paused immediately by guardian");
    }

    function test_UnauthorizedUserCannotPause() public {
        vm.prank(alice);
        vm.expectRevert(MinerV1.OnlyAdminOrGuardian.selector);
        miner.pauseSubmissions();

        vm.prank(alice);
        vm.expectRevert(StakeVault.OnlyAdminOrGuardian.selector);
        vault.pause();
    }

    // --- Guardian Cannot Perform Any Other Action (Must Revert) ---

    function test_GuardianCannotUnpauseSubmissions() public {
        vm.prank(guardian);
        miner.pauseSubmissions();

        // Guardian attempts unpause -> REVERTS OnlyAdmin
        vm.prank(guardian);
        vm.expectRevert(MinerV1.OnlyAdmin.selector);
        miner.unpause();
    }

    function test_GuardianCannotUnpauseVault() public {
        vm.prank(guardian);
        vault.pause();

        // Guardian attempts unpause -> REVERTS OnlyAdmin
        vm.prank(guardian);
        vm.expectRevert(StakeVault.OnlyAdmin.selector);
        vault.unpause();
    }

    function test_GuardianCannotSetSStar() public {
        vm.prank(guardian);
        vm.expectRevert(MinerV1.OnlyAdmin.selector);
        miner.setSStar(60_000);
    }

    function test_GuardianCannotSetAddrCapBps() public {
        vm.prank(guardian);
        vm.expectRevert(MinerV1.OnlyAdmin.selector);
        miner.setAddrCapBps(1000);
    }

    function test_GuardianCannotSetDeposit() public {
        vm.prank(guardian);
        vm.expectRevert(StakeVault.OnlyAdmin.selector);
        vault.setDeposit(0.02 ether);
    }

    function test_GuardianCannotTransferAdmin() public {
        vm.prank(guardian);
        vm.expectRevert(MinerV1.OnlyAdmin.selector);
        miner.transferAdmin(guardian);

        vm.prank(guardian);
        vm.expectRevert(StakeVault.OnlyAdmin.selector);
        vault.transferAdmin(guardian);
    }

    function test_GuardianCannotUpgradeProxy() public {
        MinerV1 newImpl = new MinerV1();

        vm.prank(guardian);
        vm.expectRevert(); // OwnableUnauthorizedAccount: caller is not Timelock
        proxyAdmin.upgradeAndCall(
            ITransparentUpgradeableProxy(address(miner)),
            address(newImpl),
            ""
        );
    }

    // --- Timelock Delay Does Not Affect Guardian Pause ---

    function test_TimelockDelayDoesNotAffectGuardianEmergencyPause() public {
        // 1. Guardian can pause at timestamp T=start with 0 seconds delay
        vm.warp(start);
        vm.prank(guardian);
        miner.pauseSubmissions();
        assertTrue(miner.paused());

        // 2. To unpause, Timelock MUST schedule with 2-day delay
        bytes memory unpauseData = abi.encodeWithSelector(MinerV1.unpause.selector);
        bytes32 salt = bytes32(uint256(1));

        vm.prank(timelockProposer);
        timelock.schedule(
            address(miner),
            0,
            unpauseData,
            bytes32(0),
            salt,
            TIMELOCK_DELAY
        );

        // Attempting to execute unpause immediately (before 2 days) REVERTS
        vm.prank(timelockProposer);
        vm.expectRevert();
        timelock.execute(
            address(miner),
            0,
            unpauseData,
            bytes32(0),
            salt
        );

        // 3. During the 2-day wait, Guardian pause status remains strictly enforced
        assertTrue(miner.paused());

        // 4. After 2-day Timelock delay elapses, execution succeeds
        vm.warp(start + TIMELOCK_DELAY);

        vm.prank(timelockProposer);
        timelock.execute(
            address(miner),
            0,
            unpauseData,
            bytes32(0),
            salt
        );

        assertFalse(miner.paused(), "Submissions unpaused through Timelock after delay");

        // 5. Guardian can IMMEDIATELY re-pause if another emergency arises (zero delay)
        vm.prank(guardian);
        miner.pauseSubmissions();
        assertTrue(miner.paused(), "Guardian re-paused immediately with zero delay");
    }

    function test_TimelockAdminParamUpdatesRequireDelay() public {
        // Warp to epoch 24
        vm.warp(start + 24 * 3600);

        bytes memory setSStarData = abi.encodeWithSelector(MinerV1.setSStar.selector, 70_000);
        bytes32 salt = bytes32(uint256(2));

        vm.prank(timelockProposer);
        timelock.schedule(
            address(miner),
            0,
            setSStarData,
            bytes32(0),
            salt,
            TIMELOCK_DELAY
        );

        // Cannot execute before delay
        vm.prank(timelockProposer);
        vm.expectRevert();
        timelock.execute(address(miner), 0, setSStarData, bytes32(0), salt);

        // Advance 2 days
        vm.warp(start + 24 * 3600 + TIMELOCK_DELAY);

        vm.prank(timelockProposer);
        timelock.execute(address(miner), 0, setSStarData, bytes32(0), salt);

        assertEq(miner.S_STAR(), 70_000, "S_STAR updated through Timelock after delay");
    }
    function test_CancellerRoleAssignedToProposerNotGuardian() public view {
        assertTrue(timelock.hasRole(timelock.CANCELLER_ROLE(), timelockProposer), "Proposer has CANCELLER_ROLE by OZ default");
        assertFalse(timelock.hasRole(timelock.CANCELLER_ROLE(), guardian), "Guardian does NOT have CANCELLER_ROLE");
        assertFalse(timelock.hasRole(timelock.PROPOSER_ROLE(), guardian), "Guardian does NOT have PROPOSER_ROLE");
        assertFalse(timelock.hasRole(timelock.DEFAULT_ADMIN_ROLE(), guardian), "Guardian does NOT have DEFAULT_ADMIN_ROLE");
    }
}