import os
import sys
import time
import subprocess
import urllib.request
import ssl
import select
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import click
from rich.console import Console, Group
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.panel import Panel
from rich import box

from .__version__ import __version__

# System specific imports for reading keys
try:
    import tty
    import termios
    WINDOWS = False
except ImportError:
    WINDOWS = True

# Define popular PyPI mirrors in China
MIRRORS = {
    "aliyun": {
        "name": "阿里云",
        "url": "https://mirrors.aliyun.com/pypi/simple/",
        "trusted_host": None
    },
    "tsinghua": {
        "name": "清华大学",
        "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "trusted_host": None
    },
    "tencent": {
        "name": "腾讯云",
        "url": "https://mirrors.cloud.tencent.com/pypi/simple/",
        "trusted_host": None
    },
    "douban": {
        "name": "豆瓣",
        "url": "https://pypi.doubanio.com/simple/",
        "trusted_host": None
    },
    "huawei": {
        "name": "华为云",
        "url": "https://repo.huaweicloud.com/repository/pypi/simple/",
        "trusted_host": None
    },
    "ustc": {
        "name": "中国科学技术大学",
        "url": "https://pypi.mirrors.ustc.edu.cn/simple/",
        "trusted_host": None
    },
    "sjtu": {
        "name": "上海交通大学",
        "url": "https://mirrors.sjtug.sjtu.edu.cn/pypi/web/simple/",
        "trusted_host": None
    },
    "pypi": {
        "name": "PyPI 官方",
        "url": "https://pypi.org/simple/",
        "trusted_host": None
    }
}

console = Console()

DEFAULT_INDEX_URL = "https://pypi.org/simple/"
TIMEOUT_TYPE = click.FloatRange(min=0.1, max=60.0)
CONFIG_SCOPE_TYPE = click.Choice(["user", "site", "global"], case_sensitive=False)

# ----------------- Helper functions for reading terminal keys -----------------

def get_key():
    """Read one keypress and normalize the arrow keys across platforms."""
    if WINDOWS:
        # Simple fallback for Windows if needed
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'): # arrow keys
            ch2 = msvcrt.getch()
            if ch2 == b'H': return '\x1b[A' # Up
            if ch2 == b'P': return '\x1b[B' # Down
        if ch == b'\r': return '\r'
        if ch == b'\n': return '\n'
        if ch == b'\x1b': return '\x1b'
        if ch == b'\x03': return '\x03'
        try:
            return ch.decode('utf-8')
        except UnicodeDecodeError:
            return None

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        # cbreak keeps terminal output processing enabled while Live redraws.
        tty.setcbreak(fd)
        # Block until stdin has data
        ch = os.read(fd, 1).decode('utf-8', errors='ignore')
        if ch == '\x1b':
            # Check if it's an arrow key escape sequence
            r2, _, _ = select.select([fd], [], [], 0.05)
            if r2:
                ch2 = os.read(fd, 1).decode('utf-8', errors='ignore')
                if ch2 == '[':
                    r3, _, _ = select.select([fd], [], [], 0.05)
                    if r3:
                        ch3 = os.read(fd, 1).decode('utf-8', errors='ignore')
                        return f"\x1b[{ch3}"
                return '\x1b'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

# ----------------- Helper functions for pip config -----------------

class PipConfigError(RuntimeError):
    """Raised when pip configuration cannot be updated safely."""


def _pip_config_command(*args, scope=None):
    command = [sys.executable, "-m", "pip", "config"]
    if scope:
        command.append(f"--{scope}")
    command.extend(args)
    return command


def _run_pip_config(*args, scope=None, check=False):
    return subprocess.run(
        _pip_config_command(*args, scope=scope),
        capture_output=True,
        text=True,
        check=check,
    )


def _get_pip_config_value(key, scope=None):
    try:
        result = _run_pip_config("get", key, scope=scope)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def get_current_index_url():
    """Return the effective pip index URL that can be determined locally."""
    environment_url = os.environ.get("PIP_INDEX_URL", "").strip()
    if environment_url:
        return environment_url

    for key in ("install.index-url", "global.index-url"):
        value = _get_pip_config_value(key)
        if value:
            return value
    return None


def _restore_pip_config(previous_values, scope):
    for key, value in previous_values.items():
        try:
            if value is None:
                _run_pip_config("unset", key, scope=scope)
            else:
                _run_pip_config("set", key, value, scope=scope)
        except OSError:
            pass


