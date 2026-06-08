from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP when running behind a reverse proxy / CDN.

    Preference order:
      1. ``CF-Connecting-IP`` / ``True-Client-IP`` — Cloudflare's real client IP.
         REQUIRED when behind Cloudflare: ``X-Forwarded-For`` and the socket peer
         resolve to a Cloudflare *edge* node, which rotates between PoPs per
         request — so relying on them fragments a single visitor into many IPs.
      2. ``X-Forwarded-For`` — leftmost entry, the original client. The header
         may be a comma-separated chain (client, proxy1, proxy2, ...).
      3. ``X-Real-IP`` — set by some proxies (e.g. nginx) as a single value.
      4. The direct socket peer (``request.client.host``) as a last resort.

    Returns ``"unknown"`` only when no source is available. Keeping this in one
    place ensures visitor pageview tracking and chat/conversation ingestion
    attribute the same IP to the same client.
    """
    # Cloudflare sets these to the original visitor IP (stable across its edge
    # nodes). Check first — otherwise we'd record the rotating edge IP instead.
    for header in ("CF-Connecting-IP", "True-Client-IP"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first

    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()

    return request.client.host if request.client else "unknown"
