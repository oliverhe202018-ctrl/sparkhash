// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Token} from "../src/Token.sol";
import {IToken} from "../src/interfaces/IToken.sol";

contract TokenTest is Test {
    Token public token;

    address public deployer = address(0x1);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);
    address public miner = address(0x5);
    address public alice = address(0x6);

    function setUp() public {
        vm.prank(deployer);
        token = new Token(liquidity, team, community);
    }

    function test_PremintDistribution() public view {
        assertEq(token.balanceOf(liquidity), 50_000_000e18);
        assertEq(token.balanceOf(team), 30_000_000e18);
        assertEq(token.balanceOf(community), 20_000_000e18);
        assertEq(token.totalSupply(), 100_000_000e18);
        assertEq(token.totalMined(), 0);
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
