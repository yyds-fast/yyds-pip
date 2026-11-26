import io
import os
import ssl
from collections import deque
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

from yyds_pip import cli, config, mirrors, prober, tui


class FakeResponse:
    def __init__(self, status=200, body=b"<html>six</html>"):
        self.status = status
        self._body = body

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


@pytest.fixture
def isolated_pip_paths(tmp_path, monkeypatch):
    """Isolate global/user/site/env pip config paths inside tmp_path."""
    paths = {
        "global": [str(tmp_path / "etc" / "pip.conf")],
        "user": [
            str(tmp_path / "home" / ".pip" / "pip.conf"),
            str(tmp_path / "home" / ".config" / "pip" / "pip.conf"),
        ],
        "site": [str(tmp_path / "venv" / "pip.conf")],
        "env": [],
    }
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.setattr(
        config,
        "get_pip_config_files",
        lambda: {
            "global": list(paths["global"]),
            "user": list(paths["user"]),
            "site": list(paths["site"]),
            "env": [os.environ["PIP_CONFIG_FILE"]]
            if os.environ.get("PIP_CONFIG_FILE", "").strip()
            else list(paths["env"]),
        },
    )
    return paths


def test_request_headers_are_minimal_and_honest():
    headers = cli.get_request_headers("https://example.com/simple/")

    assert headers["User-Agent"].startswith("yyds-pip/")
    assert "Mozilla" not in headers["User-Agent"]
    assert headers["Accept-Encoding"] == "gzip"
    assert "Host" not in headers


def test_single_mirror_uses_verified_default_context(monkeypatch):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    seen = {}

    monkeypatch.setattr(cli.ssl, "create_default_context", lambda: context)

    def fake_urlopen(request, timeout, context):
        seen["url"] = request.full_url
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
    assert seen == {
        "url": "https://example.com/simple/six/",
        "context": context,
        "timeout": 1.5,
    }


def test_single_mirror_rejects_empty_response_body(monkeypatch):
    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda request, timeout, context: FakeResponse(status=200, body=b""),
    )

    alias, latency, success = cli.test_single_mirror(
        "example",
        {"url": "https://example.com/simple/"},
        timeout=1.0,
    )

    assert alias == "example"
    assert latency == float("inf")
    assert success is False


@pytest.mark.parametrize(
    "value, message",
    [
        ("not-a-url", "合法的 HTTP/HTTPS"),
        ("https://user:secret@example.com/simple/", "用户名或密码"),
        ("https://example.com/simple/?token=secret", "查询参数"),
        (" https://example.com/simple/", "首尾空格"),
        ("https://example.com/my path/simple/", "空格或控制字符"),
        ("https://example.com/simple/\x00", "空格或控制字符"),
    ],
)
def test_custom_url_validation_rejects_unsafe_values(value, message):
    with pytest.raises(ValueError, match=message):
        cli.validate_custom_mirror_url(value)


def test_http_source_requires_explicit_opt_in_and_formats_ipv6():
    with pytest.raises(ValueError, match="--allow-insecure"):
        cli.validate_custom_mirror_url("http://localhost:8080/simple/")

    url, trusted_host = cli.validate_custom_mirror_url(
        "http://localhost:8080/simple/",
        allow_insecure=True,
    )
    assert url == "http://localhost:8080/simple/"
    assert trusted_host == "localhost:8080"

    ipv6_url, ipv6_host = cli.validate_custom_mirror_url(
        "http://[::1]:8080/simple/",
        allow_insecure=True,
    )
    assert ipv6_url == "http://[::1]:8080/simple/"
    assert ipv6_host == "[::1]:8080"


