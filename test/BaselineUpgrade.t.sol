// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy, ITransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";
import {ProxyAdmin} from "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol";

contract BaselineUpgradeTest is Test {
    Token public token;
    StakeVault public vault;
    TransparentUpgradeableProxy public proxy;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public alice = address(0x10);
    address public bob = address(0x20);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

    struct Snapshot {
        address token;
        address vault;
        address admin;
        address guardian;
        uint256 start;
        uint256 sStar;
        uint256 addrCapBps;
        uint256 lastSynced;
        uint256 ema;
        uint256 seed0;
        uint256 seed1;
        bool claimed0Alice;
        bool claimed1Bob;
    }

    function _getProxyAdminAddress(address proxyAddr) internal view returns (address) {
        bytes32 adminSlot = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;
        return address(uint160(uint256(vm.load(proxyAddr, adminSlot))));
    }

    function setUp() public {
        start = block.timestamp + 1000;

        token = new Token(liquidity, team, community);
        vault = new StakeVault(DEPOSIT, start, admin, guardian, 0.001 ether, 10 ether, 24, 2);

        // Deploy baseline implementation from be37275 artifact
        address oldImpl = deployCode("test/fixtures/MinerV1_be37275.json");
        require(oldImpl != address(0), "oldImpl deployment failed");

        bytes memory initData = abi.encodeWithSignature(
            "initialize(address,address,address,address,uint256)",
            address(token),
            address(vault),
            admin,
            guardian,
            start
        );

        proxy = new TransparentUpgradeableProxy(oldImpl, admin, initData);
        token.setMiner(address(proxy));
    }

    function _recordSnapshot() internal view returns (Snapshot memory s) {
        (, bytes memory retToken) = address(proxy).staticcall(abi.encodeWithSignature("token()"));
        s.token = abi.decode(retToken, (address));

        (, bytes memory retVault) = address(proxy).staticcall(abi.encodeWithSignature("vault()"));
        s.vault = abi.decode(retVault, (address));

        (, bytes memory retAdmin) = address(proxy).staticcall(abi.encodeWithSignature("admin()"));
        s.admin = abi.decode(retAdmin, (address));

        (, bytes memory retGuardian) = address(proxy).staticcall(abi.encodeWithSignature("guardian()"));
        s.guardian = abi.decode(retGuardian, (address));

        (, bytes memory retStart) = address(proxy).staticcall(abi.encodeWithSignature("START()"));
        s.start = abi.decode(retStart, (uint256));

        (, bytes memory retSStar) = address(proxy).staticcall(abi.encodeWithSignature("S_STAR()"));
        s.sStar = abi.decode(retSStar, (uint256));

        (, bytes memory retAddrCapBps) = address(proxy).staticcall(abi.encodeWithSignature("ADDR_CAP_BPS()"));
        s.addrCapBps = abi.decode(retAddrCapBps, (uint256));

        (, bytes memory retLastSynced) = address(proxy).staticcall(abi.encodeWithSignature("lastSynced()"));
        s.lastSynced = abi.decode(retLastSynced, (uint256));

        (, bytes memory retEma) = address(proxy).staticcall(abi.encodeWithSignature("ema()"));
        s.ema = abi.decode(retEma, (uint256));

        (, bytes memory retSeed0) = address(proxy).staticcall(abi.encodeWithSignature("epochSeed(uint256)", 0));
        s.seed0 = abi.decode(retSeed0, (uint256));

        (, bytes memory retSeed1) = address(proxy).staticcall(abi.encodeWithSignature("epochSeed(uint256)", 1));
        s.seed1 = abi.decode(retSeed1, (uint256));

        (, bytes memory retClaimed0Alice) = address(proxy).staticcall(abi.encodeWithSignature("claimed(uint256,address)", 0, alice));
        s.claimed0Alice = abi.decode(retClaimed0Alice, (bool));

        (, bytes memory retClaimed1Bob) = address(proxy).staticcall(abi.encodeWithSignature("claimed(uint256,address)", 1, bob));
        s.claimed1Bob = abi.decode(retClaimed1Bob, (bool));
    }

    function test_UpgradeFromBaselineBe37275() public {
        // --- 1. Populate state on old implementation (be37275) ---
        vm.warp(start + 1);
        vm.roll(10);

        (bool ok,) = address(proxy).call(abi.encodeWithSignature("initEpochSeed(uint256)", 0));
        require(ok, "old initEpochSeed(0) failed");

        vm.warp(start + 3601);
        vm.roll(20);

        (ok,) = address(proxy).call(abi.encodeWithSignature("initEpochSeed(uint256)", 1));
        require(ok, "old initEpochSeed(1) failed");

        (ok,) = address(proxy).call(abi.encodeWithSignature("sync(uint256)", 1));
        require(ok, "old sync(1) failed");

        // Store claimed states (mapping at slot 16): claimed[epoch][miner]
        bytes32 slotClaimedAlice0 = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(0), uint256(16)))));
        vm.store(address(proxy), slotClaimedAlice0, bytes32(uint256(1)));

        bytes32 slotClaimedBob1 = keccak256(abi.encode(bob, keccak256(abi.encode(uint256(1), uint256(16)))));
        vm.store(address(proxy), slotClaimedBob1, bytes32(uint256(1)));

        // Store userWeight, totalWeight, totalShares
        bytes32 slotUW = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(0), uint256(15)))));
        vm.store(address(proxy), slotUW, bytes32(uint256(500_000)));

        bytes32 slotTW = keccak256(abi.encode(uint256(0), uint256(12)));
        vm.store(address(proxy), slotTW, bytes32(uint256(1_000_000)));

        bytes32 slotTS = keccak256(abi.encode(uint256(0), uint256(11)));
        vm.store(address(proxy), slotTS, bytes32(uint256(10)));

        // Record old state snapshot before upgrade
        Snapshot memory s = _recordSnapshot();
        assertTrue(s.seed0 != 0, "Seed0 must be initialized");
        assertTrue(s.seed1 != 0, "Seed1 must be initialized");
        assertTrue(s.claimed0Alice, "Claimed0Alice must be true");
        assertTrue(s.claimed1Bob, "Claimed1Bob must be true");

        // --- 2. Perform upgrade to current MinerV1 implementation ---
        address proxyAdminAddr = _getProxyAdminAddress(address(proxy));
        ProxyAdmin proxyAdmin = ProxyAdmin(proxyAdminAddr);

        MinerV1 currentImpl = new MinerV1();

        vm.prank(admin);
        proxyAdmin.upgradeAndCall(ITransparentUpgradeableProxy(address(proxy)), address(currentImpl), "");

        // --- 3. Assert old state reads remain identical ---
        MinerV1 miner = MinerV1(address(proxy));

        assertEq(address(miner.token()), s.token, "token mismatch");
        assertEq(address(miner.vault()), s.vault, "vault mismatch");
        assertEq(miner.admin(), s.admin, "admin mismatch");
        assertEq(miner.guardian(), s.guardian, "guardian mismatch");
        assertEq(miner.START(), s.start, "START mismatch");
        assertEq(miner.S_STAR(), s.sStar, "S_STAR mismatch");
        assertEq(miner.ADDR_CAP_BPS(), s.addrCapBps, "ADDR_CAP_BPS mismatch");
        assertEq(miner.lastSynced(), s.lastSynced, "lastSynced mismatch");
        assertEq(miner.ema(), s.ema, "ema mismatch");
        assertEq(miner.epochSeed(0), s.seed0, "epochSeed(0) mismatch");
        assertEq(miner.epochSeed(1), s.seed1, "epochSeed(1) mismatch");
        assertEq(miner.claimed(0, alice), true, "claimed(0, alice) mismatch");
        assertEq(miner.claimed(1, bob), true, "claimed(1, bob) mismatch");
        assertEq(miner.claimed(0, bob), false, "claimed(0, bob) must be false");
        assertEq(miner.claimed(1, alice), false, "claimed(1, alice) must be false");
        assertEq(miner.userWeight(0, alice), 500_000, "userWeight mismatch");
        assertEq(miner.totalWeight(0), 1_000_000, "totalWeight mismatch");
        assertEq(miner.totalShares(0), 10, "totalShares mismatch");

        // --- 4. Assert unallocatedGap is 0 for historical / uninitialized epochs ---
        assertEq(miner.unallocatedGap(0), 0, "unallocatedGap(0) must be 0");
        assertEq(miner.unallocatedGap(1), 0, "unallocatedGap(1) must be 0");
        assertEq(miner.unallocatedGap(2), 0, "unallocatedGap(2) must be 0");
        assertEq(miner.unallocatedGap(999), 0, "unallocatedGap(999) must be 0");

        // --- 5. Verify new functions are available and working ---
        // Advance time to epoch 2
        vm.warp(start + 2 * 3600 + 1);
        vm.roll(30);

        // (a) Test InvalidMaxSteps revert
        vm.expectRevert(MinerV1.InvalidMaxSteps.selector);
        miner.initEpochSeed(2, 0);

        // (b) Test batched initEpochSeed(targetEpoch, maxSteps)
        vm.warp(start + 5 * 3600 + 1);
        vm.roll(50);

        uint256 lastInit = miner.initEpochSeed(5, 10);
        assertEq(lastInit, 5, "lastInit should reach epoch 5");
        assertTrue(miner.epochSeed(2) != 0, "epochSeed(2) should be initialized");
        assertTrue(miner.epochSeed(3) != 0, "epochSeed(3) should be initialized");
        assertTrue(miner.epochSeed(4) != 0, "epochSeed(4) should be initialized");
        assertTrue(miner.epochSeed(5) != 0, "epochSeed(5) should be initialized");

        // (c) Test backward-compatible initEpochSeed(targetEpoch)
        vm.warp(start + 6 * 3600 + 1);
        vm.roll(60);
        miner.initEpochSeed(6);
        assertTrue(miner.epochSeed(6) != 0, "epochSeed(6) should be initialized via legacy wrapper");

        // (d) Test sync settles epoch and sets unallocatedGap
        vm.store(address(proxy), bytes32(uint256(21)), bytes32(uint256(900_000_000e18)));
        miner.sync(2);
        uint256 gap1 = miner.unallocatedGap(1);
        assertTrue(gap1 > 0, "unallocatedGap(1) should be populated after sync");
    }
}
