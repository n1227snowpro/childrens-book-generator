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
SPINE_WIDTH_MARGIN_IN = 0.0625
SPINE_HEIGHT_MARGIN_IN = 0.125

MIN_HARDCOVER_PAGES = 76
MAX_HARDCOVER_PAGES = 550
MIN_SPINE_TEXT_PAGES = 79

RENDER_DPI = 300
FONT_NAME = "Helvetica-Bold"
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
        "spine_safe_width_in": round(spine_width_in - 2 * SPINE_WIDTH_MARGIN_IN, 3),
        "spine_safe_height_in": round(FRONT_PANEL_HEIGHT_IN - 2 * SPINE_HEIGHT_MARGIN_IN, 3),
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


def _draw_spine_text(c, text, safe_x, safe_y, safe_width_pt, safe_height_pt):
    """Draws spine text confined strictly to KDP's spine safe area (inset from the
    full spine bounds by the hinge/fold margins on every side)."""
    if not text:
        return
    max_font = min(SPINE_MAX_FONT, safe_width_pt - 4)
    if max_font < SPINE_MIN_FONT:
        return

    font_size = max_font
    while font_size >= SPINE_MIN_FONT:
        if stringWidth(text, FONT_NAME, font_size) <= safe_height_pt:
            break
        font_size -= 1
    if font_size < SPINE_MIN_FONT:
        return

    band_pad = 4
    c.saveState()
    c.setFillColor(Color(0, 0, 0, alpha=0.4))
    c.rect(safe_x - band_pad, safe_y, safe_width_pt + 2 * band_pad, safe_height_pt, fill=1, stroke=0)
    c.restoreState()

    c.saveState()
    c.setFillColor(white)
    c.setFont(FONT_NAME, font_size)
    c.translate(safe_x + safe_width_pt / 2, safe_y + safe_height_pt / 2)
    c.rotate(90)
    c.drawCentredString(0, -font_size / 3, text)
    c.restoreState()


def build_cover_pdf(image_path, spine_text, page_count, output_path):
    dims = calculate_dimensions(page_count)
    full_w_pt = dims["full_width_in"] * 72
    full_h_pt = dims["full_height_in"] * 72
    spine_w_pt = dims["spine_width_in"] * 72
    spine_safe_w_pt = dims["spine_safe_width_in"] * 72
    spine_safe_h_pt = dims["spine_safe_height_in"] * 72
    front_w_pt = dims["front_panel_width_in"] * 72
    wrap_pt = dims["wrap_in"] * 72

    cropped = _cover_fit(image_path, dims["full_width_px"], dims["full_height_px"])

    c = canvas.Canvas(str(output_path), pagesize=(full_w_pt, full_h_pt))
    c.drawImage(ImageReader(cropped), 0, 0, width=full_w_pt, height=full_h_pt)

    if dims["spine_text_supported"]:
        front_x = full_w_pt - wrap_pt - front_w_pt
        spine_x = front_x - spine_w_pt
        spine_safe_x = spine_x + (spine_w_pt - spine_safe_w_pt) / 2
        spine_safe_y = wrap_pt + SPINE_HEIGHT_MARGIN_IN * 72
        _draw_spine_text(c, spine_text, spine_safe_x, spine_safe_y, spine_safe_w_pt, spine_safe_h_pt)

    c.showPage()
    c.save()
    return dims
