import os

import pytest

from digest.config import load_dotenv


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("FOO_TEST_KEY", "BAR_TEST_KEY", "QUOTED_TEST_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_load_dotenv_basic(tmp_path, clean_env, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "FOO_TEST_KEY=hello\n"
        'QUOTED_TEST_KEY="AIE Videos <a@b.com>"\n'
        "export BAR_TEST_KEY=world\n"
    )
    load_dotenv(env_file)
    assert os.environ["FOO_TEST_KEY"] == "hello"
    assert os.environ["QUOTED_TEST_KEY"] == "AIE Videos <a@b.com>"
    assert os.environ["BAR_TEST_KEY"] == "world"


def test_load_dotenv_later_lines_override_earlier(tmp_path, clean_env):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_TEST_KEY=\nFOO_TEST_KEY=appended\n")
    load_dotenv(env_file)
    assert os.environ["FOO_TEST_KEY"] == "appended"


def test_load_dotenv_real_env_wins(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("FOO_TEST_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_TEST_KEY=from-file\n")
    load_dotenv(env_file)
    assert os.environ["FOO_TEST_KEY"] == "from-shell"


def test_load_dotenv_missing_file(tmp_path, clean_env):
    load_dotenv(tmp_path / "does-not-exist")  # must not raise
