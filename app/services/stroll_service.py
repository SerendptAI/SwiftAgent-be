"""
Stroll Service — BFS dashboard crawler with Playwright + Claude vision.

Core responsibilities:
- Headless browser automation (Playwright) to crawl customer dashboards
- Claude vision analysis for semantic page understanding
- Screenshot capture + upload to Cloudinary
- Navigation graph construction
- Version diffing and commit to MongoDB
"""

import base64
import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import anthropic
from playwright.async_api import async_playwright, Browser, Page

from app.core.config import settings
from app.core.database import db
from app.models.stroll_models import (
    BoundingBox,
    DiffLog,
    Edge,
    InteractiveElement,
    MovedFeature,
    NavGraph,
    PageNode,
    StrollConfig,
    StrollConfigCreate,
    WidgetStrollReport,
    StrollVersion,
)
from app.services.cloudinary_service import upload_document
from app.services import otp_challenge_service

logger = logging.getLogger(__name__)

# shared Playwright browser instance (initialized in lifespan)
_browser: Optional[Browser] = None


async def init_browser():
    """Initialize Playwright browser. Call once at app startup."""
    global _browser
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch(headless=True)
    logger.info("Playwright browser initialized")


async def close_browser():
    """Close Playwright browser. Call at app shutdown."""
    global _browser
    if _browser:
        await _browser.close()
        _browser = None
        logger.info("Playwright browser closed")


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def _analyze_page_with_vision(
    screenshot_bytes: bytes,
    raw_elements: list[dict],
    page_title: str,
    page_url: str,
) -> dict:
    """
    Use Claude vision to semantically understand a page screenshot.
    Returns enriched element descriptions and a page summary.
    """
    client = _get_anthropic_client()

    elements_text = json.dumps(raw_elements[:30], indent=2)  # cap to avoid token overflow

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(screenshot_bytes).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is a screenshot of the page '{page_title}' at {page_url}.\n\n"
                            f"Detected interactive elements from the DOM:\n{elements_text}\n\n"
                            "Please provide:\n"
                            "1. A one-sentence summary of what this page is for.\n"
                            "2. For each interactive element above, a human-readable description "
                            "of what it does (e.g., 'Opens the billing settings page'). "
                            "If an element has no text label (icon-only), describe it based on "
                            "what you see in the screenshot.\n\n"
                            "Respond in JSON format:\n"
                            '{"page_summary": "...", "elements": [{"selector": "...", '
                            '"human_description": "..."}]}'
                        ),
                    },
                ],
            }],
        )

        text = response.content[0].text
        # extract JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        return json.loads(text.strip())

    except Exception as e:
        logger.warning(f"Vision analysis failed for {page_url}: {e}")
        return {"page_summary": page_title, "elements": []}

DETECT_ELEMENTS_JS = """
() => {
    const elements = [];
    const selectors = 'a[href], button, [role="tab"], [role="menuitem"], ' +
                      '[role="button"], nav a, .nav-link, .sidebar-link, ' +
                      '[data-toggle], [data-bs-toggle]';
    const nodes = document.querySelectorAll(selectors);

    nodes.forEach((el, i) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;  // skip hidden elements

        const label = (
            el.textContent?.trim() ||
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            ''
        ).slice(0, 100);

        const href = el.getAttribute('href') || '';
        const isNav = el.tagName === 'A' || el.closest('nav') !== null ||
                      el.getAttribute('role') === 'tab' ||
                      el.getAttribute('role') === 'menuitem';

        // build a reasonably unique CSS selector
        let selector = '';
        if (el.id) {
            selector = '#' + el.id;
        } else {
            const tag = el.tagName.toLowerCase();
            const classes = Array.from(el.classList).slice(0, 3).join('.');
            selector = classes ? `${tag}.${classes}` : `${tag}:nth-of-type(${i + 1})`;
        }

        elements.push({
            selector: selector,
            label: label,
            type: isNav ? 'nav' : 'action',
            href: href,
            bbox: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height)
            }
        });
    });

    return elements;
}
"""

