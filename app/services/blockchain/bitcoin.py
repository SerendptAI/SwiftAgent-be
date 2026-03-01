"""
Bitcoin blockchain data via blockchain.info API (no API key needed).
"""
import httpx

HTTP_TIMEOUT = 15.0
CONFIRMATIONS_REQUIRED = 6


async def get_transaction(tx_hash: str) -> dict | None:
    """Fetch Bitcoin transaction details from blockchain.info."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"https://blockchain.info/rawtx/{tx_hash}")
            if resp.status_code != 200:
                return {"error": f"Transaction not found: {tx_hash}"}
            tx = resp.json()

            # Get current block height for confirmations
            height_resp = await client.get("https://blockchain.info/latestblock")
            latest = height_resp.json()
            current_height = latest.get("height", 0)

        tx_block = tx.get("block_height")
        if tx_block:
            confirmations = max(0, current_height - tx_block + 1)
            status = "confirmed" if confirmations >= CONFIRMATIONS_REQUIRED else "confirming"
        else:
            confirmations = 0
            status = "pending"

        total_input = sum(inp.get("prev_out", {}).get("value", 0) for inp in tx.get("inputs", []))
        total_output = sum(out.get("value", 0) for out in tx.get("out", []))
        fee = total_input - total_output if total_input > 0 else tx.get("fee", 0)

        return {
            "chain": "bitcoin",
            "tx_hash": tx_hash,
            "status": status,
            "confirmations": confirmations,
            "confirmations_required": CONFIRMATIONS_REQUIRED,
            "block_height": tx_block,
            "time": tx.get("time"),
            "fee_satoshis": fee,
            "fee_btc": fee / 1e8,
            "total_input_btc": total_input / 1e8,
            "total_output_btc": total_output / 1e8,
            "input_count": len(tx.get("inputs", [])),
            "output_count": len(tx.get("out", [])),
            "size_bytes": tx.get("size", 0),
            "weight": tx.get("weight", 0),
            "inputs": [
                {
                    "address": inp.get("prev_out", {}).get("addr", "unknown"),
                    "value_btc": inp.get("prev_out", {}).get("value", 0) / 1e8,
                }
                for inp in tx.get("inputs", [])[:5]
            ],
            "outputs": [
                {
                    "address": out.get("addr", "unknown"),
                    "value_btc": out.get("value", 0) / 1e8,
                }
                for out in tx.get("out", [])[:5]
            ],
        }

    except Exception as e:
        return {"error": f"Failed to fetch Bitcoin transaction: {str(e)}"}


async def get_wallet(address: str) -> dict | None:
    """Fetch Bitcoin wallet info from blockchain.info."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"https://blockchain.info/address/{address}",
                params={"format": "json", "limit": 5},
            )
            if resp.status_code != 200:
                return {"error": f"Address not found: {address}"}
            data = resp.json()

        balance = data.get("final_balance", 0)
        tx_count = data.get("n_tx", 0)
        total_received = data.get("total_received", 0)
        total_sent = data.get("total_sent", 0)

        recent_txs = []
        for tx in data.get("txs", [])[:5]:
            recent_txs.append({
                "hash": tx.get("hash"),
                "time": tx.get("time"),
                "fee_btc": tx.get("fee", 0) / 1e8,
            })

        return {
            "chain": "bitcoin",
            "address": address,
            "balance_satoshis": balance,
            "balance_btc": balance / 1e8,
            "native_symbol": "BTC",
            "tx_count": tx_count,
            "total_received_btc": total_received / 1e8,
            "total_sent_btc": total_sent / 1e8,
            "recent_transactions": recent_txs,
        }

    except Exception as e:
        return {"error": f"Failed to fetch Bitcoin wallet: {str(e)}"}
