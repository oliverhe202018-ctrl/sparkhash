// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IToken} from "./interfaces/IToken.sol";
import {TeamVestingWallet} from "./TeamVestingWallet.sol";

/// @title Token (SparkHash / SPKH)
/// @notice Non-upgradeable standard ERC-20 token with hardcoded maximum supply and mining cap.
/// @dev Only the designated Miner contract can call mint() for mining emissions.
contract Token is ERC20, IToken {
    uint256 public constant override MAX_SUPPLY = 1_000_000_000e18;
    uint256 public constant override MINING_CAP = 900_000_000e18;
    uint256 public constant PREMINT_TOTAL = 100_000_000e18;
    uint256 public constant TEAM_PREMINT = 30_000_000e18;

    uint256 public override totalMined;
    address public override miner;
    address public immutable deployer;

    address public immutable teamBeneficiary;
    address public immutable teamVestingWallet;

    event MinerSet(address indexed miner);

    error OnlyMiner();
    error OnlyDeployer();
    error MinerAlreadySet();
    error ZeroAddress();
    error MiningCapExceeded();
    error MaxSupplyExceeded();

    constructor(
        address liquidityWallet,
        address _teamBeneficiary,
        address communityMultisig
    ) ERC20("SparkHash", "SPKH") {
        if (liquidityWallet == address(0) || _teamBeneficiary == address(0) || communityMultisig == address(0)) {
            revert ZeroAddress();
        }
        deployer = msg.sender;
        teamBeneficiary = _teamBeneficiary;

        // Deploy Team Vesting Wallet (6 months cliff, 24 months total duration)
        TeamVestingWallet vesting = new TeamVestingWallet(
            _teamBeneficiary,
            uint64(block.timestamp)
        );
        teamVestingWallet = address(vesting);

        // Premint exactly 100M tokens
        _mint(liquidityWallet, 50_000_000e18);
        _mint(teamVestingWallet, TEAM_PREMINT);
        _mint(communityMultisig, 20_000_000e18);
    }

    /// @notice Set the active Miner contract once. Cannot be changed thereafter.
    function setMiner(address _miner) external {
        if (msg.sender != deployer) revert OnlyDeployer();
        if (miner != address(0)) revert MinerAlreadySet();
        if (_miner == address(0)) revert ZeroAddress();
        miner = _miner;
        emit MinerSet(_miner);
    }

    /// @notice Mint tokens for valid mining rewards.
    /// @dev Can only be called by the designated Miner contract.
    function mint(address to, uint256 amt) external override {
        if (msg.sender != miner) revert OnlyMiner();
        if (totalMined + amt > MINING_CAP) revert MiningCapExceeded();
        if (totalSupply() + amt > MAX_SUPPLY) revert MaxSupplyExceeded();

        totalMined += amt;
        _mint(to, amt);
    }
}
