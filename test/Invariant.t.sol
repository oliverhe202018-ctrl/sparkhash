// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {Token} from "../src/Token.sol";
import {StakeVault} from "../src/StakeVault.sol";
import {MinerV1} from "../src/MinerV1.sol";
import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

contract HarnessMiner is MinerV1 {
    function exposed_w(uint256 c) external pure returns (uint256) {
        return _w(c);
    }
}

contract MiningHandler is Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;

    address[] public miners;
    uint256 public totalDeposited;
    uint256 public totalWithdrawn;
    mapping(uint256 => uint256) public totalClaimedInEpoch;

    uint256 public constant DEPOSIT = 0.01 ether;

    constructor(Token _token, StakeVault _vault, MinerV1 _miner, address[] memory _miners) {
        token = _token;
        vault = _vault;
        miner = _miner;
        miners = _miners;
    }

    function deposit(uint256 minerIdx) external {
        address u = miners[minerIdx % miners.length];
        (bool active, ) = vault.status(u);
        if (active) return;

        vm.deal(u, 1 ether);
        vm.prank(u);
        try vault.deposit{value: DEPOSIT}() {
            totalDeposited += DEPOSIT;
        } catch {}
    }

    function requestWithdraw(uint256 minerIdx) external {
        address u = miners[minerIdx % miners.length];
        (bool active, ) = vault.status(u);
        if (!active) return;

        vm.prank(u);
        try vault.requestWithdraw() {} catch {}
    }

    function withdraw(uint256 minerIdx) external {
        address u = miners[minerIdx % miners.length];
        vm.prank(u);
        try vault.withdraw() {
            totalWithdrawn += DEPOSIT;
        } catch {}
    }

    function advanceEpoch(uint256 hoursCount) external {
        hoursCount = (hoursCount % 5) + 1; // 1 to 5 epochs
        vm.warp(block.timestamp + hoursCount * 3600);

        uint256 cur = miner.epoch();
        try miner.sync(5) {} catch {}
        try miner.initEpochSeed(cur) {} catch {}
    }

    function claim(uint256 minerIdx, uint256 pastEpoch) external {
        address u = miners[minerIdx % miners.length];
        uint256 cur = miner.epoch();
        if (cur == 0) return;
        pastEpoch = pastEpoch % cur;

        uint256 balBefore = token.balanceOf(u);
        vm.prank(u);
        try miner.claim(pastEpoch) {
            uint256 amt = token.balanceOf(u) - balBefore;
            totalClaimedInEpoch[pastEpoch] += amt;
        } catch {}
    }
}

contract InvariantTest is StdInvariant, Test {
    Token public token;
    StakeVault public vault;
    MinerV1 public miner;
    HarnessMiner public harness;
    MiningHandler public handler;

    address public admin = address(0xA);
    address public guardian = address(0x60A8D);
    address public liquidity = address(0x2);
    address public team = address(0x3);
    address public community = address(0x4);

    uint256 public constant DEPOSIT = 0.01 ether;
    uint256 public start;

    function setUp() public {
        start = block.timestamp + 1000;

        token = new Token(liquidity, team, community);
        vault = new StakeVault(DEPOSIT, start, admin, guardian, 0.001 ether, 10 ether, 24, 2);

        MinerV1 impl = new MinerV1();
        bytes memory initData = abi.encodeWithSelector(
            MinerV1.initialize.selector,
            address(token),
            address(vault),
            admin,
            guardian,
            start
        );
        TransparentUpgradeableProxy proxy = new TransparentUpgradeableProxy(
            address(impl),
            admin,
            initData
        );
        miner = MinerV1(address(proxy));
        token.setMiner(address(miner));

        harness = new HarnessMiner();

        address[] memory minerAddrs = new address[](3);
        minerAddrs[0] = address(0x101);
        minerAddrs[1] = address(0x102);
        minerAddrs[2] = address(0x103);

        handler = new MiningHandler(token, vault, miner, minerAddrs);

        targetContract(address(handler));
    }

    // --- Unit / Fuzz property tests ---

    function test_WMonotonicityAndExactReferenceValues() public view {
        uint256[7] memory refW = [
            uint256(0),
            1_000_000,
            1_414_213,
            1_732_050,
            2_000_000,
            2_236_067,
            2_449_489
        ];

        for (uint256 c = 0; c < 7; c++) {
            uint256 calculated = harness.exposed_w(c);
            assertEq(calculated, refW[c], "Reference value mismatch for _w(c)");
            if (c > 0) {
                assertTrue(calculated > harness.exposed_w(c - 1), "Strict monotonicity violated");
            }
        }
    }

    function testFuzz_WSqrtAccuracy(uint32 c) public view {
        uint256 w = harness.exposed_w(c);
        uint256 targetVal = uint256(c) * 1e12;
        // w^2 <= targetVal < (w + 1)^2
        assertTrue(w * w <= targetVal, "w^2 > c * 1e12");
        assertTrue((w + 1) * (w + 1) > targetVal, "(w+1)^2 <= c * 1e12");
    }

    // --- Invariants ---

    function invariant_TotalMinedNeverExceeds900M() public view {
        assertTrue(token.totalMined() <= token.MINING_CAP(), "Total mined exceeded 900M");
    }

    function invariant_VaultSolvency() public view {
        assertEq(address(vault).balance, handler.totalDeposited() - handler.totalWithdrawn(), "Vault balance mismatch");
    }
}
