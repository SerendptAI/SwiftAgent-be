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
import random
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import anthropic
import openai
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types
from playwright.async_api import async_playwright, Browser, Page
from playwright_stealth import Stealth

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
    TokenUsage,
)
from app.services.cloudinary_service import upload_document
from app.services import otp_challenge_service
from app.core.langfuse import observe

logger = logging.getLogger(__name__)

# shared Playwright browser instance (initialized in lifespan)
_browser: Optional[Browser] = None


async def init_browser():
    """Initialize Playwright browser. Call once at app startup."""
    global _browser
    pw = await async_playwright().start()
    if settings.PLAYWRIGHT_WS_ENDPOINT:
        try:
            _browser = await pw.chromium.connect_over_cdp(settings.PLAYWRIGHT_WS_ENDPOINT)
        except Exception as e:
            logger.warning(f"Failed to connect to WS endpoint {settings.PLAYWRIGHT_WS_ENDPOINT}: {e}. Falling back to local Chromium.")
            _browser = await pw.chromium.launch(headless=True)
    else:
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


async def _call_vision_with_fallback(prompt_text: str, screenshot_bytes: bytes, token_tracker: dict = None) -> str:
    """Three-tier agent fallback specifically for vision analysis."""
    # 1. Try Anthropic
    try:
        client = _get_anthropic_client()
        
        # Translate Cencori aliases to official Anthropic model names for the direct SDK
        actual_model = settings.ANTHROPIC_MODEL
        if actual_model == "claude-haiku-4.5":
            actual_model = "claude-3-5-haiku-20241022"
        elif actual_model == "claude-sonnet-4-6":
            actual_model = "claude-3-5-sonnet-20241022"
        elif "haiku" in actual_model.lower():
            actual_model = "claude-3-5-haiku-20241022"
        elif "sonnet" in actual_model.lower():
            actual_model = "claude-3-5-sonnet-20241022"
        elif "opus" in actual_model.lower():
            actual_model = "claude-3-opus-20240229"
            
        response = await client.messages.create(
            model=actual_model,
            max_tokens=4096,
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
                        "text": prompt_text,
                    },
                ],
            }],
        )
        if token_tracker is not None and hasattr(response, 'usage'):
            token_tracker['in'] += getattr(response.usage, 'input_tokens', 0)
            token_tracker['out'] += getattr(response.usage, 'output_tokens', 0)
        return response.content[0].text
    except Exception as e:
        logger.warning(f"Anthropic vision failed: {e}. Falling back to Gemini...")

    # 2. Try Gemini
    try:
        gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = await gemini_client.aio.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
                types.Part.from_text(text=prompt_text)
            ],
            config=types.GenerateContentConfig(
                temperature=0.3,
            )
        )
        reply = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    reply += part.text
                    
        if token_tracker is not None and hasattr(response, 'usage_metadata') and response.usage_metadata:
            token_tracker['in'] += getattr(response.usage_metadata, 'prompt_token_count', 0)
            token_tracker['out'] += getattr(response.usage_metadata, 'candidates_token_count', 0)
            
        if not reply:
            raise Exception("Empty response from Gemini")
        return reply
    except Exception as e:
        logger.warning(f"Gemini vision failed: {e}. Falling back to OpenRouter...")

    # 3. Try OpenRouter
    try:
        or_client = openai.AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.OPENROUTER_API_KEY,
        )
        b64_image = base64.b64encode(screenshot_bytes).decode()
        response = await or_client.chat.completions.create(
            model=settings.OPENROUTER_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt_text
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64_image}"
                        }
                    }
                ]
            }],
            max_tokens=4096,
            temperature=0.3,
        )
        if token_tracker is not None and hasattr(response, 'usage') and response.usage:
            token_tracker['in'] += getattr(response.usage, 'prompt_tokens', 0)
            token_tracker['out'] += getattr(response.usage, 'completion_tokens', 0)
            
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"OpenRouter vision failed: {e}. All vision providers exhausted.")
        raise Exception("All vision providers exhausted") from e


