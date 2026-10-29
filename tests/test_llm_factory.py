"""BYOK: keychain storage, profile isolation, factory, CLI. Mocks only."""

import threading
from pathlib import Path

import keyring
import pytest
from keyring.backends import fail
from typer.testing import CliRunner

from leak.config import load_settings
from leak.llm.credentials import (
    SERVICE,
    CredentialError,
    KeyStore,
    SecureStorageUnavailable,
    profile_id,
    resolve_key,
)
from leak.llm.factory import ModelConfigError, make_provider
from leak.llm.openrouter import OpenRouterProvider
from leak.llm.provider import Message
from tests.helpers import FakeOpenRouter, MemoryKeyring, completion

KEY_A = "sk-or-v1-" + "a" * 56
KEY_B = "sk-or-v1-" + "b" * 56


def settings_for(data_dir: Path):
    s = load_settings()
    s.paths.data_dir = data_dir
    return s


def test_no_key_means_no_llm(tmp_path):
    assert make_provider(settings_for(tmp_path)) is None


def test_provider_none_wins_over_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY_A)
    s = settings_for(tmp_path)
    s.llm.provider = "none"
    assert make_provider(s) is None
    KeyStore(s.profile_id).set(KEY_A)
    assert make_provider(s) is None


def test_keychain_key_gives_openrouter(tmp_path):
    s = settings_for(tmp_path)
    KeyStore(s.profile_id).set(KEY_A)
    p = make_provider(s)
    assert isinstance(p, OpenRouterProvider) and KEY_A not in repr(p)


def test_env_key_for_headless_use(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY_B)
    s = settings_for(tmp_path)
    KeyStore(s.profile_id).set(KEY_A)
    r = resolve_key(KeyStore(s.profile_id))
    assert r is not None and r.source == "env" and r.key == KEY_B
    assert KEY_B not in repr(r)


def test_dotenv_key_is_refused(tmp_path, monkeypatch):
    """A key in .env is never used, and we say why instead of silently ignoring it."""

    def fake_dotenv(*a, **k):
        import os

        os.environ["OPENROUTER_API_KEY"] = KEY_A
        return True

    monkeypatch.setattr("leak.config.load_dotenv", fake_dotenv)
    s = load_settings()
    s.paths.data_dir = tmp_path
    import os

    assert s.env_file_key_refused and "OPENROUTER_API_KEY" not in os.environ
    with pytest.raises(CredentialError, match=r"\.env"):
        make_provider(s)


def test_model_overrides_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL_EXTRACT", "acme/fast")
    monkeypatch.setenv("OPENROUTER_MODEL_REASON", "acme/smart")
    s = load_settings()
    assert (s.llm.extract_model, s.llm.reason_model) == ("acme/fast", "acme/smart")


def test_bare_model_id_is_an_actionable_error(tmp_path):
    s = settings_for(tmp_path)
    KeyStore(s.profile_id).set(KEY_A)
    s.llm.reason_model = "gemini-3.1-pro-preview"
    with pytest.raises(ModelConfigError, match="vendor/model"):
        make_provider(s)


