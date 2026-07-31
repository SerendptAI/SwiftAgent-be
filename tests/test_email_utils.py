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
    html = """
    <img src="images/logo 2.png" alt="Logo">
    <img src="images/logo.png" alt="Logo 1">
    <img src="images/ticket-closed.png" alt="Ticket">
    """
    msg = EmailMessage()
    msg["Subject"] = "Invite"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg.set_content("Text fallback")

    attachments = add_html_with_inline_images(msg, html)

    assert attachments == {}
    assert "logo 2.png" in str(msg.get_payload()[1].get_payload())


def test_all_email_templates_remove_local_image_paths():
    for template in TEMPLATES_DIR.glob("*.html"):
        html = template.read_text(encoding="utf-8")
        processed, _ = process_html_for_inline_images(html)

        assert "images/" not in processed, template.name
