import os
import select
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from rich import box
from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .mirrors import DEFAULT_INDEX_URL, MIRRORS, resolve_mirror_display_name, urls_match
from .prober import (
    build_shared_opener,
    create_ssl_context,
    evaluate_network_from_results,
    format_mirror_latency,
    format_network_status,
    test_single_mirror,
)

try:
    import termios
    import tty

    WINDOWS = False
except ImportError:
    WINDOWS = True

HIGHLIGHT_STYLE = "bold white on deep_sky_blue1"


def parse_posix_key(read_byte, has_input):
    """Parse a single key or ANSI/SS3 escape sequence from byte callbacks.

    Returns normalized key strings:
      - '\\x1b[A' (Up), '\\x1b[B' (Down), 'HOME', 'END'
      - '\\x1b' (bare Escape), '\\r', '\\n', '\\x03', or decoded character
      - None for unrecognized escape sequences (after draining trailing bytes)
    """
    b1 = read_byte()
    if not b1:
        return None

    if b1 == b"\x1b":
        if not has_input(0.04):
            return "\x1b"
        b2 = read_byte()
        if not b2 or b2 == b"\x1b":
            while has_input(0.005):
                read_byte()
            return "\x1b"

        if b2 == b"[":
            # CSI sequence: \x1b[ <params> <final byte 0x40..0x7E>
            seq_bytes = bytearray()
            for _ in range(16):
                if not has_input(0.04):
                    break
                ch = read_byte()
                if not ch:
                    break
                seq_bytes.extend(ch)
                if 0x40 <= ch[0] <= 0x7E:
                    break
            seq = seq_bytes.decode("ascii", errors="ignore")
            if seq == "A":
                return "\x1b[A"
            if seq == "B":
                return "\x1b[B"
            if seq in ("H", "1~", "7~"):
                return "HOME"
            if seq in ("F", "4~", "8~"):
                return "END"
            return None

        if b2 == b"O":
            # SS3 sequence (tmux / application cursor keys mode): \x1bO <final>
            if has_input(0.04):
                b3 = read_byte()
                if b3 == b"A":
                    return "\x1b[A"
                if b3 == b"B":
                    return "\x1b[B"
                if b3 == b"H":
                    return "HOME"
                if b3 == b"F":
                    return "END"
            return None

        # Alt+key combination: consume any UTF-8 continuation bytes and ignore
        if b2[0] >= 0x80:
            while has_input(0.005):
                read_byte()
        return None

    first = b1[0]
    if first >= 0x80:
        # Multi-byte UTF-8 character (e.g. active CJK input method)
        expected_more = 1 if first < 0xE0 else (2 if first < 0xF0 else 3)
        buf = bytearray(b1)
        for _ in range(expected_more):
            if has_input(0.01):
                nxt = read_byte()
                if nxt:
                    buf.extend(nxt)
        decoded = buf.decode("utf-8", errors="ignore")
        return decoded or None

    return b1.decode("ascii", errors="ignore") or None


class TerminalKeyReader:
    """Context manager that keeps POSIX terminals in cbreak mode across reads."""

    def __init__(self):
        self._fd = None
        self._old_settings = None

    def __enter__(self):
        if not WINDOWS:
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not WINDOWS and self._fd is not None and self._old_settings is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
        return False

    def read_key(self):
        if WINDOWS:
            import msvcrt

            wch = msvcrt.getwch()
            if wch in ("\x00", "\xe0"):
                wch2 = msvcrt.getwch()
                if wch2 == "H":
                    return "\x1b[A"
                if wch2 == "P":
                    return "\x1b[B"
                if wch2 == "G":
                    return "HOME"
                if wch2 == "O":
                    return "END"
                return None
            return wch

        fd = self._fd if self._fd is not None else sys.stdin.fileno()

        def _read_byte():
            return os.read(fd, 1)

        def _has_input(timeout_sec):
            ready, _, _ = select.select([fd], [], [], timeout_sec)
            return bool(ready)

        return parse_posix_key(_read_byte, _has_input)


def get_key():
    """Read one keypress and normalize arrow keys across platforms."""
    with TerminalKeyReader() as reader:
        return reader.read_key()


def sort_mirror_items(mirrors_list, alias_order):
    """Sort mirror menu entries in-place:
    0: Succeeded (ascending latency)
    1: Pending (original order)
    2: Failed/Timeout (original order)
    3: Cancel option (always last)
    """

    def _sort_key(item):
        alias = item["alias"]
        if alias == "cancel":
            return (3, 0.0, 0)
        latency = item["latency"]
        order = alias_order.get(alias, 999)
        if latency is None:
            return (1, 0.0, order)
        if latency == float("inf"):
            return (2, 0.0, order)
        return (0, latency, order)

    mirrors_list.sort(key=_sort_key)


