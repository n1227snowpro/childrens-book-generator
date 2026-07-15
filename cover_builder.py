from reportlab.lib.colors import Color, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from PIL import Image

# Verified against KDP's official cover calculator (kdp.amazon.com/cover-calculator)
# for the 8.25 x 11in hardcover / premium color / white paper combination this app
# always uses. Full cover width = 2*WRAP + 2*FRONT_PANEL_WIDTH + spine_width(pages).
WRAP_IN = 0.591
FRONT_PANEL_WIDTH_IN = 8.447
FRONT_PANEL_HEIGHT_IN = 11.236
SPINE_WIDTH_PER_PAGE_IN = 0.0023375
SPINE_WIDTH_OFFSET_IN = 0.19
SPINE_MARGIN_IN = 0.0625

MIN_HARDCOVER_PAGES = 76
MAX_HARDCOVER_PAGES = 550
MIN_SPINE_TEXT_PAGES = 79

RENDER_DPI = 300
FONT_NAME = "Helvetica-Bold"
TITLE_MAX_FONT = 40
TITLE_MIN_FONT = 16
SPINE_MAX_FONT = 28
SPINE_MIN_FONT = 6
LINE_SPACING = 1.2


def calculate_dimensions(page_count):
    spine_width_in = SPINE_WIDTH_PER_PAGE_IN * page_count + SPINE_WIDTH_OFFSET_IN
    full_width_in = 2 * WRAP_IN + 2 * FRONT_PANEL_WIDTH_IN + spine_width_in
    full_height_in = FRONT_PANEL_HEIGHT_IN + 2 * WRAP_IN

    return {
        "page_count": page_count,
        "full_width_in": round(full_width_in, 3),
        "full_height_in": round(full_height_in, 3),
        "front_panel_width_in": FRONT_PANEL_WIDTH_IN,
        "front_panel_height_in": FRONT_PANEL_HEIGHT_IN,
        "spine_width_in": round(spine_width_in, 3),
        "spine_safe_width_in": round(spine_width_in - 2 * SPINE_MARGIN_IN, 3),
        "wrap_in": WRAP_IN,
        "dpi": RENDER_DPI,
        "full_width_px": round(full_width_in * RENDER_DPI),
        "full_height_px": round(full_height_in * RENDER_DPI),
        "kdp_hardcover_compliant": MIN_HARDCOVER_PAGES <= page_count <= MAX_HARDCOVER_PAGES,
        "spine_text_supported": page_count >= MIN_SPINE_TEXT_PAGES,
    }


def _cover_fit(image_path, target_w_px, target_h_px):
    img = Image.open(image_path).convert("RGB")
    scale = max(target_w_px / img.width, target_h_px / img.height)
    new_w, new_h = round(img.width * scale), round(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w_px) // 2
    top = (new_h - target_h_px) // 2
    return img.crop((left, top, left + target_w_px, top + target_h_px))


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


def _fit_text(text, max_width, max_height, max_font, min_font):
    for font_size in range(max_font, min_font - 1, -1):
        lines = _wrap_text(text, font_size, max_width)
        leading = font_size * LINE_SPACING
        if len(lines) * leading <= max_height:
            return font_size, lines, leading
    font_size = min_font
    lines = _wrap_text(text, font_size, max_width)
    return font_size, lines, font_size * LINE_SPACING


def _draw_title(c, title, x, y, width_pt, height_pt):
    c.saveState()
    c.setFillColor(Color(0, 0, 0, alpha=0.45))
    c.rect(x, y, width_pt, height_pt, fill=1, stroke=0)
    c.restoreState()

    margin = 0.25 * 72
    max_width = width_pt - 2 * margin
    max_height = height_pt - 2 * margin
    font_size, lines, leading = _fit_text(title or "", max_width, max_height, TITLE_MAX_FONT, TITLE_MIN_FONT)

    c.setFillColor(white)
    c.setFont(FONT_NAME, font_size)

    block_height = len(lines) * leading
    start_y = y + (height_pt - block_height) / 2 + block_height - leading + (leading - font_size) / 2

    text_y = start_y
    for line in lines:
        c.drawCentredString(x + width_pt / 2, text_y, line)
        text_y -= leading


def _draw_spine_text(c, text, x, y, width_pt, height_pt):
    if not text:
        return
    max_font = min(SPINE_MAX_FONT, width_pt - 6)
    if max_font < SPINE_MIN_FONT:
        return

    font_size = max_font
    while font_size >= SPINE_MIN_FONT:
        if stringWidth(text, FONT_NAME, font_size) <= height_pt - 20:
            break
        font_size -= 1
    if font_size < SPINE_MIN_FONT:
        return

    c.saveState()
    c.setFillColor(white)
    c.setFont(FONT_NAME, font_size)
    c.translate(x + width_pt / 2, y + height_pt / 2)
    c.rotate(90)
    c.drawCentredString(0, -font_size / 3, text)
    c.restoreState()


def build_cover_pdf(image_path, title, spine_text, page_count, output_path):
    dims = calculate_dimensions(page_count)
    full_w_pt = dims["full_width_in"] * 72
    full_h_pt = dims["full_height_in"] * 72
    spine_w_pt = dims["spine_width_in"] * 72
    front_w_pt = dims["front_panel_width_in"] * 72
    wrap_pt = dims["wrap_in"] * 72

    cropped = _cover_fit(image_path, dims["full_width_px"], dims["full_height_px"])

    c = canvas.Canvas(str(output_path), pagesize=(full_w_pt, full_h_pt))
    c.drawImage(ImageReader(cropped), 0, 0, width=full_w_pt, height=full_h_pt)

    front_x = full_w_pt - wrap_pt - front_w_pt
    band_h = 2.4 * 72
    _draw_title(c, title, front_x, full_h_pt - wrap_pt - band_h, front_w_pt, band_h)

    if dims["spine_text_supported"]:
        spine_x = front_x - spine_w_pt
        _draw_spine_text(c, spine_text, spine_x, wrap_pt, spine_w_pt, full_h_pt - 2 * wrap_pt)

    c.showPage()
    c.save()
    return dims
