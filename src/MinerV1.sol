// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IToken} from "./interfaces/IToken.sol";
import {IVault} from "./interfaces/IVault.sol";
import {IMiner} from "./interfaces/IMiner.sol";

/// @title MinerV1
/// @notice Core mining logic for epoch-based, share-proportional PoW token distribution.
/// @dev Designed to be deployed behind a Transparent Upgradeable Proxy.
contract MinerV1 is Initializable, ReentrancyGuard, IMiner {
    IToken public token;
    IVault public vault;
    address public admin;
    uint256 public START;

    uint256 public constant EPOCH_LEN = 3600;
    uint256 public constant CAP = 6;                 // Max shares per address per epoch
    uint256 public constant RAMP_EPOCHS = 24;         // Warmup ramp duration in epochs
    uint256 public constant RAMP_START_BPS = 2500;    // 25% starting weight
    uint256 public constant MINING_CAP = 900_000_000e18;
    uint256 public constant LAMBDA_WAD = 137_639_085_754_704; // p=99% over 1394 days (33,456 epochs)
    uint256 public constant D0 = 3_000_000e18;        // Day 1 quota
    uint256 public constant DECAY = 0.9967e18;        // Daily decay: 0.9967
    uint256 public constant EASIEST = uint256(1) << 224;  // 32 bits
    uint256 public constant HARDEST = uint256(1) << 221;  // 35 bits
    uint256 public constant WSCALE = 1e10;            // Weight unit: sqrt(1)*1e6 * 10000 bps
    uint256 public constant MAX_SEED_STEPS = 24;

    // Configurable parameters with bounds
    uint256 public S_STAR;                            // Target counted shares per epoch
    uint256 public ADDR_CAP_BPS;                      // Max reward share per address (default 2000 = 20%)
    bool public paused;

    uint256 public lastSynced;
    uint256 public ema;

    mapping(uint256 => uint256) public epochSeed;
    mapping(uint256 => uint256) public target;
    mapping(uint256 => uint256) public totalShares;
    mapping(uint256 => uint256) public totalWeight;
    mapping(uint256 => mapping(address => uint16)) public usedMask;
    mapping(uint256 => mapping(address => uint8)) public counted;
    mapping(uint256 => mapping(address => uint256)) public userWeight;
    mapping(uint256 => mapping(address => bool)) public claimed;

    // Per-epoch parameter snapshots: ensures modifying parameters never changes past un-claimed rewards
    mapping(uint256 => uint256) public epochSStar;
    mapping(uint256 => uint256) public epochAddrCapBps;
    address public override guardian;

    // Slot 20: unallocated dilution gap (B3)
    mapping(uint256 => uint256) public unallocatedGap;

    // Slot 21: remaining unallocated mining emission R (initially 900M)
    uint256 public remainingEmission;

    // Slot 22: fixed baseline quota for settled epochs
    mapping(uint256 => uint256) public settledEpochQuota;

    event SharesSubmitted(address indexed u, uint256 indexed epoch, uint256 n, uint256 weightAdded);
    event EpochSynced(uint256 indexed epoch, uint256 target, uint256 ema);
    event EpochSeedInitialized(uint256 indexed epoch, uint256 seed);
    event SubmissionsPaused(address indexed account);
    event SubmissionsUnpaused(address indexed account);
    event SStarUpdated(uint256 oldValue, uint256 newValue);
    event AddrCapBpsUpdated(uint256 oldValue, uint256 newValue);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);
    event GuardianTransferred(address indexed oldGuardian, address indexed newGuardian);

    error OnlyAdmin();
    error OnlyAdminOrGuardian();
    error ZeroAddress();
    error MismatchStart();
    error MismatchEpochLen();
    error SubmissionsPausedError();
    error BadBatchLength();
    error SyncingRequired();
    error SeedNotInitialized();
    error FutureEpoch();
    error SeedAlreadyInitialized();
    error PrevSeedNotInitialized();
    error BlockhashUnavailable();
    error NoActiveStake();
    error AddressCapExceeded();
    error InvalidShareIndex();
    error DuplicateShareIndex();
    error InvalidProof();
    error EpochNotEnded();
    error AlreadyClaimed();
    error ZeroWeight();
    error ParameterOutOfBounds();
    error InvalidMaxSteps();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(
        address _token,
        address _vault,
        address _admin,
        address _guardian,
        uint256 _start
    ) external initializer {
        if (_token == address(0) || _vault == address(0) || _admin == address(0) || _guardian == address(0)) revert ZeroAddress();
        if (IVault(_vault).START() != _start) revert MismatchStart();
        if (IVault(_vault).EPOCH_LEN() != EPOCH_LEN) revert MismatchEpochLen();


        token = IToken(_token);
        vault = IVault(_vault);
        admin = _admin;
        guardian = _guardian;
        START = _start;

        S_STAR = 50_000;
        ADDR_CAP_BPS = 2000; // 20%
        target[0] = EASIEST;
        ema = 50_000;
        lastSynced = 0;

        epochSStar[0] = 50_000;
        epochAddrCapBps[0] = 2000;
        remainingEmission = MINING_CAP;
    }

    function epoch() public view override returns (uint256) {
        return block.timestamp < START ? 0 : (block.timestamp - START) / EPOCH_LEN;
    }

    // --- Quota & Mathematical Models ---

    function _powFixed(uint256 base, uint256 exp) internal pure returns (uint256) {
        uint256 res = 1e18;
        uint256 b = base;
        while (exp > 0) {
            if (exp & 1 == 1) {
                res = (res * b) / 1e18;
            }
            b = (b * b) / 1e18;
            exp >>= 1;
        }
        return res;
    }

    /// @notice Effective epoch quota (view). If epoch is settled, returns frozen quota; else returns current estimated quota
    function effectiveQuota(uint256 e) public view override returns (uint256) {
        if (e < lastSynced) {
            return settledEpochQuota[e];
        }
        return (remainingEmission * LAMBDA_WAD) / 1e18;
    }

    /// @notice Standard epoch quota for epoch e
    function epochQuota(uint256 e) public view override returns (uint256) {
        return effectiveQuota(e);
    }

    /// @notice Daily quota for day d (1-indexed estimate based on start epoch of day d)
    function dailyQuota(uint256 day) public view override returns (uint256) {
        if (day == 0) return 0;
        return effectiveQuota((day - 1) * 24) * 24;
    }

    /// @notice Cumulative quota allocated/minted prior to epoch e
    function cumulativeDailyQuota(uint256 /* completedDays */) public view returns (uint256) {
        return MINING_CAP - remainingEmission;
    }

    /// @notice Total scheduled mining quota allocated/minted prior to epoch e
    function cumulativeQuotaBefore(uint256 /* e */) public view override returns (uint256) {
        return MINING_CAP - remainingEmission;
    }

    /// @notice Minimum epoch weight threshold W_min
    function wMin(uint256 e) public pure returns (uint256) {
        uint256 d = e / 24;
        return (d < 30 ? 10 : d < 90 ? 50 : 100) * WSCALE;
    }

    function _isqrt(uint256 x) internal pure returns (uint256 y) {
        if (x == 0) return 0;
        uint256 z = (x + 1) / 2;
        y = x;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
    }

    function _w(uint256 c) internal pure returns (uint256) {
        return _isqrt(c * 1e12); // sqrt(c) * 1e6
    }

    // --- State & PoW Operations ---

    /// @notice Initialize epoch seed with batched backfill support.
    /// @dev Binary search precondition:
    /// The set of initialized epochs is guaranteed to ALWAYS be a contiguous prefix [0, ..., K-1].
    /// Seeds are strictly initialized sequentially from start upwards without skipping.
    /// Therefore, epochSeed[e] != 0 is monotonic (true for e < K, false for e >= K).
    /// This monotonicity is the fundamental invariant that guarantees binary search uniquely
    /// and correctly locates the earliest uninitialized epoch start.
    /// If targetEpoch is already initialized, the prefix property guarantees that all epochs
    /// from 0 to targetEpoch are already initialized (no gaps can exist), so it safely returns targetEpoch.
    /// @param targetEpoch Target epoch to initialize up to.
    /// @param maxSteps Maximum number of uninitialized epochs to process in this call (must be > 0).
    /// @return The last epoch successfully initialized in this call.
    function initEpochSeed(uint256 targetEpoch, uint256 maxSteps) public override returns (uint256) {
        if (targetEpoch > epoch()) revert FutureEpoch();
        if (epochSeed[targetEpoch] != 0) return targetEpoch;
        if (maxSteps == 0) revert InvalidMaxSteps();

        // Find the earliest uninitialized epoch up to target
        uint256 start = 0;
        if (epochSeed[0] != 0) {
            uint256 low = 0;
            uint256 high = targetEpoch;
            while (low < high) {
                uint256 mid = (low + high) / 2;
                if (epochSeed[mid] != 0) {
                    low = mid + 1;
                } else {
                    high = mid;
                }
            }
            start = low;
        }

        uint256 curEpoch = epoch();
        bytes32 curBh = blockhash(block.number - 1);
        uint256 lastInit = start;
        uint256 count = 0;

        for (uint256 e = start; e <= targetEpoch && count < maxSteps; e++) {
            bytes32 prev = (e == 0) ? bytes32(0) : bytes32(epochSeed[e - 1]);
            bytes32 bh = (e == curEpoch) ? curBh : bytes32(0);
            uint256 seed = uint256(keccak256(abi.encode(prev, bh, e, block.chainid, address(this))));
            epochSeed[e] = seed;
            emit EpochSeedInitialized(e, seed);
            lastInit = e;
            count++;
        }

        return lastInit;
    }

    /// @notice Backward-compatible wrapper defaulting to MAX_SEED_STEPS.
    function initEpochSeed(uint256 targetEpoch) external override {
        initEpochSeed(targetEpoch, MAX_SEED_STEPS);
    }

    /// @notice Catch up epoch target and EMA sequentially up to maxSteps. Unprivileged public function.
    function sync(uint256 maxSteps) public override returns (uint256) {
        uint256 cur = epoch();
        uint256 n = lastSynced;
        if (cur == 0 || n >= cur) {
            return n;
        }

        uint256 steps = cur - n;
        if (maxSteps > 0 && steps > maxSteps) {
            steps = maxSteps;
        }
        uint256 stop = n + steps;

        while (n < stop) {
            // Settle completed epoch (n)
            uint256 settledEpoch = n;
            uint256 q_e = (remainingEmission * LAMBDA_WAD) / 1e18;
            settledEpochQuota[settledEpoch] = q_e;

            uint256 tw = totalWeight[settledEpoch];
            uint256 wm = wMin(settledEpoch);
            uint256 allocatable = 0;
            uint256 b3 = 0;
            if (q_e > 0) {
                if (tw >= wm) {
                    allocatable = q_e;
                } else {
                    allocatable = (q_e * tw) / wm;
                    b3 = q_e - allocatable;
                }
            }
            remainingEmission -= allocatable;
            unallocatedGap[settledEpoch] = b3;
            emit EpochSettled(settledEpoch, q_e, tw, wm, b3);

            n++;
            uint256 shares = (n >= 2) ? totalShares[n - 2] : S_STAR;
            ema = (ema + shares) / 2;
            uint256 t = target[n - 1];
            if (t > EASIEST) t = EASIEST;
            uint256 nt = (ema == 0) ? (t * 125 / 100) : (t * S_STAR / ema);
            if (nt > t * 125 / 100) nt = t * 125 / 100;
            if (nt < t * 100 / 125) nt = t * 100 / 125;
            if (nt > EASIEST) nt = EASIEST;
            if (nt < HARDEST) nt = HARDEST;
            target[n] = nt;

            // Snapshot parameters for epoch n
            epochSStar[n] = S_STAR;
            epochAddrCapBps[n] = ADDR_CAP_BPS;

            emit EpochSynced(n, nt, ema);
        }
        lastSynced = n;
        return n;
    }

    function _getRamp(uint256 e, address user) internal view returns (uint256) {
        (bool active, uint256 since) = vault.status(user);
        if (!active || e < since + 1) revert NoActiveStake();
        uint256 age = e - (since + 1);
        if (age > RAMP_EPOCHS) age = RAMP_EPOCHS;
        return RAMP_START_BPS + (10000 - RAMP_START_BPS) * age / RAMP_EPOCHS;
    }

    function _verifyShare(
        bytes32 es,
        address user,
        uint8 k,
        uint256 nonce,
        uint256 target_
    ) internal view returns (bool) {
        bytes32 sub = keccak256(abi.encode(es, user, uint256(k), address(this), block.chainid));
        return uint256(keccak256(abi.encode(sub, nonce))) <= target_;
    }

    /// @notice Submit valid PoW shares for the current epoch.
    function submitShares(uint8[] calldata ks, uint256[] calldata nonces) external override {
        if (paused) revert SubmissionsPausedError();
        if (ks.length != nonces.length || ks.length == 0 || ks.length > CAP) revert BadBatchLength();

        uint256 e = epoch();
        if (lastSynced < e) {
            sync(48);
        }
        if (lastSynced != e) revert SyncingRequired();
        if (epochSeed[e] == 0) revert SeedNotInitialized();

        uint256 ramp = _getRamp(e, msg.sender);
        uint16 mask = usedMask[e][msg.sender];
        uint8 c = counted[e][msg.sender];
        if (uint256(c) + ks.length > CAP) revert AddressCapExceeded();

        uint256 t = target[e];
        bytes32 es = bytes32(epochSeed[e]);

        for (uint256 i = 0; i < ks.length; i++) {
            uint8 k = ks[i];
            if (k >= CAP) revert InvalidShareIndex();
            uint16 bit = uint16(1) << k;
            if ((mask & bit) != 0) revert DuplicateShareIndex();
            mask |= bit;

            if (!_verifyShare(es, msg.sender, k, nonces[i], t)) revert InvalidProof();
        }

        usedMask[e][msg.sender] = mask;
        counted[e][msg.sender] = c + uint8(ks.length);

        // Telescoping sum optimization: compute total weight increment once
        uint256 add = (_w(uint256(c) + ks.length) - _w(uint256(c))) * ramp;
        userWeight[e][msg.sender] += add;
        totalWeight[e] += add;
        totalShares[e] += ks.length;

        emit SharesSubmitted(msg.sender, e, ks.length, add);
    }

    /// @notice Pull-payment claim for finalized epoch rewards. NEVER PAUSABLE.
    /// @dev Uses epochAddrCapBps snapshot to ensure changes to ADDR_CAP_BPS never alter past ended rewards.
    function claim(uint256 e) external override nonReentrant {
        if (e >= epoch()) revert EpochNotEnded();
        if (lastSynced <= e) {
            sync(48);
            if (lastSynced <= e) revert SyncingRequired();
        }
        if (claimed[e][msg.sender]) revert AlreadyClaimed();
        claimed[e][msg.sender] = true;

        uint256 uw = userWeight[e][msg.sender];
        if (uw == 0) revert ZeroWeight();

        uint256 effQ = settledEpochQuota[e];
        if (effQ == 0) {
            emit RewardClaimed(msg.sender, e, 0, 0);
            return;
        }

        uint256 tw = totalWeight[e];
        uint256 wm = wMin(e);
        uint256 denom = tw > wm ? tw : wm;

        uint256 amt = (effQ * uw) / denom;

        // Use snapshot cap for epoch e; fall back to ADDR_CAP_BPS if unrecorded
        uint256 capBps = epochAddrCapBps[e];
        if (capBps == 0) capBps = ADDR_CAP_BPS;

        uint256 maxAmt = (effQ * capBps) / 10000;
        uint256 cappedOverflow = 0;
        if (amt > maxAmt) {
            cappedOverflow = amt - maxAmt;
            amt = maxAmt;
            remainingEmission += cappedOverflow;
        }

        emit RewardClaimed(msg.sender, e, amt, cappedOverflow);
        if (amt > 0) {
            token.mint(msg.sender, amt);
        }
    }

    // --- Admin & Governance ---

    function pauseSubmissions() external override {
        if (msg.sender != admin && msg.sender != guardian) revert OnlyAdminOrGuardian();
        paused = true;
        emit SubmissionsPaused(msg.sender);
    }

    function unpause() external override {
        if (msg.sender != admin) revert OnlyAdmin();
        paused = false;
        emit SubmissionsUnpaused(msg.sender);
    }

    function setSStar(uint256 newSStar) external override {
        if (msg.sender != admin) revert OnlyAdmin();
        if (newSStar < 1_000 || newSStar > 1_000_000) revert ParameterOutOfBounds();
        uint256 old = S_STAR;
        S_STAR = newSStar;
        emit SStarUpdated(old, newSStar);
    }

    function setAddrCapBps(uint256 newCapBps) external override {
        if (msg.sender != admin) revert OnlyAdmin();
        if (newCapBps < 500 || newCapBps > 5_000) revert ParameterOutOfBounds();
        uint256 old = ADDR_CAP_BPS;
        ADDR_CAP_BPS = newCapBps;
        emit AddrCapBpsUpdated(old, newCapBps);
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

    // Reserved storage space for proxy upgrades (reduced by 3 slots for epochSStar, epochAddrCapBps, and guardian)
    // Reserved storage space for proxy upgrades (reduced by 1 slot from 47 to 46 for unallocatedGap)
    // Reserved storage space for proxy upgrades (reduced by 2 slots from 46 to 44 for remainingEmission and settledEpochQuota)
    uint256[44] private __gap;
}
