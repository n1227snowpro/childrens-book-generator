import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import settings

BASE_URL = "https://api.kie.ai/api/v1"
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 300

# Prices and API shapes verified against docs.kie.ai (per-model doc pages) and kie.ai/pricing.
MODELS = {
    "nano-banana": {
        "label": "Nano Banana",
        "provider": "Google",
        "price_per_image": 0.02,
        "note": "Cheapest option, strong character consistency across pages",
    },
    "flux-kontext-pro": {
        "label": "Flux Kontext Pro",
        "provider": "Black Forest Labs",
        "price_per_image": 0.025,
        "note": "Default — balanced quality and cost",
    },
    "flux-kontext-max": {
        "label": "Flux Kontext Max",
        "provider": "Black Forest Labs",
        "price_per_image": 0.05,
        "note": "Higher quality, more detail",
    },
    "gpt-image-2": {
        "label": "GPT Image 2",
        "provider": "OpenAI",
        "price_per_image": 0.03,
        "note": "Strongest prompt-following and text rendering, priciest",
    },
}

DEFAULT_MODEL = "flux-kontext-pro"


def _headers():
    return {
        "Authorization": f"Bearer {settings.get('KIE_AI_API_KEY')}",
        "Content-Type": "application/json",
    }


def _create_task(path, payload):
    resp = requests.post(f"{BASE_URL}{path}", json=payload, headers=_headers(), timeout=60)
    resp.raise_for_status()
    body = resp.json()
    task_id = body.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"KIE.ai did not return a taskId: {body}")
    return task_id


def _poll_unified(task_id, timeout=POLL_TIMEOUT_SECONDS):
    """For models behind the generic /jobs/createTask + /jobs/recordInfo API (Nano Banana, GPT Image 2)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/jobs/recordInfo", params={"taskId": task_id}, headers=_headers(), timeout=30
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        state = data.get("state")
        if state == "success":
            result = json.loads(data.get("resultJson") or "{}")
            urls = result.get("resultUrls") or []
            if not urls:
                raise RuntimeError(f"KIE.ai success response had no resultUrls: {data}")
            return urls[0]
        if state == "fail":
            raise RuntimeError(f"KIE.ai generation failed for task {task_id}: {data.get('failMsg')}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"KIE.ai task {task_id} timed out after {timeout}s")


def _poll_flux_kontext(task_id, timeout=POLL_TIMEOUT_SECONDS):
    """Flux Kontext still uses its own legacy endpoint, not the unified /jobs API."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/flux/kontext/record-info", params={"taskId": task_id}, headers=_headers(), timeout=30
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        flag = data.get("successFlag")
        if flag == 1:
            url = (data.get("response") or {}).get("resultImageUrl")
            if not url:
                raise RuntimeError(f"Flux Kontext success response missing resultImageUrl: {data}")
            return url
        if flag in (2, 3):
            raise RuntimeError(f"Flux Kontext generation failed for task {task_id}: {data.get('errorMessage')}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Flux Kontext task {task_id} timed out after {timeout}s")


def _generate_nano_banana(prompt, reference_image_urls, aspect_ratio):
    if reference_image_urls:
        payload = {
            "model": "google/nano-banana-edit",
            "input": {
                "prompt": prompt,
                "image_urls": reference_image_urls,
                "output_format": "jpeg",
                "aspect_ratio": aspect_ratio,
            },
        }
    else:
        payload = {
            "model": "google/nano-banana",
            "input": {
                "prompt": prompt,
                "output_format": "jpeg",
                "aspect_ratio": aspect_ratio,
            },
        }
    task_id = _create_task("/jobs/createTask", payload)
    return _poll_unified(task_id)


def _generate_flux_kontext(prompt, model_id, reference_image_urls, aspect_ratio):
    payload = {
        "prompt": prompt,
        "model": model_id,
        "aspectRatio": aspect_ratio,
        "outputFormat": "jpeg",
        "enableTranslation": False,
    }
    if reference_image_urls:
        payload["inputImage"] = reference_image_urls[0]
    task_id = _create_task("/flux/kontext/generate", payload)
    return _poll_flux_kontext(task_id)


def _generate_gpt_image_2(prompt, reference_image_urls, aspect_ratio):
    if reference_image_urls:
        payload = {
            "model": "gpt-image-2-image-to-image",
            "input": {
                "prompt": prompt,
                "input_urls": reference_image_urls,
                "aspect_ratio": aspect_ratio,
                "resolution": "1K",
            },
        }
    else:
        payload = {
            "model": "gpt-image-2-text-to-image",
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": "1K",
            },
        }
    task_id = _create_task("/jobs/createTask", payload)
    return _poll_unified(task_id)


def generate_image(model_id, prompt, reference_image_url=None, square=True):
    if model_id not in MODELS:
        raise ValueError(f"Unknown image model: {model_id}")

    aspect_ratio = "1:1" if square else "3:4"
    reference_image_urls = [reference_image_url] if reference_image_url else None

    if model_id == "nano-banana":
        return _generate_nano_banana(prompt, reference_image_urls, aspect_ratio)
    if model_id in ("flux-kontext-pro", "flux-kontext-max"):
        return _generate_flux_kontext(prompt, model_id, reference_image_urls, aspect_ratio)
    if model_id == "gpt-image-2":
        return _generate_gpt_image_2(prompt, reference_image_urls, aspect_ratio)


def generate_character_reference(model_id, prompt, reference_image_url=None):
    return generate_image(model_id, prompt, reference_image_url=reference_image_url, square=True)


def generate_page_image(model_id, prompt):
    return generate_image(model_id, prompt, reference_image_url=None, square=False)


def generate_pages_concurrent(model_id, page_prompts, max_workers=5, on_complete=None):
    results = [None] * len(page_prompts)

    def _worker(index, prompt):
        url = generate_page_image(model_id, prompt)
        results[index] = url
        if on_complete:
            on_complete(index)
        return url

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, i, p) for i, p in enumerate(page_prompts)]
        for f in futures:
            f.result()

    return results
