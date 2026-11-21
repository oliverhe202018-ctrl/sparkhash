"""Network selection shared by the miner-facing CLIs."""

MAINNET_CHAIN_ID = 56
TESTNET_CHAIN_ID = 97
MAINNET_RPC_URL = "https://bsc-rpc.publicnode.com"
MAINNET_MINER_PROXY = "0x614b5efd21cfa99cb893abf2fb02d791b3aea843"

NETWORK_NAMES = {MAINNET_CHAIN_ID: "BSC Mainnet", TESTNET_CHAIN_ID: "BSC Testnet"}
EXPLORERS = {MAINNET_CHAIN_ID: "https://bscscan.com", TESTNET_CHAIN_ID: "https://testnet.bscscan.com"}


def add_chain_argument(parser):
    parser.add_argument(
        "--chain-id", type=int, choices=NETWORK_NAMES, default=TESTNET_CHAIN_ID,
        help="Expected chain ID (97: BSC Testnet, default; 56: BSC Mainnet)",
    )


def chain_settings(args, testnet_rpc, testnet_proxy):
    chain_id = getattr(args, "chain_id", TESTNET_CHAIN_ID)
    if chain_id not in NETWORK_NAMES:
        raise ValueError("Only BSC chain IDs 56 and 97 are supported")

    mainnet = chain_id == MAINNET_CHAIN_ID
    default_rpc = MAINNET_RPC_URL if mainnet else testnet_rpc
    default_proxy = MAINNET_MINER_PROXY if mainnet else testnet_proxy
    rpc = getattr(args, "rpc", default_rpc)
    if mainnet and not rpc:
        raise ValueError("Mainnet requires an RPC URL for chain and epoch verification")
    proxy = args.miner_contract if args.miner_contract is not None else default_proxy
    return chain_id, rpc, proxy


def verify_rpc_chain(result, expected):
    actual = int(result, 16) if isinstance(result, str) else int(result)
    if actual != expected:
        raise ValueError(f"SECURITY REJECTION: Chain ID {actual} != {expected} ({NETWORK_NAMES[expected]} required)!")
    return actual
