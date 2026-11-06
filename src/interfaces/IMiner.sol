// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IMiner {
    event EpochSettled(
        uint256 indexed epoch,
        uint256 effectiveQuota,
        uint256 totalWeight,
        uint256 wMin,
        uint256 unallocatedDilution
    );
    event RewardClaimed(
        address indexed miner,
        uint256 indexed epoch,
        uint256 claimedAmount,
        uint256 cappedOverflow
    );

    function epoch() external view returns (uint256);
    function guardian() external view returns (address);
    function initEpochSeed(uint256 targetEpoch) external;
    function initEpochSeed(uint256 targetEpoch, uint256 maxSteps) external returns (uint256);
    function sync(uint256 maxSteps) external returns (uint256);
    function submitShares(uint8[] calldata ks, uint256[] calldata nonces) external;
    function claim(uint256 e) external;
    function unallocatedGap(uint256 epoch) external view returns (uint256);
    function pauseSubmissions() external;
    function unpause() external;
    function setSStar(uint256 newSStar) external;
    function setAddrCapBps(uint256 newCapBps) external;
    function effectiveQuota(uint256 e) external pure returns (uint256);
    function epochQuota(uint256 e) external pure returns (uint256);
    function dailyQuota(uint256 d) external pure returns (uint256);
    function cumulativeQuotaBefore(uint256 e) external pure returns (uint256);
}
