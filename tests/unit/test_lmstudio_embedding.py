"""Unit tests for the LM Studio embedding-offload provider and its wiring.

Covers:

- ``work_buddy.health.components`` registers the ``lmstudio`` component
  and the ``embedding`` component carries ``soft_depends_on=["lmstudio"]``
  with the fallback note.
- ``work_buddy.health.requirements`` registers
  ``services/lmstudio/reachable`` with the right component / severity
  and the check_fn points at ``requirement_checks.check_lmstudio_reachable``.
- ``work_buddy.embedding.providers.lmstudio.resolve_base_url`` honors
  the ``lmstudio.base_url`` config key and falls back to the LM Studio
  default.
- ``work_buddy.ir.dense._encode_bulk_direct`` dispatches to the LM
  Studio provider when a model opts in, falls back to sentence-
  transformers on error (when ``on_error=fallback``), and re-raises
  when ``on_error=fail``.

No actual HTTP calls — the provider encode function is monkeypatched
for the dispatch tests. No actual sentence-transformers load either —
the fallback-path test stubs ``SentenceTransformer`` through the
``ir.dense`` module namespace.

Uses ``numpy`` directly for the fake vector returns (already a project
dep via embedding/ir code). Does NOT import ``torch`` or any GPU
machinery, so it's safe to run on CI boxes.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Dependency registration
# ---------------------------------------------------------------------------

def test_lmstudio_component_registered() -> None:
    from work_buddy.health.components import COMPONENT_CATALOG

    comp = COMPONENT_CATALOG.get("lmstudio")
    assert comp is not None, "lmstudio ComponentDef should be registered"
    assert comp.category == "external"
    assert comp.is_core is False, (
        "lmstudio must not be core — the embedding system falls back "
        "to sentence-transformers when it isn't reachable"
    )
    # Requirements link back to the setup-time reachable check.
    assert "services/lmstudio/reachable" in comp.requirements
    # Check sequence points at the runtime health probe.
    check_fns = [step.check_fn for step in comp.check_sequence]
    assert "work_buddy.health.checks.check_lmstudio" in check_fns


def test_embedding_component_soft_depends_on_lmstudio() -> None:
    from work_buddy.health.components import COMPONENT_CATALOG

    embedding = COMPONENT_CATALOG["embedding"]
    assert "lmstudio" in embedding.soft_depends_on, (
        "embedding.soft_depends_on should name lmstudio so the control "
        "graph surfaces the optional offload in Settings"
    )
    note = embedding.soft_dep_notes.get("lmstudio")
    assert note, "lmstudio soft-dep must carry a descriptive note"
    # Sanity: the note names the fallback so users know what happens
    # when LM Studio is unreachable.
    assert "sentence-transformers" in note.lower() or "fallback" in note.lower()


def test_lmstudio_requirement_registered() -> None:
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    req = REQUIREMENT_REGISTRY.get("services/lmstudio/reachable")
    assert req is not None
    assert req.component == "lmstudio"
    # Recommended (not required) — users who never opt into offloading
    # shouldn't see setup blocked by LM Studio being down.
    assert req.severity == "recommended"
    assert req.check_fn == (
        "work_buddy.health.requirement_checks.check_lmstudio_reachable"
    )
    # Setup-wizard fix path points back at the handbook runbook.
    assert req.fix_kind == "agent_handoff"
    assert req.fix_agent_brief is not None
    assert (
        "docs/handbook/features_lmstudio-offload-setup.md"
        in req.fix_agent_brief
    ), "fix_agent_brief must point at the authoritative runbook"


# ---------------------------------------------------------------------------
# Provider module: resolve_base_url
# ---------------------------------------------------------------------------

def test_resolve_base_url_defaults(monkeypatch) -> None:
    """With an empty config dict, we fall back to LM Studio's default."""
    from work_buddy.embedding.providers import lmstudio as provider

    url = provider.resolve_base_url({})
    assert url == "http://localhost:1234"


def test_resolve_base_url_honors_config() -> None:
    from work_buddy.embedding.providers import lmstudio as provider

    url = provider.resolve_base_url(
        {"lmstudio": {"base_url": "http://compute.tailnet:4321/"}}
    )
    # Trailing slash stripped, rest preserved
    assert url == "http://compute.tailnet:4321"


def test_resolve_base_url_ignores_non_string() -> None:
    """A malformed config (e.g. null/False) should still yield the default
    rather than raising — the validator warns loudly elsewhere.
    """
    from work_buddy.embedding.providers import lmstudio as provider

    url = provider.resolve_base_url({"lmstudio": {"base_url": None}})
    assert url == "http://localhost:1234"


# ---------------------------------------------------------------------------
# _encode_bulk_direct dispatch behavior
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cfg_lmstudio_on_error(monkeypatch):
    """Return a factory that installs a fake load_config with configurable
    on_error and lmstudio_model. Used by dispatch tests below."""
    import work_buddy.config as cfg_module

    def _build(on_error: str, lmstudio_model: str = "fake-model-id"):
        def _fake():
            return {
                "lmstudio": {"base_url": "http://localhost:1234"},
                "embedding": {
                    "models": {
                        "leaf-ir": {
                            "name": "MongoDB/mdbr-leaf-ir-asym",
                            "dims": 768,
                            "eager": False,
                            "provider": "lmstudio",
                            "lmstudio_model": lmstudio_model,
                            "on_error": on_error,
                        },
                    },
                },
            }
        monkeypatch.setattr(cfg_module, "load_config", _fake)

    return _build


