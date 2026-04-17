"""LLM backend implementations.

Each backend exposes a single function that takes canonical inputs
(system, user, model, max_tokens, temperature, output_schema) and returns
a canonical dict {content, input_tokens, output_tokens, model}. Backends
are selected at runtime from config-resolved profiles.

No Backend protocol / abstract base class is defined here — with only
one non-Anthropic backend in v1, the protocol would be speculative.
Introduce it when a third backend (Ollama, vLLM, etc.) lands.
"""

from work_buddy.llm.backends.openai_compat import call_openai_compat

__all__ = ["call_openai_compat"]
