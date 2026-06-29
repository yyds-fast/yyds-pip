import os
import sys
import time
import subprocess
import urllib.request
import ssl
import select
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from rich.panel import Panel
from rich import box

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
        "trusted_host": "mirrors.aliyun.com"
    },
    "tsinghua": {
        "name": "清华大学",
        "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "trusted_host": "pypi.tuna.tsinghua.edu.cn"
    },
    "tencent": {
        "name": "腾讯云",
        "url": "https://mirrors.cloud.tencent.com/pypi/simple/",
        "trusted_host": "mirrors.cloud.tencent.com"
    },
    "douban": {
        "name": "豆瓣",
        "url": "https://pypi.doubanio.com/simple/",
        "trusted_host": "pypi.doubanio.com"
    },
    "huawei": {
        "name": "华为云",
        "url": "https://repo.huaweicloud.com/repository/pypi/simple/",
        "trusted_host": "repo.huaweicloud.com"
    },
    "ustc": {
        "name": "中国科学技术大学",
        "url": "https://pypi.mirrors.ustc.edu.cn/simple/",
        "trusted_host": "pypi.mirrors.ustc.edu.cn"
    },
    "sjtu": {
        "name": "上海交通大学",
        "url": "https://mirrors.sjtug.sjtu.edu.cn/pypi/web/simple/",
        "trusted_host": "mirrors.sjtug.sjtu.edu.cn"
    },
    "pypi": {
        "name": "PyPI 官方",
        "url": "https://pypi.org/simple/",
        "trusted_host": "pypi.org"
    }
}

console = Console()

# ----------------- Helper functions for reading terminal keys -----------------

def get_key():
    """Reads a single keypress from the user in a non-blocking raw mode (Unix only)."""
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
        tty.setraw(fd)
        # Check if stdin has data
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            return None
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