def test_multi_scope_config_precedence_and_partial_site_file(isolated_pip_paths):
    # 1. Global scope alone is visible (fixes bug where `pip config get` skipped /etc/pip.conf)
    cli.set_pip_mirror("https://mirrors.aliyun.com/pypi/simple/", scope="global")
    eff = cli.get_effective_index_config()
    assert eff.url == "https://mirrors.aliyun.com/pypi/simple/"
    assert eff.scope == "global"
    assert eff.key == "global.index-url"

    # 2. User scope overrides global
    cli.set_pip_mirror("https://pypi.tuna.tsinghua.edu.cn/simple/", scope="user")
    eff = cli.get_effective_index_config()
    assert eff.url == "https://pypi.tuna.tsinghua.edu.cn/simple/"
    assert eff.scope == "user"

    # 3. A site file WITHOUT index-url (e.g. only timeout=30) must NOT shadow user index-url
    site_file = isolated_pip_paths["site"][0]
    os.makedirs(os.path.dirname(site_file), exist_ok=True)
    with open(site_file, "w", encoding="utf-8") as fh:
        fh.write("[global]\ntimeout = 30\n")
    assert cli.get_current_index_url() == "https://pypi.tuna.tsinghua.edu.cn/simple/"

    # 4. Site index-url overrides user index-url
    cli.set_pip_mirror("https://mirrors.ustc.edu.cn/pypi/simple/", scope="site")
    eff = cli.get_effective_index_config()
    assert eff.url == "https://mirrors.ustc.edu.cn/pypi/simple/"
    assert eff.scope == "site"


def test_set_mirror_cleans_up_shadowing_install_index_url(isolated_pip_paths):
    modern_user = isolated_pip_paths["user"][1]
    os.makedirs(os.path.dirname(modern_user), exist_ok=True)
    with open(modern_user, "w", encoding="utf-8") as fh:
        fh.write(
            "[global]\nindex-url = https://old.example/simple/\n"
            "[install]\nindex-url = https://shadow.example/simple/\n"
            "trusted-host = shadow.example\n"
        )

    # Before update, install.index-url wins per pip semantics
    assert cli.get_current_index_url() == "https://shadow.example/simple/"

    # Updating user mirror removes stale [install] index-url and trusted-host
    cli.set_pip_mirror("https://pypi.tuna.tsinghua.edu.cn/simple/", scope="user")
    eff = cli.get_effective_index_config()
    assert eff.url == "https://pypi.tuna.tsinghua.edu.cn/simple/"
    assert eff.key == "global.index-url"

    with open(modern_user, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "shadow.example" not in content


def test_config_update_rolls_back_all_files_on_write_failure(
    isolated_pip_paths, monkeypatch
):
    legacy_user, modern_user = isolated_pip_paths["user"]
    os.makedirs(os.path.dirname(legacy_user), exist_ok=True)
    os.makedirs(os.path.dirname(modern_user), exist_ok=True)

    original_legacy = "[global]\nindex-url = https://legacy.example/simple/\n"
    original_modern = "[global]\nindex-url = https://modern.example/simple/\n"
    with open(legacy_user, "w", encoding="utf-8") as fh:
        fh.write(original_legacy)
    with open(modern_user, "w", encoding="utf-8") as fh:
        fh.write(original_modern)

    real_write = config._write_parser_atomically
    calls = []

    def fail_on_second_write(fname, parser):
        calls.append(fname)
        if len(calls) == 2:
            raise PermissionError("permission denied")
        return real_write(fname, parser)

    monkeypatch.setattr(config, "_write_parser_atomically", fail_on_second_write)

    with pytest.raises(cli.PipConfigError, match="permission denied"):
        cli.set_pip_mirror("https://new.example/simple/", scope="user")

    with open(legacy_user, "r", encoding="utf-8") as fh:
        assert fh.read() == original_legacy
    with open(modern_user, "r", encoding="utf-8") as fh:
        assert fh.read() == original_modern


def test_environment_index_url_has_precedence(isolated_pip_paths, monkeypatch):
    cli.set_pip_mirror("https://mirrors.aliyun.com/pypi/simple/", scope="user")
    monkeypatch.setenv("PIP_INDEX_URL", "https://env.example/simple/")

    eff = cli.get_effective_index_config()
    assert eff.url == "https://env.example/simple/"
    assert eff.scope == "env-var"
    assert eff.key == "PIP_INDEX_URL"


def test_pip_config_file_devnull_disables_file_reads_and_blocks_writes(
    isolated_pip_paths, monkeypatch
):
    cli.set_pip_mirror("https://mirrors.aliyun.com/pypi/simple/", scope="user")
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)

    assert cli.get_current_index_url() is None
    with pytest.raises(cli.PipConfigError, match="PIP_CONFIG_FILE"):
        cli.set_pip_mirror("https://pypi.tuna.tsinghua.edu.cn/simple/", scope="user")