async def _capture_screenshot(page: Page) -> bytes:
    """Capture a full-page screenshot as PNG bytes."""
    return await page.screenshot(type="png", full_page=False)


def _compute_dom_hash(page_content: str) -> str:
    """Compute a hash of the page DOM for change detection."""
    return hashlib.sha256(page_content.encode()).hexdigest()[:16]


def _normalize_url(url: str, base_url: str) -> str:
    """Normalize a URL relative to the base dashboard URL."""
    parsed = urlparse(url)
    # skip external links, javascript:, mailto:, etc.
    if parsed.scheme and parsed.scheme not in ("http", "https", ""):
        return ""
    if not parsed.netloc and not parsed.path:
        return ""

    full_url = urljoin(base_url, url)
    # only crawl within the same domain
    base_domain = urlparse(base_url).netloc
    if urlparse(full_url).netloc != base_domain:
        return ""

    # strip fragments
    return full_url.split("#")[0]


async def _upload_screenshot(screenshot_data: bytes | str, company_id: str, page_id: str) -> str:
    """Upload screenshot to Cloudinary, return secure URL.
    Supports either raw PNG bytes or a base64 Data URI."""
    if isinstance(screenshot_data, str):
        # Base64 Data URI upload
        import cloudinary.uploader
        result = cloudinary.uploader.upload(
            screenshot_data,
            folder=f"stroll/{company_id}",
            resource_type="image",
        )
        return result["secure_url"]
    else:
        # Raw bytes upload
        result = await upload_document(
            content=screenshot_data,
            filename=f"stroll_{company_id}_{page_id}.png",
            folder=f"stroll/{company_id}",
        )
        return result["secure_url"]



# --- Heuristic selectors for auto-detecting login form fields ---
_USERNAME_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[name="user"]',
    'input[id*="email"]',
    'input[id*="user"]',
    'input[id*="login"]',
    'input[type="text"]',  # last resort — first visible text input
]

_PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[id*="password"]',
    'input[id*="pass"]',
]

_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'form button',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Submit")',
]


# --- Heuristic selectors for detecting OTP / 2FA pages ---
_OTP_INPUT_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp"]',
    'input[name*="code"]',
    'input[name*="verification"]',
    'input[name*="token"]',
    'input[type="tel"][maxlength="6"]',
    'input[type="number"][maxlength="6"]',
    'input[type="text"][maxlength="6"]',
]

_OTP_DIGIT_BOX_SELECTOR = 'input[maxlength="1"]'

_OTP_KEYWORDS = [
    "verification code",
    "enter the code",
    "enter code",
    "one-time",
    "otp",
    "two-factor",
    "2fa",
    "authentication code",
    "security code",
    "confirm your identity",
]

_OTP_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Verify")',
    'button:has-text("Confirm")',
    'button:has-text("Submit")',
    'button:has-text("Continue")',
    'input[type="submit"]',
]


async def _find_element(page: Page, custom_selector: Optional[str], fallback_selectors: list[str]):
    """
    Try a custom selector first (from widget), then fall through heuristic selectors.
    Returns the first visible element found, or None.
    """
    if custom_selector:
        try:
            el = page.locator(custom_selector).first
            if await el.is_visible(timeout=2000):
                return el
        except Exception:
            pass  # fall through to heuristics

    for selector in fallback_selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1000):
                return el
        except Exception:
            continue
    return None


