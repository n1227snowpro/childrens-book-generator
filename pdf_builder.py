from reportlab.lib.colors import Color, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from PIL import Image

PAGE_WIDTH = 594  # 8.25in
PAGE_HEIGHT = 792  # 11in
BAND_HEIGHT = 1.9 * 72
MARGIN_X = 0.45 * 72
FONT_NAME = "Helvetica-Bold"
MAX_FONT_SIZE = 24
MIN_FONT_SIZE = 12
LINE_SPACING = 1.25

RENDER_DPI = 150


def _cover_fit(image_path):
    target_w = round(PAGE_WIDTH / 72 * RENDER_DPI)
    target_h = round(PAGE_HEIGHT / 72 * RENDER_DPI)

    img = Image.open(image_path).convert("RGB")
    scale = max(target_w / img.width, target_h / img.height)
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

    c.saveState()
    c.setFillColor(Color(0, 0, 0, alpha=0.52))
    c.rect(0, 0, PAGE_WIDTH, BAND_HEIGHT, fill=1, stroke=0)
    c.restoreState()

    max_width = PAGE_WIDTH - 2 * MARGIN_X
    max_height = BAND_HEIGHT - 0.3 * 72
    font_size, lines, leading = _fit_text(story_text or "", max_width, max_height)

    c.setFillColor(white)
    c.setFont(FONT_NAME, font_size)

    block_height = len(lines) * leading
    start_y = (BAND_HEIGHT - block_height) / 2 + block_height - leading + (leading - font_size) / 2

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