def test_deprecated_douban_mirror_is_excluded_and_rejected_helpfully():
    assert "douban" not in cli.MIRRORS
    result = CliRunner().invoke(cli.main, ["set", "douban"])
    assert result.exit_code == 2
    assert "已停止服务" in result.output


def test_case_insensitive_alias_and_higher_scope_override_warning(isolated_pip_paths):
    cli.set_pip_mirror("https://mirrors.ustc.edu.cn/pypi/simple/", scope="site")

    result = CliRunner().invoke(cli.main, ["set", "--scope", "user", "Tsinghua"])
    assert result.exit_code == 0, result.output
    assert "清华大学" in result.output
    assert "更高优先级的配置" in result.output
    assert "mirrors.ustc.edu.cn" in result.output


def test_invalid_set_argument_returns_nonzero_exit_code():
    result = CliRunner().invoke(cli.main, ["set", "definitely-not-a-url"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_set_https_source_does_not_add_trusted_host(monkeypatch, isolated_pip_paths):
    calls = []
    monkeypatch.setattr(
        cli,
        "set_pip_mirror",
        lambda url, trusted_host, scope: calls.append((url, trusted_host, scope)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["set", "--scope", "SITE", "https://example.com/simple/"],
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


def test_show_list_test_and_reset_commands(isolated_pip_paths, monkeypatch):
    # Set legacy Douban URL in user config and verify `show` flags deprecation
    modern_user = isolated_pip_paths["user"][1]
    os.makedirs(os.path.dirname(modern_user), exist_ok=True)
    with open(modern_user, "w", encoding="utf-8") as fh:
        fh.write("[global]\nindex-url = https://pypi.doubanio.com/simple/\n")

    monkeypatch.setattr(
        cli,
        "check_network_status",
        lambda timeout=1.5, current_url=None: (32.5, True, "清华大学: 32.5 ms"),
    )
    show_res = CliRunner().invoke(cli.main, ["show"])
    assert show_res.exit_code == 0, show_res.output
    assert "已停服" in show_res.output
    assert "已停止服务" in show_res.output

    # Reset clears user config; list and test both mark official `pypi` with ★
    reset_res = CliRunner().invoke(cli.main, ["reset"])
    assert reset_res.exit_code == 0, reset_res.output
    assert cli.get_current_index_url() is None

    list_res = CliRunner().invoke(cli.main, ["list"])
    assert list_res.exit_code == 0
    assert "★" in list_res.output
    assert "bfsu" in list_res.output

    fake_results = {alias: (45.0, True) for alias in cli.MIRRORS}
    monkeypatch.setattr(cli, "test_all_mirrors_parallel", lambda timeout: fake_results)
    test_res = CliRunner().invoke(cli.main, ["test"])
    assert test_res.exit_code == 0, test_res.output
    assert "★" in test_res.output


def test_select_and_default_command_fail_cleanly_without_a_tty():
    for args in (["select"], []):
        result = CliRunner().invoke(cli.main, args)
        assert result.exit_code == 1
        assert "需要 TTY" in result.output
        assert "Traceback" not in result.output


def test_timeout_must_be_positive():
    result = CliRunner().invoke(cli.main, ["test", "--timeout", "-1"])

    assert result.exit_code == 2
    assert "0.1<=x<=60.0" in result.output


def test_best_returns_nonzero_when_all_mirrors_fail(monkeypatch):
    failed_results = {alias: (float("inf"), False) for alias in cli.MIRRORS}
    monkeypatch.setattr(
        cli,
        "test_all_mirrors_parallel",
        lambda timeout: failed_results,
    )

    result = CliRunner().invoke(cli.main, ["best", "--timeout", "0.1"])

    assert result.exit_code == 1
    assert "所有镜像源均测试失败" in result.output


@pytest.mark.parametrize(
    "raw_bytes, expected_first, expected_remaining",
    [
        (b"\x1b[A", "\x1b[A", b""),          # Standard CSI Up
        (b"\x1b[B", "\x1b[B", b""),          # Standard CSI Down
        (b"\x1bOA", "\x1b[A", b""),          # SS3 Up (tmux / app cursor mode)
        (b"\x1bOB", "\x1b[B", b""),          # SS3 Down
        (b"\x1b[H", "HOME", b""),            # Home
        (b"\x1b[4~", "END", b""),            # VT End
        (b"\x1b[5~q", None, b"q"),           # PageUp drains '~' without swallowing 'q'
        (b"\x1b[3~", None, b""),             # Delete drains '~'
        (b"\x1bx", None, b""),               # Alt+x does NOT falsely quit as Esc
        (b"\x1b", "\x1b", b""),              # Bare Esc quits
        ("中".encode("utf-8"), "中", b""),   # Multi-byte UTF-8 consumed intact
    ],
)
def test_parse_posix_key_handles_escape_sequences_cleanly(
    raw_bytes, expected_first, expected_remaining
):
    buf = deque(bytes([b]) for b in raw_bytes)

    def read_byte():
        return buf.popleft() if buf else b""

    def has_input(_timeout):
        return bool(buf)

    parsed = tui.parse_posix_key(read_byte, has_input)
    assert parsed == expected_first
    assert b"".join(buf) == expected_remaining


def test_menu_highlight_and_responsive_viewport_have_no_nested_color_clashes():
    items = [
        {
            "alias": "tsinghua",
            "name": "清华大学",
            "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",
            "trusted_host": None,
            "latency": None,
            "is_current": True,
        },
        {
            "alias": "aliyun",
            "name": "阿里云",
            "url": "https://mirrors.aliyun.com/pypi/simple/",
            "trusted_host": None,
            "latency": 22.4,
            "is_current": False,
        },
        {
            "alias": "cancel",
            "name": "❌ 取消选择 (Cancel)",
            "url": "",
            "trusted_host": None,
            "latency": float("inf"),
            "is_current": False,
        },
    ]

    # Render selected pending row on narrow + short terminal
    panel = tui.make_menu_renderable(
        items,
        selected_index=0,
        console_width=80,
        max_visible_rows=2,
    )
    buf = io.StringIO()
    test_console = Console(file=buf, width=80, force_terminal=False)
    test_console.print(panel)
    rendered = buf.getvalue()

    assert "清华大学 (当前)" in rendered
    assert "测速中..." in rendered
    assert "第 1/3 项" in rendered
    # Narrow terminal hides the wide URL column
    assert "镜像源地址" not in rendered


def test_interactive_selector_end_to_end(monkeypatch):
    sample_mirrors = {
        "slow": {
            "name": "慢速源",
            "url": "https://slow.example/simple/",
            "trusted_host": None,
        },
        "fast": {
            "name": "极速源",
            "url": "https://fast.example/simple/",
            "trusted_host": None,
        },
    }

    def fake_probe(alias, info, timeout=3.0, ssl_context=None, opener=None):
        return alias, (15.0 if alias == "fast" else 180.0), True

    keys = iter(["HOME", "\r"])

    class FakeKeyReader:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def read_key(self):
            return next(keys)

    monkeypatch.setattr(tui, "test_single_mirror", fake_probe)
    monkeypatch.setattr(tui, "TerminalKeyReader", FakeKeyReader)

    silent_console = Console(
        file=io.StringIO(),
        width=120,
        height=30,
        force_terminal=False,
    )
    # Wait briefly by letting executor finish before HOME + Enter is consumed:
    orig_read = FakeKeyReader.read_key

    def synced_read(self):
        import time

        time.sleep(0.02)
        return orig_read(self)

    FakeKeyReader.read_key = synced_read

    chosen = tui.run_interactive_selector(
        console=silent_console,
        current_url="https://slow.example/simple/",
        timeout=1.0,
        mirrors=sample_mirrors,
    )
    assert chosen is not None
    assert chosen["alias"] == "fast"
    assert chosen["url"] == "https://fast.example/simple/"