async def _authenticate(page: Page, config: StrollConfig, company_id: str) -> bool:
    """
    Handle dashboard authentication.

    Priority:
      1. Username + password form-based login (primary)
      2. Pre-authenticated URL / token (fallback)

    Returns True if auth succeeded (or was not needed), False on failure.
    """
    creds = config.credentials
    if not creds:
        return True  # no auth needed

    # --- Primary path: form-based login with username + password ---
    if creds.username and creds.password:
        login_url = creds.login_url or config.dashboard_url
        logger.info(f"Attempting form-based login for company {company_id} at {login_url}")

        try:
            await page.goto(
                login_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
        except Exception as e:
            logger.error(f"Failed to navigate to login page {login_url}: {e}")
            return False

        # find and fill username field
        username_el = await _find_element(
            page, creds.username_selector, _USERNAME_SELECTORS
        )
        if not username_el:
            logger.error(
                f"Could not find username field on {login_url} for company {company_id}"
            )
            return False

        await username_el.fill(creds.username)

        # find and fill password field
        password_el = await _find_element(
            page, creds.password_selector, _PASSWORD_SELECTORS
        )
        if not password_el:
            logger.error(
                f"Could not find password field on {login_url} for company {company_id}"
            )
            return False

        await password_el.fill(creds.password)

        # find and click submit
        submit_el = await _find_element(
            page, creds.submit_selector, _SUBMIT_SELECTORS
        )
        if not submit_el:
            logger.error(
                f"Could not find submit button on {login_url} for company {company_id}"
            )
            return False

        # click submit and wait for navigation
        try:
            async with page.expect_navigation(
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            ):
                await submit_el.click()
        except Exception:
            # some SPAs don't trigger a full navigation — wait for network idle instead
            try:
                await page.wait_for_load_state("networkidle", timeout=settings.STROLL_PAGE_TIMEOUT_MS)
            except Exception as e:
                logger.warning(f"Post-login wait timed out for company {company_id}: {e}")

        # --- verify login succeeded ---
        # heuristic: password field should no longer be visible if login succeeded
        try:
            pw_still_visible = await page.locator('input[type="password"]').first.is_visible(timeout=2000)
        except Exception:
            pw_still_visible = False

        if pw_still_visible:
            logger.error(
                f"Login appears to have failed for company {company_id} — "
                f"password field still visible after submit"
            )
            return False

        # --- detect OTP / 2FA challenge page ---
        otp_detected = await _detect_otp_page(page)
        if otp_detected:
            otp_cfg = config.otp_handling
            if otp_cfg and otp_cfg.enabled:
                logger.info(f"OTP page detected for company {company_id}, initiating challenge relay")
                otp_value = await _handle_otp_challenge(page, config, company_id)
                if otp_value:
                    filled = await _fill_and_submit_otp(page, otp_value, config)
                    if not filled:
                        logger.error(f"Failed to fill OTP for company {company_id}")
                        return False
                else:
                    logger.error(f"OTP challenge timed out or failed for company {company_id}")
                    return False
            else:
                logger.warning(
                    f"OTP page detected for company {company_id} but OTP handling is "
                    f"not enabled in config — login will likely fail"
                )
                return False

        logger.info(f"Login succeeded for company {company_id}, now at {page.url}")

        # navigate to dashboard URL if we're not already there
        current = page.url.split("?")[0].rstrip("/")
        target = config.dashboard_url.split("?")[0].rstrip("/")
        if current != target:
            try:
                await page.goto(
                    config.dashboard_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception as e:
                logger.error(f"Failed to navigate to dashboard after login: {e}")
                return False

        return True

    # --- Fallback path: pre-authenticated URL / token ---
    if creds.pre_auth_url:
        logger.info(f"Using pre-auth URL for company {company_id}")
        try:
            await page.goto(
                creds.pre_auth_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
            return True
        except Exception as e:
            logger.error(f"Pre-auth URL navigation failed for company {company_id}: {e}")
            return False

    # no credentials usable
    logger.warning(f"Credentials provided but insufficient for company {company_id}")
    return True  # proceed unauthenticated


async def _detect_otp_page(page: Page) -> bool:
    """
    Heuristic detection of OTP / 2FA challenge pages.

    Checks for:
    1. Known OTP input field selectors (autocomplete, name patterns, maxlength)
    2. Multiple single-digit input boxes (common OTP pattern)
    3. Page text containing OTP-related keywords
    """
    # Check for single OTP input fields
    for selector in _OTP_INPUT_SELECTORS:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1000):
                logger.info(f"OTP page detected via selector: {selector}")
                return True
        except Exception:
            continue

    # Check for multiple single-digit boxes (e.g., 4-6 separate inputs)
    try:
        digit_boxes = page.locator(_OTP_DIGIT_BOX_SELECTOR)
        count = await digit_boxes.count()
        visible_count = 0
        for i in range(min(count, 10)):
            try:
                if await digit_boxes.nth(i).is_visible(timeout=500):
                    visible_count += 1
            except Exception:
                continue
        if visible_count >= 4:
            logger.info(f"OTP page detected via {visible_count} single-digit input boxes")
            return True
    except Exception:
        pass

    # Check page text for OTP keywords
    try:
        body_text = await page.inner_text("body", timeout=2000)
        body_lower = body_text.lower()
        for keyword in _OTP_KEYWORDS:
            if keyword in body_lower:
                logger.info(f"OTP page detected via keyword: '{keyword}'")
                return True
    except Exception:
        pass

    return False


async def _handle_otp_challenge(
    page: Page, config: StrollConfig, company_id: str,
) -> Optional[str]:
    """
    Create an OTP challenge, push-notify the admin, and wait for a response.

    Takes a screenshot of the OTP page for context, looks up the company
    admin, creates the challenge, then blocks (async) until the admin
    responds or the timeout expires.

    Returns the OTP value string, or None on timeout/failure.
    """
    otp_cfg = config.otp_handling

    # Determine who to notify
    notify_user_id = otp_cfg.notify_user_id if otp_cfg else None
    if not notify_user_id:
        # Default to company owner
        company_doc = await db.companies.find_one(
            {"id": company_id}, {"user_id": 1, "_id": 0}
        )
        if not company_doc:
            logger.error(f"Company {company_id} not found — cannot create OTP challenge")
            return None
        notify_user_id = company_doc["user_id"]

    # Screenshot the OTP page for context in the mobile app
    screenshot_url = None
    try:
        screenshot_bytes = await _capture_screenshot(page)
        screenshot_url = await _upload_screenshot(screenshot_bytes, company_id, "otp_page")
    except Exception as e:
        logger.warning(f"Failed to capture OTP page screenshot: {e}")

    login_url = config.credentials.login_url if config.credentials else config.dashboard_url

    # Create and push the challenge
    challenge = await otp_challenge_service.create_challenge(
        company_id=company_id,
        user_id=notify_user_id,
        login_url=login_url,
        screenshot_url=screenshot_url,
    )

    # Block until the admin responds (or timeout)
    otp_value = await otp_challenge_service.wait_for_challenge_response(challenge.id)
    return otp_value


async def _fill_and_submit_otp(
    page: Page, otp_value: str, config: StrollConfig,
) -> bool:
    """
    Fill the OTP value into the detected input field(s) and submit.

    Handles two patterns:
    1. Single input field — type the full OTP
    2. Multiple single-digit boxes — distribute one digit per box

    Returns True if the OTP was filled and submitted successfully.
    """
    otp_cfg = config.otp_handling
    custom_input = otp_cfg.otp_input_selector if otp_cfg else None
    custom_submit = otp_cfg.otp_submit_selector if otp_cfg else None

    filled = False

    # Try custom selector first
    if custom_input:
        try:
            el = page.locator(custom_input).first
            if await el.is_visible(timeout=2000):
                await el.fill(otp_value)
                filled = True
        except Exception as e:
            logger.warning(f"Custom OTP selector '{custom_input}' failed: {e}")

    # Try single input field selectors
    if not filled:
        for selector in _OTP_INPUT_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.fill(otp_value)
                    filled = True
                    logger.info(f"OTP filled via selector: {selector}")
                    break
            except Exception:
                continue

    # Try multiple digit boxes
    if not filled:
        try:
            digit_boxes = page.locator(_OTP_DIGIT_BOX_SELECTOR)
            count = await digit_boxes.count()
            visible_boxes = []
            for i in range(min(count, 10)):
                try:
                    box = digit_boxes.nth(i)
                    if await box.is_visible(timeout=500):
                        visible_boxes.append(box)
                except Exception:
                    continue

            if len(visible_boxes) >= len(otp_value):
                for i, digit in enumerate(otp_value):
                    await visible_boxes[i].fill(digit)
                filled = True
                logger.info(f"OTP filled across {len(otp_value)} digit boxes")
        except Exception as e:
            logger.warning(f"Digit box OTP fill failed: {e}")

    if not filled:
        logger.error("Could not find any OTP input field to fill")
        return False

    # Submit the OTP
    submit_el = await _find_element(page, custom_submit, _OTP_SUBMIT_SELECTORS)

    if submit_el:
        try:
            async with page.expect_navigation(
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            ):
                await submit_el.click()
        except Exception:
            # Some pages auto-submit or use AJAX
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=settings.STROLL_PAGE_TIMEOUT_MS
                )
            except Exception as e:
                logger.warning(f"Post-OTP submit wait timed out: {e}")
    else:
        # No submit button found — some OTP forms auto-submit on last digit
        logger.info("No OTP submit button found — waiting for auto-submit")
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=settings.STROLL_PAGE_TIMEOUT_MS
            )
        except Exception as e:
            logger.warning(f"Post-OTP auto-submit wait timed out: {e}")

    logger.info("OTP submitted successfully")
    return True