def _draw_som_boxes(screenshot_bytes: bytes, elements: list[dict]) -> bytes:
    """Draw numbered Set-of-Mark bounding boxes on the screenshot."""
    try:
        image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            font = ImageFont.load_default()
            
        for idx, el in enumerate(elements):
            bbox = el.get("bbox")
            if not bbox:
                continue
            x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
            draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0, 200), width=2)
            
            label = str(idx)
            text_bbox = draw.textbbox((0, 0), label, font=font)
            tw = text_bbox[2] - text_bbox[0]
            th = text_bbox[3] - text_bbox[1]
            draw.rectangle([x, y, x + tw + 4, y + th + 4], fill=(255, 0, 0, 255))
            draw.text((x + 2, y + 2), label, fill="white", font=font)
            
            el["som_id"] = idx
            
        out = BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.warning(f"Failed to draw SoM boxes: {e}")
        return screenshot_bytes

async def _analyze_page_with_vision(
    screenshot_bytes: bytes,
    raw_elements: list[dict],
    page_title: str,
    page_url: str,
    token_tracker: dict = None
) -> dict:
    """
    Use Claude vision to semantically understand a page screenshot.
    Returns enriched element descriptions and a page summary.
    """
    annotated_bytes = _draw_som_boxes(screenshot_bytes, raw_elements)
    
    pruned_elements = []
    for el in raw_elements[:40]:
        if "som_id" in el:
            pruned_elements.append({
                "som_id": el["som_id"],
                "label": el.get("label", ""),
                "selector": el.get("selector", "")
            })

    elements_text = json.dumps(pruned_elements, indent=2)

    prompt_text = (
        f"This is a screenshot of the page '{page_title}' at {page_url}. "
        "It has been annotated with numbered red bounding boxes (Set-of-Mark).\n\n"
        f"Detected interactive elements and their corresponding box IDs:\n{elements_text}\n\n"
        "Please provide:\n"
        "1. A 'scratchpad' string reasoning about the layout. Plan out which elements to click next to uncover core features.\n"
        "2. A one-sentence summary of what this page is for.\n"
        "3. For each interactive element above, a human-readable description of what it does.\n"
        "4. A list of CSS selectors (chosen strictly from the provided list) that the crawler should click next. PRIORITIZE navigation to Settings, Uploads, and unmapped features. Exclude truly destructive actions.\n"
        "5. An 'is_exploration_complete' boolean. Set this to true ONLY if you are absolutely confident there are no more meaningful sub-pages to explore.\n\n"
        "Respond in JSON format:\n"
        '{"scratchpad": "...", "page_summary": "...", "elements": [{"selector": "...", '
        '"human_description": "..."}], "navigation_selectors_to_explore": ["selector1", "selector_2"], "is_exploration_complete": false}'
    )

    try:
        text = await _call_vision_with_fallback(prompt_text, annotated_bytes, token_tracker=token_tracker)
        
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
    let idCounter = 0;

    function isInteractive(el) {
        const tag = el.tagName.toLowerCase();
        if (['a', 'button', 'input', 'select', 'textarea', 'details'].includes(tag)) return true;
        if (el.hasAttribute('role')) {
            const role = el.getAttribute('role');
            if (['button', 'link', 'menuitem', 'tab', 'checkbox', 'radio', 'switch', 'treeitem'].includes(role)) return true;
        }
        if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') return true;
        try {
            const style = window.getComputedStyle(el);
            if (style.cursor === 'pointer' && !['body', 'html'].includes(tag)) return true;
        } catch(e) {}
        return false;
    }

    function isVisible(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        try {
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
        } catch(e) {}
        let current = el;
        while (current) {
            if (current.getAttribute && current.getAttribute('aria-hidden') === 'true') return false;
            current = current.parentElement;
        }
        return true;
    }

    function walk(node) {
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        
        if (isVisible(node) && isInteractive(node)) {
            const rect = node.getBoundingClientRect();
            const label = (node.getAttribute('aria-label') || node.getAttribute('title') || node.getAttribute('placeholder') || node.textContent || node.value || '').replace(/\\s+/g, ' ').trim().slice(0, 100);
            const hasPopup = node.getAttribute('aria-haspopup') === 'true' || node.getAttribute('aria-expanded') !== null;
            const href = node.getAttribute('href') || '';
            const role = node.getAttribute('role') || node.tagName.toLowerCase();
            
            let selector = '';
            if (node.id) {
                selector = '#' + CSS.escape(node.id);
            } else {
                const tag = node.tagName.toLowerCase();
                const classes = Array.from(node.classList).slice(0, 3).map(c => CSS.escape(c)).join('.');
                selector = classes ? `${tag}.${classes}` : tag;
            }
            
            const strollId = `stroll-${idCounter++}`;
            node.setAttribute('data-stroll-id', strollId);
            selector = `${selector}[data-stroll-id="${strollId}"]`;

            elements.push({
                selector: selector, label: label,
                type: hasPopup ? 'menu' : (href ? 'nav' : 'action'),
                href: href, has_popup: hasPopup, role: role,
                bbox: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }
            });
            if (['button', 'a'].includes(node.tagName.toLowerCase())) return;
        }
        for (const child of node.children) walk(child);
        if (node.shadowRoot) for (const child of node.shadowRoot.children) walk(child);
    }

    walk(document.body);
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
    "6-digit code",
    "sent to your email",
    "check your email",
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


