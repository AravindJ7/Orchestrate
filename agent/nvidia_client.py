import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL") or "openai/gpt-oss-20b"
NVIDIA_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "NVIDIA_FALLBACK_MODELS",
        "nvidia/nemotron-3.5-lightning-30b-a3b,nvidia/nemotron-3-super-120b-a12b",
    ).split(",")
    if m.strip()
]


def is_configured():
    return bool(NVIDIA_API_KEY)


def get_client():
    if not NVIDIA_API_KEY:
        raise ValueError("NVIDIA_API_KEY not found in .env")
    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=NVIDIA_API_KEY,
        timeout=90.0,
        max_retries=1,
    )


def get_default_model():
    return NVIDIA_MODEL


def get_model_candidates():
    """Primary model first, then fallbacks (deduplicated)."""
    seen = set()
    out = []
    for m in [NVIDIA_MODEL] + NVIDIA_FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out
