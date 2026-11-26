from __future__ import annotations

import configparser
import errno
import locale
import os
import sys
import tempfile
from dataclasses import dataclass
from urllib.parse import urlparse


class PipConfigError(RuntimeError):
    """Raised when pip configuration cannot be read or updated safely."""


@dataclass(frozen=True)
class EffectiveIndexConfig:
    """Represents the effective pip index-url and where it came from."""

    url: str | None = None
    key: str | None = None
    scope: str | None = None
    source: str | None = None


SCOPE_LABELS = {
    "env-var": "环境变量 (PIP_INDEX_URL)",
    "env": "环境变量配置文件 (PIP_CONFIG_FILE)",
    "site": "当前环境级 (site)",
    "user": "用户级 (user)",
    "global": "系统全局级 (global)",
}


def _is_windows():
    return sys.platform == "win32"


def _config_basename():
    return "pip.ini" if _is_windows() else "pip.conf"


def _normalize_option_name(name):
    name = name.strip().lower().replace("_", "-")
    if name.startswith("--"):
        name = name[2:]
    return name


def get_pip_config_files():
    """Return pip configuration file paths by scope in pip's exact precedence order."""
    basename = _config_basename()
    home = os.path.expanduser("~")

    # 1. Global (system-wide)
    if _is_windows():
        program_data = (
            os.environ.get("PROGRAMDATA")
            or os.environ.get("ALLUSERSPROFILE")
            or r"C:\ProgramData"
        )
        global_files = [os.path.join(program_data, "pip", basename)]
    elif sys.platform == "darwin":
        xdg_data = os.environ.get("XDG_DATA_DIRS", "").strip()
        dirs = (
            [d for d in xdg_data.split(os.pathsep) if d]
            if xdg_data
            else ["/Library/Application Support"]
        )
        global_files = [os.path.join(d, "pip", basename) for d in dirs]
    else:
        xdg_cfg = os.environ.get("XDG_CONFIG_DIRS", "").strip() or "/etc/xdg"
        dirs = [d for d in xdg_cfg.split(os.pathsep) if d]
        global_files = [os.path.join(d, "pip", basename) for d in dirs] + [
            f"/etc/{basename}"
        ]

    # 2. User
    legacy_user_file = os.path.join(
        home,
        "pip" if _is_windows() else ".pip",
        basename,
    )
    if _is_windows():
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        modern_user_file = os.path.join(appdata, "pip", basename)
    elif sys.platform == "darwin":
        xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        mac_app_support = os.path.join(home, "Library", "Application Support", "pip")
        if xdg_home:
            modern_user_file = os.path.join(xdg_home, "pip", basename)
        elif os.path.isdir(mac_app_support):
            modern_user_file = os.path.join(mac_app_support, basename)
        else:
            modern_user_file = os.path.join(home, ".config", "pip", basename)
    else:
        xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.join(
            home, ".config"
        )
        modern_user_file = os.path.join(xdg_home, "pip", basename)

    user_files = [legacy_user_file]
    if os.path.normpath(modern_user_file) != os.path.normpath(legacy_user_file):
        user_files.append(modern_user_file)

    # 3. Site (active virtualenv / conda env takes priority when invoked via pipx/uv)
    site_files = []
    active_env = (
        os.environ.get("VIRTUAL_ENV", "").strip()
        or os.environ.get("CONDA_PREFIX", "").strip()
    )
    for prefix in (sys.prefix, active_env):
        if prefix:
            candidate = os.path.join(prefix, basename)
            if all(os.path.normpath(candidate) != os.path.normpath(p) for p in site_files):
                site_files.append(candidate)

    # 4. Env (PIP_CONFIG_FILE)
    env_cfg = os.environ.get("PIP_CONFIG_FILE", "").strip()
    env_files = [env_cfg] if env_cfg else []

    return {
        "global": global_files,
        "user": user_files,
        "site": site_files,
        "env": env_files,
    }


def _get_target_files_for_write(scope):
    scope_key = (scope or "user").lower()
    files_by_scope = get_pip_config_files()
    if scope_key not in ("global", "user", "site"):
        raise PipConfigError(f"不支持的配置作用域: {scope}")
    if scope_key == "site":
        # Prefer active VIRTUAL_ENV / CONDA_PREFIX (last item) when present
        return [files_by_scope["site"][-1]]
    return list(files_by_scope[scope_key])


