from dotenv import load_dotenv

load_dotenv()

import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from queue import Queue

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont

import claude_client
import cover_builder
import db
import gemini_client
import kie_client
import pdf_builder
import r2_client
import settings
from config import BOOKS_DIR, PORT, UPLOADS_DIR

app = Flask(__name__)
CORS(app)

db.init_db()

_queues = {}
_queues_lock = threading.Lock()

STAGE_BOUNDS = {
    "blueprint": (0, 5),
    "characters": (5, 15),
    "pages": (15, 75),
    "page_uploads": (75, 85),
    "pdf": (85, 90),
    "cover": (90, 97),
    "final_upload": (97, 100),
}


def _get_queue(job_id):
    with _queues_lock:
        if job_id not in _queues:
            _queues[job_id] = Queue()
        return _queues[job_id]


def _push(job_id, event):
    with _queues_lock:
        q = _queues.get(job_id)
    if q:
        q.put(event)


def _cleanup_queue(job_id):
    with _queues_lock:
        _queues.pop(job_id, None)


def _set_progress(job_id, stage, step_text, current, total):
    lo, hi = STAGE_BOUNDS[stage]
    pct = lo if not total else lo + (hi - lo) * (current / total)
    db.update_job(job_id, step=step_text, current=current, total=total)
    _push(job_id, {"step": step_text, "pct": round(pct)})


def _slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return text or "book"


