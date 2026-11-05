// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ISafe} from "../../src/interfaces/ISafe.sol";

contract MockSafe is ISafe {
    address[] private _owners;
    uint256 private _threshold;

    constructor(address[] memory owners_, uint256 threshold_) {
        _owners = owners_;
        _threshold = threshold_;
    }

    function getOwners() external view override returns (address[] memory) {
        return _owners;
    }

    function getThreshold() external view override returns (uint256) {
        return _threshold;
    }
}
