// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IVault} from "./interfaces/IVault.sol";

/// @title StakeVault
/// @notice Non-upgradeable fixed-deposit staking vault for proof-of-work participants.
/// @dev Administrator has zero ability to seize or redirect user funds.
contract StakeVault is IVault, ReentrancyGuard {
    uint256 public override DEPOSIT;
    uint256 public immutable override COOLDOWN_EPOCHS;
    uint256 public immutable override EPOCH_LEN;
    uint256 public immutable override START;

    address public admin;
    address public override guardian;
    bool public override paused;

    uint256 public immutable override MIN_DEPOSIT;
    uint256 public immutable override MAX_DEPOSIT;
    uint256 public immutable override MIN_DEPOSIT_INTERVAL_EPOCHS;
    uint256 public immutable override MAX_DEPOSIT_MULTIPLIER;
    uint256 public override lastDepositUpdateEpoch;

    struct S {
        uint64 depositEpoch;
        uint64 withdrawReqEpoch;
        bool has;
        uint128 depositAmount;
    }

    mapping(address => S) public stakes;

    event Deposited(address indexed u, uint256 epoch, uint256 amount);
    event WithdrawRequested(address indexed u, uint256 epoch);
    event Withdrawn(address indexed u, uint256 amount);
    event VaultPaused(address indexed account);
    event VaultUnpaused(address indexed account);
    event DepositUpdated(uint256 oldDeposit, uint256 newDeposit);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);
    event GuardianTransferred(address indexed oldGuardian, address indexed newGuardian);

    error BadDeposit();
    error NoStake();
    error CooldownActive();
    error NotStarted();
    error SendFailed();
    error VaultIsPaused();
    error OnlyAdmin();
    error OnlyAdminOrGuardian();
    error ZeroAddress();
    error ParameterOutOfBounds();
    error DepositAdjustmentExceedsLimit();
    error DepositIntervalNotElapsed();

    constructor(
        uint256 d,
        uint256 start,
        address _admin,
        address _guardian,
        uint256 _minDeposit,
        uint256 _maxDeposit,
        uint256 _minDepositIntervalEpochs,
        uint256 _maxDepositMultiplier
    ) {
        if (_admin == address(0) || _guardian == address(0)) revert ZeroAddress();
        if (_minDeposit == 0 || _minDeposit > _maxDeposit || d < _minDeposit || d > _maxDeposit) {
            revert ParameterOutOfBounds();
        }
        if (_maxDepositMultiplier < 1) revert ParameterOutOfBounds();

        DEPOSIT = d;
        COOLDOWN_EPOCHS = 48;
        EPOCH_LEN = 3600;
        START = start;
        admin = _admin;
        guardian = _guardian;
        MIN_DEPOSIT = _minDeposit;
        MAX_DEPOSIT = _maxDeposit;
        MIN_DEPOSIT_INTERVAL_EPOCHS = _minDepositIntervalEpochs;
        MAX_DEPOSIT_MULTIPLIER = _maxDepositMultiplier;
        lastDepositUpdateEpoch = 0;
    }

    function epoch() public view returns (uint256) {
        return block.timestamp < START ? 0 : (block.timestamp - START) / EPOCH_LEN;
    }

    /// @notice Deposit required BNB amount. Can be paused in emergencies.
    function deposit() external payable {
        if (paused) revert VaultIsPaused();
        if (block.timestamp < START) revert NotStarted();
        if (msg.value != DEPOSIT || stakes[msg.sender].has) revert BadDeposit();

        stakes[msg.sender] = S(uint64(epoch()), 0, true, uint128(msg.value));
        emit Deposited(msg.sender, epoch(), msg.value);
    }

    /// @notice Request withdrawal and enter cooldown window.
    function requestWithdraw() external {
        S storage s = stakes[msg.sender];
        if (!s.has || s.withdrawReqEpoch != 0) revert NoStake();

        s.withdrawReqEpoch = uint64(epoch()) + 1;
        emit WithdrawRequested(msg.sender, epoch());
    }

    /// @notice Withdraw deposit after cooldown. Refunds exact deposited amount recorded. NEVER PAUSABLE.
    function withdraw() external nonReentrant {
        S storage s = stakes[msg.sender];
        if (!s.has || s.withdrawReqEpoch == 0) revert NoStake();
        if (epoch() < uint256(s.withdrawReqEpoch) - 1 + COOLDOWN_EPOCHS) revert CooldownActive();

        uint256 refund = uint256(s.depositAmount);
        delete stakes[msg.sender];
        emit Withdrawn(msg.sender, refund);

        (bool ok,) = msg.sender.call{value: refund}("");
        if (!ok) revert SendFailed();
    }

    /// @notice Returns whether address has active stake (deposited and not requested withdraw) and deposit epoch
    function status(address u) external view override returns (bool active, uint256 since) {
        S memory s = stakes[u];
        return (s.has && s.withdrawReqEpoch == 0, s.depositEpoch);
    }

    /// @notice Adjust required deposit for future stakers with rate-limit and bounds check. Does not affect existing deposits.
    function setDeposit(uint256 newDeposit) external override {
        if (msg.sender != admin) revert OnlyAdmin();
        if (newDeposit < MIN_DEPOSIT || newDeposit > MAX_DEPOSIT) revert ParameterOutOfBounds();

        uint256 curEpoch = epoch();
        if (curEpoch < lastDepositUpdateEpoch + MIN_DEPOSIT_INTERVAL_EPOCHS) {
            revert DepositIntervalNotElapsed();
        }

        uint256 old = DEPOSIT;
        // Single adjustment cannot exceed MAX_DEPOSIT_MULTIPLIER (e.g. 2x up or 2x down)
        if (newDeposit > old * MAX_DEPOSIT_MULTIPLIER || newDeposit * MAX_DEPOSIT_MULTIPLIER < old) {
            revert DepositAdjustmentExceedsLimit();
        }

        DEPOSIT = newDeposit;
        lastDepositUpdateEpoch = curEpoch;
        emit DepositUpdated(old, newDeposit);
    }

    /// @notice Emergency pause for deposits. Can be called by admin (Timelock) or guardian (Multisig).
    function pause() external {
        if (msg.sender != admin && msg.sender != guardian) revert OnlyAdminOrGuardian();
        paused = true;
        emit VaultPaused(msg.sender);
    }

    /// @notice Unpause deposits. Can ONLY be called by admin (Timelock). Guardian CANNOT unpause.
    function unpause() external {
        if (msg.sender != admin) revert OnlyAdmin();
        paused = false;
        emit VaultUnpaused(msg.sender);
    }

    function transferAdmin(address newAdmin) external {
        if (msg.sender != admin) revert OnlyAdmin();
        if (newAdmin == address(0)) revert ZeroAddress();
        address old = admin;
        admin = newAdmin;
        emit AdminTransferred(old, newAdmin);
    }

    function setGuardian(address newGuardian) external {
        if (msg.sender != admin) revert OnlyAdmin();
        if (newGuardian == address(0)) revert ZeroAddress();
        address old = guardian;
        guardian = newGuardian;
        emit GuardianTransferred(old, newGuardian);
    }
}
