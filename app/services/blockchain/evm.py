"""
Unified Etherscan API V2 client for EVM chains.
All supported chains use a single API key and base URL — only the chain ID differs.
Docs: https://docs.etherscan.io
"""
import httpx
from datetime import datetime, timezone
from app.core.config import settings

# Etherscan API V2 — single endpoint for all chains
BASE_URL = "https://api.etherscan.io/v2/api"

# Chain config: chain_id, native_symbol, coingecko_id, confirmations_required
CHAIN_CONFIG = {
    "ethereum": {
        "chain_id": 1,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 6,
    },
    "bsc": {
        "chain_id": 56,
        "symbol": "BNB",
        "coingecko_id": "binancecoin",
        "confirmations_required": 3,
    },
    "polygon": {
        "chain_id": 137,
        "symbol": "POL",
        "coingecko_id": "matic-network",
        "confirmations_required": 1,
    },
    "arbitrum": {
        "chain_id": 42161,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "base": {
        "chain_id": 8453,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "avalanche": {
        "chain_id": 43114,
        "symbol": "AVAX",
        "coingecko_id": "avalanche-2",
        "confirmations_required": 1,
    },
    "optimism": {
        "chain_id": 10,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "linea": {
        "chain_id": 59144,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "scroll": {
        "chain_id": 534352,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "blast": {
        "chain_id": 81457,
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "gnosis": {
        "chain_id": 100,
        "symbol": "xDAI",
        "coingecko_id": "xdai",
        "confirmations_required": 1,
    },
    "celo": {
        "chain_id": 42220,
        "symbol": "CELO",
        "coingecko_id": "celo",
        "confirmations_required": 1,
    },
}

HTTP_TIMEOUT = 15.0


def _get_api_key() -> str | None:
    """Get the single Etherscan API key from settings."""
    key = getattr(settings, "ETHERSCAN_API_KEY", "")
    return key if key else None


def get_supported_chains() -> list[str]:
    """Return list of supported EVM chains (available when API key is set)."""
    if _get_api_key():
        return list(CHAIN_CONFIG.keys())
    return []


def _base_params(chain: str) -> dict | None:
    """Build the common params dict with chainid and apikey. Returns None if misconfigured."""
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return None
    api_key = _get_api_key()
    if not api_key:
        return None
    return {"chainid": cfg["chain_id"], "apikey": api_key}


async def get_transaction(chain: str, tx_hash: str) -> dict | None:
    """
    Fetch transaction details from Etherscan V2.
    Returns structured tx data or None on failure.
    """
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return {"error": f"Unsupported chain: {chain}"}

    base = _base_params(chain)
    if not base:
        return {
            "error": f"No Etherscan API key configured",
            "supported_chains": get_supported_chains(),
        }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # Get transaction receipt for status
            receipt_resp = await client.get(BASE_URL, params={
                **base,
                "module": "proxy",
                "action": "eth_getTransactionReceipt",
                "txhash": tx_hash,
            })
            receipt_data = receipt_resp.json()

            # Get transaction details
            tx_resp = await client.get(BASE_URL, params={
                **base,
                "module": "proxy",
                "action": "eth_getTransactionByHash",
                "txhash": tx_hash,
            })
            tx_data = tx_resp.json()

            # Get current block number for confirmation count
            block_resp = await client.get(BASE_URL, params={
                **base,
                "module": "proxy",
                "action": "eth_blockNumber",
            })
            block_data = block_resp.json()

        tx = tx_data.get("result")
        receipt = receipt_data.get("result")

        if not tx or tx == "null" or isinstance(tx, str):
            return {"error": f"Transaction not found on {chain}", "tx_hash": tx_hash}

        current_block = int(block_data.get("result", "0x0"), 16)
        tx_block = int(tx.get("blockNumber", "0x0"), 16) if tx.get("blockNumber") else 0
        confirmations = max(0, current_block - tx_block) if tx_block > 0 else 0

        # Parse status
        if receipt and receipt != "null":
            status_hex = receipt.get("status", "0x1")
            status = "success" if status_hex == "0x1" else "failed"
            gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        elif tx_block == 0:
            status = "pending"
            gas_used = 0
        else:
            status = "unknown"
            gas_used = 0

        gas_limit = int(tx.get("gas", "0x0"), 16)
        gas_price_wei = int(tx.get("gasPrice", "0x0"), 16)
        value_wei = int(tx.get("value", "0x0"), 16)

        return {
            "chain": chain,
            "tx_hash": tx_hash,
            "status": status,
            "from_address": tx.get("from", ""),
            "to_address": tx.get("to", ""),
            "value_wei": value_wei,
            "value_native": value_wei / 1e18,
            "native_symbol": cfg["symbol"],
            "gas_limit": gas_limit,
            "gas_used": gas_used,
            "gas_price_gwei": gas_price_wei / 1e9,
            "tx_fee_native": (gas_used * gas_price_wei) / 1e18 if gas_used else 0,
            "block_number": tx_block,
            "confirmations": confirmations,
            "confirmations_required": cfg["confirmations_required"],
            "nonce": int(tx.get("nonce", "0x0"), 16),
            "input_data_length": len(tx.get("input", "0x")) - 2,  # subtract '0x'
            "is_contract_interaction": len(tx.get("input", "0x")) > 2,
        }

    except Exception as e:
        return {"error": f"Failed to fetch transaction from {chain}: {str(e)}"}


async def get_wallet(chain: str, address: str) -> dict | None:
    """Fetch wallet balance and basic info."""
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return {"error": f"Unsupported chain: {chain}"}

    base = _base_params(chain)
    if not base:
        return {
            "error": f"No Etherscan API key configured",
            "supported_chains": get_supported_chains(),
        }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # Get balance
            balance_resp = await client.get(BASE_URL, params={
                **base,
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
            })
            balance_data = balance_resp.json()

            # Get tx count
            txcount_resp = await client.get(BASE_URL, params={
                **base,
                "module": "proxy",
                "action": "eth_getTransactionCount",
                "address": address,
                "tag": "latest",
            })
            txcount_data = txcount_resp.json()

            # Get recent transactions (last 5)
            txlist_resp = await client.get(BASE_URL, params={
                **base,
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 5,
                "sort": "desc",
            })
            txlist_data = txlist_resp.json()

        balance_wei = int(balance_data.get("result", "0"))
        tx_count = int(txcount_data.get("result", "0x0"), 16)
        recent_txs = txlist_data.get("result", [])

        last_active = None
        if isinstance(recent_txs, list) and recent_txs:
            ts = int(recent_txs[0].get("timeStamp", "0"))
            if ts > 0:
                last_active = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        return {
            "chain": chain,
            "address": address,
            "balance_wei": balance_wei,
            "balance_native": balance_wei / 1e18,
            "native_symbol": cfg["symbol"],
            "tx_count": tx_count,
            "last_active": last_active,
            "recent_transactions": [
                {
                    "hash": tx.get("hash"),
                    "from": tx.get("from"),
                    "to": tx.get("to"),
                    "value_native": int(tx.get("value", "0")) / 1e18,
                    "status": "success" if tx.get("isError") == "0" else "failed",
                }
                for tx in (recent_txs if isinstance(recent_txs, list) else [])
            ][:5],
        }

    except Exception as e:
        return {"error": f"Failed to fetch wallet from {chain}: {str(e)}"}


async def get_gas_price(chain: str) -> dict | None:
    """Fetch current gas price for a chain."""
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return None

    base = _base_params(chain)
    if not base:
        return None

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(BASE_URL, params={
                **base,
                "module": "proxy",
                "action": "eth_gasPrice",
            })
            data = resp.json()

        gas_price_wei = int(data.get("result", "0x0"), 16)
        return {
            "chain": chain,
            "gas_price_gwei": gas_price_wei / 1e9,
        }
    except Exception:
        return None


async def probe_transaction_chain(tx_hash: str) -> str | None:
    """
    Try each supported EVM chain to find which one has this transaction.
    Returns the chain name or None.
    """
    if not _get_api_key():
        return None
    for chain in CHAIN_CONFIG:
        result = await get_transaction(chain, tx_hash)
        if result and "error" not in result:
            return chain
    return None


async def probe_address_chain(address: str) -> str | None:
    """
    Try each supported EVM chain to find activity for an address.
    Returns the first chain with a non-zero tx count, or 'ethereum' as default.
    """
    if not _get_api_key():
        return None
    for chain in CHAIN_CONFIG:
        result = await get_wallet(chain, address)
        if result and "error" not in result and result.get("tx_count", 0) > 0:
            return chain
    # Default to ethereum if we have the key
    return "ethereum"
