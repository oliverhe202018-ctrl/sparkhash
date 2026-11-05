// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {ProxyAdmin} from "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// @title VerifyPostDeploy
/// @notice Read-only script to verify all on-chain assertions after broadcasting Deploy.s.sol.
/// @dev Can be run without private keys: forge script script/VerifyPostDeploy.s.sol --rpc-url $RPC_URL -vvv
contract VerifyPostDeploy is Script {
    bytes32 internal constant ADMIN_SLOT = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;

    struct Context {
        address timelock;
        address token;
        address vault;
        address proxy;
        address impl;
        address proposerSafe;
        address guardianSafe;
        address deployer;
        address liquidity;
        address teamVesting;
        address community;
        uint256 start;
        uint256 delay;
        uint256 deposit;
    }

    function _loadContext() internal view returns (Context memory ctx) {
        ctx.timelock = vm.envAddress("DEPLOYED_TIMELOCK");
        ctx.token = vm.envAddress("DEPLOYED_TOKEN");
        ctx.vault = vm.envAddress("DEPLOYED_VAULT");
        ctx.proxy = vm.envAddress("DEPLOYED_PROXY");
        ctx.impl = vm.envAddress("DEPLOYED_MINER_IMPL");

        ctx.proposerSafe = vm.envAddress("TIMELOCK_PROPOSER_SAFE");
        ctx.guardianSafe = vm.envAddress("GUARDIAN_SAFE");
        ctx.deployer = vm.envAddress("DEPLOYER_ADDRESS");
        ctx.liquidity = vm.envAddress("LIQUIDITY_WALLET");
        ctx.teamVesting = vm.envAddress("TEAM_VESTING_WALLET");
        ctx.community = vm.envAddress("COMMUNITY_MULTISIG");

        ctx.start = vm.envUint("START_TIMESTAMP");
        ctx.delay = vm.envOr("TIMELOCK_MIN_DELAY", uint256(600));
        ctx.deposit = vm.envOr("DEPOSIT_BNB", uint256(0.01 ether));
    }

    function _verifyTimelock(Context memory ctx) internal view {
        TimelockController timelock = TimelockController(payable(ctx.timelock));
        require(!timelock.hasRole(timelock.DEFAULT_ADMIN_ROLE(), ctx.deployer), "FAIL: Deployer has Timelock admin role");
        require(timelock.hasRole(timelock.PROPOSER_ROLE(), ctx.proposerSafe), "FAIL: Proposer Safe missing PROPOSER_ROLE");
        require(timelock.hasRole(timelock.CANCELLER_ROLE(), ctx.proposerSafe), "FAIL: Proposer Safe missing CANCELLER_ROLE");
        require(timelock.hasRole(timelock.EXECUTOR_ROLE(), address(0)), "FAIL: address(0) missing EXECUTOR_ROLE");
        require(!timelock.hasRole(timelock.CANCELLER_ROLE(), ctx.guardianSafe), "FAIL: Guardian Safe unexpectedly has CANCELLER_ROLE");
        require(timelock.getMinDelay() == ctx.delay, "FAIL: Timelock minDelay mismatch");
        console.log("[PASS] 1. TimelockController roles and minDelay verified");
    }

    function _verifyProxyAndAdmin(Context memory ctx) internal view {
        address proxyAdminAddr = address(uint160(uint256(vm.load(ctx.proxy, ADMIN_SLOT))));
        require(proxyAdminAddr != address(0), "FAIL: ProxyAdmin address is 0");
        ProxyAdmin proxyAdmin = ProxyAdmin(proxyAdminAddr);
        require(proxyAdmin.owner() == ctx.timelock, "FAIL: ProxyAdmin owner is not Timelock");
        console.log("[PASS] 2. ERC-1967 admin slot and ProxyAdmin.owner verified");
    }

    function _verifyGovernanceEntities(Context memory ctx) internal view {
        MinerV1 miner = MinerV1(ctx.proxy);
        StakeVault vault = StakeVault(ctx.vault);

        require(miner.admin() == ctx.timelock, "FAIL: Miner admin is not Timelock");
        require(vault.admin() == ctx.timelock, "FAIL: Vault admin is not Timelock");
        require(miner.guardian() == ctx.guardianSafe, "FAIL: Miner guardian mismatch");
        require(vault.guardian() == ctx.guardianSafe, "FAIL: Vault guardian mismatch");
        console.log("[PASS] 3. Miner and Vault admin/guardian verified");
    }

    function _verifyTokenState(Context memory ctx) internal view {
        Token token = Token(ctx.token);
        require(token.miner() == ctx.proxy, "FAIL: Token miner is not proxy");
        require(token.totalSupply() == 100_000_000e18, "FAIL: Token total supply mismatch");
        require(token.balanceOf(ctx.liquidity) == 50_000_000e18, "FAIL: Liquidity premint balance mismatch");
        require(token.balanceOf(ctx.teamVesting) == 30_000_000e18, "FAIL: Team vesting premint balance mismatch");
        require(token.balanceOf(ctx.community) == 20_000_000e18, "FAIL: Community premint balance mismatch");
        console.log("[PASS] 4. Token supply, premint allocations, and miner link verified");
    }

    function _verifyParamsAndPaused(Context memory ctx) internal {
        MinerV1 miner = MinerV1(ctx.proxy);
        StakeVault vault = StakeVault(ctx.vault);

        require(miner.START() == ctx.start, "FAIL: Miner START timestamp mismatch");
        require(vault.START() == ctx.start, "FAIL: Vault START timestamp mismatch");
        require(vault.DEPOSIT() == ctx.deposit, "FAIL: Vault DEPOSIT amount mismatch");
        require(!miner.paused(), "FAIL: Miner unexpectedly paused");
        require(!vault.paused(), "FAIL: Vault unexpectedly paused");
        console.log("[PASS] 5. START timestamp, DEPOSIT, and unpaused states verified");

        // Implementation contract cannot be initialized
        MinerV1 minerImpl = MinerV1(ctx.impl);
        try minerImpl.initialize(ctx.token, ctx.vault, ctx.deployer, ctx.guardianSafe, ctx.start) {
            revert("FAIL: Miner implementation can be directly initialized");
        } catch {
            console.log("[PASS] 6. Implementation contract direct initialization protection verified");
        }
    }

    function run() external {
        Context memory ctx = _loadContext();
        console.log("=== Verifying On-chain Deploy State ===");
        console.log("Chain ID:", block.chainid);

        _verifyTimelock(ctx);
        _verifyProxyAndAdmin(ctx);
        _verifyGovernanceEntities(ctx);
        _verifyTokenState(ctx);
        _verifyParamsAndPaused(ctx);

        console.log("=== ALL POST-DEPLOYMENT ON-CHAIN ASSERTIONS PASSED ===");
    }
}