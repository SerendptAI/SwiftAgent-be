import logging
from uuid import uuid4
from playwright.async_api import async_playwright
from app.core.config import settings
from app.services import knowledge_service

logger = logging.getLogger(__name__)

async def scrape_and_ingest_docs(url: str, company_id: str, user_id: str) -> bool:
    """
    Scrape a documentation URL using Playwright and ingest it into the knowledge base.
    """
    try:
        logger.info(f"Scraping documentation from {url} for company {company_id}")
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
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait a bit for JS to render if it's an SPA
                await page.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"Timeout or error navigating to {url}, attempting to extract what loaded: {e}")
            
            # Extract main text
            title = await page.title()
            
            # Try to get main content, fallback to body
            content = ""
            try:
                content = await page.inner_text("main", timeout=2000)
            except Exception:
                try:
                    content = await page.inner_text("article", timeout=2000)
                except Exception:
                    try:
                        content = await page.inner_text("body", timeout=2000)
                    except Exception:
                        pass
                
            await browser.close()
            
            if not content or len(content.strip()) < 50:
                logger.warning(f"Scraped content from {url} is too short or empty.")
                return False
                
            # Clean up the text a bit (remove excessive newlines)
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            clean_content = '\n'.join(lines)
                
            # Chunk the content if it's very long (Gemini embed limit is ~10k tokens, but better to be safe)
            chunk_size = 4000
            chunks = [clean_content[i:i+chunk_size] for i in range(0, len(clean_content), chunk_size)]
            
            doc_id = str(uuid4())
            for i, chunk in enumerate(chunks):
                chunk_title = title if len(chunks) == 1 else f"{title} (Part {i+1})"
                metadata = {
                    "company_id": company_id,
                    "source_url": url,
                    "chunk_index": i,
                    "total_chunks": len(chunks)
                }
                await knowledge_service.ingest_document(
                    user_id=user_id,
                    doc_id=doc_id,
                    title=chunk_title,
                    content=chunk,
                    metadata=metadata
                )
                
            logger.info(f"Successfully scraped and ingested {url} into {len(chunks)} chunks.")
            return True
            
    except Exception as e:
        logger.exception(f"Failed to scrape documentation from {url}: {e}")
        return False