def get_current_index_url():
    """Gets the current configured pip global index-url."""
    try:
        res = subprocess.run(["pip", "config", "get", "global.index-url"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def set_pip_mirror(url, trusted_host=None):
    """Sets the pip global index-url and trusted-host."""
    try:
        subprocess.run(["pip", "config", "set", "global.index-url", url], check=True, capture_output=True)
        if trusted_host:
            subprocess.run(["pip", "config", "set", "install.trusted-host", trusted_host], check=True, capture_output=True)
        else:
            # Try to unset trusted-host if it's PyPI official or if not specified
            subprocess.run(["pip", "config", "unset", "install.trusted-host"], capture_output=True)
        return True
    except Exception as e:
        console.print(f"[bold red]❌ 配置镜像源失败: {e}[/bold red]")
        return False

# ----------------- Helper functions for speed testing -----------------

def test_single_mirror(alias, info, timeout=3.0):
    """Tests the latency of a single mirror URL."""
    url = info["url"]
    # Append 'pip/' to request a small packages page (reliable latency test)
    test_url = url.rstrip('/') + '/pip/'
    start = time.perf_counter()
    try:
        # Create unverified context to bypass local SSL issue
        ssl_context = ssl._create_unverified_context()
        req = urllib.request.Request(
            test_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) yyds-pip/1.0'}
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
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as executor:
        futures = {
            executor.submit(test_single_mirror, alias, info, timeout): alias
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

# ----------------- Interactive Selector -----------------

def make_menu_renderable(mirrors_list, selected_index):
    """Creates a beautifully styled Panel containing options for the interactive menu."""
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta", expand=True)
    table.add_column("选择", justify="center", width=4)
    table.add_column("别名 (Alias)", style="cyan", width=12)
    table.add_column("镜像源名称", style="green", width=20)
    table.add_column("延迟 (Latency)", justify="right", width=15)
    table.add_column("镜像源地址", style="dim")

    for i, item in enumerate(mirrors_list):
        is_selected = (i == selected_index)
        sel_marker = "[bold yellow]➔[/bold yellow]" if is_selected else " "
        
        latency = item["latency"]
        if latency == float('inf'):
            latency_str = "[bold red]超时/错误[/bold red]"
        elif latency < 150:
            latency_str = f"[bold green]{latency:.1f} ms[/bold green]"
        elif latency < 400:
            latency_str = f"[bold yellow]{latency:.1f} ms[/bold yellow]"
        else:
            latency_str = f"[bold red]{latency:.1f} ms[/bold red]"
            
        cur_marker = " [bold green](当前)[/bold green]" if item["is_current"] else ""
        name_str = f"{item['name']}{cur_marker}"

        if is_selected:
            # Highlight selected row
            name_str = f"[bold white on blue]{name_str}[/bold white on blue]"
            alias_str = f"[bold white on blue]{item['alias']}[/bold white on blue]"
            url_str = f"[bold white on blue]{item['url']}[/bold white on blue]"
        else:
            alias_str = item['alias']
            url_str = item['url']

        table.add_row(
            sel_marker,
            alias_str,
            name_str,
            latency_str,
            url_str
        )
    return Panel(
        table,
        title="[bold green]🚀 YYDS-PIP: 镜像源选择菜单[/bold green]",
        border_style="magenta",
        subtitle="[dim]使用 ↑/↓ 选择，Enter 确认，Esc/q 退出[/dim]"
    )

def interactive_selection(mirrors_list):
    """Runs the interactive arrow-key selection menu loop."""
    selected_index = 0
    # Try to find current active mirror to set default selection
    for i, item in enumerate(mirrors_list):
        if item["is_current"]:
            selected_index = i
            break

    console.print("\n[bold magenta]👉 请使用 [yellow]↑/↓ (方向键)[/yellow] 选择镜像源，按 [yellow]Enter (回车键)[/yellow] 确认配置，或按 [yellow]Esc/q[/yellow] 退出：[/bold magenta]")
    
    console.show_cursor(False)
    try:
        with Live(make_menu_renderable(mirrors_list, selected_index), console=console, auto_refresh=False) as live:
            while True:
                live.update(make_menu_renderable(mirrors_list, selected_index))
                live.refresh()
                
                key = get_key()
                if not key:
                    continue
                
                if key in ('\r', '\n'):  # Enter
                    return mirrors_list[selected_index]
                elif key in ('q', 'Q', '\x1b'):  # Esc, 'q'
                    return None
                elif key == '\x1b[A':  # Up Arrow
                    selected_index = (selected_index - 1) % len(mirrors_list)
                elif key == '\x1b[B':  # Down Arrow
                    selected_index = (selected_index + 1) % len(mirrors_list)
                elif key == '\x03':  # Ctrl+C
                    raise KeyboardInterrupt
    finally:
        console.show_cursor(True)

# ----------------- CLI Setup -----------------

@click.group(invoke_without_command=True)
@click.version_option(version="0.1.0", message="%(prog)s version %(version)s")
@click.pass_context
def main(ctx):
    """🚀 YYDS-PIP: 极速、便捷、美观的 PyPI 镜像源管理工具"""
    # Print custom beautiful banner
    if ctx.invoked_subcommand is None:
        ctx.invoke(select)

@main.command(name="show")
def show():
    """🔍 显示当前配置的镜像源"""
    current_url = get_current_index_url()
    
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
        table.add_row("pip.global.index-url", current_url)
        table.add_row("镜像名称", found_name)
    else:
        table.add_row("pip.global.index-url", "[dim]未配置 (默认使用 PyPI 官方)[/dim]")
        
    console.print(Panel(table, title="[bold green]当前 pip 配置[/bold green]", border_style="green", expand=False))

@main.command(name="list")
def list_presets():
    """📋 列出所有预设的镜像源"""
    current_url = get_current_index_url()
    
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=False)
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow")
    table.add_column("镜像源名称", style="green")
    table.add_column("镜像源地址", style="dim")

    for alias, info in MIRRORS.items():
        is_current = current_url and current_url.rstrip('/') == info["url"].rstrip('/')
        marker = "★" if is_current else ""
        table.add_row(marker, alias, info["name"], info["url"])

    console.print(Panel(table, title="[bold green]预设镜像源列表[/bold green]", border_style="cyan", expand=False))

@main.command(name="test")
@click.option("--timeout", default=3.0, type=float, help="请求超时时间（秒）")
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

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta", expand=False)
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow")
    table.add_column("镜像源名称", style="green")
    table.add_column("延迟 (Latency)", justify="right")
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
        elif latency < 150:
            latency_str = f"[bold green]{latency:.1f} ms[/bold green]"
        elif latency < 400:
            latency_str = f"[bold yellow]{latency:.1f} ms[/bold yellow]"
        else:
            latency_str = f"[bold red]{latency:.1f} ms[/bold red]"
            
        table.add_row(marker, alias, info["name"], latency_str, info["url"])

    console.print(Panel(table, title="[bold green]镜像源延迟测试结果 (按速度排序)[/bold green]", border_style="magenta", expand=False))

@main.command(name="set")
@click.argument("alias_or_url")
def set_mirror(alias_or_url):
    """🔧 设置 pip 镜像源"""
    # Check if the argument is a preset alias
    if alias_or_url in MIRRORS:
        info = MIRRORS[alias_or_url]
        url = info["url"]
        host = info["trusted_host"]
        name = info["name"]
    else:
        # Check if it looks like a URL
        parsed = urlparse(alias_or_url)
        if parsed.scheme in ('http', 'https') and parsed.netloc:
            url = alias_or_url
            host = parsed.netloc
            name = f"自定义源 ({host})"
        else:
            console.print(f"[bold red]❌ 错误的参数 '{alias_or_url}'。参数必须为预设别名或合法的 HTTP/HTTPS 镜像 URL。[/bold red]")
            console.print("[yellow]提示：使用 [bold]yyds-pip list[/bold] 查看所有预设别名。[/yellow]")
            return

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]正在应用配置项...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("setting", total=None)
        success = set_pip_mirror(url, host)

    if success:
        console.print(f"[bold green]✔ 成功将 pip 镜像源设置为: [yellow]{name}[/yellow][/bold green]")
        console.print(f"[dim]配置的链接 (index-url): {url}[/dim]")
        if host:
            console.print(f"[dim]信任的主机 (trusted-host): {host}[/dim]")

@main.command(name="best")
@click.option("--timeout", default=3.0, type=float, help="请求超时时间（秒）")
def set_best(timeout):
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
        console.print("[bold red]❌ 所有镜像源均测试失败，无法确定最快镜像。[/bold red]")
        return

    info = MIRRORS[best_alias]
    console.print(f"[bold green]⚡ 测速完毕！最快镜像源为: [yellow]{info['name']} ({best_alias})[/yellow], 延迟: [bold green]{best_latency:.1f} ms[/bold green][/bold green]")
    
    # Set it
    set_success = set_pip_mirror(info["url"], info["trusted_host"])
    if set_success:
        console.print(f"[bold green]✔ 已成功自动切换到: {info['name']}[/bold green]")

@main.command(name="reset")
def reset():
    """🔄 恢复 pip 的官方默认镜像源 (PyPI)"""
    info = MIRRORS["pypi"]
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]正在重置默认镜像源...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("resetting", total=None)
        # Clear index-url and trusted-host config to let pip use default
        try:
            # Explicitly set index-url to PyPI official URL to override any legacy configurations
            subprocess.run(["pip", "config", "set", "global.index-url", "https://pypi.org/simple/"], check=True, capture_output=True)
            subprocess.run(["pip", "config", "unset", "install.trusted-host"], capture_output=True)
            success = True
        except Exception as e:
            console.print(f"[bold red]❌ 重置失败: {e}[/bold red]")
            success = False

    if success:
        console.print("[bold green]✔ 成功重置配置！pip 现在将使用官方 PyPI 镜像源: [yellow]https://pypi.org/simple/[/yellow][/bold green]")