DETECT_LOGIN_FORM_JS = """
() => {
    const elements = [];
    const nodes = document.querySelectorAll(
        'input, button, [type="submit"], [role="button"]'
    );

    nodes.forEach((el, i) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;

        const tag = el.tagName.toLowerCase();
        let selector = '';
        if (el.id) {
            selector = '#' + el.id;
        } else if (el.getAttribute('name')) {
            selector = tag + '[name="' + el.getAttribute('name') + '"]';
        } else {
            const classes = Array.from(el.classList).slice(0, 3).join('.');
            selector = classes ? tag + '.' + classes : tag + ':nth-of-type(' + (i + 1) + ')';
        }

        elements.push({
            tag: tag,
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            id: el.id || '',
            placeholder: el.getAttribute('placeholder') || '',
            aria_label: el.getAttribute('aria-label') || '',
            autocomplete: el.getAttribute('autocomplete') || '',
            text: (el.textContent || '').trim().slice(0, 80),
            selector: selector
        });
    });

    return elements;
}
"""


async def _read_login_dom_with_vision(
    page: Page, screenshot_bytes: bytes, page_url: str, token_tracker: dict = None
) -> dict:
    """
    Use Claude vision to intelligently identify login form fields.

    Sends the page screenshot + extracted DOM elements to Claude,
    which returns the exact selectors for username, password, and submit.

    Returns dict with keys: username_selector, password_selector, submit_selector
    """
    try:
        form_elements = await page.evaluate(DETECT_LOGIN_FORM_JS)
    except Exception as e:
        logger.warning(f"Failed to extract login form DOM: {e}")
        form_elements = []

    elements_text = json.dumps(form_elements[:40], indent=2)

    prompt_text = (
        f"This is a screenshot of a login page at {page_url}.\n\n"
        f"DOM form elements detected:\n{elements_text}\n\n"
        "Identify the login form fields. Keep in mind this might be a passwordless (OTP/magic link) login or a traditional one.\n"
        "Return JSON:\n"
        '{"username_selector": "CSS selector for email/username or null",\n'
        ' "password_selector": "CSS selector for password or null",\n'
        ' "submit_selector": "CSS selector for the login/continue/submit button or null"}\n\n'
        "Use selectors from the DOM list. Prefer #id, then [name=...], then class-based."
    )

    try:
        text = await _call_vision_with_fallback(prompt_text, screenshot_bytes, token_tracker=token_tracker)
        
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        result = json.loads(text.strip())
        logger.info(f"Vision identified login selectors: {result}")
        return result

    except Exception as e:
        logger.warning(f"Vision login DOM analysis failed for {page_url}: {e}")
        return {"username_selector": None, "password_selector": None, "submit_selector": None}


def _select_auth_strategy(config: StrollConfig) -> str:
    """
    Evaluate available credentials and select the easiest auth strategy.

    Priority (easiest first):
      1. pre_auth_url — Just navigate, zero DOM interaction needed.
      2. form_login   — Username+password, requires DOM reading + form fill.
      3. none         — No credentials, proceed unauthenticated.
    """
    creds = config.credentials
    if not creds:
        return "none"

    if creds.pre_auth_url:
        return "pre_auth"

    if creds.username:
        return "form_login"

    return "none"


