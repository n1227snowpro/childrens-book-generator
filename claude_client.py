import json

import anthropic

import settings

MODEL = "claude-opus-4-5"


def _client():
    return anthropic.Anthropic(api_key=settings.get("ANTHROPIC_API_KEY"))


def _build_prompt(
    book_title, target_age, theme, main_characters, art_style_preference, page_count,
    content_instruction="", existing_character_names=None,
):
    if page_count >= 5:
        intro = max(1, round(page_count * 0.10))
        rising = max(1, round(page_count * 0.42))
        climax = max(1, round(page_count * 0.20))
        resolution = max(1, round(page_count * 0.18))
        epilogue = max(0, page_count - (intro + rising + climax + resolution))
        arc_guidance = f"""Story arc proportions across the {page_count} pages (approximate):
- Introduction: ~{intro} pages
- Rising action: ~{rising} pages
- Climax: ~{climax} pages
- Resolution: ~{resolution} pages
- Epilogue: ~{epilogue} pages"""
    else:
        arc_guidance = (
            f"This is a short test run of only {page_count} page(s). Compress a complete, "
            "self-contained story beat into the available page(s) rather than following a "
            "full story arc."
        )

    content_block = (
        f"\nDetailed content instructions (follow these closely for plot, tone, specific scenes, "
        f"and any message the story should convey):\n{content_instruction}\n"
        if content_instruction
        else ""
    )

    # Character reference images are matched across the whole library by name (see
    # db.get_character_by_name), so two unrelated books that happen to name a character the same
    # thing risk one silently reusing the other's reference image — confirmed live when one
    # book's sparrow "Pip" got rendered as another book's squirrel "Pip". Steering new character
    # names away from names already in use prevents the collision instead of just detecting it.
    existing_names_block = (
        f"\nNames already used by characters in other books in this library: "
        f"{', '.join(existing_character_names)}. To avoid this book's characters being confused "
        "with unrelated ones from other stories, do NOT give any NEW character in this book one "
        "of these exact names — unless the main characters listed above explicitly specify one "
        "of these names, in which case use it as given.\n"
        if existing_character_names
        else ""
    )

    return f"""You are a professional children's book author and illustrator art director.

Create a complete blueprint for an illustrated children's book with these inputs:
- Working title: {book_title}
- Target age: {target_age}
- Theme: {theme}
- Main characters: {main_characters}
- Art style preference: {art_style_preference}
- Total pages: {page_count}
{content_block}
{arc_guidance}
{existing_names_block}

IMPORTANT — characters who change age or life stage:
If a character appears at meaningfully different ages or life stages across the story (for example:
a baby who grows into a child, or a figure shown as an infant, a child, and an adult), you MUST create
a SEPARATE entry in "characters" for EACH distinct stage, with its own clearly distinguishing name
(e.g. "Baby Jesus", "Young Jesus", "Adult Jesus" — not just "Jesus" reused for all three) and its own
tailored visual_description and image_prompt describing exactly how that stage looks (age, size,
clothing, hair, etc). Never reuse one character entry to represent drastically different ages — each
stage needs its own reference so the illustrations stay age-accurate instead of drifting toward one
fixed appearance (e.g. accidentally giving an infant a beard).

Return a single raw JSON object with NO markdown formatting, NO code fences, and NO commentary before or after it. The JSON must have exactly this shape:

{{
  "book_title": "string",
  "tagline": "string",
  "art_style": "3 detailed sentences describing the global illustration art style, to be prepended to every image prompt",
  "characters": [
    {{
      "name": "string — must be unique; include a life-stage qualifier if this character appears at multiple ages (e.g. 'Baby Jesus')",
      "role": "string",
      "visual_description": "string",
      "personality": "string",
      "image_prompt": "string describing a clean reference portrait of this character at this specific age/stage"
    }}
  ],
  "pages": [
    {{
      "page_num": 1,
      "story_text": "2-4 age-appropriate sentences for this page",
      "scene_description": "string",
      "image_prompt": "string describing exactly what should be illustrated on this page",
      "characters_on_page": ["exact character name(s) from the characters array that appear in this page's illustration, or an empty array if none"]
    }}
  ]
}}

The "pages" array must contain exactly {page_count} entries, page_num from 1 to {page_count}, following the story arc above. Every "characters_on_page" entry must exactly match a "name" in the "characters" array — always reference the correct life-stage variant for that point in the story (e.g. use "Baby Jesus" on early pages and "Adult Jesus" on later pages, never mix them on the same page unless the scene genuinely shows both). Age-appropriate language for target age {target_age}. Output ONLY the JSON object."""


MAX_TOKENS = 64000
GENERATION_RETRY_ATTEMPTS = 2


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Claude did not return a JSON object")
    return json.loads(text[start:end + 1])


def _generate_blueprint_once(prompt, client):
    # MAX_TOKENS is well above the ~16K threshold where non-streaming requests risk an SDK
    # HTTP timeout, and a 150-page book's blueprint (this app's max page count) can itself
    # need tens of thousands of tokens — streaming is required either way.
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off before finishing the book blueprint "
            f"(hit the {MAX_TOKENS}-token limit). Try again, or use a smaller page count."
        )

    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)


def generate_blueprint(
    book_title, target_age, theme, main_characters, art_style_preference, page_count,
    content_instruction="", existing_character_names=None,
):
    prompt = _build_prompt(
        book_title, target_age, theme, main_characters, art_style_preference, page_count,
        content_instruction, existing_character_names,
    )
    client = _client()

    last_error = None
    for attempt in range(1, GENERATION_RETRY_ATTEMPTS + 1):
        try:
            blueprint = _generate_blueprint_once(prompt, client)
            break
        except (ValueError, json.JSONDecodeError) as e:
            # Malformed JSON in Claude's response is rare but not systemic (e.g. a stray
            # unescaped character in generated prose) — a fresh attempt usually succeeds.
            last_error = e
    else:
        raise last_error

    pages = blueprint.get("pages", [])
    if len(pages) != page_count:
        pages = pages[:page_count]
        while len(pages) < page_count:
            pages.append({
                "page_num": len(pages) + 1,
                "story_text": "",
                "scene_description": "",
                "image_prompt": blueprint.get("art_style", ""),
            })
        blueprint["pages"] = pages

    return blueprint
