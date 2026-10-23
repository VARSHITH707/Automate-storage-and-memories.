from __future__ import annotations

import logging
from pathlib import Path

import pytest

from captioner import config, secrets


def test_missing_config_gives_safe_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.caption.include_location is False
    assert cfg.youtube.enabled is False
    assert cfg.backup.enabled is False
    assert cfg.markers == ["#vault", "#private"]


def test_example_config_is_valid_and_safe() -> None:
    example = Path(__file__).parents[2] / "config.example.toml"
    cfg = config.load(example)
    assert cfg == config.Config()  # the example documents exactly the defaults
    text = example.read_text(encoding="utf-8").lower()
    assert "api_hash =" not in text and "token =" not in text


def test_round_trip_preserves_values(tmp_path: Path) -> None:
    cfg = config.Config()
    cfg.telegram.inbox_id = -100123
    cfg.archive.root = r"D:\Memories\archive"
    cfg.vault.aliases = []
    cfg.caption.language = "Telugu"
    cfg.setup_completed = ["archive"]
    path = tmp_path / "config.toml"
    config.save(cfg, path)
    loaded = config.load(path)
    assert loaded == cfg


@pytest.mark.parametrize("body, message", [
    ("[caption]\ninclude_location = 'yes'\n", "include_location must be bool"),
    ("[caption]\nmystery = 1\n", "Unknown setting caption.mystery"),
    ("[nonsense]\n", "Unknown section"),
    ("[backup]\nenabled = true\n", "backup.target is required"),
    ("[vault]\nmarker = 'vault'\n", "must start with '#'"),
    ("not toml = = =", "not valid TOML"),
])
def test_invalid_config_is_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


def test_secrets_use_keyring(isolated) -> None:
    secrets.set_secret(secrets.BOT_TOKEN, "123:abc")
    assert secrets.get_secret(secrets.BOT_TOKEN) == "123:abc"
    secrets.delete_secret(secrets.BOT_TOKEN)
    assert secrets.get_secret(secrets.BOT_TOKEN) is None


def test_insecure_keyring_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    keyring.set_keyring(fail.Keyring())
    name, secure = secrets.backend_status()
    assert not secure and "fail" in name
    with pytest.raises(secrets.SecretStoreError, match="plaintext storage is not allowed"):
        secrets.set_secret(secrets.API_HASH, "x")


def test_empty_and_oversized_secrets_are_refused() -> None:
    with pytest.raises(secrets.SecretStoreError):
        secrets.set_secret(secrets.API_HASH, "")
    with pytest.raises(secrets.SecretStoreError):
        secrets.set_secret(secrets.API_HASH, "x" * 5000)


@pytest.mark.parametrize("raw, leaked", [
    ("bot token 1234567890:AAH" + "x" * 33, "AAH"),
    ("hash 0123456789abcdef0123456789abcdef", "0123456789abcdef"),
    ("access ya29.a0AfH6SMBxyz", "a0AfH6"),
    ("refresh 1//0gabcdefghijklmnopqrstuvwxyz", "0gabcdef"),
    ("phone +919876543210 tried", "9876543210"),
    ("password=hunter2 code: 12345", "hunter2"),
])
def test_redaction_removes_credentials(raw: str, leaked: str) -> None:
    assert leaked not in secrets.redact(raw)


def test_log_handlers_redact(tmp_path: Path) -> None:
    secrets.setup_logging(tmp_path / "logs")
    logging.getLogger("captioner.test").info("token=%s", "supersecretvalue")
    try:
        raise RuntimeError("failed with GOCSPX-abcdefSECRET and phone 919876543210")
    except RuntimeError:
        logging.getLogger("captioner.test").exception("upload broke")
    logging.getLogger("telethon.network").warning("bot 1234567890:AAH%s", "y" * 33)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "logs" / "captioner.log").read_text(encoding="utf-8")
    assert "supersecretvalue" not in text and "[redacted]" in text
    assert "abcdefSECRET" not in text and "9876543210" not in text  # traceback text is redacted too
    assert "AAHyyy" not in text and "[bot-token]" in text  # third-party loggers too
    logging.getLogger().handlers.clear()


def test_chained_keyring_with_insecure_member_is_refused() -> None:
    import keyring
    from keyring.backends import chainer, fail

    class Chain(chainer.ChainerBackend):
        @property
        def backends(self):  # type: ignore[override]
            return [fail.Keyring()]

    keyring.set_keyring(Chain())
    assert secrets.backend_status()[1] is False