async def _authenticate(page: Page, config: StrollConfig, company_id: str, token_tracker: dict = None) -> tuple[bool, dict[str, str]]:
    """
    Handle dashboard authentication with smart strategy selection.

    Evaluates available credentials and picks the easiest path:
      1. pre_auth_url — Navigate directly (screenshot login page for reference first)
      2. form_login   — Read DOM with vision → fill → submit → verify
      3. none         — No auth needed

    Returns (success: bool, extra_screenshots: dict) where extra_screenshots
    maps special keys like "__login_page__" and "__otp_page__" to Cloudinary URLs.
    """
    extra_screenshots: dict[str, str] = {}
    creds = config.credentials
    if not creds:
        return True, extra_screenshots

    strategy = _select_auth_strategy(config)
    logger.info(f"Auth strategy selected for company {company_id}: {strategy}")
    
    # Decrypt password for usage
    if creds.password:
        from app.core.encryption import decrypt
        try:
            creds.password = decrypt(creds.password)
        except ValueError:
            # Leave as is if it's legacy unencrypted
            pass

    # ── Strategy 1: Pre-authenticated URL (easiest) ──────────────────────
    if strategy == "pre_auth":
        # Screenshot the login page for reference (if a login_url is configured)
        login_url = creds.login_url or config.dashboard_url
        try:
            await page.goto(
                login_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
            login_screenshot = await _capture_screenshot(page)
            login_ss_url = await _upload_screenshot(login_screenshot, company_id, "__login_page__")
            extra_screenshots["__login_page__"] = login_ss_url
            logger.info(f"Login page screenshotted for reference: {login_url}")
        except Exception as e:
            logger.warning(f"Failed to screenshot login page for reference: {e}")

        # Navigate to the pre-auth URL
        logger.info(f"Using pre-auth URL for company {company_id}")
        try:
            await page.goto(
                creds.pre_auth_url,
                timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                wait_until="networkidle",
            )
            return True, extra_screenshots
        except Exception as e:
            logger.error(f"Pre-auth URL navigation failed for company {company_id}: {e}")
            return False, extra_screenshots

    # ── Strategy 2: Form-based login with DOM reading ────────────────────
    if strategy == "form_login":
        login_url = creds.login_url or config.dashboard_url
        logger.info(f"Attempting form-based login for company {company_id} at {login_url}")

        try:
            try:
                await page.goto(
                    login_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception:
                await page.goto(
                    login_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="load",
                )
        except Exception as e:
            logger.error(f"Failed to navigate to login page {login_url}: {e}")
            return False, extra_screenshots

        # Screenshot the login page
        login_screenshot = await _capture_screenshot(page)
        try:
            login_ss_url = await _upload_screenshot(login_screenshot, company_id, "__login_page__")
            extra_screenshots["__login_page__"] = login_ss_url
            logger.info(f"Login page screenshotted: {login_url}")
        except Exception as e:
            logger.warning(f"Failed to upload login page screenshot: {e}")

        # Read the DOM with Claude vision to identify form fields
        vision_selectors = await _read_login_dom_with_vision(page, login_screenshot, login_url, token_tracker=token_tracker)

        # Find and fill username — prefer config override, then vision, then heuristics
        username_el = await _find_element(
            page,
            creds.username_selector or vision_selectors.get("username_selector"),
            _USERNAME_SELECTORS,
        )
        if not username_el:
            logger.error(f"Could not find username field on {login_url} for company {company_id}")
            return False, extra_screenshots

        await username_el.fill("")
        await username_el.press_sequentially(creds.username, delay=random.randint(30, 80))

        # Find and fill password — prefer config override, then vision, then heuristics
        password_el = await _find_element(
            page,
            creds.password_selector or vision_selectors.get("password_selector"),
            _PASSWORD_SELECTORS,
        )
        if password_el:
            await password_el.fill("")
            await password_el.press_sequentially(creds.password, delay=random.randint(30, 80))
        else:
            logger.info(f"No password field found on {login_url} — assuming passwordless/OTP login flow")

        # Find and click submit — prefer config override, then vision, then heuristics
        submit_el = await _find_element(
            page,
            creds.submit_selector or vision_selectors.get("submit_selector"),
            _SUBMIT_SELECTORS,
        )
        if submit_el:
            # Click submit and wait for navigation
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
        else:
            logger.info(f"No submit button found on {login_url} — pressing Enter instead")
            try:
                async with page.expect_navigation(
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                ):
                    await username_el.press("Enter")
            except Exception:
                try:
                    await page.wait_for_load_state("networkidle", timeout=settings.STROLL_PAGE_TIMEOUT_MS)
                except Exception as e:
                    logger.warning(f"Post-login wait timed out for company {company_id}: {e}")

        # --- verify login succeeded ---
        try:
            pw_still_visible = await page.locator('input[type="password"]').first.is_visible(timeout=2000)
        except Exception:
            pw_still_visible = False

        if pw_still_visible:
            logger.error(
                f"Login appears to have failed for company {company_id} — "
                f"password field still visible after submit"
            )
            return False, extra_screenshots

        # --- detect OTP / 2FA challenge page with retry loop ---
        otp_attempts = 0
        while otp_attempts < 3:
            otp_detected = await _detect_otp_page(page)
            if not otp_detected:
                break  # Not on an OTP page anymore
            
            otp_attempts += 1
            # Screenshot the OTP page
            try:
                otp_screenshot = await _capture_screenshot(page)
                otp_ss_url = await _upload_screenshot(otp_screenshot, company_id, f"__otp_page_{otp_attempts}__")
                extra_screenshots[f"__otp_page_{otp_attempts}__"] = otp_ss_url
                logger.info(f"OTP page screenshotted for company {company_id} (Attempt {otp_attempts})")
            except Exception as e:
                logger.warning(f"Failed to upload OTP page screenshot: {e}")

            otp_cfg = config.otp_handling
            if otp_cfg and otp_cfg.enabled:
                logger.info(f"OTP page detected for company {company_id}, initiating challenge relay (Attempt {otp_attempts}/3)")
                otp_value = await _handle_otp_challenge(page, config, company_id)
                if otp_value:
                    filled = await _fill_and_submit_otp(page, otp_value, config)
                    if not filled:
                        logger.error(f"Failed to fill OTP for company {company_id}")
                        return False, extra_screenshots
                    
                    # Give the SPA time to trigger the background API call and transition the DOM
                    # We use a hard wait because networkidle might return instantly before the fetch even begins
                    await page.wait_for_timeout(4000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    
                    # Check if we are STILL on the OTP page (incorrect OTP entered)
                    if await _detect_otp_page(page):
                        logger.warning(f"OTP submission {otp_attempts} failed (incorrect OTP). Prompting again.")
                        if otp_attempts >= 3:
                            logger.error("Max OTP attempts reached.")
                            return False, extra_screenshots
                        continue
                    else:
                        break  # Successfully bypassed OTP!
                else:
                    logger.error(f"OTP challenge timed out or failed for company {company_id}")
                    return False, extra_screenshots
            else:
                logger.warning(
                    f"OTP page detected for company {company_id} but OTP handling is "
                    f"not enabled in config — login will likely fail"
                )
                return False, extra_screenshots

        logger.info(f"Login succeeded for company {company_id}, now at {page.url}")

        # navigate to dashboard URL if we're not already there,
        # but avoid backwards navigation if dashboard_url was set to the login page
        current = page.url.split("?")[0].rstrip("/")
        target = config.dashboard_url.split("?")[0].rstrip("/")
        login_target = login_url.split("?")[0].rstrip("/")
        
        if current != target and target != login_target:
            try:
                await page.goto(
                    config.dashboard_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception as e:
                logger.error(f"Failed to navigate to dashboard after login: {e}")
                return False, extra_screenshots

        return True, extra_screenshots

    # ── Strategy 3: No usable credentials ────────────────────────────────
    logger.warning(f"Credentials provided but insufficient for company {company_id}")
    return True, extra_screenshots  # proceed unauthenticated


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
    otp_value = await otp_challenge_service.wait_for_challenge_response(
        challenge.id, timeout_seconds=300
    )
    if otp_value:
        logger.info(f"otp from push received: {otp_value}")
    else:
        from app.services.push_notification_service import send_generic_push
        await send_generic_push(
            user_id=notify_user_id,
            title="⏳ OTP Timeout",
            body="You missed the agent's OTP request. The crawl was aborted.",
            notification_type="otp_timeout",
            data={"challenge_id": challenge.id}
        )
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
                    await visible_boxes[i].fill("")
                    await visible_boxes[i].press_sequentially(digit, delay=random.randint(30, 80))
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
    logger.info("otp passed")
    return True


@observe(name="stroll.run_stroll")
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
    if not _browser or not _browser.is_connected():
        await init_browser()

    context = await _browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )

    graph = NavGraph()
    screenshot_urls: dict[str, str] = {}
    token_tracker = {"in": 0, "out": 0}
    previous_version = await get_latest_version(company_id)

    try:
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        # — health check —
        try:
            try:
                response = await page.goto(
                    config.dashboard_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="networkidle",
                )
            except Exception:
                response = await page.goto(
                    config.dashboard_url,
                    timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                    wait_until="load",
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
        auth_ok, extra_screenshots = await _authenticate(page, config, company_id, token_tracker=token_tracker)
        if extra_screenshots:
            screenshot_urls.update(extra_screenshots)

        if not auth_ok:
            logger.error(f"Authentication failed for company {company_id}")
            return StrollVersion(
                id=f"stroll_{str(uuid4())[:8]}",
                company_id=company_id,
                timestamp=datetime.now(tz=timezone.utc),
                graph=NavGraph(),
                status="failed",
            )

        # Screenshot dashboard home page immediately after auth
        try:
            dashboard_ss_bytes = await _capture_screenshot(page)
            dashboard_ss_url = await _upload_screenshot(dashboard_ss_bytes, company_id, "__dashboard_home__")
            screenshot_urls["__dashboard_home__"] = dashboard_ss_url
            logger.info(f"Dashboard home screenshotted for company {company_id}")
        except Exception as e:
            logger.warning(f"Failed to screenshot dashboard home: {e}")

        # — BFS crawl —
        crawl_queue: deque[str] = deque([page.url])
        visited: set[str] = set()
        max_pages = min(config.max_pages, settings.STROLL_MAX_PAGES)

        while crawl_queue and len(visited) < max_pages:
            current_cost = (token_tracker["in"] / 1_000_000 * 3.0) + (token_tracker["out"] / 1_000_000 * 15.0)
            if current_cost > 2.0:
                logger.warning(f"Aborting stroll for {company_id}: Cost limit exceeded (${current_cost:.2f})")
                break
                
            current_url = crawl_queue.popleft()
            if current_url in visited:
                continue
            visited.add(current_url)

            # Only hard-navigate if we are not already on the target URL
            if page.url.split("#")[0].rstrip("/") != current_url.split("#")[0].rstrip("/"):
                try:
                    await page.goto(
                        current_url,
                        timeout=settings.STROLL_PAGE_TIMEOUT_MS,
                        wait_until="load",
                    )
                except Exception as e:
                    logger.warning(f"Failed to navigate to {current_url}: {e}")
                    continue
                    
            # Always wait for animations/splash screens to settle before analyzing
            await page.wait_for_timeout(3000)
            
            # Update current_url to the actual resolved URL (handles redirects and history.replaceState)
            current_url = page.url

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

            logger.info(f"Reading DOM and capturing layout of {current_url} to plan next moves...")

            cached_node = None
            if previous_version:
                prev_node = previous_version.graph.nodes.get(page_id)
                if prev_node and prev_node.dom_hash == dom_hash:
                    cached_node = prev_node

            if cached_node:
                logger.info(f"DOM hash match for {current_url}. Reusing cached vision data.")
                vision_result = {
                    "elements": [
                        {"selector": e.selector, "human_description": e.human_description} 
                        for e in cached_node.elements
                    ],
                    "page_summary": cached_node.page_summary,
                    "navigation_selectors_to_explore": [
                        edge.via.selector for edge in previous_version.graph.edges 
                        if edge.from_page == page_id
                    ],
                    "is_exploration_complete": False
                }
            else:
                # Claude vision analysis for rich understanding
                vision_result = await _analyze_page_with_vision(
                    screenshot_bytes, raw_elements, page_title, current_url, token_tracker=token_tracker
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
            cached_screenshot_url = previous_version.screenshot_urls.get(page_id) if previous_version else None
            if cached_node and cached_screenshot_url:
                screenshot_url = cached_screenshot_url
                logger.info(f"Reusing cached screenshot for {page_id}")
            else:
                screenshot_url = await _upload_screenshot(screenshot_bytes, company_id, page_id)
            screenshot_urls[page_id] = screenshot_url

            # --- Navigation & Edge Building ---
            selectors_to_explore = set(vision_result.get("navigation_selectors_to_explore", []))
            
            if vision_result.get("is_exploration_complete"):
                logger.info("Agent determined exploration is complete for this branch. Skipping further clicks here.")
                selectors_to_explore = set()
                
            if "scratchpad" in vision_result:
                logger.info(f"Vision Scratchpad [{page_title}]: {vision_result['scratchpad']}")
            logger.info(f"Vision suggested nav elements: {list(selectors_to_explore)}")

            llm_candidates = [e for e in elements if e.selector in selectors_to_explore]
            heuristic_candidates = [e for e in elements if e.type in ["nav", "menu", "action"] and e.label]
            
            candidate_selectors = set(selectors_to_explore)
            nav_candidates = llm_candidates[:]
            
            if not vision_result.get("is_exploration_complete") or not selectors_to_explore:
                for hc in heuristic_candidates:
                    if hc.selector not in candidate_selectors:
                        lower_label = hc.label.lower()
                        if not any(bad in lower_label for bad in ["delete", "remove", "logout", "log out", "sign out"]):
                            nav_candidates.append(hc)
                            candidate_selectors.add(hc.selector)
            
            nav_candidates = nav_candidates[:20]
            click_nav_elements = []

            for elem in nav_candidates:
                # find the raw element's href
                raw_match = next(
                    (r for r in raw_elements if r["selector"] == elem.selector),
                    None,
                )
                if not raw_match:
                    continue

                href = raw_match.get("href", "")
                if not href:
                    # If it has no href, queue it for SPA click exploration
                    click_nav_elements.append(elem)
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

            # --- SPA Click Exploration for nav items without href ---
            # Track the element AND the node ID it originated from
            click_queue = deque([(elem, page_id) for elem in click_nav_elements])
            click_iterations = 0
            while click_queue:
                if click_iterations >= 20:
                    logger.warning("Max click iterations reached for this page, breaking to prevent loops.")
                    break
                click_iterations += 1
                elem, from_node_id = click_queue.popleft()
                try:
                    # If the URL changed, we navigated! Stop trying to click elements from the old DOM.
                    new_page_url = page.url.split("#")[0]
                    if new_page_url.rstrip("/") != current_url.split("#")[0].rstrip("/"):
                        logger.info(f"URL changed naturally (from {current_url} to {page.url}). Breaking loop.")
                        if new_page_url not in visited and new_page_url.startswith("http"):
                            crawl_queue.append(new_page_url)
                        break

                    click_el = page.locator(elem.selector).first
                    if await click_el.is_visible():
                        raw_match = next((r for r in raw_elements if r["selector"] == elem.selector), {})
                        is_menu = raw_match.get("has_popup", False)
                        if is_menu:
                            logger.info(f"Hover-exploring menu element: {elem.selector}")
                            await click_el.hover(timeout=3000)
                            await page.wait_for_timeout(1000)
                        else:
                            logger.info(f"Click-exploring SPA nav element (DOM selector): {elem.selector}")
                            await click_el.click(timeout=1000, force=True)
                    elif elem.bbox:
                        logger.info(f"Click-exploring SPA nav element (BBox fallback): {elem.selector}")
                        x = elem.bbox.x + (elem.bbox.w / 2)
                        y = elem.bbox.y + (elem.bbox.h / 2)
                        await page.mouse.click(x, y)
                    else:
                        logger.warning(f"Could not click element {elem.selector} - not visible and no bbox")
                        continue
                        
                    # Wait briefly for SPA transition (network or dom)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        await page.wait_for_timeout(1500)
                    
                    new_url = page.url.split("#")[0]
                    new_dom_hash = _compute_dom_hash(await page.content())
                    
                    # If URL changed, treat it as a new page to visit
                    if new_url != current_url.split("#")[0] and new_url.startswith("http"):
                        dest_id = hashlib.sha256(new_url.encode()).hexdigest()[:12]
                        
                        edge = Edge(
                            from_page=page_id,
                            to_page=dest_id,
                            via=elem,
                            instruction=f"Click '{elem.label or elem.human_description}' on the {page_title} page",
                        )
                        graph.edges.append(edge)
                        
                        if new_url not in visited:
                            crawl_queue.append(new_url)
                            
                    # If URL did NOT change but DOM changed significantly (e.g. a Modal opened)
                    elif new_dom_hash != dom_hash:
                        virtual_id = hashlib.sha256((current_url + new_dom_hash).encode()).hexdigest()[:12]
                        logger.info(f"Detected in-page state change (modal/tab). Mapping sub-state {virtual_id}...")
                        
                        modal_bytes = await _capture_screenshot(page)
                        modal_url = await _upload_screenshot(modal_bytes, company_id, virtual_id)
                        screenshot_urls[virtual_id] = modal_url
                        
                        modal_raw = await page.evaluate(DETECT_ELEMENTS_JS)
                        modal_vision = await _analyze_page_with_vision(
                            modal_bytes, modal_raw, f"State after clicking {elem.label or 'button'} on {page_title}", current_url, token_tracker=token_tracker
                        )
                        
                        v_map = {e.get("selector", ""): e.get("human_description", "") for e in modal_vision.get("elements", [])}
                        modal_elements = [
                            InteractiveElement(
                                selector=r["selector"],
                                label=r.get("label", ""),
                                human_description=v_map.get(r["selector"], ""),
                                type=r.get("type", "action"),
                                bbox=BoundingBox(**r["bbox"]) if r.get("bbox") else None,
                            ) for r in modal_raw
                        ]
                        
                        node = PageNode(
                            id=virtual_id,
                            url=current_url,
                            title=f"Modal/State: {elem.label or elem.human_description}",
                            page_summary=modal_vision.get("page_summary", "Modal state"),
                            elements=modal_elements,
                            dom_hash=new_dom_hash,
                        )
                        graph.nodes[virtual_id] = node
                        
                        edge = Edge(
                            from_page=from_node_id,
                            to_page=virtual_id,
                            via=elem,
                            instruction=f"Click '{elem.label or elem.human_description}' to open this state",
                        )
                        graph.edges.append(edge)
                        
                        # Add modal elements to the FRONT of the queue so we explore them while the modal is open
                        if not modal_vision.get("is_exploration_complete"):
                            modal_selectors = set(modal_vision.get("navigation_selectors_to_explore", []))
                            if modal_selectors:
                                new_candidates = [(e, virtual_id) for e in modal_elements if e.selector in modal_selectors]
                                # We must reverse them because extendleft reverses the order
                                click_queue.extendleft(reversed(new_candidates))
                                logger.info(f"Injected {len(new_candidates)} elements from new sub-state to explore immediately.")
                        else:
                            logger.info("Agent determined exploration is complete for this modal. Not clicking further inside.")
                            
                        # Removed hard reload so SPA state isn't destroyed
                        
                except Exception as e:
                    logger.warning(f"Failed to click-explore SPA element {elem.selector}: {e}")

    finally:
        try:
            await context.close()
        except Exception as e:
            logger.warning(f"Error closing browser context: {e}")

    ai_usage = TokenUsage(
        input_tokens=token_tracker["in"],
        output_tokens=token_tracker["out"],
        total_cost_usd=(token_tracker["in"] / 1_000_000 * 3.0) + (token_tracker["out"] / 1_000_000 * 15.0)
    )

    return StrollVersion(
        id=f"stroll_{str(uuid4())[:8]}",
        company_id=company_id,
        timestamp=datetime.now(tz=timezone.utc),
        graph=graph,
        screenshot_urls=screenshot_urls,
        ai_usage=ai_usage,
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
            ai_usage=TokenUsage(
                input_tokens=token_tracker["in"],
                output_tokens=token_tracker["out"],
                total_cost_usd=(token_tracker["in"] / 1_000_000 * 3.0) + (token_tracker["out"] / 1_000_000 * 15.0)
            ),
            status="failed",
        )
        logger.warning(f"Widget stroll failed for company {company_id}, not saving to DB.")

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

    # Regression Guard: if we lose more than 50% of nodes and end up with <= 15 nodes, it's likely a catastrophic login loop failure
    prev_version = await get_latest_version(company_id)
    if prev_version:
        prev_nodes = len(prev_version.graph.nodes)
        new_nodes = len(version.graph.nodes)
        if new_nodes <= 15 and new_nodes < (prev_nodes * 0.5):
            logger.error(f"Catastrophic regression detected for company {company_id}: nodes dropped from {prev_nodes} to {new_nodes}. Aborting commit.")
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
    config_data = data.model_dump()

    existing_doc = await db.stroll_configs.find_one({"company_id": company_id})

    if config_data.get("credentials"):
        new_pw = config_data["credentials"].get("password")
        if new_pw and new_pw != "********":
            from app.core.encryption import encrypt
            config_data["credentials"]["password"] = encrypt(new_pw)
        elif new_pw == "********" and existing_doc and existing_doc.get("credentials"):
            config_data["credentials"]["password"] = existing_doc["credentials"].get("password")

    config_doc = {
        "company_id": company_id,
        **config_data,
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

    return StrollConfig(company_id=company_id, **config_data, updated_at=now)
