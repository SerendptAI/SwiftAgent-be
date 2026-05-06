from email.message import EmailMessage
from pathlib import Path

from app.services.email_utils import (
    add_html_with_inline_images,
    process_html_for_inline_images,
)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "app" / "email_templates"


def _html_part(msg: EmailMessage):
    return msg.get_payload()[1].get_payload()[0]


def _related_image_parts(msg: EmailMessage):
    return [
        part
        for part in msg.walk()
        if part.get_content_maintype() == "image"
    ]


def test_process_html_embeds_img_sources_but_not_css_backgrounds():
    html = """
    <style>.heading { background-image: url("images/text-bg.jpg"); }</style>
    <img src="images/logo 2.png" alt="Logo">
    <img src='images/ticket-confirmation.png' alt='Ticket'>
    """

    processed, attachments = process_html_for_inline_images(html)

    assert "images/" not in processed
    assert "background-image: none" in processed
    assert sorted(attachments) == ["logo 2.png", "ticket-confirmation.png"]
    assert processed.count("cid:") == 2


def test_add_html_with_inline_images_marks_related_parts_inline():
    html = (TEMPLATES_DIR / "team_member_invite.html").read_text(encoding="utf-8")
    msg = EmailMessage()
    msg["Subject"] = "Invite"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg.set_content("Text fallback")

    attachments = add_html_with_inline_images(msg, html)

    assert sorted(attachments) == ["logo 2.png", "logo.png", "ticket-closed.png"]
    assert "images/" not in _html_part(msg).get_content()

    image_parts = _related_image_parts(msg)
    assert len(image_parts) == 3
    for part in image_parts:
        assert part["Content-ID"]
        assert part["X-Attachment-Id"]
        assert part.get_content_disposition() == "inline"
        assert "filename" not in part.get("Content-Disposition", "")


def test_all_email_templates_remove_local_image_paths():
    for template in TEMPLATES_DIR.glob("*.html"):
        html = template.read_text(encoding="utf-8")
        processed, _ = process_html_for_inline_images(html)

        assert "images/" not in processed, template.name
