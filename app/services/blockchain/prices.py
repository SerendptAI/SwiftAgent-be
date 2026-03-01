"""
USD price conversion via CoinGecko free API.
In-memory cache with 60s TTL to avoid rate limits.
"""
import time
import httpx

HTTP_TIMEOUT = 10.0
CACHE_TTL = 60  # seconds

# In-memory price cache: {coin_id: (price_usd, timestamp)}
_price_cache: dict[str, tuple[float, float]] = {}

# Map common symbols / chain names to CoinGecko IDs
COINGECKO_IDS = {
    "ethereum": "ethereum",
    "eth": "ethereum",
    "bsc": "binancecoin",
    "bnb": "binancecoin",
    "polygon": "matic-network",
    "matic": "matic-network",
    "arbitrum": "ethereum",
    "base": "ethereum",
    "avalanche": "avalanche-2",
    "avax": "avalanche-2",
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
}


async def get_price(coin_or_chain: str) -> dict:
    """
    Get the current USD price for a coin/chain.
    Returns {"coin": str, "price_usd": float} or {"error": str}.
    """
    coin_id = COINGECKO_IDS.get(coin_or_chain.lower())
    if not coin_id:
        return {"error": f"Unknown coin/chain: {coin_or_chain}"}

    # Check cache
    now = time.time()
    if coin_id in _price_cache:
        price, cached_at = _price_cache[coin_id]
        if now - cached_at < CACHE_TTL:
            return {"coin": coin_id, "price_usd": price}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
            )
            data = resp.json()

        price = data.get(coin_id, {}).get("usd")
        if price is None:
            return {"error": f"Price not found for {coin_id}"}

        _price_cache[coin_id] = (price, now)
        return {"coin": coin_id, "price_usd": price}

    except Exception as e:
        return {"error": f"Failed to fetch price: {str(e)}"}


def convert_to_usd(amount: float, price_usd: float) -> float:
    """Convert a native token amount to USD."""
    return round(amount * price_usd, 2)
