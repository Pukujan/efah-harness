"""DEC-003: env:NAME and env:NAME_PATH resolve interchangeably."""
import pytest
from integrations.secrets import MissingRequiredCredential, SecretRef, SecretResolver


def test_inline_env_value(tmp_path):
    r = SecretResolver({"TOKEN": "abc"})
    assert r.resolve(SecretRef("t", "env:TOKEN")) == "abc"


def test_path_valued_reference(tmp_path):
    """The GitHub App key ships as *_PATH; Section 7.1 forbids asking about it."""
    p = tmp_path / "key.pem"
    p.write_text("-----BEGIN PRIVATE KEY-----\n")
    r = SecretResolver({"KEY_PATH": str(p)})
    assert r.resolve(SecretRef("k", "env:KEY")).startswith("-----BEGIN")


def test_inline_value_wins_over_path(tmp_path):
    p = tmp_path / "key.pem"
    p.write_text("from-file")
    r = SecretResolver({"KEY": "inline", "KEY_PATH": str(p)})
    assert r.resolve(SecretRef("k", "env:KEY")) == "inline"


def test_missing_required_credential_is_typed():
    r = SecretResolver({})
    with pytest.raises(MissingRequiredCredential) as exc:
        r.resolve(SecretRef("plane", "env:NOPE"))
    assert "MISSING_REQUIRED_CREDENTIAL" in str(exc.value)


def test_optional_credential_returns_none():
    assert SecretResolver({}).resolve(SecretRef("o", "env:NOPE", required=False)) is None
