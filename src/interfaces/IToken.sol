// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface IToken is IERC20 {
    function mint(address to, uint256 amt) external;
    function totalMined() external view returns (uint256);
    function miner() external view returns (address);
    function MAX_SUPPLY() external view returns (uint256);
    function MINING_CAP() external view returns (uint256);
}
