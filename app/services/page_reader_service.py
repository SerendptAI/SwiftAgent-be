import logging
from playwright.async_api import async_playwright
from app.core.config import settings
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

async def read_website_page(url: str) -> dict:
    """
    Loads a URL, extracts visible text, and grabs available links.
    Optimized for speed (quick lookup) to provide agent context.
    """
    try:
        logger.info(f"Dynamically reading website page: {url}")
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
            
            # Fast navigation, just wait for DOM
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
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
            base_url = urlparse(url)
            valid_links = set()
            for href in hrefs:
                if href and not href.startswith(('javascript:', 'mailto:', 'tel:')):
                    full_url = urljoin(url, href.split('#')[0])
                    if urlparse(full_url).netloc == base_url.netloc:
                        valid_links.add(full_url)

            await browser.close()
            
            if not clean_content:
                return {"error": "Page loaded but no readable text was found."}
                
            return {
                "url": url,
                "content": clean_content,
                "links": list(valid_links)[:15] # Top 15 links to avoid token overload
            }
    except Exception as e:
        logger.warning(f"Failed to read page {url}: {e}")
        return {"error": str(e)}
