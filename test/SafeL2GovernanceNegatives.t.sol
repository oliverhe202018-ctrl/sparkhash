// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, Vm} from "forge-std/Test.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

interface ISafeL2Test {
    function setup(address[] calldata, uint256, address, bytes calldata, address, address, uint256, address) external;
    function nonce() external view returns (uint256);
    function getThreshold() external view returns (uint256);
    function approvedHashes(address, bytes32) external view returns (uint256);
    function approveHash(bytes32) external;
    function getTransactionHash(address, uint256, bytes calldata, uint8, uint256, uint256, uint256, address, address, uint256)
        external view returns (bytes32);
    function execTransaction(address, uint256, bytes calldata, uint8, uint256, uint256, uint256, address, address, bytes calldata)
        external payable returns (bool);
}

contract GovernanceCounter {
    uint256 public count;
    function increment() external { count++; }
}

contract SafeL2GovernanceNegativesTest is Test {
    address private constant OWNER1 = address(0x101);
    address private constant OWNER2 = address(0x102);
    bytes32 private constant SALT = keccak256("offline governance negative");
    bytes32 private constant SUCCESS = keccak256("ExecutionSuccess(bytes32,uint256)");
    bytes32 private constant FAILURE = keccak256("ExecutionFailure(bytes32,uint256)");
    bytes32 private constant SCHEDULED = keccak256("CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)");
    bytes32 private constant EXECUTED = keccak256("CallExecuted(bytes32,uint256,address,uint256,bytes)");
    bytes32 private constant CANCELLED = keccak256("Cancelled(bytes32)");

    ISafeL2Test private safe;
    TimelockController private timelock;
    GovernanceCounter private counter;
    bytes private targetData;
    bytes32 private operationId;

    function setUp() public {
        vm.warp(1000);
        address singleton = makeAddr("offline Safe L2 singleton");
        address proxy = makeAddr("offline Safe proxy");
        bytes memory singletonCode = vm.parseBytes(vm.readFile("test/fixtures/SafeL2-v1.3.0-runtime.hex"));
        bytes memory proxyCode = vm.parseBytes(vm.readFile("test/fixtures/SafeProxy-v1.3.0-runtime.hex"));
        assertEq(keccak256(singletonCode), 0x21842597390c4c6e3c1239e434a682b054bd9548eee5e9b1d6a4482731023c0f);
        assertEq(keccak256(proxyCode), 0xb89c1b3bdf2cf8827818646bce9a8f6e372885f8c55e5c07acbd307cb133b000);
        vm.etch(singleton, singletonCode);
        vm.etch(proxy, proxyCode);
        // The proxy constructor normally writes singleton to slot zero.
        vm.store(proxy, bytes32(0), bytes32(uint256(uint160(singleton))));
        safe = ISafeL2Test(proxy);
        address[] memory owners = new address[](3);
        owners[0] = OWNER1;
        owners[1] = OWNER2;
        owners[2] = address(0x103);
        safe.setup(owners, 2, address(0), "", address(0), address(0), 0, address(0));
        assertEq(safe.getThreshold(), 2);

        address[] memory proposers = new address[](1);
        proposers[0] = proxy;
        address[] memory executors = new address[](1);
        timelock = new TimelockController(600, proposers, executors, address(0));
        counter = new GovernanceCounter();
        targetData = abi.encodeCall(counter.increment, ());
        operationId = timelock.hashOperation(address(counter), 0, targetData, bytes32(0), SALT);
    }

    function test_OneValidApprovalCannotMeetThreshold() public {
        bytes memory data = _scheduleData();
        bytes32 hash = _hash(data, 0, 0);
        _approve(OWNER1, hash);
        assertEq(safe.approvedHashes(OWNER2, hash), 0);
        _assertSnapshot(TimelockController.OperationState.Unset, 0, 0, 0);
        _rejectAuthorization(data, _signature(OWNER1), "GS020", 0);
        _rejectAuthorization(data, _signatures(), "GS025", 0);
    }

    function test_WrongApprovedHashDoesNotAuthorizeCurrentHash() public {
        bytes memory data = _scheduleData();
        bytes32 hash = _hash(data, 0, 0);
        bytes32 other = _hash(abi.encodeCall(timelock.cancel, (operationId)), 0, 0);
        _approve(OWNER1, hash);
        _approve(OWNER2, other);
        assertEq(safe.approvedHashes(OWNER2, hash), 0);
        _assertSnapshot(TimelockController.OperationState.Unset, 0, 0, 0);
        _rejectAuthorization(data, _signatures(), "GS025", 0);
    }

    function test_OldNonceApprovalsCannotAuthorizeCurrentTransaction() public {
        bytes memory data = _scheduleData();
        bytes32 oldHash = _hash(data, 0, 0);
        _approveBoth(oldHash);
        _schedule();
        // Same calldata and signatures, but Safe now computes the hash at nonce 1.
        assertEq(safe.approvedHashes(OWNER1, oldHash), 1);
        assertEq(safe.approvedHashes(OWNER1, _hash(data, 1, 0)), 0);
        _rejectAuthorization(data, _signatures(), "GS025", 1);
    }

    function test_CancelledOperationCannotExecute() public {
        _schedule();
        bytes memory data = abi.encodeCall(timelock.cancel, (operationId));
        bytes32 hash = _hash(data, 1, 0);
        _approveBoth(hash);
        vm.recordLogs();
        assertTrue(_exec(data, 0, _signatures()));
        Vm.Log[] memory logs = vm.getRecordedLogs();
        _assertSafeEvent(logs, SUCCESS, hash);
        _assertOperationEvent(logs, CANCELLED);
        _assertSnapshot(TimelockController.OperationState.Unset, 0, 2, 0);
        vm.warp(1600);
        _assertTimelockRejects(TimelockController.OperationState.Unset, 0, 2, 0);
    }

    function test_DoneOperationCannotExecuteAgain() public {
        _schedule();
        vm.warp(1600);
        _assertSnapshot(TimelockController.OperationState.Ready, 1600, 1, 0);
        vm.recordLogs();
        _executeDirect();
        _assertOperationEvent(vm.getRecordedLogs(), EXECUTED);
        _assertSnapshot(TimelockController.OperationState.Done, 1, 1, 1);
        _assertTimelockRejects(TimelockController.OperationState.Done, 1, 1, 1);
    }

    function test_WaitingRejectsDirectAndZeroGasSafeExecution() public {
        _schedule();
        vm.warp(1599);
        _assertTimelockRejects(TimelockController.OperationState.Waiting, 1600, 1, 0);

        bytes memory data = _executeData();
        _approveBoth(_hash(data, 1, 0));
        vm.recordLogs();
        vm.expectRevert(_error("GS013"));
        _exec(data, 0, _signatures());
        assertEq(vm.getRecordedLogs().length, 0);
        _assertSnapshot(TimelockController.OperationState.Waiting, 1600, 1, 0);
    }

    function test_WaitingWithExplicitSafeGasReturnsFalseAndConsumesNonce() public {
        _schedule();
        vm.warp(1599);
        bytes memory data = _executeData();
        uint256 safeTxGas = 200000;
        bytes32 hash = _hash(data, 1, safeTxGas);
        _approveBoth(hash);
        vm.recordLogs();
        assertFalse(_exec(data, safeTxGas, _signatures()));
        _assertSafeEvent(vm.getRecordedLogs(), FAILURE, hash);
        _assertSnapshot(TimelockController.OperationState.Waiting, 1600, 2, 0);
    }

    function _scheduleData() private view returns (bytes memory) {
        return abi.encodeCall(timelock.schedule, (address(counter), 0, targetData, bytes32(0), SALT, 600));
    }

    function _executeData() private view returns (bytes memory) {
        return abi.encodeCall(timelock.execute, (address(counter), 0, targetData, bytes32(0), SALT));
    }

    function _hash(bytes memory data, uint256 nonce, uint256 safeTxGas) private view returns (bytes32) {
        return safe.getTransactionHash(address(timelock), 0, data, 0, safeTxGas, 0, 0, address(0), address(0), nonce);
    }

    function _exec(bytes memory data, uint256 safeTxGas, bytes memory signatures) private returns (bool) {
        return safe.execTransaction(address(timelock), 0, data, 0, safeTxGas, 0, 0, address(0), address(0), signatures);
    }

    function _approve(address owner, bytes32 hash) private {
        uint256 nonce = safe.nonce();
        vm.prank(owner);
        safe.approveHash(hash);
        assertEq(safe.approvedHashes(owner, hash), 1);
        assertEq(safe.nonce(), nonce);
    }

    function _approveBoth(bytes32 hash) private {
        _approve(OWNER1, hash);
        _approve(OWNER2, hash);
    }

    function _signature(address owner) private pure returns (bytes memory) {
        return abi.encodePacked(bytes32(uint256(uint160(owner))), bytes32(0), uint8(1));
    }

    function _signatures() private pure returns (bytes memory) {
        return bytes.concat(_signature(OWNER1), _signature(OWNER2));
    }

    function _error(string memory reason) private pure returns (bytes memory) {
        return abi.encodeWithSignature("Error(string)", reason);
    }

    function _schedule() private {
        bytes memory data = _scheduleData();
        bytes32 hash = _hash(data, 0, 0);
        _approveBoth(hash);
        vm.recordLogs();
        assertTrue(_exec(data, 0, _signatures()));
        Vm.Log[] memory logs = vm.getRecordedLogs();
        _assertSafeEvent(logs, SUCCESS, hash);
        _assertOperationEvent(logs, SCHEDULED);
        _assertSnapshot(TimelockController.OperationState.Waiting, 1600, 1, 0);
    }

    function _rejectAuthorization(bytes memory data, bytes memory signatures, string memory reason, uint256 nonce) private {
        TimelockController.OperationState state = timelock.getOperationState(operationId);
        uint256 timestamp = timelock.getTimestamp(operationId);
        vm.recordLogs();
        vm.expectRevert(_error(reason));
        _exec(data, 0, signatures);
        assertEq(vm.getRecordedLogs().length, 0);
        _assertSnapshot(state, timestamp, nonce, 0);
    }

    function _executeDirect() private {
        timelock.execute(address(counter), 0, targetData, bytes32(0), SALT);
    }

    function _assertTimelockRejects(TimelockController.OperationState state, uint256 timestamp, uint256 nonce, uint256 count)
        private
    {
        _assertSnapshot(state, timestamp, nonce, count);
        vm.recordLogs();
        vm.expectRevert(abi.encodeWithSelector(
            TimelockController.TimelockUnexpectedOperationState.selector,
            operationId,
            bytes32(uint256(1) << uint8(TimelockController.OperationState.Ready))
        ));
        _executeDirect();
        assertEq(vm.getRecordedLogs().length, 0);
        _assertSnapshot(state, timestamp, nonce, count);
    }

    function _assertSnapshot(TimelockController.OperationState state, uint256 timestamp, uint256 nonce, uint256 count)
        private view
    {
        assertEq(uint8(timelock.getOperationState(operationId)), uint8(state));
        assertEq(timelock.getTimestamp(operationId), timestamp);
        assertEq(timelock.getMinDelay(), 600);
        assertEq(safe.nonce(), nonce);
        assertEq(counter.count(), count);
    }

    function _assertSafeEvent(Vm.Log[] memory logs, bytes32 topic, bytes32 hash) private view {
        uint256 matches;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(safe)) continue;
            if (logs[i].topics[0] != SUCCESS && logs[i].topics[0] != FAILURE) continue;
            assertEq(logs[i].topics[0], topic);
            assertEq(logs[i].topics.length, 1);
            assertEq(logs[i].data, abi.encode(hash, uint256(0)));
            matches++;
        }
        assertEq(matches, 1);
        if (topic == FAILURE) {
            for (uint256 i; i < logs.length; i++) assertNotEq(logs[i].emitter, address(timelock));
        }
    }

    function _assertOperationEvent(Vm.Log[] memory logs, bytes32 topic) private view {
        uint256 matches;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(timelock) || logs[i].topics[0] != topic) continue;
            assertEq(logs[i].topics[1], operationId);
            if (topic == CANCELLED) {
                assertEq(logs[i].topics.length, 2);
                assertEq(logs[i].data.length, 0);
            } else {
                assertEq(logs[i].topics.length, 3);
                assertEq(logs[i].topics[2], bytes32(0));
                if (topic == SCHEDULED) {
                    assertEq(logs[i].data, abi.encode(address(counter), 0, targetData, bytes32(0), 600));
                } else {
                    assertEq(logs[i].data, abi.encode(address(counter), 0, targetData));
                }
            }
            matches++;
        }
        assertEq(matches, 1);
    }
}