async def run_stroll(company_id: str, config: StrollConfig) -> StrollVersion:
    """
    Execute a full BFS crawl of the customer's dashboard.

    1. Health-check the dashboard URL
    2. Authenticate if credentials provided
    3. BFS over navigation elements
    4. Screenshot + Claude vision analysis per page
    5. Build NavGraph with enriched labels and pre-computed instructions
    6. Upload screenshots to Cloudinary
    """
    global _browser
    if not _browser:
        await init_browser()

    context = await _browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )

    graph = NavGraph()
    screenshot_urls: dict[str, str] = {}

    try:
        page = await context.new_page()

        # — health check —
        try:
            response = await page.goto(
                config.dashboard_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
            if not response or response.status >= 400:
                logger.error(f"Dashboard unhealthy: HTTP {response.status if response else 'no response'}")
                return StrollVersion(
                    id=f"stroll_{str(uuid4())[:8]}",
                    company_id=company_id,
                    timestamp=datetime.now(tz=timezone.utc),
                    graph=NavGraph(),
                    status="failed",
                )
        except Exception as e:
            logger.error(f"Dashboard health check failed: {e}")
            return StrollVersion(
                id=f"stroll_{str(uuid4())[:8]}",
                company_id=company_id,
                timestamp=datetime.now(tz=timezone.utc),
                graph=NavGraph(),
                status="failed",
            )

        # — authenticate if credentials provided —
        auth_ok = await _authenticate(page, config, company_id)
        if not auth_ok:
            logger.error(f"Authentication failed for company {company_id}")
            return StrollVersion(
                id=f"stroll_{str(uuid4())[:8]}",
                company_id=company_id,
                timestamp=datetime.now(tz=timezone.utc),
                graph=NavGraph(),
                status="failed",
            )

        # — BFS crawl —
        crawl_queue: deque[str] = deque([config.dashboard_url])
        visited: set[str] = set()
        max_pages = min(config.max_pages, settings.STROLL_MAX_PAGES)

        while crawl_queue and len(visited) < max_pages:
            current_url = crawl_queue.popleft()
            if current_url in visited:
                continue
            visited.add(current_url)

            try:
                await page.goto(
                    current_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception as e:
                logger.warning(f"Failed to navigate to {current_url}: {e}")
                continue

            # generate stable page id from URL
            page_id = hashlib.sha256(current_url.encode()).hexdigest()[:12]

            # capture screenshot
            screenshot_bytes = await _capture_screenshot(page)

            # detect interactive elements from DOM
            raw_elements = await page.evaluate(DETECT_ELEMENTS_JS)

            # compute DOM hash for change detection
            dom_content = await page.content()
            dom_hash = _compute_dom_hash(dom_content)

            page_title = await page.title() or current_url

            # Claude vision analysis for rich understanding
            vision_result = await _analyze_page_with_vision(
                screenshot_bytes, raw_elements, page_title, current_url
            )

            # build enriched elements
            vision_elements_map = {
                e.get("selector", ""): e.get("human_description", "")
                for e in vision_result.get("elements", [])
            }

            elements: list[InteractiveElement] = []
            for raw in raw_elements:
                elements.append(InteractiveElement(
                    selector=raw["selector"],
                    label=raw.get("label", ""),
                    human_description=vision_elements_map.get(raw["selector"], ""),
                    type=raw.get("type", "nav"),
                    bbox=BoundingBox(**raw["bbox"]) if raw.get("bbox") else None,
                ))

            # create page node
            node = PageNode(
                id=page_id,
                url=current_url,
                title=page_title,
                page_summary=vision_result.get("page_summary", page_title),
                elements=elements,
                dom_hash=dom_hash,
            )
            graph.nodes[page_id] = node

            # upload screenshot
            screenshot_url = await _upload_screenshot(screenshot_bytes, company_id, page_id)
            screenshot_urls[page_id] = screenshot_url

            # discover navigation targets and build edges
            for elem in elements:
                if elem.type != "nav":
                    continue

                # find the raw element's href
                raw_match = next(
                    (r for r in raw_elements if r["selector"] == elem.selector),
                    None,
                )
                if not raw_match:
                    continue

                href = raw_match.get("href", "")
                if not href:
                    continue

                dest_url = _normalize_url(href, config.dashboard_url)
                if not dest_url or dest_url in visited:
                    # still add edge if we've already visited
                    dest_id = hashlib.sha256(dest_url.encode()).hexdigest()[:12] if dest_url else None
                    if dest_id and dest_id in graph.nodes:
                        edge = Edge(
                            from_page=page_id,
                            to_page=dest_id,
                            via=elem,
                            instruction=f"Click '{elem.label or elem.human_description}' on the {page_title} page",
                        )
                        graph.edges.append(edge)
                    continue

                dest_id = hashlib.sha256(dest_url.encode()).hexdigest()[:12]

                # add edge
                edge = Edge(
                    from_page=page_id,
                    to_page=dest_id,
                    via=elem,
                    instruction=f"Click '{elem.label or elem.human_description}' on the {page_title} page",
                )
                graph.edges.append(edge)

                # enqueue for crawling
                crawl_queue.append(dest_url)

    finally:
        try:
            await context.close()
        except Exception as e:
            logger.warning(f"Error closing browser context: {e}")

    return StrollVersion(
        id=f"stroll_{str(uuid4())[:8]}",
        company_id=company_id,
        timestamp=datetime.now(tz=timezone.utc),
        graph=graph,
        screenshot_urls=screenshot_urls,
        status="success",
    )

async def process_widget_stroll(company_id: str, report: WidgetStrollReport):
    """
    Process a stroll report from the widget, build the graph, and update the index.
    This is the backend part of the "DOM Intercept" stroll method.
    """
    graph = NavGraph()
    screenshot_urls: dict[str, str] = {}
    
    try:
        # 1. Process all nodes, run vision analysis, and upload screenshots
        for widget_node in report.nodes:
            page_id = hashlib.sha256(widget_node.url.encode()).hexdigest()[:12]
            
            try:
                # The widget sends a data URI (e.g. data:image/png;base64,iVBORw0KG...)
                # We need the raw bytes for the hash and fallback tasks, but we can pass the data URI directly to Cloudinary
                b64_str = widget_node.screenshot_base64
                if not b64_str or b64_str.strip() == "data:,":
                    logger.warning(f"Empty screenshot base64 for {widget_node.url}")
                    continue
                
                pure_b64 = b64_str.split(",", 1)[1] if "," in b64_str else b64_str
                screenshot_bytes = base64.b64decode(pure_b64)
                
                # Format a proper data URI for Cloudinary if it's missing the prefix
                cloudinary_data = b64_str if "," in b64_str else f"data:image/png;base64,{b64_str}"
            except Exception:
                logger.warning(f"Failed to decode base64 screenshot for {widget_node.url}")
                continue

            # Upload screenshot directly using the Data URI
            screenshot_url = await _upload_screenshot(cloudinary_data, company_id, page_id)
            screenshot_urls[page_id] = screenshot_url

            # Prepare elements for vision analysis
            raw_elements = [
                {
                    "selector": el.selector,
                    "label": el.label,
                    "bbox": el.bbox.model_dump()
                }
                for el in widget_node.elements
            ]

            # Claude vision analysis
            vision_result = await _analyze_page_with_vision(
                screenshot_bytes, raw_elements, widget_node.title, widget_node.url
            )
            vision_elements_map = {
                e.get("selector", ""): e.get("human_description", "")
                for e in vision_result.get("elements", [])
            }

            # Build enriched InteractiveElement list
            elements: list[InteractiveElement] = []
            for el in widget_node.elements:
                elements.append(InteractiveElement(
                    selector=el.selector,
                    label=el.label,
                    human_description=vision_elements_map.get(el.selector, ""),
                    type=el.type,
                    bbox=el.bbox,
                ))
            
            # Create PageNode
            node = PageNode(
                id=page_id,
                url=widget_node.url,
                title=widget_node.title,
                page_summary=vision_result.get("page_summary", widget_node.title),
                elements=elements,
                dom_hash=hashlib.sha256(str(raw_elements).encode()).hexdigest()[:16],
            )
            graph.nodes[page_id] = node

        # 2. Reconstruct edges
        url_to_id_map = {node.url: node.id for node in graph.nodes.values()}
        for widget_node in report.nodes:
            from_id = url_to_id_map.get(widget_node.url)
            if not from_id:
                continue
            
            from_page_title = graph.nodes[from_id].title
            graph_elements_map = {elem.selector: elem for elem in graph.nodes[from_id].elements}

            for el in widget_node.elements:
                if el.type != "nav" or not el.href:
                    continue
                
                dest_url = _normalize_url(el.href, report.dashboard_url)
                to_id = url_to_id_map.get(dest_url)

                if to_id:
                    via_element = graph_elements_map.get(el.selector)
                    if via_element:
                        edge = Edge(
                            from_page=from_id,
                            to_page=to_id,
                            via=via_element,
                            instruction=f"Click '{via_element.label or via_element.human_description}' on the {from_page_title} page",
                        )
                        graph.edges.append(edge)

        # 3. Create a StrollVersion and process it
        version = StrollVersion(
            id=f"stroll_{str(uuid4())[:8]}",
            company_id=company_id,
            timestamp=datetime.now(tz=timezone.utc),
            graph=graph,
            screenshot_urls=screenshot_urls,
            status="success",
        )

        prev = await get_latest_version(company_id)
        diff = diff_stroll(version.graph, prev)

        committed = await commit_stroll(company_id, version, diff)
        if committed:
            logger.info(f"Widget stroll completed for company {company_id}: {committed.id}")
        else:
            logger.info(f"Widget stroll found no changes for company {company_id}")

    except Exception as e:
        logger.exception(f"Background widget stroll processing failed for company {company_id}: {e}")
        version = StrollVersion(
            id=f"stroll_{str(uuid4())[:8]}",
            company_id=company_id,
            timestamp=datetime.now(tz=timezone.utc),
            graph=NavGraph(),
            status="failed",
        )
        await db.stroll_versions.insert_one(version.model_dump())

def diff_stroll(new_graph: NavGraph, prev_version: Optional[StrollVersion]) -> DiffLog:
    """Compare new graph against previous version. Returns a DiffLog."""
    if prev_version is None:
        return DiffLog(
            added=list(new_graph.nodes.values()),
            removed=[],
            moved=[],
            unchanged_count=0,
        )

    prev_graph = prev_version.graph
    diff = DiffLog()

    # find added or changed pages
    for node_id, node in new_graph.nodes.items():
        if node_id not in prev_graph.nodes:
            diff.added.append(node)
        elif node.dom_hash != prev_graph.nodes[node_id].dom_hash:
            diff.added.append(node)  # treat changed page as updated
        else:
            diff.unchanged_count += 1

    # find removed pages
    for node_id in prev_graph.nodes:
        if node_id not in new_graph.nodes:
            diff.removed.append(node_id)

    # detect moved features (same label, different path from root)
    new_root = new_graph.get_root_id()
    prev_root = prev_graph.get_root_id()

    if new_root and prev_root:
        # collect all unique labels in both graphs
        new_labels: dict[str, str] = {}  # label → page_id
        for node in new_graph.nodes.values():
            for elem in node.elements:
                if elem.label:
                    new_labels[elem.label] = node.id

        prev_labels: dict[str, str] = {}
        for node in prev_graph.nodes.values():
            for elem in node.elements:
                if elem.label:
                    prev_labels[elem.label] = node.id

        for label in set(new_labels.keys()) & set(prev_labels.keys()):
            new_path = new_graph.shortest_path(new_root, new_labels[label])
            prev_path = prev_graph.shortest_path(prev_root, prev_labels[label])
            if new_path and prev_path and new_path != prev_path:
                diff.moved.append(MovedFeature(
                    label=label,
                    old_path=prev_path,
                    new_path=new_path,
                ))

    return diff

async def commit_stroll(
    company_id: str,
    version: StrollVersion,
    diff: DiffLog,
) -> Optional[StrollVersion]:
    """
    Persist a stroll version to MongoDB.
    Skips write if no changes detected.
    """
    if not diff.added and not diff.removed and not diff.moved:
        logger.info(f"No changes for company {company_id} — skipping write")
        return None

    version.diff = diff

    doc = version.model_dump()
    doc["_company_id"] = company_id  # for indexing

    await db.stroll_versions.insert_one(doc)
    logger.info(f"Committed stroll {version.id} for company {company_id}")

    return version

async def get_latest_version(company_id: str) -> Optional[StrollVersion]:
    """Get the most recent successful stroll version for a company."""
    doc = await db.stroll_versions.find_one(
        {"company_id": company_id, "status": "success"},
        sort=[("timestamp", -1)],
    )
    if doc:
        doc.pop("_id", None)
        return StrollVersion(**doc)
    return None


async def list_versions(company_id: str, limit: int = 10) -> list[dict]:
    """List stroll versions for a company (summary only)."""
    cursor = db.stroll_versions.find(
        {"company_id": company_id},
        {"graph": 0, "screenshot_urls": 0},  # exclude large fields
    ).sort("timestamp", -1).limit(limit)

    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results

async def get_stroll_config(company_id: str) -> Optional[StrollConfig]:
    """Get the stroll config for a company."""
    doc = await db.stroll_configs.find_one({"company_id": company_id})
    if doc:
        doc.pop("_id", None)
        return StrollConfig(**doc)
    return None


async def save_stroll_config(
    company_id: str, data: StrollConfigCreate
) -> StrollConfig:
    """Create or update stroll configuration for a company."""
    now = datetime.now(tz=timezone.utc)

    config_doc = {
        "company_id": company_id,
        **data.model_dump(),
        "updated_at": now,
    }

    await db.stroll_configs.update_one(
        {"company_id": company_id},
        {
            "$set": config_doc,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    return StrollConfig(company_id=company_id, **data.model_dump(), updated_at=now)
