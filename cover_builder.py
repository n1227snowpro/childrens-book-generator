from reportlab.lib.utils import ImageReader
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

MIN_HARDCOVER_PAGES = 76
MAX_HARDCOVER_PAGES = 550

RENDER_DPI = 300


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
        "wrap_in": WRAP_IN,
        "dpi": RENDER_DPI,
        "full_width_px": round(full_width_in * RENDER_DPI),
        "full_height_px": round(full_height_in * RENDER_DPI),
        "kdp_hardcover_compliant": MIN_HARDCOVER_PAGES <= page_count <= MAX_HARDCOVER_PAGES,
    }


def _cover_fit(image_path, target_w_px, target_h_px):
    img = Image.open(image_path).convert("RGB")
    scale = max(target_w_px / img.width, target_h_px / img.height)
    new_w, new_h = round(img.width * scale), round(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w_px) // 2
    top = (new_h - target_h_px) // 2
    return img.crop((left, top, left + target_w_px, top + target_h_px))


def build_cover_pdf(image_path, page_count, output_path):
    dims = calculate_dimensions(page_count)
    full_w_pt = dims["full_width_in"] * 72
    full_h_pt = dims["full_height_in"] * 72

    cropped = _cover_fit(image_path, dims["full_width_px"], dims["full_height_px"])

    c = canvas.Canvas(str(output_path), pagesize=(full_w_pt, full_h_pt))
    c.drawImage(ImageReader(cropped), 0, 0, width=full_w_pt, height=full_h_pt)
    c.showPage()
    c.save()
    return dims