def test_old_gemini_config_is_rejected_with_instructions(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[llm]\nprovider = "gemini"\nextract_model = "a"\nreason_model = "b"\n')
    with pytest.raises(ValueError, match="removed"):
        load_settings(cfg)


# --- storage ------------------------------------------------------------------------------


def test_store_set_replace_remove():
    backend = MemoryKeyring()
    store = KeyStore("p1", backend)
    assert store.get() is None
    store.set(f"  {KEY_A}\n")
    assert store.get() == KEY_A
    assert backend.data == {(SERVICE, "profile:p1"): KEY_A}
    store.set(KEY_B)  # replace
    assert store.get() == KEY_B
    assert store.delete() is True and store.get() is None
    assert store.delete() is False


@pytest.mark.parametrize("bad", ["", "sk-proj-openai-key-not-ours-123456", "sk-or-v1-a b", "short"])
def test_key_format_is_checked(bad):
    with pytest.raises(CredentialError):
        KeyStore("p", MemoryKeyring()).set(bad)


def test_insecure_backends_are_refused():
    class PlaintextKeyring(MemoryKeyring):
        pass

    for backend in (fail.Keyring(), PlaintextKeyring()):
        store = KeyStore("p", backend)
        with pytest.raises(SecureStorageUnavailable, match="OPENROUTER_API_KEY"):
            store.set(KEY_A)
        assert resolve_key(store) is None  # local-only mode, not a crash
    assert backend.data == {}  # nothing written to the plaintext one


def test_key_is_not_written_to_config_db_or_traces(tmp_path):
    s = settings_for(tmp_path)
    KeyStore(s.profile_id).set(KEY_A)
    make_provider(s)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY_A.encode() not in f.read_bytes(), f
    assert KEY_A not in Path(s.source).read_text()


# --- profiles -----------------------------------------------------------------------------


def test_profiles_get_separate_keys(tmp_path):
    a, b = settings_for(tmp_path / "a"), settings_for(tmp_path / "b")
    assert a.profile_id != b.profile_id
    assert profile_id(tmp_path / "a") == profile_id(tmp_path / "x" / ".." / "a")
    KeyStore(a.profile_id).set(KEY_A)
    assert make_provider(b) is None  # no fallback to another profile's key
    assert resolve_key(KeyStore(a.profile_id)).key == KEY_A


def test_right_key_per_profile_under_concurrency(tmp_path):
    """Many threads, many profiles, one shared transport: every request carries
    its own profile's key and nothing else."""
    n = 8
    profiles = [settings_for(tmp_path / f"p{i}") for i in range(n)]
    keys = [f"sk-or-v1-{i:02d}" + "k" * 50 for i in range(n)]
    for s, k in zip(profiles, keys, strict=True):
        KeyStore(s.profile_id).set(k)
    fake = FakeOpenRouter(lambda req: completion("ok"))
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def work(i: int) -> None:
        try:
            start.wait()
            for _ in range(5):
                p = make_provider(profiles[i], transport=fake.transport)
                assert p is not None
                p.generate(
                    model="acme/m",
                    system="",
                    messages=[Message(role="user", text=f"profile-{i}")],
                    before_request=lambda a, s, m: (s, m),
                )
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(fake.requests) == n * 5
    import json

    for req in fake.requests:
        who = int(json.loads(req.content)["messages"][-1]["content"].split("-")[1])
        assert req.headers["authorization"] == f"Bearer {keys[who]}"


def test_replaced_or_removed_key_takes_effect_on_next_provider(tmp_path):
    s = settings_for(tmp_path)
    store = KeyStore(s.profile_id)
    store.set(KEY_A)
    fake = FakeOpenRouter(lambda req: completion("ok"))

    def auth_of_next_call() -> str:
        p = make_provider(s, transport=fake.transport)
        assert p is not None
        p.generate(model="acme/m", system="", messages=[Message(role="user", text="x")],
                   before_request=lambda a, s_, m: (s_, m))  # fmt: skip
        return fake.requests[-1].headers["authorization"]

    assert auth_of_next_call() == f"Bearer {KEY_A}"
    store.set(KEY_B)
    assert auth_of_next_call() == f"Bearer {KEY_B}"
    store.delete()
    assert make_provider(s) is None


# --- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from leak import cli as cli_mod

    monkeypatch.chdir(tmp_path)  # data_dir ".leak" -> a profile of its own
    monkeypatch.setattr(cli_mod, "_key_info", lambda key, s: {"label": "test", "usage": 0})
    return CliRunner(), cli_mod.app


def test_cli_set_status_replace_remove(cli):
    runner, app = cli
    r = runner.invoke(app, ["llm", "key", "set"], input=KEY_A + "\n")
    assert r.exit_code == 0, r.output
    assert KEY_A not in r.output and "…aaaa" in r.output
    s = load_settings()
    assert KeyStore(s.profile_id).get() == KEY_A

    r = runner.invoke(app, ["llm", "key", "set"], input=KEY_B + "\n")
    assert r.exit_code == 1 and "replace" in r.output  # no silent overwrite

    r = runner.invoke(app, ["llm", "key", "replace"], input=KEY_B + "\n")
    assert r.exit_code == 0 and KeyStore(s.profile_id).get() == KEY_B

    r = runner.invoke(app, ["llm", "status"])
    assert r.exit_code == 0 and "…bbbb" in r.output and KEY_B not in r.output
    assert "zdr" in r.output

    r = runner.invoke(app, ["llm", "key", "validate"])
    assert r.exit_code == 0 and "key OK" in r.output

    r = runner.invoke(app, ["llm", "key", "remove"])
    assert r.exit_code == 0 and KeyStore(s.profile_id).get() is None


def test_cli_rejects_key_on_failed_validation(cli, monkeypatch):
    from leak import cli as cli_mod
    from leak.llm.openrouter import InvalidKey

    def bad(key, s):
        raise InvalidKey("OpenRouter rejected the API key (401)")

    monkeypatch.setattr(cli_mod, "_key_info", bad)
    runner, app = cli
    r = runner.invoke(app, ["llm", "key", "set"], input=KEY_A + "\n")
    assert r.exit_code == 1 and "not stored" in r.output
    assert KeyStore(load_settings().profile_id).get() is None


def test_cli_explains_missing_keychain(cli):
    keyring.set_keyring(fail.Keyring())
    runner, app = cli
    r = runner.invoke(app, ["llm", "key", "set", "--no-validate"], input=KEY_A + "\n")
    assert r.exit_code == 1 and "OPENROUTER_API_KEY" in r.output