def find_selected_index(mirrors_list, selected_alias):
    """Locate index of selected_alias in mirrors_list, defaulting to 0."""
    for idx, item in enumerate(mirrors_list):
        if item["alias"] == selected_alias:
            return idx
    return 0


def make_menu_renderable(mirrors_list, selected_index, console_width=None, max_visible_rows=None):
    """Create the styled Panel containing options for the interactive menu."""
    show_url_col = console_width is None or console_width >= 108

    total_items = len(mirrors_list)
    start_idx = 0
    end_idx = total_items
    if max_visible_rows is not None and 0 < max_visible_rows < total_items:
        half = max_visible_rows // 2
        start_idx = max(0, min(selected_index - half, total_items - max_visible_rows))
        end_idx = start_idx + max_visible_rows

    table = Table(
        box=box.DOUBLE_EDGE,
        show_header=True,
        header_style="bold bright_magenta",
        expand=False,
        border_style="bright_blue",
    )
    table.add_column("选择 (Select)", justify="center", width=8, style="bold yellow")
    table.add_column("别名 (Alias)", style="bold cyan", width=12)
    table.add_column("镜像源 (Mirror Name)", style="bold green", width=24)
    table.add_column("网络延迟 (Latency)", justify="right", width=15)
    table.add_column("网络状态 (Status)", justify="center", width=15)
    if show_url_col:
        table.add_column("镜像源地址 (URL)", style="dim")

    for i in range(start_idx, end_idx):
        item = mirrors_list[i]
        is_selected = i == selected_index
        is_cancel = item["alias"] == "cancel"
        sel_marker = "[bold blink yellow]▶[/bold blink yellow]" if is_selected else " "

        if is_cancel:
            plain_lat, plain_status, lat_style = "", "", ""
        else:
            latency = item["latency"]
            success = latency is not None and latency != float("inf")
            plain_lat, plain_status, lat_style = format_mirror_latency(
                latency,
                success=success,
            )

        plain_name = "❌ 取消选择 (Cancel)" if is_cancel else item["name"]

        if is_selected:
            cur_suffix = " (当前)" if item["is_current"] else ""
            alias_cell = f"[{HIGHLIGHT_STYLE}]{escape(item['alias'])}[/{HIGHLIGHT_STYLE}]"
            name_cell = f"[{HIGHLIGHT_STYLE}]{escape(plain_name + cur_suffix)}[/{HIGHLIGHT_STYLE}]"
            lat_cell = (
                f"[{HIGHLIGHT_STYLE}]{escape(plain_lat)}[/{HIGHLIGHT_STYLE}]"
                if plain_lat
                else ""
            )
            status_cell = (
                f"[{HIGHLIGHT_STYLE}]{escape(plain_status)}[/{HIGHLIGHT_STYLE}]"
                if plain_status
                else ""
            )
            url_cell = (
                f"[{HIGHLIGHT_STYLE}]{escape(item['url'])}[/{HIGHLIGHT_STYLE}]"
                if not is_cancel
                else ""
            )
        else:
            alias_cell = escape(item["alias"])
            if is_cancel:
                name_cell = f"[bold red]{escape(plain_name)}[/bold red]"
            else:
                cur_marker = " [bold green](当前)[/bold green]" if item["is_current"] else ""
                name_cell = f"{escape(plain_name)}{cur_marker}"
            lat_cell = f"[{lat_style}]{escape(plain_lat)}[/{lat_style}]" if plain_lat else ""
            status_cell = (
                f"[{lat_style}]{escape(plain_status)}[/{lat_style}]" if plain_status else ""
            )
            url_cell = escape(item["url"]) if not is_cancel else ""

        row = [sel_marker, alias_cell, name_cell, lat_cell, status_cell]
        if show_url_col:
            row.append(url_cell)
        table.add_row(*row)

    scroll_hint = ""
    if start_idx > 0 or end_idx < total_items:
        scroll_hint = f"  [第 {selected_index + 1}/{total_items} 项]"

    return Panel(
        table,
        title="[bold bright_green]YYDS-PIP: 极速镜像源选择菜单[/bold bright_green]",
        border_style="bright_blue",
        subtitle=(
            f"[bold dim]💡 使用 ↑/↓ 或 j/k 移动选择，Enter 确认修改，Esc/q/❌ 退出{scroll_hint}[/bold dim]"
        ),
        subtitle_align="center",
        expand=False,
    )