def _read_raw_parser(fname, strict=False):
    parser = configparser.RawConfigParser()
    if not os.path.exists(fname):
        return parser

    encodings = ["utf-8-sig", "utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in ("utf-8", "utf8", "utf-8-sig"):
        encodings.append(preferred)

    last_decode_error = None
    for enc in encodings:
        candidate = configparser.RawConfigParser()
        try:
            with open(fname, "r", encoding=enc) as fh:
                candidate.read_file(fh, source=fname)
            return candidate
        except UnicodeDecodeError as err:
            last_decode_error = err
            continue
        except (OSError, configparser.Error) as err:
            if strict:
                raise PipConfigError(f"无法读取配置文件 {fname}: {err}") from err
            return parser

    if strict and last_decode_error is not None:
        raise PipConfigError(
            f"配置文件编码无效 ({fname}): {last_decode_error}"
        ) from last_decode_error
    return parser


def _parser_has_normalized_option(parser, section, norm_name):
    target_section = _find_matching_section(parser, section)
    if target_section is None:
        return False
    return any(
        _normalize_option_name(opt) == norm_name
        for opt, _ in parser.items(target_section)
    )


def _find_matching_section(parser, section):
    target = section.strip().lower()
    for sec in parser.sections():
        if sec.strip().lower() == target:
            return sec
    return None


def _set_normalized_option(parser, section, norm_name, value):
    sec = _find_matching_section(parser, section)
    if sec is None:
        sec = section.strip().lower()
        parser.add_section(sec)
    else:
        # Remove any alias spellings like index_url before writing canonical index-url
        for opt, _ in list(parser.items(sec)):
            if _normalize_option_name(opt) == norm_name and opt != norm_name:
                parser.remove_option(sec, opt)
    parser.set(sec, norm_name, value)


def _unset_normalized_option(parser, section, norm_name):
    sec = _find_matching_section(parser, section)
    if sec is None:
        return False
    removed = False
    for opt, _ in list(parser.items(sec)):
        if _normalize_option_name(opt) == norm_name:
            parser.remove_option(sec, opt)
            removed = True
    if removed and not parser.items(sec):
        parser.remove_section(sec)
    return removed


def _write_parser_atomically(fname, parser):
    parent = os.path.dirname(os.path.abspath(fname))
    if parent:
        os.makedirs(parent, exist_ok=True)

    existing_mode = None
    if os.path.exists(fname):
        try:
            existing_mode = os.stat(fname).st_mode
        except OSError:
            existing_mode = None

    # Attempt atomic temp-file + os.replace first; fall back to direct write
    # for single-file bind mounts (e.g. /etc/pip.conf in containers).
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".pip_conf_",
            suffix=".tmp",
            dir=parent or None,
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            parser.write(tmp_fh)
        if existing_mode is not None:
            try:
                os.chmod(tmp_path, existing_mode & 0o7777)
            except OSError:
                pass
        os.replace(tmp_path, fname)
        tmp_path = None
    except OSError as err:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if err.errno in (errno.EXDEV, errno.EBUSY, errno.EACCES, errno.EPERM) and os.path.exists(fname):
            with open(fname, "w", encoding="utf-8") as fh:
                parser.write(fh)
        else:
            raise


def _snapshot_files(file_paths):
    snapshots = {}
    for path in file_paths:
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    snapshots[path] = fh.read()
            except OSError as err:
                raise PipConfigError(f"无法读取现有配置文件 {path}: {err}") from err
        else:
            snapshots[path] = None
    return snapshots


def _restore_snapshots(snapshots):
    for path, raw_bytes in snapshots.items():
        try:
            if raw_bytes is None:
                if os.path.exists(path):
                    os.unlink(path)
            else:
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(raw_bytes)
        except OSError:
            pass


def apply_pip_config_changes(changes, scope="user"):
    """Atomically apply section.option changes to the specified pip config scope.

    Values mapped to None are removed across all files in that scope; non-None
    values are written to the primary active file for that scope.
    """
    env_cfg = os.environ.get("PIP_CONFIG_FILE", "").strip()
    if env_cfg == os.devnull:
        raise PipConfigError(
            f"环境变量 PIP_CONFIG_FILE 指向 {os.devnull}，无法写入配置文件"
        )

    target_files = _get_target_files_for_write(scope)
    snapshots = _snapshot_files(target_files)
    parsers = [(fname, _read_raw_parser(fname, strict=True)) for fname in target_files]

    # Choose primary file for setting values (mirrors pip's _get_parser_to_modify):
    # default to highest-priority (last) file unless an earlier file already has global.index-url
    # and the modern file does not exist yet.
    primary_fname, primary_parser = parsers[-1]
    if not os.path.exists(primary_fname) and len(parsers) > 1:
        for fname, parser in reversed(parsers[:-1]):
            if os.path.exists(fname) and (
                _parser_has_normalized_option(parser, "global", "index-url")
                or parser.sections()
            ):
                primary_fname, primary_parser = fname, parser
                break

    modified_files = set()
    for raw_key, value in changes.items():
        if "." not in raw_key:
            raise PipConfigError(f"无效的 pip 配置键: {raw_key}")
        section, opt = raw_key.split(".", 1)
        section = section.strip().lower()
        norm_opt = _normalize_option_name(opt)

        if value is None:
            for fname, parser in parsers:
                if _unset_normalized_option(parser, section, norm_opt):
                    modified_files.add(fname)
        else:
            # Remove conflicting definitions from secondary files in the same scope
            for fname, parser in parsers:
                if fname != primary_fname and _unset_normalized_option(
                    parser, section, norm_opt
                ):
                    modified_files.add(fname)
            _set_normalized_option(primary_parser, section, norm_opt, str(value))
            modified_files.add(primary_fname)

    try:
        for fname, parser in parsers:
            if fname in modified_files:
                _write_parser_atomically(fname, parser)
    except OSError as err:
        _restore_snapshots(snapshots)
        raise PipConfigError(str(err)) from err


def get_effective_index_config():
    """Determine the effective pip index-url following pip's exact precedence rules.

    Precedence:
      1. PIP_INDEX_URL environment variable (:env:)
      2. install.index-url across scopes: global -> user -> site -> env (PIP_CONFIG_FILE)
      3. global.index-url across scopes: global -> user -> site -> env (PIP_CONFIG_FILE)
    """
    env_url = os.environ.get("PIP_INDEX_URL", "").strip()
    if env_url:
        return EffectiveIndexConfig(
            url=env_url,
            key="PIP_INDEX_URL",
            scope="env-var",
            source="PIP_INDEX_URL",
        )

    files_by_scope = get_pip_config_files()
    env_files = files_by_scope["env"]
    if env_files and env_files[0] == os.devnull:
        return EffectiveIndexConfig()

    # Track merged section dicts like pip's ConfigOptionParser._get_ordered_configuration_items
    section_winners = {
        "global": None,   # (val, scope, fname)
        "install": None,  # (val, scope, fname)
    }

    for scope in ("global", "user", "site", "env"):
        for fname in files_by_scope[scope]:
            if not fname or not os.path.exists(fname):
                continue
            parser = _read_raw_parser(fname, strict=False)
            for target_sec in ("global", "install"):
                actual_sec = _find_matching_section(parser, target_sec)
                if actual_sec is None:
                    continue
                for opt, val in parser.items(actual_sec):
                    if _normalize_option_name(opt) == "index-url":
                        cleaned = val.strip()
                        section_winners[target_sec] = (cleaned, scope, fname)

    for sec in ("install", "global"):
        winner = section_winners[sec]
        if winner and winner[0]:
            val, scope, fname = winner
            return EffectiveIndexConfig(
                url=val,
                key=f"{sec}.index-url",
                scope=scope,
                source=fname,
            )

    return EffectiveIndexConfig()


def get_current_index_url():
    """Return the effective pip index URL string, or None when unconfigured."""
    return get_effective_index_config().url


def set_pip_mirror(url, trusted_host=None, scope="user"):
    """Set pip's index URL and clean up conflicting/legacy mirror options in the target scope."""
    apply_pip_config_changes(
        {
            "global.index-url": url,
            "install.index-url": None,
            "global.trusted-host": trusted_host,
            "install.trusted-host": None,
        },
        scope=scope,
    )


def reset_pip_config(scope="user"):
    """Remove mirror-related values in the target scope so pip falls back to defaults."""
    apply_pip_config_changes(
        {
            "global.index-url": None,
            "install.index-url": None,
            "global.trusted-host": None,
            "install.trusted-host": None,
        },
        scope=scope,
    )


def validate_custom_mirror_url(value, allow_insecure=False):
    """Validate a custom index URL and return (url, explicit_trusted_host)."""
    if not value:
        raise ValueError("参数必须为预设别名或合法的 HTTP/HTTPS 镜像 URL")
    if value != value.strip():
        raise ValueError("URL 不能包含首尾空格或控制字符")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("URL 不能包含空格或控制字符")

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"URL 端口无效: {error}") from error

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("参数必须为预设别名或合法的 HTTP/HTTPS 镜像 URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 中不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("镜像 URL 不能包含查询参数或片段")
    if scheme == "http" and not allow_insecure:
        raise ValueError("HTTP 源不安全；如确需使用，请显式添加 --allow-insecure")

    trusted_host = None
    if scheme == "http":
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        trusted_host = f"{hostname}:{port}" if port is not None else hostname
    return value, trusted_host
