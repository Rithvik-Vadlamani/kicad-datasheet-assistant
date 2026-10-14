import httpx
import numpy as np
import pytest

from kicad_rag import providers
from kicad_rag.config import Settings
from kicad_rag.providers import Provider


def test_ollama_embedding_protocol_and_normalization(monkeypatch):
    def request(url, payload, **kwargs):
        assert url == "http://127.0.0.1:11434/api/embed"
        assert payload["truncate"] is False
        return {"embeddings": [[3, 4]]}
    monkeypatch.setattr(providers, "request_json", request)
    provider = Provider(Settings(embedding_provider="ollama", embedding_model="local"))
    np.testing.assert_allclose(provider.embed(["text"]), [[0.6, 0.8]])


def test_openai_embedding_order_is_restored(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setattr(providers, "request_json", lambda *a, **k: {"data": [
        {"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [2, 0]}]})
    provider = Provider(Settings(embedding_provider="openai", embedding_model="test"))
    np.testing.assert_allclose(provider.embed(["one", "two"]), [[1, 0], [0, 1]])


def test_cloud_generation_uses_responses_and_store_false(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    def request(url, payload, **kwargs):
        assert url.endswith("/v1/responses")
        assert payload["store"] is False
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]}
    monkeypatch.setattr(providers, "request_json", request)
    provider = Provider(Settings(chat_provider="openai", chat_model="test"))
    assert provider.generate("system", "context") == "answer"


def test_local_mode_rejects_remote_ollama_url():
    provider = Provider(Settings(ollama_url="https://external.example"))
    with pytest.raises(ValueError, match="loopback"):
        provider.ollama("/api/chat")


def test_error_output_does_not_expose_api_key(monkeypatch):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(401, request=request)
    def fail(*args, **kwargs):
        raise httpx.HTTPStatusError("sensitive-secret", request=request, response=response)
    monkeypatch.setattr(httpx.Client, "post", fail)
    with pytest.raises(ValueError, match="HTTP 401") as error:
        providers.request_json(str(request.url), {}, api_key="sensitive-secret")
    assert "sensitive-secret" not in str(error.value)
