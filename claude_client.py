import json

import anthropic

import settings

MODEL = "claude-opus-4-5"


def _client():
    return anthropic.Anthropic(api_key=settings.get("ANTHROPIC_API_KEY"))


def _build_prompt(book_title, target_age, theme, main_characters, art_style_preference, page_count):
    intro = max(1, round(page_count * 0.10))
    rising = max(1, round(page_count * 0.42))
    climax = max(1, round(page_count * 0.20))
    resolution = max(1, round(page_count * 0.18))
    epilogue = max(0, page_count - (intro + rising + climax + resolution))

    return f"""You are a professional children's book author and illustrator art director.

Create a complete blueprint for an illustrated children's book with these inputs:
- Working title: {book_title}
- Target age: {target_age}
- Theme: {theme}
- Main characters: {main_characters}
- Art style preference: {art_style_preference}
- Total pages: {page_count}

Story arc proportions across the {page_count} pages (approximate):
- Introduction: ~{intro} pages
- Rising action: ~{rising} pages
- Climax: ~{climax} pages
- Resolution: ~{resolution} pages
- Epilogue: ~{epilogue} pages

Return a single raw JSON object with NO markdown formatting, NO code fences, and NO commentary before or after it. The JSON must have exactly this shape:

{{
  "book_title": "string",
  "tagline": "string",
  "art_style": "3 detailed sentences describing the global illustration art style, to be prepended to every image prompt",
  "characters": [
    {{
      "name": "string",
      "role": "string",
      "visual_description": "string",
      "personality": "string",
      "image_prompt": "string describing a clean reference portrait of this character"
    }}
  ],
  "pages": [
    {{
      "page_num": 1,
      "story_text": "2-4 age-appropriate sentences for this page",
      "scene_description": "string",
      "image_prompt": "string describing exactly what should be illustrated on this page"
    }}
  ]
}}

The "pages" array must contain exactly {page_count} entries, page_num from 1 to {page_count}, following the story arc above. Age-appropriate language for target age {target_age}. Output ONLY the JSON object."""


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


def generate_blueprint(book_title, target_age, theme, main_characters, art_style_preference, page_count):
    prompt = _build_prompt(book_title, target_age, theme, main_characters, art_style_preference, page_count)
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    blueprint = _extract_json(text)

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
