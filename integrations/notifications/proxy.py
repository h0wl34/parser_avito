SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def build_requests_proxy(proxy: str | None):
    """Build a ``requests`` proxy mapping for notifier traffic.

    Values without an explicit scheme are kept backward compatible and treated
    as HTTP proxies. Explicit HTTP(S) and SOCKS5/SOCKS5H URLs are preserved.
    """
    if not proxy:
        return None

    proxy_url = proxy.strip()
    if not proxy_url:
        return None

    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    else:
        scheme = proxy_url.split("://", 1)[0].lower()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            supported = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
            raise ValueError(
                f"Unsupported notifier proxy scheme '{scheme}'. "
                f"Supported schemes: {supported}"
            )

    return {
        "http": proxy_url,
        "https": proxy_url,
    }
