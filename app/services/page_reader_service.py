import ipaddress
import logging
import socket
import asyncio
import httpx
from playwright.async_api import async_playwright
from app.core.config import settings
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


async def _is_url_safe(url: str) -> tuple[bool, str]:
    """
    Resolve the URL's hostname and reject if it points to a private,
    loopback, link-local, or reserved IP address (SSRF protection).
    Also checks explicit domain blocklist.
    Returns (is_safe, reason).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "URL has no hostname"

        # Explicit blocklist to prevent internal endpoint probing
        if hostname.lower() in ("api.swiftagents.org", "localhost", "127.0.0.1", "169.254.169.254"):
            return False, "Access to internal API domains or restricted hosts is prohibited"

        # Resolve all IPs for the hostname using async wrapper
        loop = asyncio.get_running_loop()
        addr_infos = await loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        
        if not addr_infos:
            return False, "Could not resolve hostname"

        for info in addr_infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                logger.warning(
                    "SSRF blocked: %s resolved to restricted IP %s", hostname, ip_str
                )
                return False, "URL targets a restricted network address"

        return True, ""
    except Exception as e:
        return False, f"URL validation failed: {e}"


async def _resolve_final_url(url: str) -> tuple[bool, str, str]:
    """Follow redirects and validate safety at each step to prevent HTTP redirect SSRF."""
    current_url = url
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=5.0) as client:
            for _ in range(5): # Max 5 redirects
                is_safe, reason = await _is_url_safe(current_url)
                if not is_safe:
                    return False, reason, ""
                
                try:
                    # Use GET with stream to avoid downloading body
                    async with client.stream("GET", current_url) as response:
                        if 300 <= response.status_code < 400 and "location" in response.headers:
                            next_url = urljoin(current_url, response.headers["location"])
                            current_url = next_url
                            continue
                        break
                except httpx.RequestError as e:
                    # If we can't fetch it, we let playwright try (but we validated the hostname already)
                    break
            
            return True, "", current_url
    except Exception as e:
        return False, f"Redirect tracing failed: {e}", ""


async def read_website_page(url: str) -> dict:
    """
    Loads a URL, extracts visible text, and grabs available links.
    Optimized for speed (quick lookup) to provide agent context.
    """
    try:
        # SSRF protection: block private/internal IPs, resolve HTTP redirects safely
        is_safe, reason, final_url = await _resolve_final_url(url)
        if not is_safe:
            logger.warning(f"SSRF protection blocked URL: {url} — {reason}")
            return {"error": reason}

        logger.info(f"Dynamically reading website page: {final_url}")
        async with async_playwright() as p:
            if settings.PLAYWRIGHT_WS_ENDPOINT:
                try:
                    browser = await p.chromium.connect_over_cdp(settings.PLAYWRIGHT_WS_ENDPOINT)
                except Exception as e:
                    logger.warning(f"Failed to connect to WS endpoint: {e}. Falling back to local Chromium.")
                    browser = await p.chromium.launch(headless=True)
            else:
                browser = await p.chromium.launch(headless=True)
                
            context = await browser.new_context()
            page = await context.new_page()
            
            # Intercept sub-requests and JS redirects to ensure they don't bypass SSRF protections
            async def route_handler(route):
                req_is_safe, req_reason = await _is_url_safe(route.request.url)
                if not req_is_safe:
                    logger.warning(f"SSRF blocked sub-request to {route.request.url}: {req_reason}")
                    await route.abort("accessdenied")
                else:
                    await route.continue_()
            
            await page.route("**/*", route_handler)
            
            # Fast navigation, just wait for DOM
            await page.goto(final_url, wait_until="domcontentloaded", timeout=15000)
            
            # Brief pause for React hydration
            await page.wait_for_timeout(1500)
            
            # 1. Extract visible text
            try:
                content = await page.inner_text("main", timeout=5000)
            except Exception:
                content = await page.inner_text("body", timeout=5000)
                
            # Clean up text to save tokens
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            clean_content = '\n'.join(lines)[:12000] # Truncate to ~12k chars
            
            # 2. Extract Links
            hrefs = await page.eval_on_selector_all(
                "a[href]", 
                "elements => elements.map(e => e.getAttribute('href'))"
            )
            
            # Normalize and filter links (keep same domain, remove fragments)
            base_url = urlparse(final_url)
            valid_links = set()
            for href in hrefs:
                if href and not href.startswith(('javascript:', 'mailto:', 'tel:')):
                    full_url = urljoin(final_url, href.split('#')[0])
                    if urlparse(full_url).netloc == base_url.netloc:
                        valid_links.add(full_url)

            await browser.close()
            
            if not clean_content:
                return {"error": "Page loaded but no readable text was found."}
                
            return {
                "url": final_url,
                "content": clean_content,
                "links": list(valid_links)[:15] # Top 15 links to avoid token overload
            }
    except Exception as e:
        logger.warning(f"Failed to read page {url}: {e}")
        return {"error": str(e)}