def _apply_pip_config(changes, scope="user"):
    previous_values = {
        key: _get_pip_config_value(key, scope=scope)
        for key in changes
    }
    try:
        for key, value in changes.items():
            if value is None:
                if previous_values[key] is not None:
                    _run_pip_config("unset", key, scope=scope, check=True)
            else:
                _run_pip_config("set", key, value, scope=scope, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        _restore_pip_config(previous_values, scope)
        detail = getattr(error, "stderr", "") or str(error)
        raise PipConfigError(detail.strip()) from error


def set_pip_mirror(url, trusted_host=None, scope="user"):
    """Set pip's index URL and remove implicit TLS trust exceptions."""
    _apply_pip_config({
        "global.index-url": url,
        "global.trusted-host": trusted_host,
        # Clean up the key written by yyds-pip versions before 0.4.7.
        "install.trusted-host": None,
    }, scope=scope)


def reset_pip_config(scope="user"):
    """Remove mirror-related values so pip can fall back to its defaults."""
    _apply_pip_config({
        "global.index-url": None,
        "install.index-url": None,
        "global.trusted-host": None,
        "install.trusted-host": None,
    }, scope=scope)


def warn_if_environment_overrides(configured_url=None):
    environment_url = os.environ.get("PIP_INDEX_URL", "").strip()
    if not environment_url:
        return
    if configured_url and environment_url.rstrip("/") == configured_url.rstrip("/"):
        return
    console.print(
        "[bold yellow]⚠ 环境变量 PIP_INDEX_URL 仍会覆盖上述配置：[/bold yellow] "
        f"[yellow]{escape(environment_url)}[/yellow]"
    )


def validate_custom_mirror_url(value, allow_insecure=False):
    """Validate a custom index URL and return its explicit trust exception."""
    if value != value.strip() or any(char in value for char in "\r\n\t"):
        raise ValueError("URL 不能包含首尾空格或控制字符")

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

# ----------------- Helper functions for HTTP headers -----------------

def get_request_headers(url=None):
    """Return minimal, honest headers suitable for a PEP 503 endpoint."""
    return {
        "User-Agent": f"yyds-pip/{__version__} (+https://github.com/yyds-fast/yyds-pip)",
        "Accept": (
            "application/vnd.pypi.simple.v1+json, "
            "application/vnd.pypi.simple.v1+html, text/html;q=0.9"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

# ----------------- Helper functions for speed testing -----------------

def test_single_mirror(alias, info, timeout=3.0, ssl_context=None):
    """Tests the latency of a single mirror URL."""
    url = info["url"]
    # Append 'pip/' to request a small packages page (reliable latency test)
    test_url = url.rstrip('/') + '/pip/'
    if ssl_context is None:
        ssl_context = ssl.create_default_context()
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            test_url,
            headers=get_request_headers(test_url)
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            if response.status == 200:
                duration = (time.perf_counter() - start) * 1000
                return alias, duration, True
    except Exception:
        pass
    return alias, float('inf'), False

def test_all_mirrors_parallel(timeout=3.0):
    """Concurrently tests speeds of all mirrors using a ThreadPoolExecutor."""
    results = {}
    ssl_context = ssl.create_default_context()
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as executor:
        futures = {
            executor.submit(
                test_single_mirror,
                alias,
                info,
                timeout,
                ssl_context,
            ): alias
            for alias, info in MIRRORS.items()
        }
        for future in as_completed(futures):
            alias = futures[future]
            try:
                alias_res, duration, success = future.result()
                results[alias_res] = (duration, success)
            except Exception:
                results[alias] = (float('inf'), False)
    return results

# ----------------- Helper functions for network status -----------------

def check_network_status(timeout=1.5):
    """检测当前网络连接状态及延迟，返回 (延迟, 是否成功, 详细网络状况描述)"""
    target_urls = [
        ("https://www.baidu.com", "baidu.com"),
        ("https://www.aliyun.com", "aliyun.com"),
        ("https://www.360.cn", "360.cn")
    ]
    ssl_context = ssl.create_default_context()
    
    def test_url(url, label):
        start = time.perf_counter()
        try:
            req = urllib.request.Request(
                url,
                headers=get_request_headers(url)
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
                if response.status == 200:
                    duration = (time.perf_counter() - start) * 1000
                    return label, duration, True
        except Exception:
            pass
        return label, float('inf'), False

    results = {}
    with ThreadPoolExecutor(max_workers=len(target_urls)) as executor:
        futures = [
            executor.submit(test_url, url, label)
            for url, label in target_urls
        ]
        for future in as_completed(futures):
            label, duration, success = future.result()
            results[label] = (duration, success)

    # Process results
    success_results = {label: res for label, res in results.items() if res[1]}

    # Format detail string
    detail_parts = []
    for _, label in target_urls:
        if label in results:
            duration, success = results[label]
            if success:
                detail_parts.append(f"{label}: {duration:.1f} ms")
            else:
                detail_parts.append(f"{label}: [bold red]🔴 异常[/bold red]")
        else:
            detail_parts.append(f"{label}: [bold red]🔴 超时[/bold red]")

    details_str = ", ".join(detail_parts)

    if success_results:
        min_label = min(success_results.keys(), key=lambda l: success_results[l][0])
        min_latency = success_results[min_label][0]
        return min_latency, True, details_str

    return float('inf'), False, details_str

def format_network_status(latency, success, target=None):
    """格式化网络状况为富文本格式，包含状态文字、颜色和警告提示"""
    if not success or latency == float('inf'):
        return "[bold red]🔴 已断开[/bold red]", "[bold red]⚠️  当前网络已断开，请检查您的网络连接！[/bold red]"
    
    if target:
        if "ms" in target:
            target_info = f" ({target})"
        else:
            target_info = f" ({latency:.1f} ms via {target})"
    else:
        target_info = f" ({latency:.1f} ms)"
        
    if latency < 50:
        status = f"[bold green]🟢 极佳{target_info}[/bold green]"
        warning = None
    elif latency < 150:
        status = f"[bold green]🟢 良好{target_info}[/bold green]"
        warning = None
    elif latency < 300:
        status = f"[bold yellow]🟡 一般{target_info}[/bold yellow]"
        warning = "[bold yellow]⚠️  当前网络延迟一般，可能会影响下载速度。[/bold yellow]"
    else:
        status = f"[bold red]🟠 较慢{target_info}[/bold red]"
        warning = "[bold red]⚠️  当前网络状况较慢，建议您检查网络或更换更快的镜像源！[/bold red]"
        
    return status, warning

def evaluate_network_from_results(results):
    """根据镜像源测速结果评估当前网络状态，返回 (延迟, 是否成功, 测速目标)"""
    successful_items = [(lat, alias) for alias, (lat, succ) in results.items() if succ and lat != float('inf')]
    if not successful_items:
        return float('inf'), False, None
    min_lat, best_alias = min(successful_items, key=lambda x: x[0])
    mirror_name = MIRRORS.get(best_alias, {}).get("name", best_alias)
    return min_lat, True, f"测试源: {mirror_name}"

# ----------------- Interactive Selector -----------------

def make_menu_renderable(mirrors_list, selected_index):
    """Creates a beautifully styled Panel containing options for the interactive menu."""
    table = Table(
        box=box.DOUBLE_EDGE, 
        show_header=True, 
        header_style="bold bright_magenta", 
        expand=False,
        border_style="bright_blue"
    )
    table.add_column("选择 (Select)", justify="center", width=8, style="bold yellow")
    table.add_column("别名 (Alias)", style="bold cyan", width=12)
    table.add_column("镜像源 (Mirror Name)", style="bold green", width=22)
    table.add_column("网络延迟 (Latency)", justify="right", width=15)
    table.add_column("网络状态 (Status)", justify="center", width=15)
    table.add_column("镜像源地址 (URL)", style="dim")

    for i, item in enumerate(mirrors_list):
        is_selected = (i == selected_index)
        sel_marker = "[bold blink yellow]▶[/bold blink yellow]" if is_selected else " "
        
        latency = item["latency"]
        if item["alias"] == "cancel":
            latency_str = ""
            status_str = ""
        elif latency is None:
            latency_str = "[bold blue]测速中...[/bold blue]"
            status_str = "[bold blue]测速中...[/bold blue]"
        elif latency == float('inf'):
            latency_str = "[bold red]--[/bold red]"
            status_str = "🔴 [bold red]异常[/bold red]"
        else:
            if latency < 100:
                latency_str = f"[bold green]{latency:.1f} ms[/bold green]"
                status_str = "🟢 [bold green]极速[/bold green]"
            elif latency < 300:
                latency_str = f"[bold yellow]{latency:.1f} ms[/bold yellow]"
                status_str = "🟡 [bold yellow]中速[/bold yellow]"
            else:
                latency_str = f"[bold red]{latency:.1f} ms[/bold red]"
                status_str = "🟠 [bold red]较慢[/bold red]"
            
        cur_marker = " [bold green](当前)[/bold green]" if item["is_current"] else ""
        name_str = f"{item['name']}{cur_marker}"

        if is_selected:
            # Gorgeous highlight: white text on deep sky blue background
            name_str = f"[bold white on deep_sky_blue1]{name_str}[/bold white on deep_sky_blue1]"
            alias_str = f"[bold white on deep_sky_blue1]{item['alias']}[/bold white on deep_sky_blue1]"
            url_str = f"[bold white on deep_sky_blue1]{item['url']}[/bold white on deep_sky_blue1]" if item["alias"] != "cancel" else ""
            if latency_str:
                clean_lat = latency_str.replace('[bold green]', '').replace('[bold yellow]', '').replace('[bold red]', '').replace('[/bold green]', '').replace('[/bold yellow]', '').replace('[/bold red]', '')
                latency_str = f"[bold white on deep_sky_blue1]{clean_lat}[/bold white on deep_sky_blue1]"
            if status_str:
                clean_status = status_str.replace('[bold green]', '').replace('[bold yellow]', '').replace('[bold red]', '').replace('[/bold green]', '').replace('[/bold yellow]', '').replace('[/bold red]', '')
                status_str = f"[bold white on deep_sky_blue1]{clean_status}[/bold white on deep_sky_blue1]"
        else:
            alias_str = item['alias']
            url_str = item['url'] if item["alias"] != "cancel" else ""

        table.add_row(
            sel_marker,
            alias_str,
            name_str,
            latency_str,
            status_str,
            url_str
        )
    return Panel(
        table,
        title="[bold bright_green]YYDS-PIP: 极速镜像源选择菜单[/bold bright_green]",
        border_style="bright_blue",
        subtitle="[bold dim]💡 使用键盘 ↑/↓ 移动选择，Enter 确认修改，Esc/q/❌ 退出[/bold dim]",
        subtitle_align="center",
        expand=False
    )



# ----------------- CLI Setup -----------------

@click.group(invoke_without_command=True, context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(__version__, '-v', '-V', '--version', message="%(prog)s version %(version)s")
@click.pass_context
def main(ctx):
    """🚀 YYDS-PIP: 极速、便捷、美观的 PyPI 镜像源管理工具"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(select_mirror)

@main.command(name="show")
def show():
    """🔍 显示当前配置的镜像源"""
    current_url = get_current_index_url()
    
    # Check network status with progress spinner
    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold blue]正在检测当前网络状况...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("checking", total=None)
        latency, success, target = check_network_status(timeout=1.0)
        
    status_str, warning = format_network_status(latency, success, target)
    
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("配置项 (Key)", style="bold yellow")
    table.add_column("配置值 (Value)", style="green")

    if current_url:
        # Find matching preset alias
        found_name = "自定义/未知"
        for name, info in MIRRORS.items():
            if info["url"].rstrip('/') == current_url.rstrip('/'):
                found_name = f"{info['name']} ({name})"
                break
        table.add_row("pip.global.index-url", escape(current_url))
        table.add_row("镜像名称", found_name)
    else:
        table.add_row("pip.global.index-url", "[dim]未配置 (默认使用 PyPI 官方)[/dim]")
        table.add_row("镜像名称", "PyPI 官方 (pypi)")
        
    table.add_row("当前网络状况", status_str)
        
    console.print(Panel(table, title="[bold green]当前 pip 配置与网络状态[/bold green]", border_style="green", expand=False))
    
    if warning:
        console.print(f"\n{warning}")

@main.command(name="list")
def list_presets():
    """📋 列出所有预设的镜像源"""
    current_url = get_current_index_url()
    
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=False)
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow")
    table.add_column("镜像源名称", style="green")
    table.add_column("镜像源地址", style="dim")

    effective_url = current_url or DEFAULT_INDEX_URL
    for alias, info in MIRRORS.items():
        is_current = effective_url.rstrip('/') == info["url"].rstrip('/')
        marker = "★" if is_current else ""
        table.add_row(marker, alias, info["name"], info["url"])

    console.print(Panel(table, title="[bold green]预设镜像源列表[/bold green]", border_style="cyan", expand=False))

@main.command(name="test")
@click.option(
    "--timeout",
    default=3.0,
    type=TIMEOUT_TYPE,
    show_default=True,
    help="请求超时时间（秒）",
)
def test(timeout):
    """⚡ 对所有镜像源进行并发测速"""
    current_url = get_current_index_url()
    
    results = {}
    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold blue]正在并发测速，请稍候...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("testing", total=None)
        results = test_all_mirrors_parallel(timeout)

    table = Table(
        box=box.DOUBLE_EDGE, 
        show_header=True, 
        header_style="bold bright_magenta", 
        expand=False, 
        border_style="bright_blue"
    )
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow")
    table.add_column("镜像源名称", style="green")
    table.add_column("延迟 (Latency)", justify="right")
    table.add_column("网络状态", justify="center")
    table.add_column("镜像源地址", style="dim")

    # Sort results: faster first, offline last
    sorted_aliases = sorted(MIRRORS.keys(), key=lambda a: results.get(a, (float('inf'), False))[0])

    for alias in sorted_aliases:
        info = MIRRORS[alias]
        latency, success = results.get(alias, (float('inf'), False))
        is_current = current_url and current_url.rstrip('/') == info["url"].rstrip('/')
        
        marker = "★" if is_current else ""
        
        if not success:
            latency_str = "[bold red]超时/错误[/bold red]"
            status_str = "🔴 [bold red]异常[/bold red]"
        elif latency < 100:
            latency_str = f"[bold green]{latency:.1f} ms[/bold green]"
            status_str = "🟢 [bold green]极速[/bold green]"
        elif latency < 300:
            latency_str = f"[bold yellow]{latency:.1f} ms[/bold yellow]"
            status_str = "🟡 [bold yellow]中速[/bold yellow]"
        else:
            latency_str = f"[bold red]{latency:.1f} ms[/bold red]"
            status_str = "🟠 [bold red]较慢[/bold red]"
            
        table.add_row(marker, alias, info["name"], latency_str, status_str, info["url"])

    console.print(Panel(table, title="[bold bright_green]镜像源延迟测试结果 (按速度排序)[/bold bright_green]", border_style="bright_blue", expand=False))

    # Evaluate network status from results
    net_lat, net_succ, net_target = evaluate_network_from_results(results)
    net_status_str, net_warning = format_network_status(net_lat, net_succ, net_target)
    console.print(f"\n🌍 当前网络状况评估: {net_status_str}")
    if net_warning:
        console.print(net_warning)

    if not any(success for _, success in results.values()):
        raise click.ClickException("所有镜像源均测试失败")

@main.command(name="set")
@click.argument("alias_or_url")
@click.option(
    "--allow-insecure",
    is_flag=True,
    help="明确允许自定义 HTTP 源，并将其主机写入 trusted-host",
)
@click.option(
    "--scope",
    type=CONFIG_SCOPE_TYPE,
    default="user",
    show_default=True,
    help="pip 配置文件作用域",
)
def set_mirror(alias_or_url, allow_insecure, scope):
    """🔧 设置 pip 镜像源"""
    # Check if the argument is a preset alias
    if alias_or_url in MIRRORS:
        info = MIRRORS[alias_or_url]
        url = info["url"]
        host = info["trusted_host"]
        name = info["name"]
    else:
        try:
            url, host = validate_custom_mirror_url(
                alias_or_url,
                allow_insecure=allow_insecure,
            )
        except ValueError as error:
            raise click.BadParameter(str(error), param_hint="ALIAS_OR_URL") from error
        name = f"自定义源 ({urlparse(url).hostname})"

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]正在应用配置项...[/bold blue]"),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("setting", total=None)
            set_pip_mirror(url, host, scope=scope)
    except PipConfigError as error:
        raise click.ClickException(f"配置镜像源失败: {error}") from error

    console.print(
        "[bold green]✔ 成功将 pip 镜像源设置为: "
        f"[yellow]{escape(name)}[/yellow][/bold green]"
    )
    console.print(f"[dim]配置的链接 (index-url): {escape(url)}[/dim]")
    console.print(f"[dim]配置作用域 (scope): {scope}[/dim]")
    if host:
        console.print(f"[bold yellow]显式信任的主机: {escape(host)}[/bold yellow]")
    warn_if_environment_overrides(url)

@main.command(name="best")
@click.option(
    "--timeout",
    default=3.0,
    type=TIMEOUT_TYPE,
    show_default=True,
    help="请求超时时间（秒）",
)
@click.option(
    "--scope",
    type=CONFIG_SCOPE_TYPE,
    default="user",
    show_default=True,
    help="pip 配置文件作用域",
)
def set_best(timeout, scope):
    """⚡ 自动测试速度并应用最快的镜像源"""
    results = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]正在并发检测速度并计算最佳节点...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("testing", total=None)
        results = test_all_mirrors_parallel(timeout)

    # Sort results
    sorted_aliases = sorted(MIRRORS.keys(), key=lambda a: results.get(a, (float('inf'), False))[0])
    best_alias = sorted_aliases[0]
    best_latency, success = results[best_alias]

    if not success or best_latency == float('inf'):
        raise click.ClickException("所有镜像源均测试失败，无法确定最快镜像")

    info = MIRRORS[best_alias]
    console.print(f"[bold green]⚡ 测速完毕！最快镜像源为: [yellow]{info['name']} ({best_alias})[/yellow], 延迟: [bold green]{best_latency:.1f} ms[/bold green][/bold green]")
    
    # Check if network latency is poor
    mirror_name = MIRRORS.get(best_alias, {}).get("name", best_alias)
    _, warning = format_network_status(best_latency, success, f"测试源: {mirror_name}")
    if warning:
        console.print(warning)
        
    # Set it
    try:
        set_pip_mirror(info["url"], info["trusted_host"], scope=scope)
    except PipConfigError as error:
        raise click.ClickException(f"配置最快镜像源失败: {error}") from error
    console.print(f"[bold green]✔ 已成功自动切换到: {info['name']}[/bold green]")
    warn_if_environment_overrides(info["url"])

@main.command(name="reset")
@click.option(
    "--scope",
    type=CONFIG_SCOPE_TYPE,
    default="user",
    show_default=True,
    help="pip 配置文件作用域",
)
def reset(scope):
    """🔄 恢复 pip 的官方默认镜像源 (PyPI)"""
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]正在重置默认镜像源...[/bold blue]"),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("resetting", total=None)
            reset_pip_config(scope=scope)
    except PipConfigError as error:
        raise click.ClickException(f"重置失败: {error}") from error

    effective_url = get_current_index_url()
    if effective_url and effective_url.rstrip('/') != DEFAULT_INDEX_URL.rstrip('/'):
        console.print(
            "[bold yellow]⚠ 已清除指定作用域的配置，但当前仍有更高优先级的源：[/bold yellow] "
            f"[yellow]{escape(effective_url)}[/yellow]"
        )
    else:
        console.print(
            "[bold green]✔ 成功重置配置！pip 现在将使用官方 PyPI："
            f"[yellow]{DEFAULT_INDEX_URL}[/yellow][/bold green]"
        )

def make_full_layout(mirrors_list, selected_index, current_url, found_name, network_status_str, warning_message=None):
    """组合状态看板与菜单列表为一个完整的渲染对象"""
    # 状态看板 Table
    status_table = Table(show_header=False, box=None, padding=(0, 2))
    status_table.add_row(
        "💡 [bold yellow]当前所用源 (Current):[/bold yellow]",
        f"[bold green]{escape(found_name)}[/bold green]",
    )
    status_table.add_row(
        "🔗 [bold yellow]镜像源地址 (Index URL):[/bold yellow]",
        f"[cyan]{escape(current_url or DEFAULT_INDEX_URL)}[/cyan]",
    )
    status_table.add_row("🌍 [bold yellow]当前网络状况 (Network):[/bold yellow]", network_status_str)
    
    status_panel = Panel(
        status_table, 
        title="[bold bright_cyan]pip 当前网络配置状态 (Active Configuration)[/bold bright_cyan]", 
        border_style="bright_cyan",
        expand=False
    )
    
    # 菜单 Panel
    menu_panel = make_menu_renderable(mirrors_list, selected_index)
    
    # Renderables list
    renderables = [status_panel]
    if warning_message:
        renderables.append(f"\n  {warning_message}")
        
    renderables.extend([
        "\n[bold magenta]👉 请使用 [yellow]↑/↓ (方向键)[/yellow] 选择镜像源，按 [yellow]Enter (回车键)[/yellow] 确认配置，或按 [yellow]Esc/q[/yellow] 退出：[/bold magenta]\n",
        menu_panel
    ])
    
    return Group(*renderables)

@main.command(name="select")
@click.option(
    "--timeout",
    default=3.0,
    type=TIMEOUT_TYPE,
    show_default=True,
    help="请求超时时间（秒）",
)
@click.option(
    "--scope",
    type=CONFIG_SCOPE_TYPE,
    default="user",
    show_default=True,
    help="pip 配置文件作用域",
)
def select_mirror(timeout, scope):
    """🎮 并发测速并进入键盘交互式选择菜单"""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise click.ClickException(
            "交互选择需要 TTY 终端；请改用 'yyds-pip list'、'set' 或 'best'"
        )

    current_url = get_current_index_url()
    effective_current_url = current_url or DEFAULT_INDEX_URL
    
    # 1. Resolve current active mirror name
    found_name = "PyPI 官方 (pypi)"
    if current_url:
        found_name = "自定义/未知源"
        for name, info in MIRRORS.items():
            if info["url"].rstrip('/') == current_url.rstrip('/'):
                found_name = f"{info['name']} ({name})"
                break

    # 2. Build initial mirrors list (latency=None -> "测速中...")
    mirrors_list = []
    for alias, info in MIRRORS.items():
        is_current = (
            effective_current_url.rstrip('/') == info["url"].rstrip('/')
        )
        mirrors_list.append({
            "alias": alias,
            "name": info["name"],
            "url": info["url"],
            "trusted_host": info["trusted_host"],
            "latency": None,
            "is_current": is_current
        })
        
    # Add cancel option at the bottom
    mirrors_list.append({
        "alias": "cancel",
        "name": "[bold red]❌ 取消选择 (Cancel)[/bold red]",
        "url": "",
        "trusted_host": "",
        "latency": float('inf'),
        "is_current": False
    })

    # Thread-safe UI and selection state
    state = {
        "selected_alias": "cancel",
        "network_status": "[bold blue]⏳ 正在检测...[/bold blue]",
        "warning_message": None
    }
    # Try to find current active mirror to set default selection
    for item in mirrors_list:
        if item["is_current"]:
            state["selected_alias"] = item["alias"]
            break
    if state["selected_alias"] == "cancel" and len(mirrors_list) > 1:
        # Default to first non-cancel mirror
        state["selected_alias"] = mirrors_list[0]["alias"]

    render_lock = threading.Lock()
    stop_event = threading.Event()
    alias_order = {alias: index for index, alias in enumerate(MIRRORS)}
    ssl_context = ssl.create_default_context()
    live = None

    def safe_refresh():
        if live is not None and not stop_event.is_set():
            with render_lock:
                try:
                    # Find selected_index based on selected_alias
                    selected_index = 0
                    for idx, item in enumerate(mirrors_list):
                        if item["alias"] == state["selected_alias"]:
                            selected_index = idx
                            break
                    live.update(make_full_layout(
                        mirrors_list, 
                        selected_index, 
                        current_url, 
                        found_name, 
                        state["network_status"], 
                        state["warning_message"]
                    ))
                except Exception:
                    pass

    # 3. Define background worker logic to test speeds and network status
    def bg_test_mirror(alias):
        _, latency, success = test_single_mirror(
            alias,
            MIRRORS[alias],
            timeout=timeout,
            ssl_context=ssl_context,
        )
        if stop_event.is_set():
            return
        with render_lock:
            for item in mirrors_list:
                if item["alias"] == alias:
                    item["latency"] = latency if success else float('inf')
                    break
            
            # Real-time sort (Live Sort)
            # Order:
            # 0: Completed and success (latency < inf) - sorted by latency ascending
            # 1: Pending (latency is None) - sorted by original MIRRORS keys order
            # 2: Failed/Timeout (latency == inf and alias != cancel) - sorted by original MIRRORS keys order
            # 3: Cancel option (alias == cancel) - always at the bottom
            def get_sort_key(item):
                if item["alias"] == "cancel":
                    return (3, 0, 0)
                if item["latency"] is None:
                    return (1, 0, alias_order[item["alias"]])
                if item["latency"] == float('inf'):
                    return (2, 0, alias_order[item["alias"]])
                return (0, item["latency"], alias_order[item["alias"]])
            
            mirrors_list.sort(key=get_sort_key)
            
        safe_refresh()

    def bg_check_network():
        latency, success, target = check_network_status(timeout=2.0)
        if stop_event.is_set():
            return
        status_str, warning = format_network_status(latency, success, target)
        with render_lock:
            state["network_status"] = status_str
            state["warning_message"] = warning
        safe_refresh()

    selected = None
    executor = ThreadPoolExecutor(max_workers=len(MIRRORS))
    mirror_futures = []

    try:
        console.show_cursor(False)
        # Resolve initial index
        initial_index = 0
        for idx, item in enumerate(mirrors_list):
            if item["alias"] == state["selected_alias"]:
                initial_index = idx
                break
        
        initial_renderable = make_full_layout(
            mirrors_list, 
            initial_index, 
            current_url, 
            found_name, 
            state["network_status"], 
            state["warning_message"]
        )
        with Live(initial_renderable, console=console, auto_refresh=True, refresh_per_second=10) as live_instance:
            live = live_instance
            
            # Start background network check thread
            net_thread = threading.Thread(target=bg_check_network)
            net_thread.daemon = True
            net_thread.start()
            
            # Launch background thread pool for testing mirrors
            for alias in MIRRORS.keys():
                mirror_futures.append(executor.submit(bg_test_mirror, alias))

            # Keyboard event loop
            while True:
                key = get_key()
                if not key:
                    continue
                
                if key in ('\r', '\n'):  # Enter
                    with render_lock:
                        selected = None
                        for item in mirrors_list:
                            if item["alias"] == state["selected_alias"]:
                                selected = item
                                break
                        if selected and selected["alias"] != "cancel":
                            if selected["latency"] is None:
                                state["warning_message"] = (
                                    "[bold yellow]⚠ 该镜像仍在测速，请稍候再确认。[/bold yellow]"
                                )
                                selected = None
                            elif selected["latency"] == float('inf'):
                                state["warning_message"] = (
                                    "[bold red]⚠ 该镜像当前不可用，请选择其他镜像。[/bold red]"
                                )
                                selected = None
                    if selected is None:
                        safe_refresh()
                        continue
                    break
                elif key in ('q', 'Q', '\x1b'):  # Esc, 'q'
                    selected = None
                    break
                elif key == '\x03':
                    raise KeyboardInterrupt
                elif key == '\x1b[A':  # Up Arrow
                    with render_lock:
                        curr_idx = 0
                        for idx, item in enumerate(mirrors_list):
                            if item["alias"] == state["selected_alias"]:
                                curr_idx = idx
                                break
                        new_idx = (curr_idx - 1) % len(mirrors_list)
                        state["selected_alias"] = mirrors_list[new_idx]["alias"]
                    safe_refresh()
                elif key == '\x1b[B':  # Down Arrow
                    with render_lock:
                        curr_idx = 0
                        for idx, item in enumerate(mirrors_list):
                            if item["alias"] == state["selected_alias"]:
                                curr_idx = idx
                                break
                        new_idx = (curr_idx + 1) % len(mirrors_list)
                        state["selected_alias"] = mirrors_list[new_idx]["alias"]
                    safe_refresh()
        with render_lock:
            live = None
    except KeyboardInterrupt:
        console.print("\n[bold red]✖ 用户取消选择。[/bold red]")
        return
    finally:
        stop_event.set()
        for future in mirror_futures:
            future.cancel()
        console.show_cursor(True)
        executor.shutdown(wait=False)

    # 4. Process the selected mirror
    if selected and selected["alias"] != "cancel":
        try:
            set_pip_mirror(
                selected["url"],
                selected["trusted_host"],
                scope=scope,
            )
        except PipConfigError as error:
            raise click.ClickException(f"配置镜像源失败: {error}") from error
        console.print(f"\n[bold green]✔ 配置成功！已切换到镜像源: [yellow]{selected['name']}[/yellow][/bold green]")
        console.print(f"[dim]地址: {selected['url']}[/dim]")
        warn_if_environment_overrides(selected["url"])
    else:
        console.print("\n[yellow]⚠ 未选择任何镜像源，配置未更改。[/yellow]")

if __name__ == "__main__":
    main()
