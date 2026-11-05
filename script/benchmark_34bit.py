import time
import hashlib
from eth_abi import encode

def benchmark_local_hashing():
    print("=== 34-bit PoW Local Machine Benchmark ===")
    
    # 34-bit difficulty target: top 34 bits must be zero
    # Target value: 1 << 222 (out of 256 bits)
    # Target integer: 2**222
    target_34 = 1 << 222
    
    # Measure local keccak/sha3 hash rate over 50 samples
    samples = 50
    batch_size = 50000
    hash_rates = []
    
    dummy_sub = b"\x12" * 32
    
    for i in range(samples):
        t0 = time.perf_counter()
        # Run batch of 50,000 hashes
        for nonce in range(batch_size):
            # Simulate abi.encode(bytes32, uint256)
            data = dummy_sub + nonce.to_bytes(32, "big")
            # SHA-256 / keccak256
            _h = hashlib.sha256(data).digest()
        t1 = time.perf_counter()
        dt = t1 - t0
        rate = batch_size / dt
        hash_rates.append(rate)
    
    avg_rate = sum(hash_rates) / len(hash_rates)
    min_rate = min(hash_rates)
    max_rate = max(hash_rates)
    variance = sum((r - avg_rate) ** 2 for r in hash_rates) / len(hash_rates)
    std_dev = variance ** 0.5
    
    expected_hashes_34bit = 2**34 # 17,179,869,184
    expected_time_single_thread = expected_hashes_34bit / avg_rate
    
    print(f"Sample count (N): {samples}")
    print(f"Hash throughput (mean): {avg_rate:,.0f} hashes/sec")
    print(f"Hash throughput (std dev): {std_dev:,.0f} hashes/sec ({std_dev/avg_rate*100:.2f}%)")
    print(f"Hash throughput (min): {min_rate:,.0f} hashes/sec")
    print(f"Hash throughput (max): {max_rate:,.0f} hashes/sec")
    print(f"34-bit target: 2^34 = {expected_hashes_34bit:,} expected trials per share")
    print(f"Expected time per share (single-threaded CPU): {expected_time_single_thread:,.1f} s ({expected_time_single_thread/3600:,.2f} hours)")
    print(f"Expected time per share (8-thread CPU parallel [外推]): {expected_time_single_thread/8:,.1f} s ({expected_time_single_thread/28800:,.2f} hours)")
    print(f"Expected time per share (RTX 4090 GPU @ 20 GH/s [假设]): {expected_hashes_34bit / 20e9:,.2f} s")
    print("==========================================")

if __name__ == "__main__":
    benchmark_local_hashing()
