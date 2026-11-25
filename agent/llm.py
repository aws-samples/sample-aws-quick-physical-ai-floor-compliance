"""Narrative layer: NVIDIA Nemotron on Amazon Bedrock, through the Converse API.

The model writes short explanations from facts the rules engine already
computed. It never decides severity and never invents numbers: every prompt
carries the facts as JSON and asks for a summary of those facts only.

If Bedrock is unreachable, model access is not enabled, or LLM_ENABLED=false,
callers get None and fall back to deterministic text, so the tools keep working.
"""
from __future__ import annotations

import json
import os

DEFAULT_MODEL_ID = "nvidia.nemotron-nano-3-30b"

SYSTEM = (
    "You write two to four plain sentences for a plant operations manager. "
    "Use only the facts in the JSON you are given. Do not add numbers that are not in it. "
    "Name the station or worker when the facts do. No bullet points, no headings, no advice "
    "about discipline. Mention the evidence clip only if asked."
)


def enabled() -> bool:
    return os.environ.get("LLM_ENABLED", "true").lower() not in ("0", "false", "no")


def narrate(question: str, facts: dict, max_tokens: int = 300) -> str | None:
    """Return a short narrative or None when the model is unavailable."""
    if not enabled():
        return None
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION"),
            config=Config(connect_timeout=5, read_timeout=25, retries={"max_attempts": 1}),
        )
        model_id = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)
        resp = client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [
                {"text": f"Question: {question}\n\nFacts (JSON):\n{json.dumps(facts, default=str)}"}
            ]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
        )
        parts = resp.get("output", {}).get("message", {}).get("content", [])
        text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
        return text or None
    except Exception:
        return None
