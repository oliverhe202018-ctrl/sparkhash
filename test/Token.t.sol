// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {IToken} from "../src/interfaces/IToken.sol";
import {TeamVestingWallet} from "../src/TeamVestingWallet.sol";

contract TokenTest is Test {
    Token public token;
    TeamVestingWallet public vesting;

    address public deployer = address(0x1);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public miner = address(0x5);
    address public alice = address(0x6);

    function setUp() public {
        vm.prank(deployer);
        token = new Token(liquidity, team, community);
        vesting = TeamVestingWallet(payable(token.teamVestingWallet()));
    }

    function test_Metadata() public view {
        assertEq(token.name(), "SparkHash");
        assertEq(token.symbol(), "SPKH");
        assertEq(token.decimals(), 18);
    }

    function test_PremintDistribution() public view {
        assertEq(token.balanceOf(liquidity), 50_000_000e18);
        assertEq(token.balanceOf(address(vesting)), 30_000_000e18);
        assertEq(token.balanceOf(team), 0);
        assertEq(token.balanceOf(community), 20_000_000e18);
        assertEq(token.totalSupply(), 100_000_000e18);
        assertEq(token.totalMined(), 0);
    }

    function test_VestingWalletConfiguration() public view {
        assertEq(token.teamBeneficiary(), team);
        assertEq(token.teamVestingWallet(), address(vesting));
        assertEq(vesting.owner(), team);
        assertEq(vesting.start(), block.timestamp);
        assertEq(vesting.duration(), 720 days);
        assertEq(vesting.cliff(), block.timestamp + 180 days);
        assertEq(vesting.end(), block.timestamp + 720 days);
    }

    function test_VestingScheduleBeforeCliff() public {
        // Warp 90 days forward (halfway to cliff)
        vm.warp(block.timestamp + 90 days);

        assertEq(vesting.releasable(address(token)), 0);
        assertEq(vesting.vestedAmount(address(token), uint64(block.timestamp)), 0);

        // Calling release releases 0 tokens
        vesting.release(address(token));
        assertEq(token.balanceOf(team), 0);
        assertEq(token.balanceOf(address(vesting)), 30_000_000e18);
    }

    function test_VestingScheduleAtCliff() public {
        uint256 startTs = block.timestamp;
        // Warp exactly to cliff (180 days)
        vm.warp(startTs + 180 days);

        // Exactly 25% (6 / 24 months) vested at cliff: 30,000,000 * 180 / 720 = 7,500,000
        uint256 expectedCliffAmount = 7_500_000e18;
        assertEq(vesting.releasable(address(token)), expectedCliffAmount);
        assertEq(vesting.vestedAmount(address(token), uint64(block.timestamp)), expectedCliffAmount);

        // Release tokens to beneficiary
        vesting.release(address(token));
        assertEq(token.balanceOf(team), expectedCliffAmount);
        assertEq(token.balanceOf(address(vesting)), 30_000_000e18 - expectedCliffAmount);
        assertEq(vesting.released(address(token)), expectedCliffAmount);
        assertEq(vesting.releasable(address(token)), 0);
    }

    function test_VestingScheduleLinearReleaseToCompletion() public {
        uint256 startTs = block.timestamp;

        // 1. Warp to 12 months (360 days, 50% through schedule)
        vm.warp(startTs + 360 days);
        uint256 halfAmount = 15_000_000e18;
        assertEq(vesting.releasable(address(token)), halfAmount);

        vesting.release(address(token));
        assertEq(token.balanceOf(team), halfAmount);
        assertEq(vesting.released(address(token)), halfAmount);

        // 2. Warp to 24 months (720 days, 100% completed)
        vm.warp(startTs + 720 days);
        assertEq(vesting.releasable(address(token)), 15_000_000e18);

        vesting.release(address(token));
        assertEq(token.balanceOf(team), 30_000_000e18);
        assertEq(token.balanceOf(address(vesting)), 0);
        assertEq(vesting.released(address(token)), 30_000_000e18);
        assertEq(vesting.releasable(address(token)), 0);

        // 3. Beyond 24 months (e.g. 1000 days), no more tokens
        vm.warp(startTs + 1000 days);
        assertEq(vesting.releasable(address(token)), 0);
    }

    function test_SetMinerOnlyDeployer() public {
        vm.prank(alice);
        vm.expectRevert(Token.OnlyDeployer.selector);
        token.setMiner(miner);

        vm.prank(deployer);
        token.setMiner(miner);
        assertEq(token.miner(), miner);

        // Cannot set miner a second time
        vm.prank(deployer);
        vm.expectRevert(Token.MinerAlreadySet.selector);
        token.setMiner(alice);
    }

    function test_OnlyMinerCanMint() public {
        vm.prank(deployer);
        token.setMiner(miner);

        vm.prank(alice);
        vm.expectRevert(Token.OnlyMiner.selector);
        token.mint(alice, 100e18);

        vm.prank(miner);
        token.mint(alice, 100e18);
        assertEq(token.balanceOf(alice), 100e18);
        assertEq(token.totalMined(), 100e18);
        assertEq(token.totalSupply(), 100_000_100e18);
    }

    function test_MiningCapEnforcement() public {
        vm.prank(deployer);
        token.setMiner(miner);

        // Mint up to 900M
        vm.prank(miner);
        token.mint(alice, 900_000_000e18);
        assertEq(token.totalMined(), 900_000_000e18);
        assertEq(token.totalSupply(), 1_000_000_000e18);

        // Exceeding mining cap reverts
        vm.prank(miner);
        vm.expectRevert(Token.MiningCapExceeded.selector);
        token.mint(alice, 1);
    }
}
