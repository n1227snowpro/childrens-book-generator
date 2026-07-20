from reportlab.lib.colors import Color, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from PIL import Image

TRIM_WIDTH_IN = 8.25
TRIM_HEIGHT_IN = 11.0

# KDP interior bleed spec: add 0.125in to width (outer edge) and 0.25in to height (0.125 top +
# 0.125 bottom) — https://kdp.amazon.com/en_US/help/topic/GVBQ3CMEQW3W2VL6. Our page template is
# a single uniform layout (no recto/verso mirroring), so the extra width bleed is split evenly
# across left/right to keep the trim rectangle centered; height bleed is 0.125in top and bottom
# per spec. Images still bleed edge-to-edge on the full page either way.
BLEED_LEFT_IN = 0.0625
BLEED_RIGHT_IN = 0.0625
BLEED_BOTTOM_IN = 0.125
BLEED_TOP_IN = 0.125

PAGE_WIDTH = round((TRIM_WIDTH_IN + BLEED_LEFT_IN + BLEED_RIGHT_IN) * 72)
PAGE_HEIGHT = round((TRIM_HEIGHT_IN + BLEED_TOP_IN + BLEED_BOTTOM_IN) * 72)

# KDP's minimum text margin for bleed interiors is 0.375in from the trim edge (also covers the
# inside/gutter margin bracket for this app's max supported page count of 150). We use a bit more
# than the bare minimum so descenders (g, y, p) and KDP's own measurement rounding can't tip a
# bottom line back into the "outside the margins" danger zone.
SAFE_MARGIN_IN = 0.5
SAFE_MARGIN_PT = SAFE_MARGIN_IN * 72
TRIM_BOTTOM_PT = BLEED_BOTTOM_IN * 72

BAND_HEIGHT = 1.9 * 72
MARGIN_X = 0.55 * 72
FONT_NAME = "Helvetica-Bold"
MAX_FONT_SIZE = 24
MIN_FONT_SIZE = 12
LINE_SPACING = 1.25

RENDER_DPI = 150

# The image model sometimes paints a picture-frame-style border with white margin around the
# scene, right up to its own edges — prompt instructions telling it not to have proven unreliable
# (confirmed: the border persisted even after adding explicit "no border/full-bleed" language to
# every page prompt). A plain scale-to-cover crop doesn't help either: measured on an affected
# page, the border sat at ~4.5% of width / ~2.5% of height from the edge, but scaling only just
# enough to cover the target canvas crops ~0% off the axis that already matches the target aspect
# ratio most closely — so the border can survive completely untouched on that axis. Overscanning
# by this extra factor before cropping guarantees a real margin (~6.5%+ per side, worked out
# below) gets cut from every edge regardless of source aspect ratio, deterministically removing
# the border instead of hoping the model didn't paint one.
PAGE_IMAGE_OVERSCAN = 1.15


def _cover_fit(image_path):
    target_w = round(PAGE_WIDTH / 72 * RENDER_DPI)
    target_h = round(PAGE_HEIGHT / 72 * RENDER_DPI)

    img = Image.open(image_path).convert("RGB")
    scale = max(target_w / img.width, target_h / img.height) * PAGE_IMAGE_OVERSCAN
    new_w, new_h = round(img.width * scale), round(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))
    return img


def _wrap_text(text, font_size, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if stringWidth(candidate, FONT_NAME, font_size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text(text, max_width, max_height):
    for font_size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        lines = _wrap_text(text, font_size, max_width)
        leading = font_size * LINE_SPACING
        if len(lines) * leading <= max_height:
            return font_size, lines, leading
    font_size = MIN_FONT_SIZE
    lines = _wrap_text(text, font_size, max_width)
    return font_size, lines, font_size * LINE_SPACING


def _draw_page(c, image_path, story_text):
    cropped = _cover_fit(image_path)
    c.drawImage(ImageReader(cropped), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)

    # The band itself is decorative art and may bleed to the page edge like the illustration —
    # only the actual text glyphs are subject to KDP's margin rule, so the text is confined to
    # start no lower than TRIM_BOTTOM_PT + SAFE_MARGIN_PT above the page's bottom (bleed) edge.
    max_width = PAGE_WIDTH - 2 * MARGIN_X
    text_bottom = TRIM_BOTTOM_PT + SAFE_MARGIN_PT
    top_padding = 0.15 * 72
    max_height = (BAND_HEIGHT - top_padding) - text_bottom
    font_size, lines, leading = _fit_text(story_text or "", max_width, max_height)
    block_height = len(lines) * leading

    # _fit_text can't always fit the text even at MIN_FONT_SIZE (an unusually long final page,
    # for example) — this got flagged by KDP as a margin violation, because the leftover
    # centering math pushed the overflow below text_bottom instead of respecting it. Grow the
    # band upward by exactly the overflow instead, so the bottom of the text block never moves.
    band_height = BAND_HEIGHT + max(0, block_height - max_height)

    c.saveState()
    c.setFillColor(Color(0, 0, 0, alpha=0.52))
    c.rect(0, 0, PAGE_WIDTH, band_height, fill=1, stroke=0)
    c.restoreState()

    c.setFillColor(white)
    c.setFont(FONT_NAME, font_size)

    available = band_height - top_padding - text_bottom
    start_y = text_bottom + (available - block_height) / 2 + block_height - leading + (leading - font_size) / 2

    y = start_y
    for line in lines:
        c.drawCentredString(PAGE_WIDTH / 2, y, line)
        y -= leading


def build_pdf(pages, output_path):
    """pages: list of dicts sorted by page_num, each {"image_path": str, "story_text": str}"""
    c = canvas.Canvas(str(output_path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    for page in pages:
        _draw_page(c, page["image_path"], page.get("story_text", ""))
        c.showPage()
    c.save()
    return output_path
