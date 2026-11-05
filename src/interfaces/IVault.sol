// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IVault {
    function status(address u) external view returns (bool active, uint256 since);
    function START() external view returns (uint256);
    function EPOCH_LEN() external view returns (uint256);
    function DEPOSIT() external view returns (uint256);
    function COOLDOWN_EPOCHS() external view returns (uint256);
    function paused() external view returns (bool);
    function guardian() external view returns (address);
    function MIN_DEPOSIT() external view returns (uint256);
    function MAX_DEPOSIT() external view returns (uint256);
    function MIN_DEPOSIT_INTERVAL_EPOCHS() external view returns (uint256);
    function MAX_DEPOSIT_MULTIPLIER() external view returns (uint256);
    function lastDepositUpdateEpoch() external view returns (uint256);
    function setDeposit(uint256 newDeposit) external;
}