def test_encode_bulk_direct_routes_to_lmstudio(
    fake_cfg_lmstudio_on_error, monkeypatch
) -> None:
    """Happy path: provider=lmstudio with a working endpoint routes there
    and returns whatever the provider produces, without touching the
    sentence-transformers fallback."""
    fake_cfg_lmstudio_on_error(on_error="fail")

    captured_kwargs: dict = {}
    fake_vecs = np.ones((3, 768), dtype=np.float32)

    def _fake_encode(texts, *, model_id, base_url, batch_size):
        captured_kwargs["texts"] = list(texts)
        captured_kwargs["model_id"] = model_id
        captured_kwargs["base_url"] = base_url
        captured_kwargs["batch_size"] = batch_size
        return fake_vecs

    import work_buddy.embedding.providers.lmstudio as prov
    monkeypatch.setattr(prov, "encode", _fake_encode)

    # Also ensure _IN_SERVICE is False so a bug forcing the fallback path
    # would try to load SentenceTransformer — which we'd catch below.
    import work_buddy.ir.dense as dense
    monkeypatch.setattr(dense, "_IN_SERVICE", False)

    def _boom(*a, **kw):
        raise AssertionError(
            "SentenceTransformer must not be touched on the happy LM "
            "Studio path"
        )
    # Guard: if the dispatcher accidentally falls through, this trips.
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", _boom
    )

    result = dense._encode_bulk_direct(["a", "b", "c"], kind="passage")

    assert result is fake_vecs
    assert captured_kwargs["model_id"] == "fake-model-id"
    assert captured_kwargs["base_url"] == "http://localhost:1234"
    assert captured_kwargs["texts"] == ["a", "b", "c"]


def test_encode_bulk_direct_fallback_on_error(
    fake_cfg_lmstudio_on_error, monkeypatch
) -> None:
    """When on_error=fallback and the provider raises, we must drop to
    the sentence-transformers path without propagating."""
    fake_cfg_lmstudio_on_error(on_error="fallback")

    def _fake_encode(texts, **_kw):
        raise RuntimeError("simulated LM Studio outage")

    import work_buddy.embedding.providers.lmstudio as prov
    monkeypatch.setattr(prov, "encode", _fake_encode)

    import work_buddy.ir.dense as dense
    monkeypatch.setattr(dense, "_IN_SERVICE", False)

    # Stub SentenceTransformer so the fallback returns deterministic
    # vectors without loading any real weights.
    fake_vecs = np.full((2, 768), 0.5, dtype=np.float32)

    class _FakeST:
        def __init__(self, *_a, **_kw):
            pass

        def encode(self, _texts, **_kw):
            return fake_vecs

    import sentence_transformers as st_pkg
    monkeypatch.setattr(st_pkg, "SentenceTransformer", _FakeST)

    result = dense._encode_bulk_direct(["x", "y"], kind="passage")
    assert result.shape == (2, 768)
    # Our fake was used, not the real loader
    assert np.allclose(result, 0.5)


def test_encode_bulk_direct_fail_propagates(
    fake_cfg_lmstudio_on_error, monkeypatch
) -> None:
    """on_error=fail must re-raise the provider's exception untouched."""
    fake_cfg_lmstudio_on_error(on_error="fail")

    class _Sentinel(Exception):
        pass

    def _fake_encode(_texts, **_kw):
        raise _Sentinel("boom")

    import work_buddy.embedding.providers.lmstudio as prov
    monkeypatch.setattr(prov, "encode", _fake_encode)

    import work_buddy.ir.dense as dense
    monkeypatch.setattr(dense, "_IN_SERVICE", False)

    with pytest.raises(_Sentinel):
        dense._encode_bulk_direct(["x"], kind="passage")


def test_encode_bulk_direct_missing_lmstudio_model_falls_back(
    fake_cfg_lmstudio_on_error, monkeypatch
) -> None:
    """Guard-rail: provider=lmstudio without lmstudio_model should fall
    back (on on_error=fallback) rather than calling provider.encode with
    an empty id."""
    fake_cfg_lmstudio_on_error(on_error="fallback", lmstudio_model="")

    called = SimpleNamespace(encode_ran=False)

    def _fake_encode(*_a, **_kw):
        called.encode_ran = True
        raise AssertionError("should not be called with empty model id")

    import work_buddy.embedding.providers.lmstudio as prov
    monkeypatch.setattr(prov, "encode", _fake_encode)

    import work_buddy.ir.dense as dense
    monkeypatch.setattr(dense, "_IN_SERVICE", False)

    fake_vecs = np.zeros((1, 768), dtype=np.float32)

    class _FakeST:
        def __init__(self, *_a, **_kw):
            pass

        def encode(self, _texts, **_kw):
            return fake_vecs

    import sentence_transformers as st_pkg
    monkeypatch.setattr(st_pkg, "SentenceTransformer", _FakeST)

    result = dense._encode_bulk_direct(["z"], kind="passage")
    assert result.shape == (1, 768)
    assert called.encode_ran is False


