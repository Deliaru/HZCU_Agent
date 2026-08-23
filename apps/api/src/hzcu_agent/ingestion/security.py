from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from hzcu_agent.ingestion.catalog import SourceConfig


class SourceUrlRejected(ValueError):
    pass


def canonicalize_source_url(
    url: str,
    source: SourceConfig,
    base_url: str | None = None,
) -> str:
    absolute = urljoin(base_url or source.base_url, url.strip())
    try:
        parsed = urlsplit(absolute)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        scheme = (parsed.scheme or "").lower()
    except ValueError as exc:
        raise SourceUrlRejected("Malformed source URL") from exc
    if not _is_exact_allowlisted_url(
        scheme=scheme,
        host=host,
        port=port,
        username=parsed.username,
        password=parsed.password,
        allowed_hosts=source.allowed_hosts,
    ):
        raise SourceUrlRejected("URL is outside the registered source allowlist")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "spm"}
    ]
    query = urlencode(sorted(query_items), doseq=True)
    path = parsed.path or "/"
    # Preserve registered scheme: many campus CMS hosts only serve HTTP.
    return urlunsplit((scheme, host, path, query, ""))


def _is_exact_allowlisted_url(
    *,
    scheme: str,
    host: str,
    port: int | None,
    username: str | None,
    password: str | None,
    allowed_hosts: list[str],
) -> bool:
    if username is not None or password is not None:
        return False
    if not host or host not in allowed_hosts:
        return False
    if scheme == "https" and port in (None, 443):
        return True
    if scheme == "http" and port in (None, 80):
        return True
    return False
