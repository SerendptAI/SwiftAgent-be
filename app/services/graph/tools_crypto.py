from langchain_core.tools import tool
from typing import Dict, Any

from app.services.base_crypto import base_crypto

@tool
async def get_wallet_balance(address: str) -> Dict[str, Any]:
    """Get the ETH and ERC20 balance for a given wallet address on the Base network."""
    return await base_crypto.get_wallet_balance(address)

@tool
async def get_transaction_status(tx_hash: str) -> Dict[str, Any]:
    """Get the status of a transaction on the Base network given its hash."""
    return await base_crypto.get_transaction_status(tx_hash)
