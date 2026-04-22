import mimetypes
import re
from email.utils import make_msgid
from pathlib import Path
from typing import Dict, Tuple

IMAGES_DIR = Path(__file__).resolve().parent.parent / "email_templates" / "images"

def get_image_data(filename: str) -> Tuple[bytes, str, str]:
    """Return file data, maintype, and subtype for a given image filename."""
    path = IMAGES_DIR / filename
    with path.open("rb") as f:
        data = f.read()
    ctype, _ = mimetypes.guess_type(str(path))
    maintype, subtype = (ctype or "image/jpeg").split("/")
    return data, maintype, subtype

def process_html_for_inline_images(html: str) -> Tuple[str, Dict[str, str]]:
    """
    Scans HTML for exact matches of 'images/' paths,
    replaces with 'cid:x', and returns (new_html, map_of_filename_to_cid).
    """
    attachments = {}
    
    # We look for src="images/filename.ext" or url('images/filename.ext')
    # Because of CSS url('images/...') and img src="images/..."
    
    def replace_match(match) -> str:
        prefix = match.group(1) # e.g. " or ' 
        filename = match.group(2)
        suffix = match.group(3)
        
        if filename not in attachments:
            cid = make_msgid(domain="swiftagent.com")
            attachments[filename] = cid[1:-1] # remove < >
        
        return f"{prefix}cid:{attachments[filename]}{suffix}"
    
    # Regex replaces 'images/' followed by filename, wrapped in quotes or parens
    # This covers src="images/logo.png" and url('images/bg.jpg')
    new_html = re.sub(r'([\"\']|url\([\'\"]?)images/([^\"\'\)]+)([\"\']|[\'\"]?\))', replace_match, html)
    
    return new_html, attachments
