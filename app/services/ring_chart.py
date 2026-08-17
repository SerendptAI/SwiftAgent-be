"""Render the Swift Wrap donut chart as a PNG.

Email clients cannot draw arcs: Gmail strips inline SVG, and Outlook's Word
engine supports neither CSS gradients nor border-radius. The ring therefore
ships as an image, and because it bakes its own value into the artwork it has
to be rendered per value rather than served as one static asset.

Geometry and colours match the Figma frame (210px, rendered at 2x); the type is
Roboto, so the baked-in value matches the rest of the email templates.
"""
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"
IMAGES_DIR = _TEMPLATES_DIR / "images"
FONTS_DIR = _TEMPLATES_DIR / "fonts"

DISPLAY_SIZE = 210          # CSS size in the email
SCALE = 2                   # retina factor, matching the other swift-wrap assets
SUPERSAMPLE = 4             # drawn large then downscaled, for antialiased edges

_RING_RADIUS = 105          # outer radius at 1x
_RING_THICKNESS = 21        # from the Figma vector (outer 105, inner 84)
_TRACK_COLOR = (117, 117, 117, 38)  # #757575 at 14.9%, per the Figma vector
_ARC_COLOR = (242, 176, 53)         # #f2b035
_VALUE_COLOR = (255, 255, 255)
_LABEL = "RESOLVED"
_LABEL_TRACKING = 2         # px at 2x, matching the template's uppercase letter-spacing
_LABEL_GAP = 10             # px at 2x, between the value and the label

_VALUE_SIZE = 64            # px at 1x, the design size
_VALUE_FONT = "Roboto-Bold.ttf"
_LABEL_FONT = "Roboto-Medium.ttf"
# Roboto is proportional rather than condensed, so a 4-character value ("100%")
# overruns the ring at the design size. Cap the drawn width to keep it clear of
# the stroke; the inner circle is 168px across at 1x.
_VALUE_MAX_WIDTH = 130      # px at 1x

DEFAULT_PERCENT = 67
FALLBACK_FILENAME = "swift-wrap-ring-67.png"


def ring_filename(percent: int) -> str:
    return f"swift-wrap-ring-{percent}.png"


def _draw_ring(percent: int) -> Image.Image:
    px = DISPLAY_SIZE * SCALE * SUPERSAMPLE
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # PIL treats the arc's bounding box as the OUTER edge and extends `width` inward.
    inset = _RING_RADIUS * SCALE * SUPERSAMPLE
    box = (px / 2 - inset, px / 2 - inset, px / 2 + inset, px / 2 + inset)
    width = int(_RING_THICKNESS * SCALE * SUPERSAMPLE)

    draw.arc(box, start=0, end=360, fill=_TRACK_COLOR, width=width)
    if percent > 0:
        # PIL measures from 3 o'clock; the design sweeps clockwise from 12.
        draw.arc(box, start=-90, end=-90 + 360 * percent / 100, fill=_ARC_COLOR, width=width)

    ring = img.resize((DISPLAY_SIZE * SCALE, DISPLAY_SIZE * SCALE), Image.LANCZOS)
    _draw_centered_text(ring, f"{percent}%")
    return ring


def _fit_value_font(draw: ImageDraw.ImageDraw, value: str) -> ImageFont.FreeTypeFont:
    """The design-size value font, scaled down if `value` would overrun the ring."""
    path = str(FONTS_DIR / _VALUE_FONT)
    size = _VALUE_SIZE * SCALE
    font = ImageFont.truetype(path, size)

    limit = _VALUE_MAX_WIDTH * SCALE
    width = draw.textlength(value, font=font)
    if width <= limit:
        return font
    # Glyph advance scales linearly with size, so one proportional step suffices.
    return ImageFont.truetype(path, max(1, int(size * limit / width)))


def _draw_centered_text(ring: Image.Image, value: str) -> None:
    draw = ImageDraw.Draw(ring)
    value_font = _fit_value_font(draw, value)
    label_font = ImageFont.truetype(str(FONTS_DIR / _LABEL_FONT), 16 * SCALE)

    cx = ring.width / 2
    cy = ring.height / 2 - 1
    value_box = draw.textbbox((0, 0), value, font=value_font)
    label_box = draw.textbbox((0, 0), _LABEL, font=label_font)
    value_h = value_box[3] - value_box[1]
    label_h = label_box[3] - label_box[1]

    top = cy - (value_h + _LABEL_GAP + label_h) / 2
    draw.text((cx - (value_box[2] + value_box[0]) / 2, top - value_box[1]),
              value, font=value_font, fill=_VALUE_COLOR)

    label_w = sum(draw.textlength(c, font=label_font) + _LABEL_TRACKING for c in _LABEL) - _LABEL_TRACKING
    x = cx - label_w / 2
    y = top + value_h + _LABEL_GAP - label_box[1]
    for char in _LABEL:
        draw.text((x, y), char, font=label_font, fill=_ARC_COLOR)
        x += draw.textlength(char, font=label_font) + _LABEL_TRACKING


def render_ring(percent: float) -> str:
    """Return the images/ filename for a ring at `percent`, rendering it on first use.

    Falls back to the committed 67% asset if the images directory is not
    writable, so a read-only deployment still sends a valid email.
    """
    percent = max(0, min(100, int(round(percent))))
    filename = ring_filename(percent)
    path = IMAGES_DIR / filename
    if path.exists():
        return filename

    try:
        _draw_ring(percent).save(path, optimize=True)
    except OSError as e:
        logger.warning("Could not render %s (%s); falling back to %s", filename, e, FALLBACK_FILENAME)
        return FALLBACK_FILENAME
    return filename
