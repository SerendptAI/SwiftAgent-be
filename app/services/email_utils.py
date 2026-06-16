import logging
import mimetypes
import re
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

IMAGES_DIR = Path(__file__).resolve().parent.parent / "email_templates" / "images"
logger = logging.getLogger(__name__)
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=)(["\'])(?:[^"\']*/)?images/([^"\']+)\2', re.IGNORECASE)
_CSS_LOCAL_IMAGE_RE = re.compile(r"url\(\s*(['\"]?)(?:[^)'\"\s]*/)?images/[^)'\"\s]+\1\s*\)", re.IGNORECASE)


def get_image_data(filename: str) -> tuple[bytes, str, str]:
    """Return file data, maintype, and subtype for a given image filename."""
    path = IMAGES_DIR / filename
    with path.open("rb") as f:
        data = f.read()
    ctype, _ = mimetypes.guess_type(str(path))
    maintype, subtype = (ctype or "image/jpeg").split("/")
    return data, maintype, subtype

def process_html_for_inline_images(html: str) -> tuple[str, dict[str, str]]:
    """
    Replace local img src paths with CID references.

    CSS background CID references are intentionally not embedded. Several email
    clients do not resolve CIDs inside CSS and expose those related parts as
    paperclip attachments instead.
    """
    attachments: dict[str, str] = {}

    def replace_img_src(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2)
        filename = match.group(3)

        if filename not in attachments:
            cid = make_msgid(domain="swiftagent.com")
            attachments[filename] = cid[1:-1]

        return f"{prefix}{quote}cid:{attachments[filename]}{quote}"

    new_html = _IMG_SRC_RE.sub(replace_img_src, html)
    new_html = _CSS_LOCAL_IMAGE_RE.sub("none", new_html)

    return new_html, attachments


def add_html_with_inline_images(msg: EmailMessage, html: str) -> dict[str, str]:
    """
    Add an HTML alternative.
    We no longer embed template images as inline attachments (CIDs)
    because email clients (like Gmail) still render them as attachment pills
    at the bottom of the email, cluttering the UI.
    The templates already use absolute URLs (e.g. {{base_url}}/images/...)
    so remote HTTP loading will work perfectly.
    """
    msg.add_alternative(html, subtype="html")
    return {}
