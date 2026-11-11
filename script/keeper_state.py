# Dedicated state persistence and domain-bound manual review locking for PoolCoin KeeperBot.
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple


CURRENT_STATE_VERSION = 1


class KeeperStateError(RuntimeError):
    """Base exception for keeper state persistence and locking errors."""
    pass


class DomainValidationError(KeeperStateError):
    """Raised when state domain (chain_id, miner, sender) cannot be securely determined."""
    pass


class DomainLockedError(KeeperStateError):
    """Raised when another keeper process holds the mutual exclusion lock on this domain."""
    pass


class CorruptStateError(KeeperStateError):
    """Raised when state file content is corrupted or unparseable JSON."""
    pass


class UnknownStateVersionError(KeeperStateError):
    """Raised when state file version is not supported."""
    pass


class InvalidStateError(KeeperStateError):
    """Raised when state file is missing required fields or domain info."""
    pass


class StateWriteError(KeeperStateError):
    """Raised when writing persistent state to disk fails."""
    pass


def to_checksum_address(address: str) -> str:
    """Converts address to EIP-55 checksum format."""
    if not address or address == "0x0000000000000000000000000000000000000000":
        return address
    try:
        from web3 import Web3
        return Web3.to_checksum_address(address)
    except Exception:
        pass
    try:
        from eth_hash.auto import keccak
        clean = address.lower().replace("0x", "")
        h = keccak(clean.encode("utf-8")).hex()
        return "0x" + "".join(c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(clean))
    except Exception:
        return address


class StateDomain:
    """
    Cryptographic and operational domain binding for persistent state.
    Binds state uniquely to (chain_id, miner_address, sender_address).
    """
    def __init__(self, chain_id: Any, miner_address: Any, sender_address: Any):
        self.chain_id = self._validate_chain_id(chain_id)
        self.miner_address = self._validate_address(miner_address, "miner_address")
        self.sender_address = self._validate_address(sender_address, "sender_address")

    @staticmethod
    def _validate_chain_id(val: Any) -> int:
        if val is None or isinstance(val, bool):
            raise DomainValidationError("chain_id must be a valid positive integer, got None or bool")
        try:
            cid = int(val)
            if cid <= 0:
                raise ValueError("non-positive")
            return cid
        except Exception as e:
            raise DomainValidationError(f"Invalid chain_id: {val}") from e

    @staticmethod
    def _validate_address(addr: Any, field_name: str) -> str:
        if not addr or not isinstance(addr, str):
            raise DomainValidationError(f"{field_name} must be a non-empty string address")
        cleaned = addr.strip()
        if not re.match(r"^0x[0-9a-fA-F]{40}$", cleaned):
            raise DomainValidationError(f"{field_name} is not a valid 20-byte hex address: {cleaned}")
        return to_checksum_address(cleaned)

    def is_valid(self) -> bool:
        return self.chain_id > 0 and bool(self.miner_address) and bool(self.sender_address)

    def get_slug(self) -> str:
        raw_key = f"{self.chain_id}:{self.miner_address.lower()}:{self.sender_address.lower()}"
        slug_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"keeper_{self.chain_id}_{self.miner_address[:10]}_{self.sender_address[:10]}_{slug_hash}"

    def get_sender_slug(self) -> str:
        raw_key = f"{self.chain_id}:{self.sender_address.lower()}"
        slug_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"keeper_{self.chain_id}_sender_{self.sender_address[:10]}_{slug_hash}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "miner_address": self.miner_address,
            "sender_address": self.sender_address
        }

    def matches(self, other_dict: Any) -> bool:
        if not isinstance(other_dict, dict):
            return False
        other_cid = other_dict.get("chain_id")
        other_miner = other_dict.get("miner_address")
        other_sender = other_dict.get("sender_address")
        try:
            return (
                int(other_cid) == self.chain_id
                and str(other_miner).lower() == self.miner_address.lower()
                and str(other_sender).lower() == self.sender_address.lower()
            )
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"<StateDomain chain_id={self.chain_id} miner={self.miner_address} sender={self.sender_address}>"


