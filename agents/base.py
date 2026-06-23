"""
agents/base.py — Shared LLM factory + pipeline metadata fingerprinting.
"""
from __future__ import annotations

import hashlib
import json
import re

from langchain_ollama import ChatOllama
import config


def get_llm(temperature: float = 0.0) -> ChatOllama:
    return ChatOllama(
        model=config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=temperature,
        num_predict=config.OLLAMA_NUM_PREDICT,
    )


def extract_json(text: str) -> dict:
    """Robustly extract a JSON object from an LLM response string."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    return json.loads(text.strip())


def build_pipeline_metadata(prompt_texts: dict[str, str]) -> str:
    """
    Build a JSON string containing model fingerprint + prompt hashes.
    Stored on-chain alongside every emission record for audit reproducibility.

    Args:
        prompt_texts: dict of {agent_name: system_prompt_text}

    Returns:
        JSON string (max 300 chars when stored on-chain — truncate if needed).
    """
    prompt_hashes = {
        name: hashlib.sha256(text.encode()).hexdigest()[:12]
        for name, text in prompt_texts.items()
    }
    meta = {
        "model":   config.OLLAMA_MODEL,
        "version": config.PIPELINE_VERSION,
        "prompts": prompt_hashes,
    }
    return json.dumps(meta, separators=(",", ":"))
