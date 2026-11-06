// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract AllocationCapsTest is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public alice = address(0x10);
    address public bob = address(0x20);

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
        vm.deal(bob, 10 ether);
    }

    function _mineNonce(uint256 e, address user, uint8 k) internal view returns (uint256) {
        bytes32 es = bytes32(miner.epochSeed(e));
        bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(miner), block.chainid));
        uint256 nonce = 0;
        uint256 t = miner.target(e);
        while (uint256(keccak256(abi.encode(sub, nonce))) > t) {
            nonce++;
        }
        return nonce;
    }

    function test_SingleAddress20PercentCapEnforcedWhenMinersScarce() public {
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        bytes32 slot = keccak256(abi.encode(uint256(1), uint256(10)));
        vm.store(address(miner), slot, bytes32(uint256(1) << 248));

        uint8[] memory ks = new uint8[](6);
        uint256[] memory nonces = new uint256[](6);
        for (uint8 i = 0; i < 6; i++) {
            ks[i] = i;
            nonces[i] = _mineNonce(1, alice, i);
        }

        vm.prank(alice);
        miner.submitShares(ks, nonces);

        vm.warp(start + 7200);

        uint256 eq = miner.epochQuota(1);
        uint256 maxAllowed = (eq * miner.ADDR_CAP_BPS()) / 10000;
        assertEq(maxAllowed, 25_000e18);

        uint256 balBefore = token.balanceOf(alice);
        vm.prank(alice);
        miner.claim(1);
        uint256 reward = token.balanceOf(alice) - balBefore;

        assertTrue(reward <= maxAllowed, "Reward must be <= 20% of epoch quota");
    }

    function test_SingleAddress20PercentCapTriggersWhenAboveCap() public {
        vm.warp(start + 7200);

        bytes32 uwSlot = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), uwSlot, bytes32(uint256(100_000_000_000)));

        bytes32 twSlot = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), twSlot, bytes32(uint256(100_000_000_000)));

        uint256 balBefore = token.balanceOf(alice);
        vm.prank(alice);
        miner.claim(1);
        uint256 reward = token.balanceOf(alice) - balBefore;

        assertEq(reward, 25_000e18, "Must be capped exactly at 20% (25,000 TTK)");
    }

    function test_ParamUpdateDoesNotChangePastUnclaimedEpochReward() public {
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        vm.prank(alice);
        vault.deposit{value: DEPOSIT}();

        // Advance to epoch 1 and sync
        vm.warp(start + 3600);
        miner.initEpochSeed(1);
        miner.sync(1);

        // Record Alice weight in epoch 1
        bytes32 uwSlot = keccak256(abi.encode(alice, keccak256(abi.encode(uint256(1), uint256(15)))));
        vm.store(address(miner), uwSlot, bytes32(uint256(100_000_000_000)));

        bytes32 twSlot = keccak256(abi.encode(uint256(1), uint256(12)));
        vm.store(address(miner), twSlot, bytes32(uint256(100_000_000_000)));

        // Advance to epoch 2 (epoch 1 is ended, but Alice has NOT claimed yet)
        vm.warp(start + 7200);
        miner.initEpochSeed(2);
        miner.sync(1);

        // Verify epoch 1 snapshot cap is 2000 (20%) and S_STAR is 50,000
        assertEq(miner.epochAddrCapBps(1), 2000);
        assertEq(miner.epochSStar(1), 50_000);

        // Admin modifies BOTH S_STAR to 80,000 and ADDR_CAP_BPS to 1000 (10%)
        vm.startPrank(admin);
        miner.setSStar(80_000);
        miner.setAddrCapBps(1000);
        vm.stopPrank();

        assertEq(miner.S_STAR(), 80_000);
        assertEq(miner.ADDR_CAP_BPS(), 1000);

        // Alice claims epoch 1 reward
        uint256 balBefore = token.balanceOf(alice);
        vm.prank(alice);
        miner.claim(1);
        uint256 reward = token.balanceOf(alice) - balBefore;

        // Alice MUST still receive 20% (25,000 TTK), completely unaffected by admin change
        assertEq(reward, 25_000e18, "Past unclaimed epoch must retain its snapshot 20% cap");

        // Now advance to epoch 3 and sync: epoch 3 snapshot adopts new parameters
        vm.warp(start + 3 * 3600);
        miner.initEpochSeed(3);
        miner.sync(1); // syncs epoch 3
        assertEq(miner.epochAddrCapBps(3), 1000, "Future epoch adopts new 10% cap");
        assertEq(miner.epochSStar(3), 80_000, "Future epoch adopts new S_STAR");
    }

    /// @notice Tests modifying parameters BEFORE first sync to an epoch.
    /// @dev Snapshot timing: Parameters are snapshotted in sync() at the moment the loop advances to epoch n.
    function test_ParamUpdateBeforeFirstSyncSnapshotsAtFirstSync() public {
        vm.warp(start + 1);
        miner.initEpochSeed(0);
        // Epoch 0 is synced at initialize
        assertEq(miner.lastSynced(), 0);
        assertEq(miner.epochSStar(0), 50_000);
        assertEq(miner.epochAddrCapBps(0), 2000);

        // Time advances to Epoch 2 without calling sync yet (lastSynced remains 0)
        vm.warp(start + 7200);
        assertEq(miner.epoch(), 2);
        assertEq(miner.lastSynced(), 0);

        // Snapshot values for epoch 1 and 2 are currently unrecorded (0)
        assertEq(miner.epochSStar(1), 0);
        assertEq(miner.epochAddrCapBps(1), 0);

        // Admin updates parameters BEFORE first sync reaches Epoch 1 or 2
        vm.startPrank(admin);
        miner.setSStar(75_000);
        miner.setAddrCapBps(1500); // 15%
        vm.stopPrank();

        // Now sync is executed for 2 steps (catching up epochs 1 and 2)
        miner.sync(2);
        assertEq(miner.lastSynced(), 2);

        // Both Epoch 1 and Epoch 2 snapshot the new parameters at the time of their first sync
        assertEq(miner.epochSStar(1), 75_000, "Epoch 1 snapshot captured S_STAR at first sync");
        assertEq(miner.epochAddrCapBps(1), 1500, "Epoch 1 snapshot captured ADDR_CAP_BPS at first sync");
        assertEq(miner.epochSStar(2), 75_000, "Epoch 2 snapshot captured S_STAR at first sync");
        assertEq(miner.epochAddrCapBps(2), 1500, "Epoch 2 snapshot captured ADDR_CAP_BPS at first sync");

        // Subsequent parameter change in Epoch 2 will NOT modify the already-snapshotted Epoch 1
        vm.prank(admin);
        miner.setAddrCapBps(1000); // 10%
        assertEq(miner.epochAddrCapBps(1), 1500, "Epoch 1 snapshot remains immutable once synced");
        assertEq(miner.epochAddrCapBps(2), 1500, "Epoch 2 snapshot remains immutable once synced");
    }

    function test_WMinTransitionsAcrossDays() public view {
        uint256 WSCALE = 1e10;

        assertEq(miner.wMin(0), 10 * WSCALE);
        assertEq(miner.wMin(719), 10 * WSCALE);

        assertEq(miner.wMin(720), 50 * WSCALE);
        assertEq(miner.wMin(2159), 50 * WSCALE);

        assertEq(miner.wMin(2160), 100 * WSCALE);
        assertEq(miner.wMin(5000), 100 * WSCALE);
    }
}
