"""Perception, NIM mode (optional, not exercised by the reference deployment).

Integration point for a hosted NVIDIA vision model. It sends one frame per
clip to an OpenAI-compatible NVIDIA endpoint and asks for the action and tool
as JSON, producing the same action rows as labels mode. Frame extraction is
left to the caller (for example ffmpeg in a batch job), because the agent
container does not carry video libraries.

Environment:
    NVIDIA_API_KEY   required
    NIM_BASE_URL     default https://integrate.api.nvidia.com/v1
    NIM_MODEL        default nvidia/cosmos-reason1-7b

Validate the model name and request format against build.nvidia.com before
use; vision model APIs change. This module was not run as part of the
reference deployment described in the whitepaper.
"""
from __future__ import annotations

import base64
import json
import os

import requests

from .labels_mode import norm

PROMPT = (
    "You see one frame from a top-down camera over a manual assembly bench. "
    "Answer with JSON only: {\"action\": one of place|insert|screw|inspect|none, "
    "\"tool\": one of screwdriver|hex_key|nut_wrench|shaft_wrench|none}."
)


def classify_frame(jpeg_bytes: bytes, timeout: int = 30) -> dict:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set")
    base = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = os.environ.get("NIM_MODEL", "nvidia/cosmos-reason1-7b")
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "max_tokens": 64,
        "temperature": 0.0,
    }
    resp = requests.post(f"{base}/chat/completions", json=payload, timeout=timeout,
                         headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        parsed = {}
    action = norm(parsed.get("action"))
    tool = norm(parsed.get("tool"))
    return {"action": action, "tool": tool or "", "raw": text}
