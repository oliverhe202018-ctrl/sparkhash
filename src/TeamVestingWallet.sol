// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {VestingWallet} from "@openzeppelin/contracts/finance/VestingWallet.sol";
import {VestingWalletCliff} from "@openzeppelin/contracts/finance/VestingWalletCliff.sol";

/// @title TeamVestingWallet
/// @notice OpenZeppelin VestingWallet with a 6-month cliff and 24-month total linear vesting schedule.
contract TeamVestingWallet is VestingWalletCliff {
    uint64 public constant VESTING_DURATION = 720 days; // 24 months (30 days/month)
    uint64 public constant VESTING_CLIFF = 180 days;    // 6 months (30 days/month)

    /// @notice Constructs the TeamVestingWallet with cliff.
    /// @param beneficiary The address of the beneficiary (owner) entitled to released tokens.
    /// @param startTimestamp The Unix timestamp marking the start of the vesting schedule.
    constructor(
        address beneficiary,
        uint64 startTimestamp
    )
        VestingWallet(beneficiary, startTimestamp, VESTING_DURATION)
        VestingWalletCliff(VESTING_CLIFF)
    {}
}
