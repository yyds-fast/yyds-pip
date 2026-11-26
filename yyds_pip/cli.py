import ssl
import sys
import urllib.request
from urllib.parse import urlparse

import click
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .__version__ import __version__
from .config import (
    EffectiveIndexConfig,
    PipConfigError,
    SCOPE_LABELS,
    apply_pip_config_changes,
    get_current_index_url,
    get_effective_index_config,
    get_pip_config_files,
    reset_pip_config,
    set_pip_mirror,
    validate_custom_mirror_url,
)
from .mirrors import (
    DEFAULT_INDEX_URL,
    DEPRECATED_MIRRORS,
    MIRRORS,
    match_mirror_by_url,
    normalize_index_url,
    resolve_mirror_display_name,
    urls_match,
)
from .prober import (
    check_network_status,
    evaluate_network_from_results,
    format_mirror_latency,
    format_network_status,
    get_request_headers,
    test_all_mirrors_parallel,
    test_single_mirror,
)
from .tui import (
    TerminalKeyReader,
    get_key,
    make_full_layout,
    make_menu_renderable,
    parse_posix_key,
    run_interactive_selector,
    sort_mirror_items,
)

console = Console()

TIMEOUT_TYPE = click.FloatRange(min=0.1, max=60.0)
CONFIG_SCOPE_TYPE = click.Choice(["user", "site", "global"], case_sensitive=False)


def warn_if_environment_overrides(configured_url=None):
    """Warn when a higher-priority source (env var or higher scope) overrides configured_url."""
    effective = get_effective_index_config()
    if not effective.url:
        return
    if configured_url and urls_match(effective.url, configured_url):
        return
    if effective.scope == "env-var":
        console.print(
            "[bold yellow]⚠ 环境变量 PIP_INDEX_URL 仍会覆盖上述配置：[/bold yellow] "
            f"[yellow]{escape(effective.url)}[/yellow]"
        )
    else:
        scope_desc = SCOPE_LABELS.get(effective.scope, effective.scope or "未知作用域")
        key_desc = effective.key or "index-url"
        console.print(
            f"[bold yellow]⚠ 更高优先级的配置（{escape(scope_desc)} -> {escape(key_desc)}）仍会覆盖上述修改：[/bold yellow] "
            f"[yellow]{escape(effective.url)}[/yellow]"
        )


