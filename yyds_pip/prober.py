import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .__version__ import __version__
from .mirrors import MIRRORS, match_mirror_by_url, urls_match

PROBE_PACKAGE = "six"
FAST_LATENCY_MS = 100.0
MEDIUM_LATENCY_MS = 300.0

_ORIGINAL_URLOPEN = urllib.request.urlopen


def create_ssl_context():
    """Create a verified TLS client context configured for HTTP/1.1."""
    ctx = ssl.create_default_context()
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except (AttributeError, NotImplementedError, ssl.SSLError):
        pass
    return ctx


def build_shared_opener(ssl_context):
    """Build a reusable urllib OpenerDirector sharing the given SSLContext."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_context)
    )


def get_request_headers(url=None):
    """Return minimal, honest headers suitable for a PEP 503 / PEP 691 endpoint."""
    return {
        "User-Agent": f"yyds-pip/{__version__} (+https://github.com/yyds-fast/yyds-pip)",
        "Accept": (
            "application/vnd.pypi.simple.v1+json, "
            "application/vnd.pypi.simple.v1+html, text/html;q=0.9"
        ),
        "Accept-Encoding": "gzip",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def test_single_mirror(alias, info, timeout=3.0, ssl_context=None, opener=None):
    """Test the round-trip latency and body availability of a single mirror URL."""
    url = info["url"]
    test_url = f"{url.rstrip('/')}/{PROBE_PACKAGE}/"
    if ssl_context is None:
        ssl_context = create_ssl_context()

    req = urllib.request.Request(
        test_url,
        headers=get_request_headers(test_url),
    )

    start = time.perf_counter()
    try:
        if opener is not None and urllib.request.urlopen is _ORIGINAL_URLOPEN:
            ctx_mgr = opener.open(req, timeout=timeout)
        else:
            ctx_mgr = urllib.request.urlopen(
                req,
                timeout=timeout,
                context=ssl_context,
            )
        with ctx_mgr as response:
            if response.status == 200:
                if hasattr(response, "read"):
                    chunk = response.read(256)
                    if not chunk:
                        return alias, float("inf"), False
                duration = (time.perf_counter() - start) * 1000.0
                return alias, duration, True
    except Exception:
        pass
    return alias, float("inf"), False


def test_all_mirrors_parallel(timeout=3.0, mirrors=None):
    """Concurrently test speeds of all configured mirrors using a shared Opener."""
    target_mirrors = MIRRORS if mirrors is None else mirrors
    if not target_mirrors:
        return {}

    results = {}
    ssl_context = create_ssl_context()
    opener = build_shared_opener(ssl_context)

    with ThreadPoolExecutor(max_workers=len(target_mirrors)) as executor:
        futures = {
            executor.submit(
                test_single_mirror,
                alias,
                info,
                timeout,
                ssl_context,
                opener,
            ): alias
            for alias, info in target_mirrors.items()
        }
        for future in as_completed(futures):
            alias = futures[future]
            try:
                alias_res, duration, success = future.result()
                results[alias_res] = (duration, success)
            except Exception:
                results[alias] = (float("inf"), False)
    return results


def format_mirror_latency(latency, success=True):
    """Format a single mirror's latency and status badge using unified thresholds.

    Returns (plain_latency, plain_status, color_style).
    """
    if latency is None:
        return "测速中...", "⏳ 测速中", "bold blue"
    if not success or latency == float("inf"):
        return "--", "🔴 异常", "bold red"
    if latency < FAST_LATENCY_MS:
        return f"{latency:.1f} ms", "🟢 极速", "bold green"
    if latency < MEDIUM_LATENCY_MS:
        return f"{latency:.1f} ms", "🟡 良好", "bold yellow"
    return f"{latency:.1f} ms", "🟠 较慢", "bold red"


def format_network_status(latency, success, target=None):
    """Format overall network status into Rich markup (status_str, warning_str)."""
    if not success or latency == float("inf"):
        return (
            "[bold red]🔴 不可达[/bold red]",
            "[bold red]⚠️  未能连接到 PyPI 镜像源，请检查网络连接或代理设置！[/bold red]",
        )

    if target:
        if "ms" in target:
            target_info = f" ({target})"
        else:
            target_info = f" ({latency:.1f} ms, {target})"
    else:
        target_info = f" ({latency:.1f} ms)"

    if latency < FAST_LATENCY_MS:
        return f"[bold green]🟢 极佳{target_info}[/bold green]", None
    if latency < MEDIUM_LATENCY_MS:
        return f"[bold yellow]🟡 良好{target_info}[/bold yellow]", None
    return (
        f"[bold red]🟠 较慢{target_info}[/bold red]",
        "[bold yellow]⚠️  当前镜像访问延迟偏高，建议切换至更快的镜像源。[/bold yellow]",
    )


def evaluate_network_from_results(results, mirrors=None):
    """Evaluate network status directly from mirror probe results.

    Returns (min_latency, any_success, target_description).
    """
    source_map = MIRRORS if mirrors is None else mirrors
    successful_items = [
        (lat, alias)
        for alias, (lat, succ) in results.items()
        if succ and lat is not None and lat != float("inf")
    ]
    if not successful_items:
        return float("inf"), False, None
    min_lat, best_alias = min(successful_items, key=lambda item: item[0])
    mirror_name = source_map.get(best_alias, {}).get("name", best_alias)
    return min_lat, True, f"最快节点: {mirror_name}"


def check_network_status(timeout=1.5, current_url=None):
    """Probe representative PyPI endpoints (including custom active mirror if set)."""
    probe_targets = []
    seen_urls = []

    def add_target(key, label, url):
        if not url or any(urls_match(url, existing) for existing in seen_urls):
            return
        seen_urls.append(url)
        probe_targets.append((key, label, {"url": url}))

    if current_url:
        _, matched_info, _ = match_mirror_by_url(current_url)
        cur_label = f"{matched_info['name']}(当前)" if matched_info else "当前自定义源"
        add_target("current", cur_label, current_url)
    for preset_key in ("aliyun", "tsinghua", "pypi"):
        if preset_key in MIRRORS:
            info = MIRRORS[preset_key]
            add_target(preset_key, info["name"], info["url"])

    ssl_context = create_ssl_context()
    opener = build_shared_opener(ssl_context)
    results = {}

    with ThreadPoolExecutor(max_workers=len(probe_targets)) as executor:
        futures = {
            executor.submit(
                test_single_mirror,
                key,
                info,
                timeout,
                ssl_context,
                opener,
            ): (key, label)
            for key, label, info in probe_targets
        }
        for future in as_completed(futures):
            key, label = futures[future]
            try:
                _, duration, success = future.result()
                results[key] = (label, duration, success)
            except Exception:
                results[key] = (label, float("inf"), False)

    detail_parts = []
    success_items = []
    for key, label, _ in probe_targets:
        _, duration, success = results.get(key, (label, float("inf"), False))
        if success and duration != float("inf"):
            detail_parts.append(f"{label}: {duration:.1f} ms")
            success_items.append((duration, label))
        else:
            detail_parts.append(f"{label}: [bold red]不可达[/bold red]")

    details_str = ", ".join(detail_parts)
    if success_items:
        min_latency, _ = min(success_items, key=lambda item: item[0])
        return min_latency, True, details_str
    return float("inf"), False, details_str
