// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy, ITransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";
import {ProxyAdmin} from "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol";
import {ERC1967Utils} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Utils.sol";

contract MinerV2 is MinerV1 {
    uint256 public extraFieldV2;

    function version() external pure returns (string memory) {
        return "V2";
    }

    function setExtraFieldV2(uint256 val) external {
        extraFieldV2 = val;
    }
}

contract ProxyUpgradeTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public minerImpl;
    TransparentUpgradeableProxy public proxy;
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

        minerImpl = new MinerV1();
        bytes memory initData = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(token),
            address(vault),
            admin,
            guardian,
            start
        );
        proxy = new TransparentUpgradeableProxy(
            address(minerImpl),
            admin,
            initData
        );
        miner = MinerV1(address(proxy));
        token.setMiner(address(miner));
    }

    function _getProxyAdminAddress() internal view returns (address) {
        bytes32 adminSlot = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;
        return address(uint160(uint256(vm.load(address(proxy), adminSlot))));
    }

    function test_ImplementationCannotBeDirectlyInitialized() public {
        vm.expectRevert();
        minerImpl.initialize(address(token), address(vault), admin, guardian, start);
    }

    function test_ProxyAdminAuthorization() public {
        address proxyAdminAddr = _getProxyAdminAddress();
        ProxyAdmin proxyAdmin = ProxyAdmin(proxyAdminAddr);

        MinerV2 v2Impl = new MinerV2();

        // Alice (unauthorized) attempts to upgrade proxy
        vm.prank(alice);
        vm.expectRevert();
        proxyAdmin.upgradeAndCall(ITransparentUpgradeableProxy(address(proxy)), address(v2Impl), "");

        // Admin (authorized owner) upgrades proxy
        vm.prank(admin);
        proxyAdmin.upgradeAndCall(ITransparentUpgradeableProxy(address(proxy)), address(v2Impl), "");

        // Calling version() on proxy succeeds
        assertEq(MinerV2(address(proxy)).version(), "V2");
    }

    function test_StorageLayoutConsistencyAcrossUpgrade() public {
        // 1. Populate state in V1
        vm.warp(start + 1);
        miner.initEpochSeed(0);

        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        bytes32 slotTarget = keccak256(abi.encode(uint256(1), uint256(10)));
        vm.store(address(miner), slotTarget, bytes32(uint256(1) << 221));

        bytes32 slotUW = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), slotUW, bytes32(uint256(777_888)));

        bytes32 slotTW = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), slotTW, bytes32(uint256(999_000)));

        // Record old values
        address oldToken = address(miner.token());
        address oldVault = address(miner.vault());
        address oldAdmin = miner.admin();
        uint256 oldStart = miner.START();
        uint256 oldLastSynced = miner.lastSynced();
        uint256 oldEma = miner.ema();
        uint256 oldSeed1 = miner.epochSeed(1);
        uint256 oldTarget1 = miner.target(1);
        uint256 oldUW = miner.userWeight(1, alice);
        uint256 oldTW = miner.totalWeight(1);

        // 2. Perform upgrade to MinerV2
        address proxyAdminAddr = _getProxyAdminAddress();
        ProxyAdmin proxyAdmin = ProxyAdmin(proxyAdminAddr);
        MinerV2 v2Impl = new MinerV2();

        vm.prank(admin);
        proxyAdmin.upgradeAndCall(ITransparentUpgradeableProxy(address(proxy)), address(v2Impl), "");

        // 3. Verify old state variables have ZERO misalignments or corruptions
        MinerV2 minerV2 = MinerV2(address(proxy));
        assertEq(address(minerV2.token()), oldToken, "Token address misaligned");
        assertEq(address(minerV2.vault()), oldVault, "Vault address misaligned");
        assertEq(minerV2.admin(), oldAdmin, "Admin address misaligned");
        assertEq(minerV2.START(), oldStart, "START misaligned");
        assertEq(minerV2.lastSynced(), oldLastSynced, "lastSynced misaligned");
        assertEq(minerV2.ema(), oldEma, "ema misaligned");
        assertEq(minerV2.epochSeed(1), oldSeed1, "epochSeed misaligned");
        assertEq(minerV2.target(1), oldTarget1, "target misaligned");
        assertEq(minerV2.userWeight(1, alice), oldUW, "userWeight misaligned");
        assertEq(minerV2.totalWeight(1), oldTW, "totalWeight misaligned");

        // 4. Verify V2 functionality
        assertEq(minerV2.version(), "V2");
        minerV2.setExtraFieldV2(42);
        assertEq(minerV2.extraFieldV2(), 42);
    }
}
