from leak.config import load_settings
from leak.llm.factory import make_provider


def test_no_key_means_no_llm(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("leak.config.load_dotenv", lambda: None)
    assert make_provider(load_settings()) is None


def test_provider_none_wins_over_a_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    s = load_settings()
    s.llm.provider = "none"
    assert make_provider(s) is None


def test_key_gives_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    p = make_provider(load_settings())
    assert p is not None and p.name == "gemini"
