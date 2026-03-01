"""
Auto-detect blockchain chain from transaction hash or wallet address format.
"""
import re

# EVM chains in priority order for probing
EVM_CHAINS = ["ethereum", "bsc", "polygon", "arbitrum", "base", "avalanche"]

# Regex patterns
EVM_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
BTC_TX_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
BTC_ADDRESS_LEGACY = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
BTC_ADDRESS_BECH32 = re.compile(r"^bc1[a-zA-HJ-NP-Z0-9]{25,62}$")


def detect_hash(value: str) -> dict:
    """
    Detect whether a string is a tx hash or address and which chain it belongs to.

    Returns:
        {"type": "tx"|"address", "chain": str, "possible_chains": list}
        or {"type": "unknown"} if unrecognised.
    """
    value = value.strip()

    # EVM transaction hash (0x + 64 hex)
    if EVM_TX_HASH.match(value):
        return {
            "type": "tx",
            "chain": "ethereum",  # default, will be probed
            "possible_chains": EVM_CHAINS,
            "value": value,
        }

    # EVM address (0x + 40 hex)
    if EVM_ADDRESS.match(value):
        return {
            "type": "address",
            "chain": "ethereum",  # default, will be probed
            "possible_chains": EVM_CHAINS,
            "value": value,
        }

    # Bitcoin transaction hash (64 hex, no 0x prefix)
    if BTC_TX_HASH.match(value):
        return {
            "type": "tx",
            "chain": "bitcoin",
            "possible_chains": ["bitcoin"],
            "value": value,
        }

    # Bitcoin address (legacy or bech32)
    if BTC_ADDRESS_LEGACY.match(value) or BTC_ADDRESS_BECH32.match(value):
        return {
            "type": "address",
            "chain": "bitcoin",
            "possible_chains": ["bitcoin"],
            "value": value,
        }

    return {"type": "unknown", "value": value}