def _run_with_timeout(fn, timeout_seconds):
    """Runs fn() in a worker thread and raises TimeoutError if it doesn't finish in time.
    Belt-and-suspenders for background jobs: no matter what stalls inside fn (a network call
    without its own timeout, or anything else), the caller gets an exception back within a bounded
    time instead of the job — and its SSE stream — hanging forever with no way for the user to
    know or retry. Doesn't cancel the underlying thread (Python can't do that); it just stops
    waiting on it."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout_seconds)
    except FutureTimeoutError:
        raise TimeoutError(f"Operation timed out after {timeout_seconds}s")
    finally:
        pool.shutdown(wait=False)


def _download(url, dest_path, attempts=3, backoff=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise last_error


def _build_placeholder_image(path, text):
    img = Image.new("RGB", (768, 1024), color=(230, 214, 191))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= 680:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    line_height = 18
    y = (1024 - len(lines) * line_height) / 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((768 - w) / 2, y), line, fill=(110, 90, 70), font=font)
        y += line_height

    img.save(path, "JPEG", quality=90)


def _download_path(book_id):
    return f"/api/books/{book_id}/download"


def _cover_path(book_id):
    return f"/api/books/{book_id}/cover"


def _page_image_path(book_id, page_num):
    return f"/api/books/{book_id}/pages/{page_num}/image"


def _style_reference_prompt(art_style):
    return (
        f"A single reference illustration showcasing this art style: {art_style} "
        "Show a simple, generic scene with no specific characters, clearly demonstrating the "
        "color palette, linework, shading, and overall rendering technique. No text."
    )


def _character_reference_urls(characters):
    return [r2_client.presigned_url(c["s3_key"]) for c in characters if c.get("s3_key")]


def _consistency_reference_urls(art_style_ref_url, characters, location_ref_url=None):
    # Order matters: nano-banana (the default model) silently truncates to its first 3 reference
    # images (see kie_client.NANO_BANANA_MAX_REFERENCE_IMAGES). Character references must win that
    # budget over location — confirmed live as a regression on "Chief Iggy's Big Red Rescue" page
    # 4/5 (style + location + Chief Iggy already filled all 3 slots, silently dropping Mateo's
    # reference entirely and making him inconsistent). A wrong-looking character is a worse
    # failure than an inconsistent background, so location goes last and only survives if there's
    # room left after style and every character on the page.
    urls = [art_style_ref_url] + _character_reference_urls(characters) + [location_ref_url]
    return [u for u in urls if u]


def _page_location_ref_url(locations, page):
    """Resolves the presigned reference image for the location a page is set in, if the blueprint
    tracked a recurring location for this page and a reference was successfully generated for it.
    Without this, every page's background was generated from a fresh text description alone, with
    nothing anchoring it to what the same location looked like on an earlier page — confirmed live
    as inconsistent backgrounds across pages meant to share one scene."""
    location_name = (page.get("location") or "").strip().lower()
    if not location_name:
        return None
    for loc in locations:
        if (loc.get("name") or "").strip().lower() == location_name and loc.get("s3_key"):
            return r2_client.presigned_url(loc["s3_key"])
    return None


def _consistency_suffix(reference_urls):
    if not reference_urls:
        return ""
    return (
        " Match the exact illustration style, character appearances, and background/setting "
        "shown in the reference images."
    )


# Applied to every page prompt unconditionally, regardless of what Claude's blueprint generation
# produced — a prompt-side instruction alone isn't a guarantee the model complies with it every
# time. Confirmed live: a page's own image_prompt asked for "'The End' in gentle script" baked
# into the artwork, and that text landed too close to the trim edge, causing a real KDP interior
# rejection. Text drawn inside the illustration itself is completely outside the margin-safety
# math that protects the app's own separately-rendered story-text band, so this is a hard,
# always-on guardrail rather than something left to prompt-following.
_PAGE_NO_TEXT_SUFFIX = (
    " Do not render any text, words, letters, captions, labels, signage, or lettering anywhere "
    "in this illustration — including a closing phrase like \"The End\" if this is the final page. "
    "The story's own text is rendered separately by the app; text drawn into the image itself is "
    "not covered by that safety margin."
)

# Same always-on pattern as _PAGE_NO_TEXT_SUFFIX, for a different KDP interior rejection:
# "insufficient bleed" on pages 1-3, 36, 38 of "Chief Iggy's Big Red Rescue". Confirmed by
# rendering the actual generated pages that nothing in this app crops or composites a border —
# the image model itself was painting a picture-frame-style rounded border with solid white
# background outside it, going all the way to the image's own edges on multiple unrelated pages.
# _cover_fit's scale-to-cover crop can't remove that: it's real painted content spanning the
# full source image, not empty canvas, so cropping tighter would just cut into the scene instead
# of the border. The only fix is stopping the model from painting a border/frame at all.
_PAGE_FULL_BLEED_SUFFIX = (
    " This illustration must be a full-bleed, edge-to-edge scene with continuous artwork filling "
    "the entire canvas, corner to corner. Do not add a border, frame, rounded corners, vignette, "
    "or any white/blank margin around the edges — nothing resembling a picture frame or postcard "
    "border. The scene's content must extend all the way to every edge of the image."
)


def _character_names_joined(characters):
    return ", ".join(c.get("name", "") for c in characters if c.get("name"))


def _character_clothing_notes(characters):
    """Restates each on-page character's exact outfit directly in the page prompt's own text,
    independent of whether their reference image actually reaches the model — nano-banana's
    3-image cap (see kie_client.NANO_BANANA_MAX_REFERENCE_IMAGES) means a character's reference
    gets silently dropped outright on busier pages, and relying on the image alone to carry
    clothing consistency was confirmed live as characters' outfits randomly changing color or
    disappearing between pages. Blueprint generation is instructed to write a precise "clothing"
    field per character for exactly this reason (see claude_client.py)."""
    notes = [
        f"{c['name']} wears {c['clothing'].strip()}."
        for c in characters
        if c.get("name") and (c.get("clothing") or "").strip()
    ]
    return " " + " ".join(notes) if notes else ""


def _character_brief_description(character):
    """First sentence (or ~140 chars) of a character's visual_description — enough to visually
    distinguish similar-looking characters in a prompt without bloating it with the character's
    full multi-sentence reference-portrait description."""
    desc = (character.get("visual_description") or "").strip()
    if not desc:
        return ""
    return desc.split(".")[0].strip()[:140]


def _character_names_with_descriptions(characters):
    """Unlike a bare name list, this pairs each name with a short visual descriptor so the model
    can tell apart characters sent as separate reference images. Confirmed live as necessary on
    "Silas and the Silver Seeds": its protagonist and mentor are both owls, and with only bare
    names in the prompt, nothing distinguished which reference image was which — the protagonist
    was left out of the regenerated cover entirely."""
    parts = []
    for c in characters:
        name = c.get("name")
        if not name:
            continue
        desc = _character_brief_description(c)
        parts.append(f"{name} ({desc})" if desc else name)
    return ", ".join(parts)


_CHARACTER_DESC_STOPWORDS = {
    "the", "and", "with", "his", "her", "its", "she", "him", "for", "from", "that", "this",
    "who", "has", "have", "are", "was", "were", "into", "onto", "very", "small", "tiny", "large",
    "big", "little", "short", "long", "soft", "warm", "cool", "bright", "curious", "kind",
    "sweet", "cute", "lovely", "wonderful", "beautiful", "clean", "shows", "showing", "front",
    "side", "view", "pose", "background", "watercolor", "style", "reference", "portrait",
    "character", "against",
}


def _character_desc_words(text):
    return {
        w for w in re.findall(r"[a-z]+", (text or "").lower())
        if len(w) >= 3 and w not in _CHARACTER_DESC_STOPWORDS
    }


def _same_character(saved_description, new_description):
    """Characters are tracked globally by name only (see db.upsert_character), so two unrelated
    books that happen to give a character the same name — e.g. one story's sparrow 'Pip' and
    another's squirrel 'Pip' — would otherwise silently reuse the wrong species' reference image.
    Confirmed live: 'The Feathered Talent Show' had its sparrow Pip rendered as a squirrel because
    an earlier, unrelated book already owned that name. Gate reuse on the visual descriptions
    actually overlapping before trusting a same-name match."""
    a, b = _character_desc_words(saved_description), _character_desc_words(new_description)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.2


def _page_characters(characters, page):
    """Restricts a page's reference images to only the characters actually present on that
    page (per Claude's characters_on_page field), so a character shown at different ages/life
    stages doesn't get conditioned on the wrong stage's reference image.

    None (the field is entirely missing — older blueprints predating this field) falls back to
    every character. [] (Claude explicitly listed no characters — a landscape/establishing-shot
    page) must NOT fall back to every character: sending every character's reference photo for a
    scene that shows none of them was confirmed live to trigger KIE.ai's nano-banana-edit
    "invalid param" rejection, on top of being semantically wrong."""
    names_on_page = page.get("characters_on_page")
    if names_on_page is None:
        return characters
    if not names_on_page:
        return []
    wanted = {n.strip().lower() for n in names_on_page if n}
    matched = [c for c in characters if (c.get("name") or "").strip().lower() in wanted]
    return matched or characters


COVER_MAX_CHARACTERS = 3


def _cover_characters(characters):
    """Unlike pages, a cover has no characters_on_page-equivalent telling us who'll actually
    appear in the composition, so a book with many characters (e.g. an 11-character book) would
    otherwise send every one of them as a reference image and list all their names in the prompt.
    That dilutes how closely any single reference gets followed — confirmed live for page
    generation (see _page_characters/NANO_BANANA_MAX_REFERENCE_IMAGES) and the same failure mode
    applies here. Capping to a small subset keeps the ones that ARE sent from getting diluted.

    The protagonist must survive this cap — _build_cover_prompt always positions them on the
    front cover, so cutting them out here (e.g. if the blueprint didn't list them among the first
    few characters) would leave that instruction with nothing to point at."""
    protagonist = next(
        (c for c in characters if (c.get("role") or "").strip().lower().startswith("protagonist")),
        None,
    )
    if protagonist and protagonist not in characters[:COVER_MAX_CHARACTERS]:
        return [protagonist] + characters[:COVER_MAX_CHARACTERS - 1]
    return characters[:COVER_MAX_CHARACTERS]


def _build_cover_prompt(title, subtitle, theme, characters, reference_urls):
    # The AI renders the title directly into the artwork (no code-drawn overlay — see
    # cover_builder.py), so keeping it inside KDP's safe area is prompt-guidance only, not
    # something we can enforce after the fact. An earlier version of this prompt spelled out
    # numeric margins ("leave at least 12% of the image height..."), which backfired badly: the
    # model rendered those percentages as literal on-image labels with tick-mark rulers, like a
    # design annotation. A later version said "spine fold" and "band of open background", which
    # backfired differently: the model rendered a literal 3D book mockup — a shaded/darkened
    # crease down the center simulating a physical spine, and blank empty margins (instead of
    # continued artwork) around the title. This version is explicit that the output is a flat,
    # full-bleed print file with no mockup shading and no blank space anywhere. A further issue
    # showed up once title+subtitle both appeared: the model spread them apart, putting the
    # subtitle by itself near the very bottom of the panel where it got clipped by KDP's trim/
    # safe-area guides. Now explicit that title+subtitle must sit together as one tight block,
    # clear of the bottom edge specifically.
    subtitle_clause = (
        f" Directly beneath the title, in smaller text, render the subtitle \"{subtitle}\" — "
        "as part of the same tight text block as the title, not placed separately elsewhere "
        "in the image."
        if subtitle
        else ""
    )
    prompt = (
        f"Full-bleed, perfectly flat two-dimensional wraparound illustration for a children's "
        f"book cover titled '{title}' — one single continuous painted scene spanning the back "
        "cover (left half) and front cover (right half), the kind of flat print-ready artwork "
        "file an illustrator delivers to a printer. This is NOT a 3D product photo or book "
        "mockup: do not render any shadow, crease, gutter darkening, drop shadow, page curl, "
        "spine fold line, or any other effect suggesting a physical three-dimensional book — the "
        "image must be evenly lit and visually continuous from edge to edge, with no seam, fold, "
        "or darkened strip down the center. "
        f"Render the title text \"{title}\" prominently and legibly as part of the illustration, "
        f"placed within the upper half of the right half (front cover) of the image.{subtitle_clause} "
        "This title+subtitle block must stay entirely within the upper half of the front cover "
        "panel and must be nowhere near the bottom edge — leave the whole lower portion of the "
        "front cover free of any text, since it is the area most likely to be trimmed or hidden "
        "by cover-tool safe-area guides. "
        "The illustration must fill the entire canvas edge-to-edge with continuous scenery and "
        "artwork — no blank, empty, or solid-color margins anywhere, including at the top, "
        "bottom, or sides. Keep the title/subtitle text legible by placing it over open sky, "
        "foliage, or other background elements within the scene itself, comfortably away from "
        "the exact center and from all four edges — not by leaving empty space around it. "
        "Do not add any extra text, labels, numbers, captions, rulers, or measurement marks "
        "anywhere in the image — the only text should be the title" +
        (" and subtitle" if subtitle else "") +
        " described above, rendered as part of the illustration. Theme: " + theme + "."
    )
    names_with_desc = _character_names_with_descriptions(characters)
    if names_with_desc:
        prompt += f" Characters present: {names_with_desc}."
    protagonist = next(
        (c for c in characters if (c.get("role") or "").strip().lower().startswith("protagonist")),
        characters[0] if characters else None,
    )
    main_character_name = protagonist.get("name") if protagonist else None
    if main_character_name:
        protagonist_desc = _character_brief_description(protagonist)
        desc_clause = f" — {protagonist_desc} — " if protagonist_desc else " "
        prompt += (
            f" {main_character_name}{desc_clause}is the story's main character and MUST be "
            "unmistakably featured: the largest, most visually prominent figure in the "
            "illustration, positioned on the right half (front cover), never on the left half "
            "(back cover) and never straddling the center. If another character looks visually "
            f"similar to {main_character_name} (e.g. another animal of the same species), make "
            f"sure the differences described above are clearly rendered so {main_character_name} "
            "is unambiguously who they are — any other characters present are smaller, clearly "
            "secondary figures, never mistaken for or blended with the main character."
        )
    prompt += _character_clothing_notes(characters)
    prompt += _consistency_suffix(reference_urls)
    return prompt


def _load_characters_with_refs(book):
    blueprint = json.loads(book["blueprint_json"]) if book.get("blueprint_json") else {}
    characters = []
    for c in blueprint.get("characters", []):
        saved = db.get_character_by_name(c.get("name", ""))
        matches = saved and _same_character(saved.get("visual_description", ""), c.get("visual_description", ""))
        characters.append({**c, "s3_key": saved["s3_key"] if matches else None})
    # Unlike characters, locations aren't matched across the library by name — a background is
    # scoped to the one book it belongs to, so its s3_key is baked directly into blueprint_json
    # once generated (see the location-reference loop in _run_pipeline) rather than looked up
    # from a separate table.
    locations = blueprint.get("locations", [])
    return characters, blueprint.get("art_style", ""), blueprint.get("book_title", book.get("title", "")), locations


def _run_pipeline(job_id, params, uploaded_paths, resume_book_id=None):
    book_id = resume_book_id or str(uuid.uuid4())
    image_model = params["image_model"]
    try:
        db.update_job(job_id, status="running", book_id=book_id)

        if resume_book_id:
            existing_book = db.get_book(book_id)
            blueprint = json.loads(existing_book["blueprint_json"])
            title = existing_book["title"]
            db.update_book(book_id, status="running")
            _set_progress(job_id, "blueprint", "Reusing saved story", 1, 1)
        else:
            _set_progress(job_id, "blueprint", "Generating blueprint", 0, 1)
            existing_character_names = [c["name"] for c in db.list_characters()]
            blueprint = claude_client.generate_blueprint(
                params["book_title"],
                params["target_age"],
                params["theme"],
                params["main_characters"],
                params["art_style_preference"],
                params["page_count"],
                params["content_instruction"],
                existing_character_names,
            )
            _set_progress(job_id, "blueprint", "Generating blueprint", 1, 1)

            title = blueprint.get("book_title") or params["book_title"]
            db.create_book(
                book_id, title, params["page_count"], status="running", image_model=image_model,
                target_age=params["target_age"], theme=params["theme"],
                content_instruction=params["content_instruction"],
                main_characters=params["main_characters"],
                art_style_preference=params["art_style_preference"],
                blueprint_json=json.dumps(blueprint),
            )
            # Seeded from Claude's tagline as a starting point — editable afterward from History,
            # and Regenerate Cover always uses whatever is currently saved on the book.
            db.update_book(book_id, subtitle=blueprint.get("tagline", ""))

        book_dir = BOOKS_DIR / book_id
        pages_dir = book_dir / "pages"
        final_dir = book_dir / "final"
        char_refs_dir = book_dir / "character-refs"
        location_refs_dir = book_dir / "location-refs"
        pages_dir.mkdir(parents=True, exist_ok=True)
        final_dir.mkdir(parents=True, exist_ok=True)
        char_refs_dir.mkdir(parents=True, exist_ok=True)
        location_refs_dir.mkdir(parents=True, exist_ok=True)

        art_style = blueprint.get("art_style", "")

        art_style_ref_key = existing_book.get("art_style_ref_key") if resume_book_id else None
        if art_style_ref_key:
            art_style_ref_url = r2_client.presigned_url(art_style_ref_key)
        else:
            art_style_ref_url = None
            _set_progress(job_id, "characters", "Generating art style reference", 0, 1)
            try:
                style_image_url = kie_client.generate_style_reference(
                    image_model, _style_reference_prompt(art_style)
                )
                style_local_path = final_dir / "art-style-reference.jpg"
                _download(style_image_url, style_local_path)
                art_style_ref_key = f"books/{book_id}/art-style-reference.jpg"
                r2_client.upload_file(str(style_local_path), art_style_ref_key)
                db.update_book(book_id, art_style_ref_key=art_style_ref_key)
                art_style_ref_url = r2_client.presigned_url(art_style_ref_key)
            except Exception:
                art_style_ref_key = None

        characters = blueprint.get("characters", [])
        _set_progress(job_id, "characters", "Generating character references", 0, max(len(characters), 1))
        for i, char in enumerate(characters):
            name = char.get("name") or f"Character {i + 1}"

            uploaded_ref_url = None
            if i < len(uploaded_paths):
                key = f"books/{book_id}/character-refs/upload-{i}{Path(uploaded_paths[i]).suffix}"
                r2_client.upload_file(uploaded_paths[i], key)
                uploaded_ref_url = r2_client.presigned_url(key)

            saved = None if uploaded_ref_url else db.get_character_by_name(name)
            if saved and not _same_character(saved.get("visual_description", ""), char.get("visual_description", "")):
                saved = None

            if saved and saved.get("s3_key"):
                char["s3_key"] = saved["s3_key"]
                step_text = f"Reusing saved reference for {name} ({i + 1}/{len(characters)})"
            else:
                try:
                    char_ref_urls = [u for u in [uploaded_ref_url, art_style_ref_url] if u]
                    char_prompt = char.get("image_prompt", "")
                    char_prompt += _character_clothing_notes([char])
                    char_prompt += _consistency_suffix(
                        [art_style_ref_url] if art_style_ref_url else []
                    )
                    kie_url = kie_client.generate_character_reference(
                        image_model, char_prompt, reference_image_urls=char_ref_urls
                    )
                    local_path = char_refs_dir / f"char-{i}.jpg"
                    _download(kie_url, local_path)
                    s3_key = f"books/{book_id}/character-refs/{_slugify(name)}.jpg"
                    r2_client.upload_file(str(local_path), s3_key)
                    char["s3_key"] = s3_key
                    db.upsert_character(
                        name=name,
                        visual_description=char.get("visual_description", ""),
                        personality=char.get("personality", ""),
                        role=char.get("role", ""),
                        image_prompt=char.get("image_prompt", ""),
                        s3_key=s3_key,
                        image_model=image_model,
                    )
                except Exception:
                    char["s3_key"] = None
                step_text = f"Generating character reference {i + 1}/{len(characters)}"

            _set_progress(job_id, "characters", step_text, i + 1, len(characters))

        # One reference image per recurring location (see claude_client's "locations" schema),
        # so pages set in the same place have a visual anchor instead of each independently
        # reinterpreting the text description — confirmed live as inconsistent backgrounds across
        # pages meant to share one scene. Unlike characters, locations aren't matched across the
        # whole library by name (a background belongs to this one book), so there's no DB table to
        # check for a reusable reference — resuming an interrupted run instead relies on the
        # s3_key already being baked into this book's own blueprint_json from the persist below.
        locations = blueprint.get("locations", [])
        _set_progress(job_id, "characters", "Generating location references", 0, max(len(locations), 1))
        for i, loc in enumerate(locations):
            name = loc.get("name") or f"Location {i + 1}"
            if not loc.get("s3_key"):
                try:
                    loc_prompt = loc.get("image_prompt", "") + _consistency_suffix(
                        [art_style_ref_url] if art_style_ref_url else []
                    )
                    kie_url = kie_client.generate_location_reference(
                        image_model, loc_prompt,
                        reference_image_urls=[art_style_ref_url] if art_style_ref_url else [],
                    )
                    local_path = location_refs_dir / f"loc-{i}.jpg"
                    _download(kie_url, local_path)
                    s3_key = f"books/{book_id}/location-refs/{_slugify(name)}.jpg"
                    r2_client.upload_file(str(local_path), s3_key)
                    loc["s3_key"] = s3_key
                except Exception:
                    loc["s3_key"] = None
            _set_progress(job_id, "characters", f"Generating location reference {i + 1}/{len(locations)}", i + 1, len(locations))
        if locations:
            db.update_book(book_id, blueprint_json=json.dumps(blueprint))

        pages = blueprint["pages"]
        # Used for the cover (which isn't tied to one story moment) — capped to a few characters
        # so their references don't get diluted; see _cover_characters.
        cover_characters = _cover_characters(characters)
        cover_reference_urls = _consistency_reference_urls(art_style_ref_url, cover_characters)

        page_prompts = []
        page_reference_urls_per_page = []
        for p in pages:
            page_chars = _page_characters(characters, p)
            location_ref_url = _page_location_ref_url(locations, p)
            page_refs = _consistency_reference_urls(art_style_ref_url, page_chars, location_ref_url)
            names_joined = _character_names_joined(page_chars)
            prompt = p.get("image_prompt", "")
            if names_joined:
                prompt += f" Characters present: {names_joined}."
            prompt += _character_clothing_notes(page_chars)
            prompt += _consistency_suffix(page_refs)
            prompt += _PAGE_NO_TEXT_SUFFIX
            prompt += _PAGE_FULL_BLEED_SUFFIX
            page_prompts.append(prompt)
            page_reference_urls_per_page.append(page_refs)
        total_pages = len(page_prompts)

        existing_pages = {p["page_num"]: p for p in db.get_book(book_id)["pages"]} if resume_book_id else {}

        def _page_already_done(page_num):
            existing = existing_pages.get(page_num)
            return bool(existing) and not existing["is_placeholder"]

        needs_generation = [i for i, p in enumerate(pages) if not _page_already_done(p.get("page_num", i + 1))]

        image_urls = [None] * total_pages
        page_errors = [None] * total_pages
        prompts_to_generate = [page_prompts[i] for i in needs_generation]
        refs_to_generate = [page_reference_urls_per_page[i] for i in needs_generation]

        if prompts_to_generate:
            progress_lock = threading.Lock()
            completed = {"n": 0}

            def on_complete(_sub_index, error):
                with progress_lock:
                    completed["n"] += 1
                    suffix = " (failed after retries, will use a placeholder)" if error else ""
                    _set_progress(
                        job_id, "pages",
                        f"Generating page {completed['n']}/{len(prompts_to_generate)}{suffix}",
                        completed["n"], len(prompts_to_generate),
                    )

            _set_progress(job_id, "pages", f"Generating page 0/{len(prompts_to_generate)}", 0, len(prompts_to_generate))
            partial_urls, partial_errors = kie_client.generate_pages_concurrent(
                image_model, prompts_to_generate, reference_image_urls_per_page=refs_to_generate,
                max_workers=5, on_complete=on_complete,
            )
            for sub_i, real_i in enumerate(needs_generation):
                image_urls[real_i] = partial_urls[sub_i]
                page_errors[real_i] = partial_errors[sub_i]
        else:
            _set_progress(job_id, "pages", "All pages already illustrated", 1, 1)

        pages_for_pdf = []
        placeholder_pages = []
        _set_progress(job_id, "page_uploads", "Uploading pages to storage", 0, total_pages)
        for idx, page in enumerate(pages):
            page_num = page.get("page_num", idx + 1)
            local_path = pages_dir / f"page-{page_num:03d}.jpg"
            existing = existing_pages.get(page_num)

            if idx not in needs_generation and existing:
                if not local_path.exists():
                    _download(r2_client.presigned_url(existing["s3_key"]), local_path)
                pages_for_pdf.append({
                    "page_num": page_num,
                    "image_path": str(local_path),
                    "story_text": existing.get("story_text") or page.get("story_text", ""),
                })
                _set_progress(job_id, "page_uploads", f"Reusing page {idx + 1}/{total_pages}", idx + 1, total_pages)
                continue

            is_placeholder = not image_urls[idx]

            if image_urls[idx]:
                try:
                    _download(image_urls[idx], local_path)
                except Exception:
                    is_placeholder = True

            if is_placeholder:
                _build_placeholder_image(local_path, f"Illustration unavailable for page {page_num}")
                placeholder_pages.append(page_num)

            r2_key = f"books/{book_id}/pages/page-{page_num:03d}.jpg"
            s3_key = r2_client.upload_file(str(local_path), r2_key)
            page_fields = dict(
                s3_key=s3_key, story_text=page.get("story_text", ""),
                image_prompt=page_prompts[idx], is_placeholder=is_placeholder,
                characters_on_page=json.dumps(page.get("characters_on_page") or []),
                location=page.get("location"),
            )
            if existing:
                db.update_page(book_id, page_num, **page_fields)
            else:
                db.add_page(book_id, page_num, **page_fields)
            pages_for_pdf.append({
                "page_num": page_num,
                "image_path": str(local_path),
                "story_text": page.get("story_text", ""),
            })
            _set_progress(job_id, "page_uploads", f"Uploading page {idx + 1}/{total_pages}", idx + 1, total_pages)

        pages_for_pdf.sort(key=lambda p: p["page_num"])
        slug = _slugify(title)
        pdf_path = final_dir / f"{slug}.pdf"

        _set_progress(job_id, "pdf", "Compiling PDF", 0, 1)
        pdf_builder.build_pdf(pages_for_pdf, pdf_path)
        _set_progress(job_id, "pdf", "Compiling PDF", 1, 1)

        _set_progress(job_id, "cover", "Generating cover art", 0, 1)
        cover_key = existing_book.get("cover_key") if resume_book_id else None
        if not cover_key:
            try:
                # Re-fetch rather than trust a locally-held title/tagline: covers the resume path
                # (subtitle may have been edited from History since the original run) and the
                # fresh-generation path (subtitle was just seeded from blueprint's tagline above).
                current_subtitle = (db.get_book(book_id) or {}).get("subtitle") or ""
                cover_prompt = _build_cover_prompt(
                    title, current_subtitle, params["theme"], cover_characters, cover_reference_urls
                )
                cover_image_url = kie_client.generate_cover_image(
                    image_model, cover_prompt, reference_image_urls=cover_reference_urls
                )
                cover_image_path = final_dir / "cover-art.jpg"
                _download(cover_image_url, cover_image_path)

                cover_pdf_path = final_dir / f"{slug}-cover.pdf"
                cover_builder.build_cover_pdf(str(cover_image_path), params["page_count"], cover_pdf_path)
                cover_key = f"books/{book_id}/final/{slug}-cover.pdf"
                r2_client.upload_file(str(cover_pdf_path), cover_key)
            except Exception:
                cover_key = None
        _set_progress(job_id, "cover", "Generating cover art", 1, 1)

        _set_progress(job_id, "final_upload", "Uploading final PDF", 0, 1)
        pdf_key = f"books/{book_id}/final/{slug}.pdf"
        r2_client.upload_file(str(pdf_path), pdf_key)
        _set_progress(job_id, "final_upload", "Uploading final PDF", 1, 1)

        pdf_url = _download_path(book_id)
        cover_url = _cover_path(book_id) if cover_key else None
        warning = None
        if placeholder_pages:
            page_list = ", ".join(str(p) for p in placeholder_pages)
            warning = (
                f"{len(placeholder_pages)} page(s) could not be illustrated after retries "
                f"(page {page_list}) — a placeholder was used instead."
            )
        db.update_book(book_id, pdf_key=pdf_key, cover_key=cover_key, status="done")
        db.update_job(job_id, status="done", book_id=book_id, pdf_url=pdf_url, step="Done", current=1, total=1)
        _push(job_id, {
            "done": True, "pdf_url": pdf_url, "cover_url": cover_url, "book_id": book_id, "warning": warning
        })

    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        try:
            db.update_book(book_id, status="error")
        except Exception:
            pass
        _push(job_id, {"done": True, "error": str(e)})
    finally:
        _cleanup_queue(job_id)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "1.0.0"})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(settings.status())


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True, silent=True) or {}
    updated = []
    for key in settings.KEYS:
        if key in data and data[key]:
            settings.set(key, data[key].strip())
            updated.append(key)
    return jsonify({"updated": updated, "settings": settings.status()})


@app.route("/api/image-models")
def image_models():
    return jsonify({"models": kie_client.MODELS, "default": kie_client.DEFAULT_MODEL})


@app.route("/api/books/auto-generate", methods=["POST"])
def auto_generate_fields():
    data = request.get_json(force=True, silent=True) or {}
    idea = (data.get("idea") or "").strip()
    target_age = data.get("target_age", "4-8")

    if not idea:
        return jsonify({"error": "idea is required"}), 400

    try:
        existing_character_names = [c["name"] for c in db.list_characters()]
        fields = gemini_client.generate_book_fields(idea, target_age, existing_character_names)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify(fields)


@app.route("/api/books/generate", methods=["POST"])
def generate_book():
    book_title = request.form.get("book_title", "").strip()
    target_age = request.form.get("target_age", "4-8")
    theme = request.form.get("theme", "").strip()
    content_instruction = request.form.get("content_instruction", "").strip()
    main_characters = request.form.get("main_characters", "").strip()
    art_style_preference = request.form.get("art_style_preference", "").strip()
    image_model = request.form.get("image_model", kie_client.DEFAULT_MODEL).strip()

    try:
        page_count = int(request.form.get("page_count", 75))
    except ValueError:
        page_count = 75
    page_count = max(1, min(150, page_count))

    if not book_title or not theme:
        return jsonify({"error": "book_title and theme are required"}), 400

    if image_model not in kie_client.MODELS:
        return jsonify({"error": f"Unknown image_model: {image_model}"}), 400

    job_id = str(uuid.uuid4())
    db.create_job(job_id)
    _get_queue(job_id)

    uploaded_paths = []
    files = request.files.getlist("character_images[]") + request.files.getlist("character_images")
    for f in files:
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1] or ".jpg"
            dest = UPLOADS_DIR / f"{job_id}-{len(uploaded_paths)}{ext}"
            f.save(dest)
            uploaded_paths.append(str(dest))

    params = dict(
        book_title=book_title,
        target_age=target_age,
        theme=theme,
        content_instruction=content_instruction,
        main_characters=main_characters,
        art_style_preference=art_style_preference,
        page_count=page_count,
        image_model=image_model,
    )

    thread = threading.Thread(target=_run_pipeline, args=(job_id, params, uploaded_paths), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/books/status/<job_id>")
def job_status(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    result = {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": {"step": job["step"], "current": job["current"], "total": job["total"]},
    }
    if job.get("book_id"):
        result["book_id"] = job["book_id"]
    if job.get("pdf_url"):
        result["pdf_url"] = job["pdf_url"]
    if job.get("error"):
        result["error"] = job["error"]
    return jsonify(result)


@app.route("/api/books/<book_id>/job")
def book_job(book_id):
    """Lets a fresh page load (browser reopened after being closed mid-generation) find and
    reconnect to whatever job is/was running for this book — generation itself runs as a
    detached background thread and was never tied to any one browser connection, this just gives
    the UI a way to find it again without the original job_id."""
    job = db.get_latest_job_for_book(book_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "job_id": job["job_id"],
        "status": job["status"],
        "step": job["step"],
        "current": job["current"],
        "total": job["total"],
        "error": job.get("error"),
    })


@app.route("/api/books/stream/<job_id>")
def job_stream(job_id):
    def generate():
        job = db.get_job(job_id)
        if not job:
            yield f"data: {json.dumps({'error': 'not found'})}\n\n"
            return
        if job["status"] in ("done", "error"):
            event = {"done": True}
            if job.get("pdf_url"):
                event["pdf_url"] = job["pdf_url"]
            if job.get("book_id"):
                event["book_id"] = job["book_id"]
            if job.get("error"):
                event["error"] = job["error"]
            yield f"data: {json.dumps(event)}\n\n"
            return

        q = _get_queue(job_id)
        while True:
            event = q.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("done"):
                break

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def _serialize_book(book):
    book = dict(book)
    book["pdf_url"] = _download_path(book["book_id"]) if book.get("pdf_key") else None
    book.pop("pdf_key", None)
    book["cover_url"] = _cover_path(book["book_id"]) if book.get("cover_key") else None
    book.pop("cover_key", None)
    book["can_continue"] = book["status"] == "error" and bool(book.get("blueprint_json"))
    book["can_regenerate_cover"] = book["status"] in ("done", "error") and bool(book.get("blueprint_json"))
    book["blueprint_available"] = bool(book.get("blueprint_json"))
    book.pop("blueprint_json", None)
    book.pop("art_style_ref_key", None)
    book["cover_dimensions"] = cover_builder.calculate_dimensions(book["page_count"])
    if "pages" in book:
        pages = []
        for p in book["pages"]:
            p = dict(p)
            p["s3_url"] = _page_image_path(book["book_id"], p["page_num"]) if p.get("s3_key") else None
            p.pop("s3_key", None)
            p["is_placeholder"] = bool(p.get("is_placeholder"))
            p["characters_on_page"] = json.loads(p["characters_on_page"]) if p.get("characters_on_page") else []
            pages.append(p)
        book["pages"] = pages
    return book


@app.route("/api/books")
def list_books():
    return jsonify([_serialize_book(b) for b in db.list_books()])


@app.route("/api/books/<book_id>")
def get_book(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_book(book))


@app.route("/api/books/<book_id>/edit", methods=["POST"])
def edit_book(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    fields = {}
    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "Title cannot be empty"}), 400
        fields["title"] = title
    if "subtitle" in data:
        fields["subtitle"] = (data.get("subtitle") or "").strip()
    if "amazon_description" in data:
        fields["amazon_description"] = (data.get("amazon_description") or "").strip()

    if fields:
        db.update_book(book_id, **fields)

    return jsonify(_serialize_book(db.get_book(book_id)))


def _story_outline(book):
    """The saved blueprint's page-by-page story_text IS the book's story outline — already
    persisted at generation time (blueprint_json), nothing new to store. Used as grounding
    context for title suggestions and the Amazon description so they reflect what the story
    actually contains rather than just the pre-generation theme/prompt."""
    blueprint = json.loads(book["blueprint_json"]) if book.get("blueprint_json") else {}
    pages = blueprint.get("pages", [])
    lines = [f"Page {p.get('page_num')}: {p['story_text']}" for p in pages if p.get("story_text")]
    return "\n".join(lines)


@app.route("/api/books/<book_id>/title-ideas", methods=["POST"])
def book_title_ideas(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    if not book.get("blueprint_json"):
        return jsonify({"error": "No saved story data for this book"}), 400

    story_outline = _story_outline(book)
    if not story_outline:
        return jsonify({"error": "This book has no story text saved yet"}), 400

    try:
        titles = gemini_client.generate_title_ideas(
            story_outline, book.get("theme") or "", book["title"], book.get("target_age") or "4-8"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"titles": titles})


@app.route("/api/books/<book_id>/amazon-description", methods=["POST"])
def book_amazon_description(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    if not book.get("blueprint_json"):
        return jsonify({"error": "No saved story data for this book"}), 400

    story_outline = _story_outline(book)
    if not story_outline:
        return jsonify({"error": "This book has no story text saved yet"}), 400

    try:
        description = gemini_client.generate_amazon_description(
            story_outline, book.get("theme") or "", book["title"], book.get("subtitle") or "",
            book.get("target_age") or "4-8",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    db.update_book(book_id, amazon_description=description)
    return jsonify({"amazon_description": description})


@app.route("/api/books/<book_id>/download")
def download_book(book_id):
    book = db.get_book(book_id)
    if not book or not book.get("pdf_key"):
        return jsonify({"error": "not found"}), 404
    return redirect(r2_client.presigned_url(book["pdf_key"]))


@app.route("/api/books/<book_id>/cover")
def download_cover(book_id):
    book = db.get_book(book_id)
    if not book or not book.get("cover_key"):
        return jsonify({"error": "not found"}), 404
    return redirect(r2_client.presigned_url(book["cover_key"]))


def _regenerate_cover_job(job_id, book_id, image_model, cover_prompt, reference_urls, title, page_count):
    try:
        db.update_job(job_id, status="running", book_id=book_id)
        _push(job_id, {"step": "Generating cover art", "pct": 15})
        cover_image_url = kie_client.generate_cover_image(
            image_model, cover_prompt, reference_image_urls=reference_urls
        )

        _push(job_id, {"step": "Downloading cover art", "pct": 50})
        book_dir = BOOKS_DIR / book_id
        final_dir = book_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        cover_image_path = final_dir / "cover-art.jpg"
        _download(cover_image_url, cover_image_path)

        _push(job_id, {"step": "Building cover PDF", "pct": 75})
        slug = _slugify(title)
        cover_pdf_path = final_dir / f"{slug}-cover.pdf"
        cover_builder.build_cover_pdf(str(cover_image_path), page_count, cover_pdf_path)

        _push(job_id, {"step": "Uploading cover", "pct": 90})
        cover_key = f"books/{book_id}/final/{slug}-cover.pdf"
        r2_client.upload_file(str(cover_pdf_path), cover_key)
        db.update_book(book_id, cover_key=cover_key)

        db.update_job(job_id, status="done", step="Done", current=1, total=1)
        _push(job_id, {"done": True, "book_id": book_id, "cover_url": _cover_path(book_id)})
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        _push(job_id, {"done": True, "error": str(e)})
    finally:
        _cleanup_queue(job_id)


@app.route("/api/books/<book_id>/cover/regenerate", methods=["POST"])
def regenerate_cover(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    if not book.get("blueprint_json"):
        return jsonify({"error": "No saved story data for this book"}), 400

    image_model = book.get("image_model") or kie_client.DEFAULT_MODEL
    # Use the book's own title/subtitle (editable from History), not the blueprint's frozen
    # copy — this is what lets an edited title/subtitle actually take effect on regeneration.
    title = book["title"]
    subtitle = book.get("subtitle") or ""
    characters, _art_style, _blueprint_title, _locations = _load_characters_with_refs(book)
    art_style_ref_url = r2_client.presigned_url(book["art_style_ref_key"]) if book.get("art_style_ref_key") else None
    cover_characters = _cover_characters(characters)
    reference_urls = _consistency_reference_urls(art_style_ref_url, cover_characters)
    cover_prompt = _build_cover_prompt(title, subtitle, book.get("theme") or "", cover_characters, reference_urls)

    job_id = str(uuid.uuid4())
    db.create_job(job_id)
    _get_queue(job_id)

    thread = threading.Thread(
        target=_regenerate_cover_job,
        args=(job_id, book_id, image_model, cover_prompt, reference_urls, title, book["page_count"]),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/books/<book_id>/pages/<int:page_num>/image")
def page_image(book_id, page_num):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    page = next((p for p in book["pages"] if p["page_num"] == page_num), None)
    if not page or not page.get("s3_key"):
        return jsonify({"error": "not found"}), 404
    return redirect(r2_client.presigned_url(page["s3_key"]))


def _rebuild_book_pdf(book, on_progress=None):
    book_dir = BOOKS_DIR / book["book_id"]
    pages_dir = book_dir / "pages"
    final_dir = book_dir / "final"
    pages_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(book["pages"], key=lambda x: x["page_num"])
    pages_for_pdf = []
    for i, p in enumerate(sorted_pages):
        local_path = pages_dir / f"page-{p['page_num']:03d}.jpg"
        if not local_path.exists() and p.get("s3_key"):
            _download(r2_client.presigned_url(p["s3_key"]), local_path)
        pages_for_pdf.append({
            "page_num": p["page_num"],
            "image_path": str(local_path),
            "story_text": p.get("story_text", ""),
        })
        if on_progress:
            on_progress(i + 1, len(sorted_pages))

    slug = _slugify(book["title"])
    pdf_path = final_dir / f"{slug}.pdf"
    pdf_builder.build_pdf(pages_for_pdf, pdf_path)

    pdf_key = f"books/{book['book_id']}/final/{slug}.pdf"
    r2_client.upload_file(str(pdf_path), pdf_key)
    db.update_book(book["book_id"], pdf_key=pdf_key)


def _regenerate_page_job(job_id, book_id, page_num, image_model, prompt, reference_urls, persist_prompt=True):
    """Regenerates a single page's image only. Does NOT rebuild the PDF — with many pages queued
    for regeneration, rebuilding after every single one would be slow and redundant (and racy if
    two regenerates run concurrently). The PDF is rebuilt on demand via a separate job/button once
    the user is happy with all the images they've regenerated.

    persist_prompt=False is used for edit-mode regenerations, where `prompt` is a short one-off
    instruction ("make the sky orange") rather than the page's full scene description — saving it
    over image_prompt would destroy the description needed if this page ever needs a from-scratch
    regeneration later (e.g. if edits stop being enough and the page needs redrawing)."""
    try:
        db.update_job(job_id, status="running", book_id=book_id)
        _push(job_id, {"step": "Generating image", "pct": 20})
        image_url = kie_client.generate_page_image(image_model, prompt, reference_image_urls=reference_urls)

        _push(job_id, {"step": "Downloading image", "pct": 60})
        book_dir = BOOKS_DIR / book_id
        pages_dir = book_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        local_path = pages_dir / f"page-{page_num:03d}.jpg"
        _download(image_url, local_path)

        _push(job_id, {"step": "Uploading image", "pct": 85})
        r2_key = f"books/{book_id}/pages/page-{page_num:03d}.jpg"
        r2_client.upload_file(str(local_path), r2_key)
        page_fields = {"s3_key": r2_key, "is_placeholder": 0}
        if persist_prompt:
            page_fields["image_prompt"] = prompt
        db.update_page(book_id, page_num, **page_fields)

        db.update_job(job_id, status="done", step="Done", current=1, total=1)
        _push(job_id, {"done": True, "book_id": book_id, "s3_url": _page_image_path(book_id, page_num)})
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        _push(job_id, {"done": True, "error": str(e)})
    finally:
        _cleanup_queue(job_id)


def _rebuild_pdf_job(job_id, book_id):
    try:
        db.update_job(job_id, status="running", book_id=book_id)
        book = db.get_book(book_id)
        if not book:
            raise RuntimeError("Book not found")
        if not book["pages"]:
            raise RuntimeError("This book has no pages yet")

        def on_progress(current, total):
            pct = 10 + round(70 * current / total) if total else 10
            _push(job_id, {"step": f"Collecting page images ({current}/{total})", "pct": pct})

        _push(job_id, {"step": "Starting PDF rebuild", "pct": 5})
        _run_with_timeout(lambda: _rebuild_book_pdf(book, on_progress=on_progress), timeout_seconds=600)

        pdf_url = _download_path(book_id)
        db.update_job(job_id, status="done", step="Done", current=1, total=1, pdf_url=pdf_url)
        _push(job_id, {"done": True, "book_id": book_id, "pdf_url": pdf_url})
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        _push(job_id, {"done": True, "error": str(e)})
    finally:
        _cleanup_queue(job_id)


@app.route("/api/books/<book_id>/rebuild-pdf", methods=["POST"])
def rebuild_pdf(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    if not book["pages"]:
        return jsonify({"error": "This book has no pages yet"}), 400

    job_id = str(uuid.uuid4())
    db.create_job(job_id)
    _get_queue(job_id)

    thread = threading.Thread(target=_rebuild_pdf_job, args=(job_id, book_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/books/<book_id>/pages/<int:page_num>/regenerate", methods=["POST"])
def regenerate_page(book_id, page_num):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    page = next((p for p in book["pages"] if p["page_num"] == page_num), None)
    if not page:
        return jsonify({"error": "page not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    image_model = data.get("image_model") or book.get("image_model") or kie_client.DEFAULT_MODEL
    edit_instruction = (data.get("prompt") or "").strip()
    from_scratch = bool(data.get("from_scratch"))

    if image_model not in kie_client.MODELS:
        return jsonify({"error": f"Unknown image_model: {image_model}"}), 400

    if page.get("s3_key") and not page.get("is_placeholder") and not from_scratch:
        # Edit mode: use the page's current image as the reference and apply the instruction to
        # it, rather than regenerating the scene from scratch — keeps everything about the page
        # that wasn't mentioned in the instruction unchanged, since the model is editing the
        # actual pixels instead of reinterpreting a text description.
        if not edit_instruction:
            return jsonify({"error": "Describe the edit you want, e.g. \"make the sky orange\""}), 400
        prompt = edit_instruction
        reference_urls = [r2_client.presigned_url(page["s3_key"])]
        persist_prompt = False
    else:
        # from_scratch (or no successful image to edit yet) — regenerate from the character/style
        # references instead of editing the current pixels. This is what recovers a page whose
        # character already drifted off-model: editing only ever looks at the page's own (already
        # wrong) image, so it can never pull the character back in line with its reference — only
        # a fresh generation conditioned on the actual reference images can.
        base_prompt = page.get("image_prompt") or ""
        if from_scratch and edit_instruction and base_prompt:
            prompt = f"{base_prompt}\n\nAdditional instruction: {edit_instruction}"
        else:
            prompt = edit_instruction or base_prompt
        if not prompt:
            return jsonify({"error": "No prompt available for this page; provide one"}), 400
        characters, _art_style, _title, locations = _load_characters_with_refs(book)
        art_style_ref_url = r2_client.presigned_url(book["art_style_ref_key"]) if book.get("art_style_ref_key") else None
        characters_on_page = json.loads(page["characters_on_page"]) if page.get("characters_on_page") else None
        page_chars = _page_characters(characters, {"characters_on_page": characters_on_page})
        location_ref_url = _page_location_ref_url(locations, page)
        reference_urls = _consistency_reference_urls(art_style_ref_url, page_chars, location_ref_url)
        clothing_notes = _character_clothing_notes(page_chars)
        if clothing_notes and clothing_notes not in prompt:
            prompt += clothing_notes
        if _PAGE_NO_TEXT_SUFFIX not in prompt:
            prompt += _PAGE_NO_TEXT_SUFFIX
        if _PAGE_FULL_BLEED_SUFFIX not in prompt:
            prompt += _PAGE_FULL_BLEED_SUFFIX
        persist_prompt = True

    job_id = str(uuid.uuid4())
    db.create_job(job_id)
    _get_queue(job_id)

    thread = threading.Thread(
        target=_regenerate_page_job,
        args=(job_id, book_id, page_num, image_model, prompt, reference_urls),
        kwargs={"persist_prompt": persist_prompt},
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/books/<book_id>", methods=["DELETE"])
def delete_book(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    try:
        r2_client.delete_prefix(f"books/{book_id}/")
    except Exception:
        pass
    book_dir = BOOKS_DIR / book_id
    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
    db.delete_book(book_id)
    return jsonify({"status": "deleted"})


@app.route("/api/books/<book_id>/continue", methods=["POST"])
def continue_book(book_id):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    if book["status"] != "error":
        return jsonify({"error": "Only books that failed can be continued"}), 400
    if not book.get("blueprint_json"):
        return jsonify({"error": "No saved story data for this book — generate a new one instead"}), 400

    job_id = str(uuid.uuid4())
    db.create_job(job_id)
    db.update_job(job_id, book_id=book_id)
    _get_queue(job_id)

    params = dict(
        book_title=book["title"],
        target_age=book.get("target_age") or "4-8",
        theme=book.get("theme") or "",
        content_instruction=book.get("content_instruction") or "",
        main_characters=book.get("main_characters") or "",
        art_style_preference=book.get("art_style_preference") or "",
        page_count=book["page_count"],
        image_model=book.get("image_model") or kie_client.DEFAULT_MODEL,
    )

    thread = threading.Thread(
        target=_run_pipeline, args=(job_id, params, []), kwargs={"resume_book_id": book_id}, daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued", "book_id": book_id})


def _serialize_character(char):
    char = dict(char)
    char["image_url"] = f"/api/characters/{char['id']}/image" if char.get("s3_key") else None
    char.pop("s3_key", None)
    char.pop("name_key", None)
    return char


@app.route("/api/characters")
def list_characters():
    return jsonify([_serialize_character(c) for c in db.list_characters()])


@app.route("/api/characters/<int:character_id>/image")
def character_image(character_id):
    char = db.get_character(character_id)
    if not char or not char.get("s3_key"):
        return jsonify({"error": "not found"}), 404
    return redirect(r2_client.presigned_url(char["s3_key"]))


@app.route("/api/characters/<int:character_id>", methods=["DELETE"])
def delete_character(character_id):
    char = db.get_character(character_id)
    if not char:
        return jsonify({"error": "not found"}), 404
    if char.get("s3_key"):
        try:
            r2_client.delete_prefix(char["s3_key"])
        except Exception:
            pass
    db.delete_character(character_id)
    return jsonify({"status": "deleted"})


if __name__ == "__main__":
    print(f"Children's Book Generator running at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