@click.group(
    invoke_without_command=True,
    context_settings=dict(help_option_names=["-h", "--help"]),
)
@click.version_option(
    __version__,
    "-v",
    "-V",
    "--version",
    message="%(prog)s version %(version)s",
)
@click.pass_context
def main(ctx):
    """🚀 YYDS-PIP: 极速、便捷、美观的 PyPI 镜像源管理工具"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(select_mirror)


@main.command(name="show")
def show():
    """🔍 显示当前配置的镜像源"""
    effective = get_effective_index_config()
    current_url = effective.url

    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold blue]正在检测镜像网络连通性...[/bold blue]"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("checking", total=None)
        latency, success, target = check_network_status(
            timeout=1.5,
            current_url=current_url,
        )

    status_str, warning = format_network_status(latency, success, target)

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("配置项 (Key)", style="bold yellow")
    table.add_column("配置值 (Value)", style="green")

    deprecated_warning = None
    if current_url:
        _, _, is_deprecated = match_mirror_by_url(current_url)
        found_name = resolve_mirror_display_name(current_url)
        key_label = effective.key or "global.index-url"
        scope_label = SCOPE_LABELS.get(effective.scope, effective.scope or "user")
        if effective.source and effective.scope != "env-var":
            source_display = f"{scope_label} ({escape(effective.source)})"
        else:
            source_display = scope_label

        table.add_row(escape(key_label), escape(current_url))
        table.add_row(
            "镜像名称",
            f"[bold red]{escape(found_name)}[/bold red]"
            if is_deprecated
            else escape(found_name),
        )
        table.add_row("配置来源", source_display)
        if is_deprecated:
            deprecated_warning = (
                "[bold red]⚠️  当前配置的镜像源已停止服务，建议立即运行 'yyds-pip best' 切换可用镜像源！[/bold red]"
            )
    else:
        table.add_row("global.index-url", "[dim]未配置 (默认使用 PyPI 官方)[/dim]")
        table.add_row("镜像名称", "PyPI 官方 (pypi)")

    table.add_row("当前网络状况", status_str)

    console.print(
        Panel(
            table,
            title="[bold green]当前 pip 配置与网络状态[/bold green]",
            border_style="green",
            expand=False,
        )
    )

    if deprecated_warning:
        console.print(f"\n{deprecated_warning}")
    if warning:
        console.print(f"\n{warning}")


@main.command(name="list")
def list_presets():
    """📋 列出所有预设的镜像源"""
    effective_url = get_current_index_url() or DEFAULT_INDEX_URL

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow", no_wrap=True)
    table.add_column("镜像源名称", style="green", no_wrap=True)
    table.add_column("镜像源地址", style="dim", overflow="fold")

    for alias, info in MIRRORS.items():
        candidates = (info["url"],) + tuple(info.get("legacy_urls", ()))
        is_current = any(urls_match(effective_url, cand) for cand in candidates)
        marker = "★" if is_current else ""
        table.add_row(marker, alias, info["name"], info["url"])

    console.print(
        Panel(
            table,
            title="[bold green]预设镜像源列表[/bold green]",
            border_style="cyan",
            expand=False,
        )
    )


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
    effective_url = get_current_index_url() or DEFAULT_INDEX_URL

    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold blue]正在并发测速，请稍候...[/bold blue]"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("testing", total=None)
        results = test_all_mirrors_parallel(timeout)

    table = Table(
        box=box.DOUBLE_EDGE,
        show_header=True,
        header_style="bold bright_magenta",
        expand=False,
        border_style="bright_blue",
    )
    table.add_column("当前", justify="center", style="bold green", width=5)
    table.add_column("别名 (Alias)", style="bold yellow")
    table.add_column("镜像源名称", style="green")
    table.add_column("延迟 (Latency)", justify="right")
    table.add_column("网络状态", justify="center")
    table.add_column("镜像源地址", style="dim")

    sorted_aliases = sorted(
        MIRRORS.keys(),
        key=lambda a: results.get(a, (float("inf"), False))[0],
    )

    for alias in sorted_aliases:
        info = MIRRORS[alias]
        latency, success = results.get(alias, (float("inf"), False))
        candidates = (info["url"],) + tuple(info.get("legacy_urls", ()))
        is_current = any(urls_match(effective_url, cand) for cand in candidates)
        marker = "★" if is_current else ""

        if not success:
            latency_str = "[bold red]超时/错误[/bold red]"
            status_str = "[bold red]🔴 异常[/bold red]"
        else:
            plain_lat, plain_status, lat_style = format_mirror_latency(
                latency,
                success=True,
            )
            latency_str = f"[{lat_style}]{plain_lat}[/{lat_style}]"
            status_str = f"[{lat_style}]{plain_status}[/{lat_style}]"

        table.add_row(
            marker,
            alias,
            info["name"],
            latency_str,
            status_str,
            info["url"],
        )

    console.print(
        Panel(
            table,
            title="[bold bright_green]镜像源延迟测试结果 (按速度排序)[/bold bright_green]",
            border_style="bright_blue",
            expand=False,
        )
    )

    net_lat, net_succ, net_target = evaluate_network_from_results(
        results,
        mirrors=MIRRORS,
    )
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
    scope = scope.lower()
    alias_key = alias_or_url.strip().lower() if alias_or_url == alias_or_url.strip() else ""

    if alias_key in DEPRECATED_MIRRORS:
        dep_name = DEPRECATED_MIRRORS[alias_key]["name"]
        raise click.BadParameter(
            f"镜像源 '{alias_key}' ({dep_name}) 已停止服务，请改用 tsinghua、aliyun 等可用镜像源",
            param_hint="ALIAS_OR_URL",
        )

    if alias_key in MIRRORS:
        info = MIRRORS[alias_key]
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
    scope = scope.lower()
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]正在并发检测速度并计算最佳节点...[/bold blue]"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("testing", total=None)
        results = test_all_mirrors_parallel(timeout)

    sorted_aliases = sorted(
        MIRRORS.keys(),
        key=lambda a: results.get(a, (float("inf"), False))[0],
    )
    best_alias = sorted_aliases[0]
    best_latency, success = results[best_alias]

    if not success or best_latency == float("inf"):
        raise click.ClickException("所有镜像源均测试失败，无法确定最快镜像")

    info = MIRRORS[best_alias]
    console.print(
        f"[bold green]⚡ 测速完毕！最快镜像源为: "
        f"[yellow]{info['name']} ({best_alias})[/yellow], "
        f"延迟: [bold green]{best_latency:.1f} ms[/bold green][/bold green]"
    )

    _, warning = format_network_status(
        best_latency,
        success,
        f"最快节点: {info['name']}",
    )
    if warning:
        console.print(warning)

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
    scope = scope.lower()
    try:
        reset_pip_config(scope=scope)
    except PipConfigError as error:
        raise click.ClickException(f"重置失败: {error}") from error

    effective = get_effective_index_config()
    if effective.url and not urls_match(effective.url, DEFAULT_INDEX_URL):
        scope_desc = SCOPE_LABELS.get(effective.scope, effective.scope or "更高优先级")
        console.print(
            f"[bold yellow]⚠ 已清除 {scope} 作用域的配置，但仍有更高优先级的源生效（{escape(scope_desc)}）：[/bold yellow] "
            f"[yellow]{escape(effective.url)}[/yellow]"
        )
    else:
        console.print(
            "[bold green]✔ 成功重置配置！pip 现在将使用官方 PyPI："
            f"[yellow]{DEFAULT_INDEX_URL}[/yellow][/bold green]"
        )


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
    scope = scope.lower()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise click.ClickException(
            "交互选择需要 TTY 终端；请改用 'yyds-pip list'、'set' 或 'best'"
        )

    current_url = get_current_index_url()
    try:
        selected = run_interactive_selector(
            console=console,
            current_url=current_url,
            timeout=timeout,
            mirrors=MIRRORS,
        )
    except KeyboardInterrupt:
        console.print("\n[bold red]✖ 用户取消选择。[/bold red]")
        return

    if selected and selected["alias"] != "cancel":
        try:
            set_pip_mirror(
                selected["url"],
                selected["trusted_host"],
                scope=scope,
            )
        except PipConfigError as error:
            raise click.ClickException(f"配置镜像源失败: {error}") from error
        console.print(
            f"\n[bold green]✔ 配置成功！已切换到镜像源: "
            f"[yellow]{escape(selected['name'])}[/yellow][/bold green]"
        )
        console.print(f"[dim]地址: {escape(selected['url'])}[/dim]")
        warn_if_environment_overrides(selected["url"])
    else:
        console.print("\n[yellow]⚠ 未选择任何镜像源，配置未更改。[/yellow]")


if __name__ == "__main__":
    main()