def make_full_layout(
    mirrors_list,
    selected_index,
    current_url,
    found_name,
    network_status_str,
    warning_message=None,
    console_size=None,
):
    """Combine the active configuration panel and interactive mirror menu."""
    console_width = console_size.width if console_size is not None else None
    console_height = console_size.height if console_size is not None else None

    max_visible_rows = None
    if console_height is not None and console_height > 0:
        overhead = 15 + (2 if warning_message else 0)
        if console_height < overhead + len(mirrors_list):
            max_visible_rows = max(3, console_height - overhead)

    status_table = Table(show_header=False, box=None, padding=(0, 2))
    status_table.add_row(
        "💡 [bold yellow]当前所用源 (Current):[/bold yellow]",
        f"[bold green]{escape(found_name)}[/bold green]",
    )
    status_table.add_row(
        "🔗 [bold yellow]镜像源地址 (Index URL):[/bold yellow]",
        f"[cyan]{escape(current_url or DEFAULT_INDEX_URL)}[/cyan]",
    )
    status_table.add_row(
        "🌍 [bold yellow]当前网络状况 (Network):[/bold yellow]",
        network_status_str,
    )

    status_panel = Panel(
        status_table,
        title="[bold bright_cyan]pip 当前网络配置状态 (Active Configuration)[/bold bright_cyan]",
        border_style="bright_cyan",
        expand=False,
    )

    menu_panel = make_menu_renderable(
        mirrors_list,
        selected_index,
        console_width=console_width,
        max_visible_rows=max_visible_rows,
    )

    renderables = [status_panel]
    if warning_message:
        renderables.append(f"\n  {warning_message}")

    renderables.extend(
        [
            "\n[bold magenta]👉 请使用 [yellow]↑/↓ (方向键)[/yellow] 选择镜像源，按 [yellow]Enter (回车键)[/yellow] 确认配置，或按 [yellow]Esc/q[/yellow] 退出：[/bold magenta]\n",
            menu_panel,
        ]
    )

    return Group(*renderables)


