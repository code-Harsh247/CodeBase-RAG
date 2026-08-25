"""Token counting for the model actually in use.

Character counts are a poor proxy for tokens — the ratio swings several-fold
between prose and dense code — and the one place this project needs a real
number is trimming conversation history to a budget before a prompt goes out.
With a single provider pinned to one model, that number can be exact rather
than estimated.

Loads only the tokenizer (a few MB of vocabulary and merge rules), never model
weights, via the Rust-backed `tokenizers` package — no torch, matching the
constraint that already keeps the Chroma embedding model on ONNX.
"""

from __future__ import annotations

from tokenizers import Tokenizer

#: The model behind OpenRouter's `qwen/qwen3-coder`
#: (agent/openrouter_provider.py's DEFAULT_MODEL). Its HuggingFace repo is
#: public and ungated, so this needs no token to fetch.
MODEL_ID = "Qwen/Qwen3-Coder-480B-A35B-Instruct"

_tokenizer: Tokenizer | None = None


def _get() -> Tokenizer:
    """Load once per process, on first use.

    The first call downloads `tokenizer.json` and caches it under
    ~/.cache/huggingface; every later call in any process is offline.
    """
    global _tokenizer
    if _tokenizer is None:
        _tokenizer = Tokenizer.from_pretrained(MODEL_ID)
    return _tokenizer


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_get().encode(text).ids)