class DomainFileLock:
    """
    OS-level non-blocking mutual exclusion file lock.
    Guarantees that two keeper processes in the same domain cannot concurrently
    enter the broadcast or transaction preparation phase.
    Works portably across Windows (msvcrt.locking) and POSIX (fcntl.flock).
    """
    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fp = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        lock_dir = os.path.dirname(os.path.abspath(self.lock_path))
        os.makedirs(lock_dir, exist_ok=True)
        try:
            self._fp = open(self.lock_path, "a+b")
            if self._fp.tell() == 0:
                self._fp.write(b"L")
                self._fp.flush()
            self._fp.seek(0)

            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._acquired = True
            return True
        except (IOError, OSError, PermissionError):
            if self._fp:
                try:
                    self._fp.close()
                except Exception:
                    pass
                self._fp = None
            self._acquired = False
            return False

    def release(self):
        if not self._fp:
            self._acquired = False
            return
        try:
            self._fp.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    if self._acquired:
                        msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    if self._acquired:
                        fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            self._fp.close()
        except Exception:
            pass
        finally:
            self._fp = None
            self._acquired = False

    def __enter__(self):
        if not self.acquire():
            raise DomainLockedError(f"Failed to acquire process lock for domain on {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def sanitize_state_value(text: Any) -> str:
    """Sanitizes text to prevent leakage of credentials, passwords, or raw secrets into state files."""
    if text is None:
        return ""
    val = str(text)
    val = re.sub(r"://([^:@\s]+):([^@\s]+)@", r"://[REDACTED]@", val)
    val = re.sub(r"://([^:@\s]+)@", r"://[REDACTED]@", val)
    val = re.sub(r"([?&](?:token|key|api_key|password|secret)=)[^&\s]+", r"\1[REDACTED]", val, flags=re.IGNORECASE)
    val = re.sub(r"(--password-file\s+)[^\s]+", r"\1[REDACTED_PATH]", val, flags=re.IGNORECASE)
    val = re.sub(r"(--private-key\s+)[^\s]+", r"\1[REDACTED_KEY]", val, flags=re.IGNORECASE)
    val = re.sub(r'''(private_key["':\s=]+)(?:0x)?[0-9a-fA-F]{64}''', r'\1[REDACTED_KEY]', val, flags=re.IGNORECASE)
    return val


def atomic_write_json(file_path: str, data: Dict[str, Any]):
    """
    Atomically writes JSON file using a temp file in the same directory,
    fsyncing data to disk, and performing atomic os.replace.
    """
    dir_path = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(dir_path, exist_ok=True)
    temp_path = os.path.join(dir_path, f"{os.path.basename(file_path)}.tmp.{os.getpid()}_{uuid.uuid4().hex[:8]}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    except Exception as e:
        raise StateWriteError(f"Failed to atomically write state file {file_path}: {e}") from e
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def append_audit_log(audit_path: str, entry: Dict[str, Any]):
    """Appends a sanitized JSON record to the audit log."""
    dir_path = os.path.dirname(os.path.abspath(audit_path))
    os.makedirs(dir_path, exist_ok=True)
    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
    except Exception:
        pass


class KeeperStateManager:
    """
    Manages persistent state, audit trails, and concurrency locks for KeeperBot.
    Enforces that manual review locks survive restarts and crashes,
    and prevents concurrent broadcasts within the same domain.
    """
    def __init__(self, state_dir: Optional[str] = None, domain: Optional[StateDomain] = None):
        if state_dir:
            self.state_dir = os.path.abspath(state_dir)
        elif "KEEPER_STATE_DIR" in os.environ and os.environ["KEEPER_STATE_DIR"].strip():
            self.state_dir = os.path.abspath(os.environ["KEEPER_STATE_DIR"].strip())
        else:
            self.state_dir = os.path.abspath(os.path.expanduser("~/.poolcoin/keeper_state"))

        self.domain = domain
        self._lock = None
        self._thread_lock = threading.Lock()

    def set_domain(self, domain: StateDomain):
        self.domain = domain
        self._lock = None

    @property
    def domain_valid(self) -> bool:
        return self.domain is not None and self.domain.is_valid()

    def get_slug(self) -> str:
        if not self.domain_valid:
            raise DomainValidationError("Cannot compute slug without a valid StateDomain")
        return self.domain.get_slug()

    def get_state_path(self) -> str:
        return os.path.join(self.state_dir, f"{self.get_slug()}.json")

    def get_sender_slug(self) -> str:
        if not self.domain_valid:
            raise DomainValidationError("Cannot compute sender slug without a valid StateDomain")
        return self.domain.get_sender_slug()

    def get_lock_path(self) -> str:
        # Sender-level lock file guarantees mutual exclusion across any processes
        # broadcasting with the same (chain_id, sender_address), preventing cross-miner nonce races.
        return os.path.join(self.state_dir, f"{self.get_sender_slug()}.lock")

    def get_audit_path(self) -> str:
        return os.path.join(self.state_dir, f"{self.get_slug()}_audit.jsonl")

    def acquire_lock(self) -> bool:
        if not self.domain_valid:
            return False
        with self._thread_lock:
            if self._lock is not None and self._lock._acquired:
                return False
            if self._lock is None:
                self._lock = DomainFileLock(self.get_lock_path())
            return self._lock.acquire()

    def release_lock(self):
        with self._thread_lock:
            if self._lock:
                self._lock.release()

    def load_state(self) -> Optional[Dict[str, Any]]:
        """
        Loads and strictly validates state file from disk.
        Fails and raises specific exceptions if:
        - Corrupted / invalid JSON
        - Unsupported state version
        - Missing required fields or domain info (an old lock missing fields cannot be treated as no lock)
        - Domain mismatch
        """
        if not self.domain_valid:
            raise DomainValidationError("Cannot load state without a valid StateDomain")

        state_path = self.get_state_path()
        if not os.path.exists(state_path):
            return None

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            raise CorruptStateError(f"Failed to read state file: {e}") from e

        if not content or not content.strip():
            raise CorruptStateError("State file is empty")

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise CorruptStateError(f"State file is not valid JSON: {e}") from e

        if not isinstance(data, dict):
            raise CorruptStateError("State file JSON root is not an object")

        ver = data.get("version")
        if ver != CURRENT_STATE_VERSION:
            raise UnknownStateVersionError(f"Unsupported state version: {ver} (expected {CURRENT_STATE_VERSION})")

        required_keys = ["version", "domain", "status", "updated_at"]
        for k in required_keys:
            if k not in data or data[k] is None:
                raise InvalidStateError(f"State file is missing required field: {k}")

        domain_dict = data.get("domain")
        if not isinstance(domain_dict, dict):
            raise InvalidStateError("State file domain is not an object")

        for dk in ["chain_id", "miner_address", "sender_address"]:
            if dk not in domain_dict or domain_dict[dk] is None or domain_dict[dk] == "Unknown":
                raise InvalidStateError(f"State file domain is missing required field: {dk}")

        if not self.domain.matches(domain_dict):
            raise DomainValidationError(
                f"State file domain mismatch: file has {domain_dict}, expected {self.domain.to_dict()}"
            )

        if "tx_hashes" not in data or not isinstance(data.get("tx_hashes"), list):
            single = data.get("tx_hash")
            if single and single != "Unknown" and re.match(r"^0x[0-9a-fA-F]{64}$", str(single)):
                data["tx_hashes"] = [single]
            else:
                data["tx_hashes"] = []

        return data

    def check_cross_miner_protection(self) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Inspects state_dir for other domains using the same sender on the same chain in locked/sending state.
        Returns (is_blocked: bool, block_reason: Optional[str], other_data: Optional[Dict])
        """
        if not os.path.exists(self.state_dir):
            return (False, None, None)
        try:
            for fname in os.listdir(self.state_dir):
                if not fname.endswith(".json") or fname.startswith(".tmp.") or fname.endswith("_audit.jsonl"):
                    continue
                if fname == os.path.basename(self.get_state_path()):
                    continue
                fpath = os.path.join(self.state_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        other_data = json.load(f)
                except Exception:
                    continue
                other_domain = other_data.get("domain", {})
                try:
                    other_cid = int(other_domain.get("chain_id", 0))
                    other_sender = str(other_domain.get("sender_address", "")).lower()
                    other_miner = str(other_domain.get("miner_address", "")).lower()
                except Exception:
                    continue

                if other_cid == self.domain.chain_id and other_sender == self.domain.sender_address.lower():
                    if other_miner != self.domain.miner_address.lower():
                        other_status = other_data.get("status")
                        if other_status in ("PREPARING_TO_SEND", "MANUAL_REVIEW_LOCKED", "UNCERTAIN"):
                            other_reason = other_data.get("reason_code", other_status)
                            block_reason = (
                                f"Cross-miner collision protection: Sender {self.domain.sender_address} has an "
                                f"active/unresolved lock on Miner {other_domain.get('miner_address')} "
                                f"(status: {other_status}, reason: {other_reason}). "
                                f"Broadcast blocked to prevent nonce collision."
                            )
                            return (True, block_reason, other_data)
        except Exception as e:
            return (True, f"Failed during cross-miner state inspection: {str(e)}", None)

        return (False, None, None)

    def check_startup_protection(self) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Pure read-only verification of persistent lock and crash state before any transaction execution.
        STRICTLY READ-ONLY: Never writes any file and never modifies any lock state.
        Returns:
            (can_proceed: bool, block_reason: Optional[str], state_data: Optional[Dict])
        """
        try:
            state_data = self.load_state()
        except KeeperStateError as e:
            return (False, f"Persistent state validation failed: {str(e)}", None)
        except Exception as e:
            return (False, f"Unexpected error reading persistent state: {str(e)}", None)

        if state_data:
            status = state_data.get("status")

            if status in ("MANUAL_REVIEW_LOCKED", "UNCERTAIN"):
                reason = state_data.get("reason_code") or "MANUAL_REVIEW_LOCKED"
                return (False, f"Keeper is locked for manual review (reason: {reason})", state_data)

            if status == "PREPARING_TO_SEND":
                return (False, "Interrupted in-flight transaction detected from previous run (status: PREPARING_TO_SEND). Crash before confirmation suspected; automated broadcast blocked.", state_data)

        cross_blocked, cross_reason, other_data = self.check_cross_miner_protection()
        if cross_blocked:
            return (False, cross_reason, other_data)

        return (True, None, state_data)

    def recover_crash_to_manual_review(self, state_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Transitions PREPARING_TO_SEND to MANUAL_REVIEW_LOCKED with reason_code CRASH_BEFORE_CONFIRMATION.
        STRICT REQUIREMENT: Caller MUST already hold the sender mutex lock (self._lock and self._lock._acquired).
        Writes to state file and writes an audit log entry.
        """
        if not self.domain_valid:
            raise DomainValidationError("Cannot recover crash without a valid StateDomain")

        if not (self._lock and getattr(self._lock, "_acquired", False)):
            raise DomainLockedError(
                "Crash recovery transition refused: Caller must hold sender mutex lock before converting crash state"
            )

        if state_data is None:
            state_data = self.load_state() or {}

        reason_code = "CRASH_BEFORE_CONFIRMATION"
        details = "Interrupted in-flight transaction detected from previous run. Must manually verify on-chain status before proceeding."
        now = datetime.now(timezone.utc).isoformat()

        clean_hash = state_data.get("tx_hash", "Unknown")
        tx_hashes = state_data.get("tx_hashes", [])
        if not isinstance(tx_hashes, list):
            tx_hashes = [clean_hash] if clean_hash != "Unknown" else []
        elif clean_hash != "Unknown" and clean_hash not in tx_hashes:
            tx_hashes.append(clean_hash)

        updated_state = {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "status": "MANUAL_REVIEW_LOCKED",
            "operation": sanitize_state_value(state_data.get("operation", "Unknown")),
            "epoch": state_data.get("epoch", "Unknown"),
            "nonce": state_data.get("nonce", "Unknown"),
            "tx_hash": clean_hash,
            "tx_hashes": tx_hashes,
            "reason_code": reason_code,
            "created_at": state_data.get("created_at", now),
            "updated_at": now,
            "details": sanitize_state_value(details)
        }
        atomic_write_json(self.get_state_path(), updated_state)
        append_audit_log(self.get_audit_path(), {
            "action": "MANUAL_REVIEW_LOCKED",
            "timestamp": now,
            "domain": self.domain.to_dict(),
            "reason": sanitize_state_value(details),
            "nonce": updated_state["nonce"],
            "tx_hash": clean_hash,
            "tx_hashes": tx_hashes,
            "reason_code": reason_code
        })
        return updated_state

    def record_preparing_to_send(self, operation: str, epoch: Any, nonce: Any, details: Optional[str] = None):
        """
        Records in-flight state immediately before signing or broadcast.
        If writing fails, caller must abort broadcast immediately.
        """
        if not self.domain_valid:
            raise DomainValidationError("Cannot record state without a valid StateDomain")

        now = datetime.now(timezone.utc).isoformat()
        state_data = {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "status": "PREPARING_TO_SEND",
            "operation": sanitize_state_value(operation),
            "epoch": epoch if epoch is not None else "Unknown",
            "nonce": int(nonce) if (nonce is not None and str(nonce).isdigit()) else (nonce if nonce is not None else "Unknown"),
            "tx_hash": "Unknown",
            "tx_hashes": [],
            "reason_code": "PREPARING_TO_SEND",
            "created_at": now,
            "updated_at": now,
            "details": sanitize_state_value(details or "Preparing to sign and broadcast transaction")
        }
        atomic_write_json(self.get_state_path(), state_data)

    def record_broadcasted(self, tx_hash: str):
        """Updates in-flight state with verified broadcasted txHash."""
        if not self.domain_valid:
            return
        state_data = self.load_state() or {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "status": "PREPARING_TO_SEND",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        clean_hash = tx_hash if (tx_hash and re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash)) else "Unknown"
        state_data["tx_hash"] = clean_hash
        existing_hashes = state_data.get("tx_hashes")
        if not isinstance(existing_hashes, list):
            existing_hashes = []
        if clean_hash != "Unknown" and clean_hash not in existing_hashes:
            existing_hashes.append(clean_hash)
        state_data["tx_hashes"] = existing_hashes
        state_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.get_state_path(), state_data)

    def record_confirmed_success(self, tx_hash: str, block_number: Any, gas_used: Any):
        """Transitions state to CONFIRMED_SUCCESS upon receipt verification."""
        if not self.domain_valid:
            return
        now = datetime.now(timezone.utc).isoformat()
        state_data = self.load_state() or {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "created_at": now,
        }
        clean_hash = tx_hash if (tx_hash and re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash)) else "Unknown"
        existing_hashes = state_data.get("tx_hashes")
        if not isinstance(existing_hashes, list):
            existing_hashes = []
        if clean_hash != "Unknown" and clean_hash not in existing_hashes:
            existing_hashes.append(clean_hash)
        state_data.update({
            "status": "CONFIRMED_SUCCESS",
            "tx_hash": clean_hash,
            "tx_hashes": existing_hashes,
            "block_number": block_number,
            "gas_used": gas_used,
            "reason_code": "CONFIRMED_SUCCESS",
            "updated_at": now,
            "details": f"Transaction confirmed in block {block_number} using {gas_used} gas"
        })
        atomic_write_json(self.get_state_path(), state_data)
        append_audit_log(self.get_audit_path(), {
            "action": "CONFIRMED_SUCCESS",
            "timestamp": now,
            "domain": self.domain.to_dict(),
            "tx_hash": clean_hash,
            "tx_hashes": existing_hashes,
            "block_number": block_number,
            "gas_used": gas_used
        })

    def record_confirmed_revert(self, tx_hash: str, block_number: Any, gas_used: Any, revert_reason: Optional[str] = None):
        """Records on-chain confirmed revert outcome."""
        if not self.domain_valid:
            return
        now = datetime.now(timezone.utc).isoformat()
        state_data = self.load_state() or {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "created_at": now,
        }
        clean_hash = tx_hash if (tx_hash and re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash)) else "Unknown"
        existing_hashes = state_data.get("tx_hashes")
        if not isinstance(existing_hashes, list):
            existing_hashes = []
        if clean_hash != "Unknown" and clean_hash not in existing_hashes:
            existing_hashes.append(clean_hash)
        state_data.update({
            "status": "CONFIRMED_REVERT",
            "tx_hash": clean_hash,
            "tx_hashes": existing_hashes,
            "block_number": block_number,
            "gas_used": gas_used,
            "reason_code": "CONFIRMED_REVERT",
            "updated_at": now,
            "details": sanitize_state_value(f"Transaction reverted in block {block_number}: {revert_reason or 'revert'}")
        })
        atomic_write_json(self.get_state_path(), state_data)
        append_audit_log(self.get_audit_path(), {
            "action": "CONFIRMED_REVERT",
            "timestamp": now,
            "domain": self.domain.to_dict(),
            "tx_hash": clean_hash,
            "tx_hashes": existing_hashes,
            "block_number": block_number,
            "gas_used": gas_used,
            "revert_reason": sanitize_state_value(str(revert_reason))
        })

    def inspect_lock_status(self) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """
        Pure read-only inspection for --check-lock CLI.
        STRICTLY READ-ONLY: Never writes or deletes any state or lock file.
        Distinguishes 5 outputs:
        - 无锁 (NO_LOCK)
        - 发送中 (SENDING)
        - 疑似崩溃 (SUSPECTED_CRASH)
        - 人工核对锁 (MANUAL_REVIEW_LOCKED)
        - 状态损坏 (CORRUPT_STATE)
        """
        if not self.domain_valid:
            return ("状态损坏", "Domain configuration invalid or unresolvable", None)

        try:
            state_data = self.load_state()
        except KeeperStateError as e:
            return ("状态损坏", f"State file corrupted or invalid: {str(e)}", None)
        except Exception as e:
            return ("状态损坏", f"Unexpected error loading state file: {str(e)}", None)

        if state_data:
            st = state_data.get("status")
            if st in ("MANUAL_REVIEW_LOCKED", "UNCERTAIN"):
                reason = state_data.get("reason_code") or "MANUAL_REVIEW_LOCKED"
                return ("人工核对锁", f"Locked pending manual review (reason: {reason})", state_data)

            if st == "PREPARING_TO_SEND":
                lock = DomainFileLock(self.get_lock_path())
                if not lock.acquire():
                    return ("发送中", "Active Keeper process currently broadcasting/in-flight (lock is held)", state_data)
                else:
                    lock.release()
                    return ("疑似崩溃", "In-flight transaction interrupted by process death or crash (lock is not held)", state_data)

            if st in ("IDLE", "CONFIRMED_SUCCESS", "CONFIRMED_REVERT"):
                cross_blocked, cross_reason, other_data = self.check_cross_miner_protection()
                if cross_blocked:
                    return ("人工核对锁", cross_reason, other_data)
                return ("无锁", f"Clean / Last status: {st}", state_data)

            return ("状态损坏", f"Unrecognized status in state file: {st}", state_data)

        cross_blocked, cross_reason, other_data = self.check_cross_miner_protection()
        if cross_blocked:
            return ("人工核对锁", cross_reason, other_data)

        return ("无锁", "Clean / No persistent state file found", None)

    def record_manual_review_lock(
        self,
        reason: str,
        nonce: Any,
        operation: str,
        tx_hash: Optional[str] = None,
        reason_code: Optional[str] = None,
        details: Optional[str] = None,
        tx_hashes: Optional[List[str]] = None
    ):
        """Persists manual review lock for the domain."""
        if not self.domain_valid:
            return
        now = datetime.now(timezone.utc).isoformat()
        clean_hash = tx_hash if (tx_hash and re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash)) else "Unknown"

        hashes = list(tx_hashes) if isinstance(tx_hashes, list) else []
        if not hashes:
            try:
                prev_state = self.load_state()
                if prev_state and isinstance(prev_state.get("tx_hashes"), list):
                    hashes = list(prev_state["tx_hashes"])
            except Exception:
                pass
        if clean_hash != "Unknown" and clean_hash not in hashes:
            hashes.append(clean_hash)

        state_data = {
            "version": CURRENT_STATE_VERSION,
            "domain": self.domain.to_dict(),
            "status": "MANUAL_REVIEW_LOCKED",
            "operation": sanitize_state_value(operation),
            "nonce": int(nonce) if (nonce is not None and str(nonce).isdigit()) else (nonce if nonce is not None else "Unknown"),
            "tx_hash": clean_hash,
            "tx_hashes": hashes,
            "reason_code": reason_code or "MANUAL_REVIEW_LOCKED",
            "created_at": now,
            "updated_at": now,
            "details": sanitize_state_value(details or reason)
        }
        atomic_write_json(self.get_state_path(), state_data)
        append_audit_log(self.get_audit_path(), {
            "action": "MANUAL_REVIEW_LOCKED",
            "timestamp": now,
            "domain": self.domain.to_dict(),
            "reason": sanitize_state_value(reason),
            "nonce": state_data["nonce"],
            "tx_hash": clean_hash,
            "tx_hashes": hashes,
            "reason_code": state_data["reason_code"]
        })

    def manual_unlock(
        self,
        expected_nonce: Optional[int] = None,
        operator_reason: Optional[str] = None,
        onchain_snapshot: Optional[Any] = None,
        snapshot_provider: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes manual unlock workflow after operator verification.
        Acquires sender/domain lock to guarantee mutual exclusion during unlock.
        Re-reads state inside the lock, optionally collects a read-only on-chain snapshot
        via snapshot_provider(state_data), then writes audit log and updated state.
        Does not delete state file or audit log; updates state to IDLE and preserves history.
        Writes onchain_snapshot to audit log ('快照不可用' if unavailable).
        """
        if not self.domain_valid:
            raise DomainValidationError("Cannot unlock without a valid StateDomain")

        if not self.acquire_lock():
            raise DomainLockedError(
                f"Cannot perform manual unlock: Domain/Sender lock on {self.get_lock_path()} is held by an active Keeper process"
            )
        try:
            state_data = self.load_state()
            if not state_data:
                raise InvalidStateError("No existing state file found to unlock")

            cur_status = state_data.get("status")
            if cur_status not in ("MANUAL_REVIEW_LOCKED", "UNCERTAIN", "PREPARING_TO_SEND"):
                raise InvalidStateError(f"Current state is not locked (status={cur_status}); manual unlock not required")

            if not operator_reason or not str(operator_reason).strip():
                raise InvalidStateError("Operator audit reason is mandatory for manual unlock")

            file_nonce = state_data.get("nonce")
            if file_nonce is not None and file_nonce != "Unknown":
                if expected_nonce is None:
                    raise InvalidStateError(
                        f"State file has recorded nonce {file_nonce}, but no expected_nonce was provided. "
                        f"Operator must verify on-chain transaction outcome and explicitly provide expected_nonce."
                    )
                if int(expected_nonce) != int(file_nonce):
                    raise InvalidStateError(f"Expected nonce mismatch: expected {expected_nonce}, file has {file_nonce}")

            snapshot_value = onchain_snapshot
            if snapshot_provider is not None:
                snapshot_value = snapshot_provider(state_data)
            if snapshot_value is None:
                snapshot_value = "快照不可用"

            now = datetime.now(timezone.utc).isoformat()
            audit_entry = {
                "action": "MANUAL_UNLOCK",
                "timestamp": now,
                "domain": self.domain.to_dict(),
                "previous_status": state_data.get("status"),
                "previous_reason": state_data.get("reason_code"),
                "previous_nonce": state_data.get("nonce"),
                "previous_tx_hash": state_data.get("tx_hash"),
                "previous_tx_hashes": state_data.get("tx_hashes", []),
                "operator_reason": sanitize_state_value(operator_reason.strip()),
                "onchain_snapshot": snapshot_value,
                "note": "Unlocked manually by operator affirmation after on-chain verification. Not automated cryptographic proof of on-chain confirmation."
            }
            append_audit_log(self.get_audit_path(), audit_entry)

            state_data.update({
                "status": "IDLE",
                "reason_code": "UNLOCKED_MANUALLY",
                "updated_at": now,
                "details": f"Unlocked manually by operator: {audit_entry['operator_reason']}"
            })
            atomic_write_json(self.get_state_path(), state_data)
            return state_data
        finally:
            self.release_lock()

    def build_context_from_state(self, state_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Builds manual_review_context dictionary matching KeeperBot specifications."""
        if not state_data:
            return {}
        domain = state_data.get("domain", {})
        clean_hash = state_data.get("tx_hash")
        if clean_hash == "Unknown":
            clean_hash = None
        hashes = state_data.get("tx_hashes")
        if not isinstance(hashes, list):
            hashes = [clean_hash] if clean_hash else []
        return {
            "chain_id": domain.get("chain_id", "Unknown"),
            "signer_address": domain.get("sender_address", "Unknown"),
            "target_contract": domain.get("miner_address", "Unknown"),
            "nonce": state_data.get("nonce"),
            "function_name": state_data.get("operation", "Unknown"),
            "tx_hash": clean_hash,
            "tx_hashes": hashes,
            "reason_code": state_data.get("reason_code"),
            "status": state_data.get("status")
        }
