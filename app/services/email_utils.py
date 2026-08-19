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


import re
from pathlib import Path
from email.mime.image import MIMEImage

def add_html_with_inline_images(msg: EmailMessage, html: str) -> dict[str, str]:
    """
    Parse HTML for image tags, attach them as inline CIDs, and replace the src.
    """
    images_dir = Path(__file__).parent.parent / "email_templates" / "images"
    
    # Find all image sources that look like they belong to our templates
    pattern = re.compile(r'src="[^"]*/images/([^"]+\.png)"')
    found_images = set(pattern.findall(html))
    
    # First we modify HTML
    for img_name in found_images:
        html = re.sub(rf'src="[^"]*/images/{img_name}"', f'src="cid:{img_name}"', html)

    msg.add_alternative(html, subtype="html")
    
    # The HTML part is the last payload after add_alternative
    html_part = msg.get_payload()[-1]
    
    for img_name in found_images:
        img_path = images_dir / img_name
        if img_path.exists():
            with open(img_path, "rb") as f:
                img_data = f.read()
            html_part.add_related(img_data, 'image', 'png', cid=f"<{img_name}>")

    return {}
