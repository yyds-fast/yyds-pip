from urllib.parse import urlparse

DEFAULT_INDEX_URL = "https://pypi.org/simple/"

# Mainstream active PyPI mirrors in China + official PyPI
MIRRORS = {
    "aliyun": {
        "name": "阿里云",
        "url": "https://mirrors.aliyun.com/pypi/simple/",
        "trusted_host": None,
    },
    "tsinghua": {
        "name": "清华大学",
        "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "trusted_host": None,
    },
    "tencent": {
        "name": "腾讯云",
        "url": "https://mirrors.cloud.tencent.com/pypi/simple/",
        "trusted_host": None,
    },
    "huawei": {
        "name": "华为云",
        "url": "https://repo.huaweicloud.com/repository/pypi/simple/",
        "trusted_host": None,
    },
    "ustc": {
        "name": "中国科学技术大学",
        "url": "https://mirrors.ustc.edu.cn/pypi/simple/",
        "trusted_host": None,
        "legacy_urls": (
            "https://pypi.mirrors.ustc.edu.cn/simple/",
            "https://mirrors.ustc.edu.cn/pypi/web/simple/",
        ),
    },
    "bfsu": {
        "name": "北京外国语大学",
        "url": "https://mirrors.bfsu.edu.cn/pypi/web/simple/",
        "trusted_host": None,
    },
    "sjtu": {
        "name": "上海交通大学",
        "url": "https://mirrors.sjtug.sjtu.edu.cn/pypi/web/simple/",
        "trusted_host": None,
        "legacy_urls": (
            "https://mirror.sjtu.edu.cn/pypi/web/simple/",
        ),
    },
    "pypi": {
        "name": "PyPI 官方",
        "url": DEFAULT_INDEX_URL,
        "trusted_host": None,
    },
}

# Mirrors that have permanently shut down; kept for clear diagnostics and display
DEPRECATED_MIRRORS = {
    "douban": {
        "name": "豆瓣 (已停服)",
        "url": "https://pypi.doubanio.com/simple/",
        "legacy_urls": (
            "http://pypi.douban.com/simple/",
            "https://pypi.douban.com/simple/",
            "http://pypi.doubanio.com/simple/",
        ),
    },
}


def normalize_index_url(url):
    """Normalize an index URL for case-insensitive scheme/host comparison."""
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Strip default ports so https://pypi.org:443/simple/ matches https://pypi.org/simple/
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    elif scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def urls_match(url_a, url_b):
    """Return True when two index URLs point to the same normalized endpoint."""
    if not url_a or not url_b:
        return False
    return normalize_index_url(url_a) == normalize_index_url(url_b)


def match_mirror_by_url(url):
    """Match a URL against active or deprecated presets.

    Returns (alias, info_dict, is_deprecated) or (None, None, False).
    """
    if not url:
        return None, None, False
    target = normalize_index_url(url)
    for alias, info in MIRRORS.items():
        candidates = (info["url"],) + tuple(info.get("legacy_urls", ()))
        if any(normalize_index_url(candidate) == target for candidate in candidates):
            return alias, info, False
    for alias, info in DEPRECATED_MIRRORS.items():
        candidates = (info["url"],) + tuple(info.get("legacy_urls", ()))
        if any(normalize_index_url(candidate) == target for candidate in candidates):
            return alias, info, True
    return None, None, False


def resolve_mirror_display_name(url):
    """Return a human-friendly display name for the given configured URL."""
    if not url:
        return f"{MIRRORS['pypi']['name']} (pypi)"
    alias, info, is_deprecated = match_mirror_by_url(url)
    if alias and info:
        if is_deprecated:
            return f"{info['name']} ({alias})"
        return f"{info['name']} ({alias})"
    return "自定义/未知源"
