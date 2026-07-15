import time
from concurrent.futures import ThreadPoolExecutor

import requests

from config import KIE_AI_API_KEY

BASE_URL = "https://api.kie.ai/api/v1/flux/kontext"
DEFAULT_NEGATIVE_PROMPT = (
    "text, watermark, realistic photograph, adult content, violence, scary, dark themes, blurry"
)
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 300


def _headers():
    return {
        "Authorization": f"Bearer {KIE_AI_API_KEY}",
        "Content-Type": "application/json",
    }


def _submit(prompt, image_size, output_format="jpeg", reference_image_url=None, negative_prompt=None):
    payload = {
        "prompt": prompt,
        "image_size": image_size,
        "output_format": output_format,
        "negative_prompt": negative_prompt or DEFAULT_NEGATIVE_PROMPT,
    }
    if reference_image_url:
        payload["reference_image_url"] = reference_image_url

    resp = requests.post(f"{BASE_URL}/generate", json=payload, headers=_headers(), timeout=60)
    resp.raise_for_status()
    body = resp.json()
    task_id = body.get("data", {}).get("taskId") or body.get("taskId")
    if not task_id:
        raise RuntimeError(f"KIE.ai submit did not return a taskId: {body}")
    return task_id


def _extract_image_url(data):
    response = data.get("response") or {}
    for key in ("resultImageUrl", "imageUrl", "url"):
        if response.get(key):
            return response[key]
    for key in ("resultUrls", "resultImageUrls", "urls"):
        urls = response.get(key) or data.get(key)
        if urls:
            return urls[0]
    raise RuntimeError(f"Could not find image URL in KIE.ai response: {data}")


def _poll(task_id, timeout=POLL_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/record-info", params={"taskId": task_id}, headers=_headers(), timeout=30
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if data.get("successFlag") == 1:
            return _extract_image_url(data)
        if data.get("successFlag") in (2, 3):
            raise RuntimeError(f"KIE.ai generation failed for task {task_id}: {data}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"KIE.ai task {task_id} timed out after {timeout}s")


def generate_image(prompt, image_size, output_format="jpeg", reference_image_url=None, negative_prompt=None):
    task_id = _submit(prompt, image_size, output_format, reference_image_url, negative_prompt)
    return _poll(task_id)


def generate_character_reference(prompt, reference_image_url=None):
    return generate_image(prompt, image_size="1024x1024", reference_image_url=reference_image_url)


def generate_page_image(prompt, negative_prompt=None):
    return generate_image(prompt, image_size="768x1024", negative_prompt=negative_prompt)


def generate_pages_concurrent(page_prompts, max_workers=5, on_complete=None):
    results = [None] * len(page_prompts)

    def _worker(index, prompt):
        url = generate_page_image(prompt)
        results[index] = url
        if on_complete:
            on_complete(index)
        return url

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, i, p) for i, p in enumerate(page_prompts)]
        for f in futures:
            f.result()

    return results
