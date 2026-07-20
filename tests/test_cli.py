import ssl
import subprocess
import sys

import pytest
from click.testing import CliRunner

from yyds_pip import cli


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def test_request_headers_are_minimal_and_honest():
    headers = cli.get_request_headers("https://example.com/simple/")

    assert headers["User-Agent"].startswith("yyds-pip/")
    assert "Mozilla" not in headers["User-Agent"]
    assert "Host" not in headers


def test_single_mirror_uses_verified_default_context(monkeypatch):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    seen = {}

    monkeypatch.setattr(cli.ssl, "create_default_context", lambda: context)

    def fake_urlopen(request, timeout, context):
        seen["context"] = context
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    alias, latency, success = cli.test_single_mirror(
        "example",
        {"url": "https://example.com/simple/"},
        timeout=1.5,
    )

    assert alias == "example"
    assert latency >= 0
    assert success is True
    assert seen == {"context": context, "timeout": 1.5}


@pytest.mark.parametrize(
    "value, message",
    [
        ("not-a-url", "合法的 HTTP/HTTPS"),
        ("https://user:secret@example.com/simple/", "用户名或密码"),
        ("https://example.com/simple/?token=secret", "查询参数"),
        (" https://example.com/simple/", "首尾空格"),
    ],
)
def test_custom_url_validation_rejects_unsafe_values(value, message):
    with pytest.raises(ValueError, match=message):
        cli.validate_custom_mirror_url(value)


def test_http_source_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="--allow-insecure"):
        cli.validate_custom_mirror_url("http://localhost:8080/simple/")

    url, trusted_host = cli.validate_custom_mirror_url(
        "http://localhost:8080/simple/",
        allow_insecure=True,
    )
    assert url == "http://localhost:8080/simple/"
    assert trusted_host == "localhost:8080"


def test_pip_config_command_uses_running_interpreter():
    command = cli._pip_config_command(
        "set",
        "global.index-url",
        "https://example.com/simple/",
        scope="site",
    )

    assert command[:4] == [sys.executable, "-m", "pip", "config"]
    assert command[4:] == [
        "--site",
        "set",
        "global.index-url",
        "https://example.com/simple/",
    ]


def test_config_update_rolls_back_after_partial_failure(monkeypatch):
    previous = {
        "global.index-url": "https://old.example/simple/",
        "global.trusted-host": None,
        "install.trusted-host": None,
    }
    calls = []

    monkeypatch.setattr(
        cli,
        "_get_pip_config_value",
        lambda key, scope=None: previous[key],
    )

    def fake_run(*args, scope=None, check=False):
        calls.append((args, scope, check))
        if args[:2] == ("set", "global.trusted-host") and check:
            raise subprocess.CalledProcessError(
                1,
                list(args),
                stderr="permission denied",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli, "_run_pip_config", fake_run)

    with pytest.raises(cli.PipConfigError, match="permission denied"):
        cli.set_pip_mirror(
            "http://new.example/simple/",
            "new.example",
            scope="user",
        )

    assert (("set", "global.index-url", "https://old.example/simple/"), "user", False) in calls
    assert (("unset", "global.trusted-host"), "user", False) in calls


def test_environment_index_url_has_precedence(monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://env.example/simple/")

    def unexpected_read(*args, **kwargs):
        raise AssertionError("pip config should not be queried")

    monkeypatch.setattr(cli, "_get_pip_config_value", unexpected_read)
    assert cli.get_current_index_url() == "https://env.example/simple/"


def test_invalid_set_argument_returns_nonzero_exit_code():
    result = CliRunner().invoke(cli.main, ["set", "definitely-not-a-url"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_set_https_source_does_not_add_trusted_host(monkeypatch):
    calls = []
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setattr(
        cli,
        "set_pip_mirror",
        lambda url, trusted_host, scope: calls.append((url, trusted_host, scope)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["set", "--scope", "site", "https://example.com/simple/"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("https://example.com/simple/", None, "site")]


def test_set_configuration_failure_returns_nonzero(monkeypatch):
    def fail(*args, **kwargs):
        raise cli.PipConfigError("write failed")

    monkeypatch.setattr(cli, "set_pip_mirror", fail)
    result = CliRunner().invoke(cli.main, ["set", "tsinghua"])

    assert result.exit_code == 1
    assert "write failed" in result.output


def test_select_fails_cleanly_without_a_tty():
    result = CliRunner().invoke(cli.main, ["select"])

    assert result.exit_code == 1
    assert "需要 TTY" in result.output
    assert "Traceback" not in result.output


def test_default_command_fails_cleanly_without_a_tty():
    result = CliRunner().invoke(cli.main, [])

    assert result.exit_code == 1
    assert "需要 TTY" in result.output
    assert "Traceback" not in result.output


def test_timeout_must_be_positive():
    result = CliRunner().invoke(cli.main, ["test", "--timeout", "-1"])

    assert result.exit_code == 2
    assert "0.1<=x<=60.0" in result.output


def test_best_returns_nonzero_when_all_mirrors_fail(monkeypatch):
    failed_results = {
        alias: (float("inf"), False)
        for alias in cli.MIRRORS
    }
    monkeypatch.setattr(
        cli,
        "test_all_mirrors_parallel",
        lambda timeout: failed_results,
    )

    result = CliRunner().invoke(cli.main, ["best", "--timeout", "0.1"])

    assert result.exit_code == 1
    assert "所有镜像源均测试失败" in result.output
