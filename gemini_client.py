import json

import requests

import settings

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.5-flash"

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "book_title": {"type": "STRING"},
        "theme": {"type": "STRING"},
        "content_instruction": {"type": "STRING"},
        "main_characters": {"type": "STRING"},
        "art_style_preference": {"type": "STRING"},
    },
    "required": ["book_title", "theme", "content_instruction", "main_characters", "art_style_preference"],
}


def _build_prompt(idea, target_age):
    return f"""You are helping an author brainstorm a children's picture book from a rough idea.

Book idea: {idea}
Target age: {target_age}

Generate these five fields:
- book_title: a short, catchy, age-appropriate title
- theme: one or two sentences describing the book's theme or central message
- content_instruction: a detailed paragraph describing the story's plot, key scenes/beats in order, \
tone, and any lesson or message it should convey — detailed enough that a writer could draft the \
full story from it alone
- main_characters: 1-3 main characters as "Name — short visual and personality description", \
separated by semicolons
- art_style_preference: a vivid description of the illustration art style (medium, palette, mood)

Return ONLY a JSON object with exactly these five fields, values in plain text (no markdown)."""


TITLE_IDEAS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "titles": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "subtitle": {"type": "STRING"},
                },
                "required": ["title", "subtitle"],
            },
        },
    },
    "required": ["titles"],
}


def _build_title_prompt(story_outline, theme, current_title, target_age, count):
    return f"""You are helping an author choose a title for a children's picture book.

Current title: {current_title}
Target age: {target_age}
Theme: {theme}

Full story, page by page:
{story_outline}

Generate {count} alternative title ideas for this book, each paired with a short subtitle. Base \
them on what actually happens in the story above — evocative of the specific plot, characters, \
and emotional arc, not generic. Avoid reusing the current title. Vary the style across the ideas \
(e.g., some whimsical, some literal/descriptive, some emotionally resonant).

Return ONLY a JSON object with a "titles" array of exactly {count} {{"title": ..., "subtitle": ...}} objects."""


def generate_title_ideas(story_outline, theme, current_title, target_age="4-8", count=5):
    api_key = settings.get("GEMINI_API_KEY")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": _build_title_prompt(story_outline, theme, current_title, target_age, count)}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": TITLE_IDEAS_SCHEMA,
            "temperature": 1.0,
        },
    }
    resp = requests.post(
        f"{BASE_URL}/models/{MODEL}:generateContent",
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {message}")
    body = resp.json()

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        raise RuntimeError(f"Gemini candidate had no content parts: {candidates[0]}")

    return json.loads(parts[0]["text"]).get("titles", [])


AMAZON_DESCRIPTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "description": {"type": "STRING"},
    },
    "required": ["description"],
}


def _build_amazon_description_prompt(story_outline, theme, title, subtitle, target_age):
    subtitle_line = f"\nSubtitle: {subtitle}" if subtitle else ""
    return f"""You are writing an Amazon book listing description for a children's picture book, \
in the style of compelling back-cover copy that helps parents and gift-buyers decide to purchase it.

Title: {title}{subtitle_line}
Target age: {target_age}
Theme: {theme}

Full story, page by page:
{story_outline}

Write a warm, engaging Amazon product description (3-5 short paragraphs) that:
- Hooks the reader in the first line
- Summarizes the story's premise and emotional journey WITHOUT spoiling the ending
- Highlights the book's theme or lesson and who it's perfect for (age range, occasions like \
bedtime or gifts)
- Ends with a warm call-to-action inviting the reader to buy the book
- Uses plain text only — no markdown, no headers, no bullet points, just natural paragraphs \
separated by blank lines, ready to paste directly into Amazon's description field

Return ONLY a JSON object with a single "description" field containing the full text."""


def generate_amazon_description(story_outline, theme, title, subtitle, target_age="4-8"):
    api_key = settings.get("GEMINI_API_KEY")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": _build_amazon_description_prompt(story_outline, theme, title, subtitle, target_age)}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": AMAZON_DESCRIPTION_SCHEMA,
            "temperature": 0.85,
        },
    }
    resp = requests.post(
        f"{BASE_URL}/models/{MODEL}:generateContent",
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {message}")
    body = resp.json()

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        raise RuntimeError(f"Gemini candidate had no content parts: {candidates[0]}")

    return json.loads(parts[0]["text"]).get("description", "")


def generate_book_fields(idea, target_age="4-8"):
    api_key = settings.get("GEMINI_API_KEY")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": _build_prompt(idea, target_age)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.9,
        },
    }
    resp = requests.post(
        f"{BASE_URL}/models/{MODEL}:generateContent",
        params={"key": api_key},
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {message}")
    body = resp.json()

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        raise RuntimeError(f"Gemini candidate had no content parts: {candidates[0]}")

    return json.loads(parts[0]["text"])