@main.command(name="select")
@click.option("--timeout", default=3.0, type=float, help="请求超时时间（秒）")
def select(timeout):
    """🎮 并发测速并进入键盘交互式选择菜单"""
    current_url = get_current_index_url()
    
    results = {}
    with Progress(
        SpinnerColumn(spinner_name="dots12"),
        TextColumn("[bold blue]正在极速并发测速，请稍候...[/bold blue]"),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("testing", total=None)
        results = test_all_mirrors_parallel(timeout)

    # Format list
    mirrors_list = []
    for alias, info in MIRRORS.items():
        latency, success = results.get(alias, (float('inf'), False))
        is_current = current_url and current_url.rstrip('/') == info["url"].rstrip('/')
        mirrors_list.append({
            "alias": alias,
            "name": info["name"],
            "url": info["url"],
            "trusted_host": info["trusted_host"],
            "latency": latency,
            "is_current": is_current
        })

    # Sort list: faster first
    mirrors_list.sort(key=lambda x: x["latency"])

    # Run selection
    try:
        selected = interactive_selection(mirrors_list)
    except KeyboardInterrupt:
        console.print("\n[bold red]✖ 用户取消选择。[/bold red]")
        return

    if selected:
        # Set mirror
        success = set_pip_mirror(selected["url"], selected["trusted_host"])
        if success:
            console.print(f"\n[bold green]✔ 配置成功！已切换到镜像源: [yellow]{selected['name']}[/yellow][/bold green]")
            console.print(f"[dim]地址: {selected['url']}[/dim]")
    else:
        console.print("\n[yellow]⚠ 未选择任何镜像源，配置未更改。[/yellow]")

if __name__ == "__main__":
    main()
