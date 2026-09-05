"""
test_env_file.py — the app loads <app dir>/.env into the environment itself.

On the droplet the key is no longer inherited from a login shell (pm2 is started
from a scrubbed environment), so <app dir>/.env is the only copy and app.py has
to read it. These pin the loader's contract: file read; values already in the
environment win; quoting/comments/export/CRLF tolerated; a missing file is fine.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLY_DB_PATH", "/tmp/test_env_file.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import app  # noqa: E402


def _load(tmp_path, text, environ=None):
    path = tmp_path / ".env"
    path.write_bytes(text.encode("utf-8"))
    env = {} if environ is None else environ
    loaded = app.load_env_file(str(path), env)
    return env, loaded


def test_reads_key_from_file(tmp_path):
    env, loaded = _load(tmp_path, "ANTHROPIC_API_KEY=sk-ant-test\n")
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-test"}
    assert loaded == ["ANTHROPIC_API_KEY"]


def test_existing_environment_wins_over_file(tmp_path):
    # A value already in the process environment (a test's "", an operator's
    # one-off export) must not be replaced by the file.
    env, loaded = _load(tmp_path, "ANTHROPIC_API_KEY=from-file\nOTHER=x\n",
                        {"ANTHROPIC_API_KEY": ""})
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["OTHER"] == "x"
    assert loaded == ["OTHER"]


def test_quotes_comments_export_and_crlf(tmp_path):
    text = (
        "# leading comment\r\n"
        "\r\n"
        "export A=one\r\n"
        "B='single quoted # not a comment'\r\n"
        'C="double quoted"\r\n'
        "D=unquoted value # trailing comment\r\n"
        "E = spaced \r\n"
        "F=has=equals\r\n"
        "G=\r\n"
        "not a key value line\r\n"
        "9BAD=starts with digit\r\n"
    )
    env, _ = _load(tmp_path, text)
    assert env == {
        "A": "one",
        "B": "single quoted # not a comment",
        "C": "double quoted",
        "D": "unquoted value",
        "E": "spaced",
        "F": "has=equals",
        "G": "",
    }


def test_no_expansion_or_execution(tmp_path):
    # Values are taken literally: no $VAR expansion, no command substitution.
    env, _ = _load(tmp_path, 'A=$HOME\nB="$(id)"\nC=`id`\n')
    assert env == {"A": "$HOME", "B": "$(id)", "C": "`id`"}


def test_missing_file_is_fine(tmp_path):
    env = {}
    assert app.load_env_file(str(tmp_path / "absent"), env) == []
    assert env == {}


def test_default_path_is_app_dir_dot_env():
    assert app.ENV_FILE == os.path.join(app.BASE_DIR, ".env")
