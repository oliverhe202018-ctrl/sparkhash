// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IMiner {
    function epoch() external view returns (uint256);
    function guardian() external view returns (address);
    function initEpochSeed(uint256 e) external;
    function sync(uint256 maxSteps) external returns (uint256);
    function submitShares(uint8[] calldata ks, uint256[] calldata nonces) external;
    function claim(uint256 e) external;
    function pauseSubmissions() external;
    function unpause() external;
    function setSStar(uint256 newSStar) external;
    function setAddrCapBps(uint256 newCapBps) external;
    function effectiveQuota(uint256 e) external pure returns (uint256);
    function epochQuota(uint256 e) external pure returns (uint256);
    function dailyQuota(uint256 d) external pure returns (uint256);
    function cumulativeQuotaBefore(uint256 e) external pure returns (uint256);
}
