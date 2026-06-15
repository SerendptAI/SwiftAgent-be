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
    """Add an HTML alternative and attach local template img assets inline."""
    html, attachments = process_html_for_inline_images(html)
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[1]

    for filename, cid in attachments.items():
        try:
            data, maintype, subtype = get_image_data(filename)
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
            image_part = html_part.get_payload()[-1]
            # Replace Content-Disposition to be strictly inline without a filename
            # to prevent email clients from rendering attachment pills at the bottom
            del image_part["Content-Disposition"]
            image_part["Content-Disposition"] = "inline"
            image_part["X-Attachment-Id"] = cid
        except Exception as e:
            logger.warning("Could not attach inline email image %s: %s", filename, e)

    return attachments
