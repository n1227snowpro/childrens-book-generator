from dotenv import load_dotenv

load_dotenv()

import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from queue import Queue

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context
from flask_cors import CORS

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


def _download(url, dest_path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def _download_path(book_id):
    return f"/api/books/{book_id}/download"


def _cover_path(book_id):
    return f"/api/books/{book_id}/cover"


def _page_image_path(book_id, page_num):
    return f"/api/books/{book_id}/pages/{page_num}/image"


def _run_pipeline(job_id, params, uploaded_paths):
    book_id = str(uuid.uuid4())
    image_model = params["image_model"]
    try:
        db.update_job(job_id, status="running")
        _set_progress(job_id, "blueprint", "Generating blueprint", 0, 1)

        blueprint = claude_client.generate_blueprint(
            params["book_title"],
            params["target_age"],
            params["theme"],
            params["main_characters"],
            params["art_style_preference"],
            params["page_count"],
            params["content_instruction"],
        )
        _set_progress(job_id, "blueprint", "Generating blueprint", 1, 1)

        title = blueprint.get("book_title") or params["book_title"]
        db.create_book(book_id, title, params["page_count"], status="running", image_model=image_model)

        book_dir = BOOKS_DIR / book_id
        pages_dir = book_dir / "pages"
        final_dir = book_dir / "final"
        char_refs_dir = book_dir / "character-refs"
        pages_dir.mkdir(parents=True, exist_ok=True)
        final_dir.mkdir(parents=True, exist_ok=True)
        char_refs_dir.mkdir(parents=True, exist_ok=True)

        characters = blueprint.get("characters", [])
        char_visual_descriptions = []
        _set_progress(job_id, "characters", "Generating character references", 0, max(len(characters), 1))
        for i, char in enumerate(characters):
            name = char.get("name") or f"Character {i + 1}"

            uploaded_ref_url = None
            if i < len(uploaded_paths):
                key = f"books/{book_id}/character-refs/upload-{i}{Path(uploaded_paths[i]).suffix}"
                r2_client.upload_file(uploaded_paths[i], key)
                uploaded_ref_url = r2_client.presigned_url(key)

            saved = None if uploaded_ref_url else db.get_character_by_name(name)

            if saved and saved.get("s3_key"):
                char["s3_key"] = saved["s3_key"]
                step_text = f"Reusing saved reference for {name} ({i + 1}/{len(characters)})"
            else:
                try:
                    kie_url = kie_client.generate_character_reference(
                        image_model, char.get("image_prompt", ""), reference_image_url=uploaded_ref_url
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

            char_visual_descriptions.append(f"{char.get('name', '')}: {char.get('visual_description', '')}")
            _set_progress(job_id, "characters", step_text, i + 1, len(characters))

        art_style = blueprint.get("art_style", "")
        char_desc_joined = "; ".join(char_visual_descriptions)
        pages = blueprint["pages"]
        page_prompts = [f"{art_style} {p.get('image_prompt', '')} Characters: {char_desc_joined}" for p in pages]
        total_pages = len(page_prompts)

        progress_lock = threading.Lock()
        completed = {"n": 0}

        def on_complete(_index):
            with progress_lock:
                completed["n"] += 1
                _set_progress(
                    job_id, "pages", f"Generating page {completed['n']}/{total_pages}",
                    completed["n"], total_pages,
                )

        _set_progress(job_id, "pages", f"Generating page 0/{total_pages}", 0, total_pages)
        image_urls = kie_client.generate_pages_concurrent(
            image_model, page_prompts, max_workers=5, on_complete=on_complete
        )

        pages_for_pdf = []
        _set_progress(job_id, "page_uploads", "Uploading pages to storage", 0, total_pages)
        for idx, page in enumerate(pages):
            page_num = page.get("page_num", idx + 1)
            local_path = pages_dir / f"page-{page_num:03d}.jpg"
            _download(image_urls[idx], local_path)
            r2_key = f"books/{book_id}/pages/page-{page_num:03d}.jpg"
            s3_key = r2_client.upload_file(str(local_path), r2_key)
            db.add_page(book_id, page_num, s3_key, page.get("story_text", ""))
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
        cover_key = None
        try:
            primary_char = characters[0] if characters else None
            cover_ref_url = None
            if primary_char and primary_char.get("s3_key"):
                cover_ref_url = r2_client.presigned_url(primary_char["s3_key"])

            cover_prompt = (
                f"{art_style} Wraparound children's book cover illustration for '{title}'. "
                f"Theme: {params['theme']}. Featuring: {char_desc_joined}. "
                "Leave open, uncluttered space on the right-hand side for a title."
            )
            cover_image_url = kie_client.generate_cover_image(
                image_model, cover_prompt, reference_image_url=cover_ref_url
            )
            cover_image_path = final_dir / "cover-art.jpg"
            _download(cover_image_url, cover_image_path)

            cover_pdf_path = final_dir / f"{slug}-cover.pdf"
            cover_builder.build_cover_pdf(
                str(cover_image_path), title, title, params["page_count"], cover_pdf_path
            )
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
        db.update_book(book_id, pdf_key=pdf_key, cover_key=cover_key, status="done")
        db.update_job(job_id, status="done", book_id=book_id, pdf_url=pdf_url, step="Done", current=1, total=1)
        _push(job_id, {"done": True, "pdf_url": pdf_url, "cover_url": cover_url, "book_id": book_id})

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
        fields = gemini_client.generate_book_fields(idea, target_age)
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
    book["cover_dimensions"] = cover_builder.calculate_dimensions(book["page_count"])
    if "pages" in book:
        pages = []
        for p in book["pages"]:
            p = dict(p)
            p["s3_url"] = _page_image_path(book["book_id"], p["page_num"]) if p.get("s3_key") else None
            p.pop("s3_key", None)
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


@app.route("/api/books/<book_id>/pages/<int:page_num>/image")
def page_image(book_id, page_num):
    book = db.get_book(book_id)
    if not book:
        return jsonify({"error": "not found"}), 404
    page = next((p for p in book["pages"] if p["page_num"] == page_num), None)
    if not page or not page.get("s3_key"):
        return jsonify({"error": "not found"}), 404
    return redirect(r2_client.presigned_url(page["s3_key"]))


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
