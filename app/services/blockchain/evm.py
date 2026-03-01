"""
Unified Etherscan-compatible API client for EVM chains.
All supported chains use the same API shape — only the base URL and key differ.
"""
import httpx
from datetime import datetime, timezone
from app.core.config import settings

# Chain config: base_url, api_key_attr, native_symbol, coingecko_id, confirmations_required
CHAIN_CONFIG = {
    "ethereum": {
        "base_url": "https://api.etherscan.io/api",
        "api_key_attr": "ETHERSCAN_API_KEY",
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 6,
    },
    "bsc": {
        "base_url": "https://api.bscscan.com/api",
        "api_key_attr": "BSCSCAN_API_KEY",
        "symbol": "BNB",
        "coingecko_id": "binancecoin",
        "confirmations_required": 3,
    },
    "polygon": {
        "base_url": "https://api.polygonscan.com/api",
        "api_key_attr": "POLYGONSCAN_API_KEY",
        "symbol": "MATIC",
        "coingecko_id": "matic-network",
        "confirmations_required": 1,
    },
    "arbitrum": {
        "base_url": "https://api.arbiscan.io/api",
        "api_key_attr": "ARBISCAN_API_KEY",
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "base": {
        "base_url": "https://api.basescan.org/api",
        "api_key_attr": "BASESCAN_API_KEY",
        "symbol": "ETH",
        "coingecko_id": "ethereum",
        "confirmations_required": 1,
    },
    "avalanche": {
        "base_url": "https://api.routescan.io/v2/network/mainnet/evm/43114/etherscan/api",
        "api_key_attr": "AVALANCHE_API_KEY",
        "symbol": "AVAX",
        "coingecko_id": "avalanche-2",
        "confirmations_required": 1,
    },
}

HTTP_TIMEOUT = 15.0


def _get_api_key(chain: str) -> str | None:
    """Get the API key for a chain from settings."""
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return None
    key = getattr(settings, cfg["api_key_attr"], "")
    return key if key else None


def get_supported_chains() -> list[str]:
    """Return list of chains that have API keys configured."""
    return [chain for chain in CHAIN_CONFIG if _get_api_key(chain)]


async def get_transaction(chain: str, tx_hash: str) -> dict | None:
    """
    Fetch transaction details from the block explorer.
    Returns structured tx data or None on failure.
    """
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return {"error": f"Unsupported chain: {chain}"}

    api_key = _get_api_key(chain)
    if not api_key:
        supported = get_supported_chains()
        return {
            "error": f"No API key configured for {chain}",
            "supported_chains": supported,
        }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # Get transaction receipt for status
            receipt_resp = await client.get(cfg["base_url"], params={
                "module": "proxy",
                "action": "eth_getTransactionReceipt",
                "txhash": tx_hash,
                "apikey": api_key,
            })
            receipt_data = receipt_resp.json()

            # Get transaction details
            tx_resp = await client.get(cfg["base_url"], params={
                "module": "proxy",
                "action": "eth_getTransactionByHash",
                "txhash": tx_hash,
                "apikey": api_key,
            })
            tx_data = tx_resp.json()

            # Get current block number for confirmation count
            block_resp = await client.get(cfg["base_url"], params={
                "module": "proxy",
                "action": "eth_blockNumber",
                "apikey": api_key,
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

    api_key = _get_api_key(chain)
    if not api_key:
        supported = get_supported_chains()
        return {
            "error": f"No API key configured for {chain}",
            "supported_chains": supported,
        }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # Get balance
            balance_resp = await client.get(cfg["base_url"], params={
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
                "apikey": api_key,
            })
            balance_data = balance_resp.json()

            # Get tx count
            txcount_resp = await client.get(cfg["base_url"], params={
                "module": "proxy",
                "action": "eth_getTransactionCount",
                "address": address,
                "tag": "latest",
                "apikey": api_key,
            })
            txcount_data = txcount_resp.json()

            # Get recent transactions (last 5)
            txlist_resp = await client.get(cfg["base_url"], params={
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 5,
                "sort": "desc",
                "apikey": api_key,
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

    api_key = _get_api_key(chain)
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(cfg["base_url"], params={
                "module": "proxy",
                "action": "eth_gasPrice",
                "apikey": api_key,
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
    for chain in CHAIN_CONFIG:
        if not _get_api_key(chain):
            continue
        result = await get_transaction(chain, tx_hash)
        if result and "error" not in result:
            return chain
    return None


async def probe_address_chain(address: str) -> str | None:
    """
    Try each supported EVM chain to find activity for an address.
    Returns the first chain with a non-zero tx count, or 'ethereum' as default.
    """
    for chain in CHAIN_CONFIG:
        if not _get_api_key(chain):
            continue
        result = await get_wallet(chain, address)
        if result and "error" not in result and result.get("tx_count", 0) > 0:
            return chain
    # Default to ethereum if we have the key
    if _get_api_key("ethereum"):
        return "ethereum"
    return None