def run_interactive_selector(console, current_url, timeout=3.0, mirrors=None):
    """Run the concurrent mirror probe + interactive keyboard menu.

    Returns the selected mirror dict, None if cancelled, or raises KeyboardInterrupt.
    """
    active_mirrors = MIRRORS if mirrors is None else mirrors
    effective_current_url = current_url or DEFAULT_INDEX_URL
    found_name = resolve_mirror_display_name(current_url)

    mirrors_list = []
    for alias, info in active_mirrors.items():
        candidates = (info["url"],) + tuple(info.get("legacy_urls", ()))
        is_current = any(urls_match(effective_current_url, cand) for cand in candidates)
        mirrors_list.append(
            {
                "alias": alias,
                "name": info["name"],
                "url": info["url"],
                "trusted_host": info["trusted_host"],
                "latency": None,
                "is_current": is_current,
            }
        )

    mirrors_list.append(
        {
            "alias": "cancel",
            "name": "❌ 取消选择 (Cancel)",
            "url": "",
            "trusted_host": None,
            "latency": float("inf"),
            "is_current": False,
        }
    )

    default_alias = "cancel"
    for item in mirrors_list:
        if item["is_current"]:
            default_alias = item["alias"]
            break
    if default_alias == "cancel" and len(mirrors_list) > 1:
        default_alias = mirrors_list[0]["alias"]

    state = {
        "selected_alias": default_alias,
        "network_status": "[bold blue]⏳ 正在并发检测镜像节点...[/bold blue]",
        "network_warning": None,
        "action_warning": None,
        "live": None,
    }
    probe_results = {}
    render_lock = threading.Lock()
    stop_event = threading.Event()
    alias_order = {alias: index for index, alias in enumerate(active_mirrors)}

    ssl_context = create_ssl_context()
    opener = build_shared_opener(ssl_context)

    def _effective_warning():
        return state["action_warning"] or state["network_warning"]

    def _refresh_locked():
        live_obj = state["live"]
        if live_obj is None or stop_event.is_set():
            return
        try:
            selected_idx = find_selected_index(mirrors_list, state["selected_alias"])
            live_obj.update(
                make_full_layout(
                    mirrors_list,
                    selected_idx,
                    current_url,
                    found_name,
                    state["network_status"],
                    _effective_warning(),
                    console_size=console.size,
                ),
                refresh=True,
            )
        except Exception:
            pass

    def bg_test_mirror(alias):
        _, latency, success = test_single_mirror(
            alias,
            active_mirrors[alias],
            timeout=timeout,
            ssl_context=ssl_context,
            opener=opener,
        )
        if stop_event.is_set():
            return
        with render_lock:
            if stop_event.is_set():
                return
            probe_results[alias] = (latency, success)
            for item in mirrors_list:
                if item["alias"] == alias:
                    item["latency"] = latency if success else float("inf")
                    break

            sort_mirror_items(mirrors_list, alias_order)

            # Clear stale "still testing" toast if the selected mirror just completed
            if alias == state["selected_alias"] and success:
                state["action_warning"] = None

            # Update top network health panel from real mirror probe results
            completed_count = len(probe_results)
            any_success = any(succ for _, succ in probe_results.values())
            if any_success or completed_count >= len(active_mirrors):
                net_lat, net_succ, net_target = evaluate_network_from_results(
                    probe_results,
                    mirrors=active_mirrors,
                )
                status_str, net_warn = format_network_status(
                    net_lat,
                    net_succ,
                    net_target,
                )
                state["network_status"] = status_str
                state["network_warning"] = net_warn

            _refresh_locked()

    selected = None
    executor = ThreadPoolExecutor(max_workers=max(1, len(active_mirrors)))
    mirror_futures = []

    try:
        console.show_cursor(False)
        initial_index = find_selected_index(mirrors_list, state["selected_alias"])
        initial_renderable = make_full_layout(
            mirrors_list,
            initial_index,
            current_url,
            found_name,
            state["network_status"],
            _effective_warning(),
            console_size=console.size,
        )

        with TerminalKeyReader() as key_reader:
            with Live(
                initial_renderable,
                console=console,
                auto_refresh=False,
            ) as live_instance:
                with render_lock:
                    state["live"] = live_instance

                for alias in active_mirrors:
                    mirror_futures.append(executor.submit(bg_test_mirror, alias))

                try:
                    while True:
                        key = key_reader.read_key()
                        if not key:
                            continue

                        if key in ("\r", "\n"):
                            should_exit = False
                            with render_lock:
                                idx = find_selected_index(
                                    mirrors_list,
                                    state["selected_alias"],
                                )
                                candidate = mirrors_list[idx]
                                if candidate["alias"] == "cancel":
                                    selected = None
                                    should_exit = True
                                elif candidate["latency"] is None:
                                    state["action_warning"] = (
                                        "[bold yellow]⚠ 该镜像仍在测速，请稍候再确认。[/bold yellow]"
                                    )
                                    _refresh_locked()
                                elif candidate["latency"] == float("inf"):
                                    state["action_warning"] = (
                                        "[bold red]⚠ 该镜像当前不可用，请选择其他镜像。[/bold red]"
                                    )
                                    _refresh_locked()
                                else:
                                    selected = dict(candidate)
                                    should_exit = True

                                if should_exit:
                                    stop_event.set()
                                    state["live"] = None
                            if should_exit:
                                break

                        elif key in ("q", "Q", "\x1b"):
                            with render_lock:
                                selected = None
                                stop_event.set()
                                state["live"] = None
                            break

                        elif key == "\x03":
                            with render_lock:
                                stop_event.set()
                                state["live"] = None
                            raise KeyboardInterrupt

                        elif key in ("\x1b[A", "k", "K"):
                            with render_lock:
                                curr_idx = find_selected_index(
                                    mirrors_list,
                                    state["selected_alias"],
                                )
                                new_idx = (curr_idx - 1) % len(mirrors_list)
                                state["selected_alias"] = mirrors_list[new_idx]["alias"]
                                state["action_warning"] = None
                                _refresh_locked()

                        elif key in ("\x1b[B", "j", "J"):
                            with render_lock:
                                curr_idx = find_selected_index(
                                    mirrors_list,
                                    state["selected_alias"],
                                )
                                new_idx = (curr_idx + 1) % len(mirrors_list)
                                state["selected_alias"] = mirrors_list[new_idx]["alias"]
                                state["action_warning"] = None
                                _refresh_locked()

                        elif key == "HOME":
                            with render_lock:
                                state["selected_alias"] = mirrors_list[0]["alias"]
                                state["action_warning"] = None
                                _refresh_locked()

                        elif key == "END":
                            with render_lock:
                                state["selected_alias"] = mirrors_list[-1]["alias"]
                                state["action_warning"] = None
                                _refresh_locked()
                finally:
                    with render_lock:
                        stop_event.set()
                        state["live"] = None
    finally:
        stop_event.set()
        for future in mirror_futures:
            future.cancel()
        console.show_cursor(True)
        executor.shutdown(wait=False)

    return selected