# ---------------------------------------------------------------------------
# GGUF header parser (audit script)
# ---------------------------------------------------------------------------

def _build_minimal_gguf(
    tmp_path,
    *,
    arch: str = "bert",
    pooling_type: int = 2,  # CLS
    embedding_length: int = 768,
):
    """Emit a tiny valid GGUF file (no tensors) carrying the key metadata
    fields the audit script checks. Returns the path."""
    import struct

    path = tmp_path / "tiny.gguf"
    with open(path, "wb") as f:
        f.write(b"GGUF")
        f.write(struct.pack("<I", 3))  # version
        f.write(struct.pack("<Q", 0))  # tensor_count (none)

        # Metadata KVs we want to materialize
        kvs = [
            ("general.architecture", "STRING", arch),
            ("bert.pooling_type", "UINT32", pooling_type),
            ("bert.embedding_length", "UINT32", embedding_length),
        ]
        f.write(struct.pack("<Q", len(kvs)))

        # Minimal encoder matching the audit script's parser
        TYPE_STRING = 8
        TYPE_UINT32 = 4
        for key, typ, val in kvs:
            key_bytes = key.encode("utf-8")
            f.write(struct.pack("<Q", len(key_bytes)))
            f.write(key_bytes)
            if typ == "STRING":
                f.write(struct.pack("<I", TYPE_STRING))
                val_bytes = val.encode("utf-8")
                f.write(struct.pack("<Q", len(val_bytes)))
                f.write(val_bytes)
            elif typ == "UINT32":
                f.write(struct.pack("<I", TYPE_UINT32))
                f.write(struct.pack("<I", val))
            else:
                raise ValueError(f"unsupported test type: {typ}")
    return path


def test_gguf_audit_parses_valid_file(tmp_path) -> None:
    # Load the script module directly to avoid subprocess overhead.
    import importlib.util
    import os

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    script_path = os.path.join(
        repo_root, "scripts", "audit_lmstudio_gguf.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_audit_script_under_test", script_path
    )
    assert spec and spec.loader
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)

    good = _build_minimal_gguf(tmp_path, pooling_type=2)
    header = audit.parse_gguf_header(good)
    md = header["metadata"]
    assert md["general.architecture"] == "bert"
    assert md["bert.pooling_type"] == 2
    assert md["bert.embedding_length"] == 768

    # audit() returns 0 on pass, 1 on fail
    assert audit.audit(good, expected_pooling="CLS") == 0

    # A MEAN-pooled GGUF should fail when we expect CLS. Use a fresh
    # subdirectory so we don't overwrite the good-path artifact above.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    bad = _build_minimal_gguf(bad_dir, pooling_type=1)
    assert audit.audit(bad, expected_pooling="CLS") == 1


# ---------------------------------------------------------------------------
# Startup validator
# ---------------------------------------------------------------------------

def test_validator_silent_when_no_lmstudio_models(capsys) -> None:
    """If nothing opts into lmstudio, the validator emits nothing."""
    from work_buddy.embedding.service import _validate_lmstudio_providers

    _validate_lmstudio_providers({
        "embedding": {
            "models": {
                "leaf-ir": {"provider": "sentence_transformer"},
            },
        },
    })
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_validator_warns_when_lmstudio_unreachable(
    monkeypatch, capsys
) -> None:
    """Validator logs a warning when any model opts into lmstudio but
    the provider reports unreachable."""
    from work_buddy.embedding.service import _validate_lmstudio_providers
    import work_buddy.embedding.providers.lmstudio as prov

    monkeypatch.setattr(
        prov, "validate_reachable",
        lambda _cfg=None: {
            "ok": False,
            "base_url": "http://localhost:1234",
            "detail": "simulated unreachable",
        },
    )

    _validate_lmstudio_providers({
        "embedding": {
            "models": {
                "leaf-ir": {
                    "provider": "lmstudio",
                    "lmstudio_model": "anything",
                },
            },
        },
    })
    out = capsys.readouterr()
    assert "WARNING" in out.err
    assert "leaf-ir" in out.err


def test_validator_ok_line_when_model_loaded(monkeypatch, capsys) -> None:
    from work_buddy.embedding.service import _validate_lmstudio_providers
    import work_buddy.embedding.providers.lmstudio as prov

    monkeypatch.setattr(
        prov, "validate_reachable",
        lambda _cfg=None: {
            "ok": True,
            "base_url": "http://localhost:1234",
            "detail": "ok",
            "model_ids": ["text-embedding-some-model"],
        },
    )

    _validate_lmstudio_providers({
        "embedding": {
            "models": {
                "leaf-ir": {
                    "provider": "lmstudio",
                    "lmstudio_model": "text-embedding-some-model",
                },
            },
        },
    })
    out = capsys.readouterr()
    assert "verified" in out.err
    assert "text-embedding-some-model" in out.err
