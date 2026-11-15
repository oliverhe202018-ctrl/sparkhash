// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";
import {ProxyAdmin} from "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {ISafe} from "../src/interfaces/ISafe.sol";
import {MockSafe} from "../test/mocks/MockSafe.sol";

contract DeployScript is Script {
    uint256 public startOverride;
    uint256 public delayOverride;

    function setStartOverride(uint256 s) external {
        startOverride = s;
    }

    function setDelayOverride(uint256 d) external {
        delayOverride = d;
    }
    // ERC-1967 admin storage slot
    bytes32 internal constant ADMIN_SLOT = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;

    struct DeployConfig {
        address liquidity;
        address teamBeneficiary;
        address community;
        address timelockProposerSafe;
        address guardianSafe;
        uint256 timelockMinDelay;
        uint256 start;
        uint256 depositBnb;
        uint256 minDeposit;
        uint256 maxDeposit;
        uint256 minDepositIntervalEpochs;
        uint256 maxDepositMultiplier;
    }

    struct DeployedContracts {
        TimelockController timelock;
        Token token;
        StakeVault vault;
        MinerV1 minerImpl;
        TransparentUpgradeableProxy proxy;
        MinerV1 miner;
        ProxyAdmin proxyAdmin;
        address teamVesting;
    }

    function _loadConfig() internal view returns (DeployConfig memory cfg) {
        cfg.liquidity = vm.envOr("LIQUIDITY_WALLET", address(0x1111111111111111111111111111111111111111));
        cfg.teamBeneficiary = vm.envOr("TEAM_BENEFICIARY", vm.envOr("TEAM_VESTING_WALLET", address(0x2222222222222222222222222222222222222222)));
        cfg.community = vm.envOr("COMMUNITY_MULTISIG", address(0x3333333333333333333333333333333333333333));
        cfg.timelockProposerSafe = vm.envOr("TIMELOCK_PROPOSER_SAFE", address(0x4444444444444444444444444444444444444444));
        cfg.guardianSafe = vm.envOr("GUARDIAN_SAFE", address(0x5555555555555555555555555555555555555555));

        // Testnet default: 600s (10 min); Mainnet default: 172800s (48 hours)
        uint256 defaultDelay = block.chainid == 97 ? 600 : 172800;
        cfg.timelockMinDelay = delayOverride != 0 ? delayOverride : vm.envOr("TIMELOCK_MIN_DELAY", defaultDelay);

        // 人工核对提醒: 部署前须人工核对 TIMELOCK_MIN_DELAY 是否与 docs/DISCLOSURE.md 中声明的值一致
        // 环境变量 DISCLOSURE_TIMELOCK_DELAY 作为操作员已完成人工核对的确认输入 (二者不一致时报错拒绝)
        uint256 expectedDisclosureDelay = vm.envOr("DISCLOSURE_TIMELOCK_DELAY", defaultDelay);
        require(
            cfg.timelockMinDelay == expectedDisclosureDelay,
            "Deploy: TIMELOCK_MIN_DELAY does not match operator-confirmed DISCLOSURE_TIMELOCK_DELAY (manual check reminder)"
        );

        uint256 current = block.timestamp;
        uint256 defaultStart = ((current + 7200 + 3599) / 3600) * 3600;
        cfg.start = startOverride != 0 ? startOverride : vm.envOr("START_TIMESTAMP", defaultStart);
        require(
            cfg.start >= block.timestamp + 3600,
            "Deploy: START_TIMESTAMP must be at least 1 hour in the future"
        );
        require(
            cfg.start % 3600 == 0,
            "Deploy: START_TIMESTAMP must be an on-the-hour Unix timestamp (multiple of 3600)"
        );
        cfg.depositBnb = vm.envOr("DEPOSIT_BNB", uint256(0.01 ether));
        cfg.minDeposit = vm.envOr("MIN_DEPOSIT", uint256(0.001 ether));
        cfg.maxDeposit = vm.envOr("MAX_DEPOSIT", uint256(10 ether));
        cfg.minDepositIntervalEpochs = vm.envOr("MIN_DEPOSIT_INTERVAL_EPOCHS", uint256(24));
        cfg.maxDepositMultiplier = vm.envOr("MAX_DEPOSIT_MULTIPLIER", uint256(2));
    }

    function _validateSafe(
        string memory roleName,
        address safeAddr,
        address deployer
    ) internal view returns (address[] memory owners, uint256 threshold) {
        require(safeAddr.code.length > 0, string.concat(roleName, " has no code (must be contract)"));

        // Verify ISafe getThreshold
        try ISafe(safeAddr).getThreshold() returns (uint256 t) {
            threshold = t;
        } catch {
            revert(string.concat(roleName, " does not implement getThreshold() - not a valid Safe"));
        }

        // Verify ISafe getOwners
        try ISafe(safeAddr).getOwners() returns (address[] memory o) {
            owners = o;
        } catch {
            revert(string.concat(roleName, " does not implement getOwners() - not a valid Safe"));
        }

        console.log(string.concat("Safe [", roleName, "] address:"), safeAddr);
        console.log(string.concat("Safe [", roleName, "] threshold:"), threshold);
        console.log(string.concat("Safe [", roleName, "] owners count:"), owners.length);
        for (uint256 i = 0; i < owners.length; i++) {
            console.log(string.concat("  owner[", vm.toString(i), "]:"), owners[i]);
        }

        bool isGuardian = keccak256(bytes(roleName)) == keccak256(bytes("GUARDIAN_SAFE"));
        uint256 minThreshold = isGuardian ? 1 : 2;
        uint256 minOwners = isGuardian ? 2 : 3;

        require(threshold >= minThreshold, string.concat(roleName, " threshold must be >= ", vm.toString(minThreshold)));
        require(owners.length >= minOwners, string.concat(roleName, " owners count must be >= ", vm.toString(minOwners)));
        require(threshold <= owners.length, string.concat(roleName, " threshold cannot exceed owners count"));

        for (uint256 i = 0; i < owners.length; i++) {
            require(owners[i] != deployer, string.concat(roleName, " owner cannot be deployer"));
        }
    }

    function _assertNoIntersection(
        address[] memory setA,
        address[] memory setB
    ) internal pure {
        for (uint256 i = 0; i < setA.length; i++) {
            for (uint256 j = 0; j < setB.length; j++) {
                require(setA[i] != setB[j], "TIMELOCK_PROPOSER_SAFE and GUARDIAN_SAFE signers overlap");
            }
        }
    }

    function _deployCore(
        DeployConfig memory cfg,
        address deployer
    ) internal returns (DeployedContracts memory dc) {
        // 1. TimelockController
        address[] memory proposers = new address[](1);
        proposers[0] = cfg.timelockProposerSafe;
        address[] memory executors = new address[](1);
        executors[0] = address(0);

        dc.timelock = new TimelockController(
            cfg.timelockMinDelay,
            proposers,
            executors,
            deployer
        );
        console.log("1. TimelockController deployed at:", address(dc.timelock));

        // 2. Token
        dc.token = new Token(cfg.liquidity, cfg.teamBeneficiary, cfg.community);
        dc.teamVesting = dc.token.teamVestingWallet();
        console.log("2. Token deployed at:", address(dc.token));
        console.log("2b. TeamVestingWallet deployed at:", dc.teamVesting);

        // 3. StakeVault
        dc.vault = new StakeVault(
            cfg.depositBnb,
            cfg.start,
            deployer,
            cfg.guardianSafe,
            cfg.minDeposit,
            cfg.maxDeposit,
            cfg.minDepositIntervalEpochs,
            cfg.maxDepositMultiplier
        );
        console.log("3. StakeVault deployed at:", address(dc.vault));

        // 4. MinerV1 implementation
        dc.minerImpl = new MinerV1();
        console.log("4. MinerV1 implementation at:", address(dc.minerImpl));

        // 5. Miner Proxy
        bytes memory initData = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(dc.token),
            address(dc.vault),
            deployer,
            cfg.guardianSafe,
            cfg.start
        );
        dc.proxy = new TransparentUpgradeableProxy(
            address(dc.minerImpl),
            deployer,
            initData
        );
        dc.miner = MinerV1(address(dc.proxy));
        console.log("5. Miner proxy deployed at:", address(dc.miner));

        // ProxyAdmin reference
        address proxyAdminAddr = address(uint160(uint256(vm.load(address(dc.proxy), ADMIN_SLOT))));
        dc.proxyAdmin = ProxyAdmin(proxyAdminAddr);
    }

    function _handoverPermissions(
        DeployedContracts memory dc,
        address deployer
    ) internal {
        // 6. Link Token to Miner
        dc.token.setMiner(address(dc.miner));
        console.log("6. Token.setMiner linked to proxy successfully");

        // 7. ProxyAdmin ownership
        dc.proxyAdmin.transferOwnership(address(dc.timelock));
        console.log("7. ProxyAdmin ownership transferred to Timelock:", address(dc.timelock));

        // 8. MinerV1 admin
        dc.miner.transferAdmin(address(dc.timelock));
        console.log("8. MinerV1 admin transferred to Timelock:", address(dc.timelock));

        // 9. StakeVault admin
        dc.vault.transferAdmin(address(dc.timelock));
        console.log("9. StakeVault admin transferred to Timelock:", address(dc.timelock));

        // 10. Renounce deployer admin on TimelockController
        dc.timelock.renounceRole(dc.timelock.DEFAULT_ADMIN_ROLE(), deployer);
        console.log("10. Deployer renounced Timelock DEFAULT_ADMIN_ROLE");
    }

    function _verifyPostDeployment(
        DeployedContracts memory dc,
        DeployConfig memory cfg,
        address deployer
    ) internal {
        require(dc.proxyAdmin.owner() == address(dc.timelock), "Post-check: ProxyAdmin owner mismatch");
        require(dc.miner.admin() == address(dc.timelock), "Post-check: Miner admin mismatch");
        require(dc.vault.admin() == address(dc.timelock), "Post-check: Vault admin mismatch");
        require(dc.miner.guardian() == cfg.guardianSafe, "Post-check: Miner guardian mismatch");
        require(dc.vault.guardian() == cfg.guardianSafe, "Post-check: Vault guardian mismatch");
        require(!dc.timelock.hasRole(dc.timelock.DEFAULT_ADMIN_ROLE(), deployer), "Post-check: Deployer still has Timelock admin role");
        require(dc.timelock.hasRole(dc.timelock.PROPOSER_ROLE(), cfg.timelockProposerSafe), "Post-check: Proposer Safe missing PROPOSER_ROLE");
        require(dc.timelock.hasRole(dc.timelock.EXECUTOR_ROLE(), address(0)), "Post-check: address(0) missing EXECUTOR_ROLE");
        require(dc.timelock.getMinDelay() == cfg.timelockMinDelay, "Post-check: Timelock getMinDelay mismatch");
        require(dc.token.miner() != address(0) && dc.token.miner() == address(dc.miner), "Post-check: Token.miner mismatch");
        require(dc.token.totalSupply() == 100_000_000e18, "Post-check: Token totalSupply mismatch");
        require(dc.token.balanceOf(cfg.liquidity) == 50_000_000e18, "Post-check: Liquidity premint balance mismatch");
        require(dc.token.balanceOf(dc.token.teamVestingWallet()) == 30_000_000e18, "Post-check: Team vesting premint balance mismatch");
        require(dc.token.teamBeneficiary() == cfg.teamBeneficiary, "Post-check: Team beneficiary mismatch");
        require(dc.token.balanceOf(cfg.community) == 20_000_000e18, "Post-check: Community premint balance mismatch");
        require(dc.miner.START() == cfg.start && dc.vault.START() == cfg.start, "Post-check: START timestamp mismatch");
        require(dc.vault.DEPOSIT() == cfg.depositBnb, "Post-check: DEPOSIT value mismatch");
        require(!dc.miner.paused() && !dc.vault.paused(), "Post-check: System initial paused state unexpected");

        // Verify implementation contract cannot be directly initialized
        try dc.minerImpl.initialize(address(dc.token), address(dc.vault), deployer, cfg.guardianSafe, cfg.start) {
            revert("Post-check: Miner implementation can be directly initialized");
        } catch {}
    }

    function run() external {
        uint256 deployerPrivateKey = vm.envOr("PRIVATE_KEY", uint256(0));
        address deployer = deployerPrivateKey != 0 ? vm.addr(deployerPrivateKey) : msg.sender;
        DeployConfig memory cfg = _loadConfig();

        console.log("=== Deploying PoolCoin Ecosystem (Option B Architecture) ===");
        console.log("Chain ID:", block.chainid);
        console.log("Deployer EOA:", deployer);
        console.log("Timelock Proposer Safe:", cfg.timelockProposerSafe);
        console.log("Guardian Safe (Emergency Pause):", cfg.guardianSafe);
        console.log("Timelock Min Delay (seconds):", cfg.timelockMinDelay);

        // Pre-flight governance safety validations
        require(cfg.timelockProposerSafe != address(0), "Zero TIMELOCK_PROPOSER_SAFE");
        require(cfg.guardianSafe != address(0), "Zero GUARDIAN_SAFE");
        require(cfg.timelockProposerSafe != deployer, "Proposer Safe cannot be deployer");
        require(cfg.guardianSafe != deployer, "Guardian Safe cannot be deployer");
        require(cfg.timelockProposerSafe != cfg.guardianSafe, "Proposer Safe and Guardian Safe must be distinct");

        // In local dry-run (chainId 31337) without deployed Safes, instantiate mock Safes for pre-flight check
        if (block.chainid == 31337 && cfg.timelockProposerSafe.code.length == 0) {
            address[] memory mockOwnersA = new address[](3);
            mockOwnersA[0] = address(0x1001);
            mockOwnersA[1] = address(0x1002);
            mockOwnersA[2] = address(0x1003);
            MockSafe mockA = new MockSafe(mockOwnersA, 2);
            cfg.timelockProposerSafe = address(mockA);

            address[] memory mockOwnersB = new address[](3);
            mockOwnersB[0] = address(0x2001);
            mockOwnersB[1] = address(0x2002);
            mockOwnersB[2] = address(0x2003);
            MockSafe mockB = new MockSafe(mockOwnersB, 2);
            cfg.guardianSafe = address(mockB);
        }

        // Validate both Safe contracts: code.length > 0, interface existence, threshold >= 2 (proposer) / >= 1 (guardian), owners >= 3 / >= 2, no deployer
        (address[] memory propOwners, ) = _validateSafe("TIMELOCK_PROPOSER_SAFE", cfg.timelockProposerSafe, deployer);
        (address[] memory guardOwners, ) = _validateSafe("GUARDIAN_SAFE", cfg.guardianSafe, deployer);
        _assertNoIntersection(propOwners, guardOwners);

        if (deployerPrivateKey != 0) {
            vm.startBroadcast(deployerPrivateKey);
        } else {
            vm.startBroadcast();
        }

        DeployedContracts memory dc = _deployCore(cfg, deployer);
        _handoverPermissions(dc, deployer);

        vm.stopBroadcast();

        _verifyPostDeployment(dc, cfg, deployer);

        console.log("=== Deployment and Ownership Handover Completed Successfully ===");
    }
}
