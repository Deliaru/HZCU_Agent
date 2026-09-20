"""Client network admission; only configured immediate proxies may assert an IP."""

from ipaddress import ip_address, ip_network

from fastapi import Request


def client_ip(request: Request) -> str | None:
    try:
        peer = ip_address(request.client.host) if request.client else None
        if peer is None:
            return None
        trusted = [
            ip_network(value.strip())
            for value in request.app.state.settings.network_trusted_proxy_cidrs.split(",")
            if value.strip()
        ]
        if any(peer in network for network in trusted):
            # The public edge overwrites this header. Missing/duplicate/malformed
            # headers fail closed; never use the untrusted X-Forwarded-For list.
            values = request.headers.getlist("x-hzcu-client-ip")
            if len(values) != 1:
                return None
            peer = ip_address(values[0].strip())
        if getattr(peer, "ipv4_mapped", None):
            peer = peer.ipv4_mapped
        return str(peer)
    except ValueError:
        return None


def network_access(request: Request, principal, snapshot) -> dict:
    ip = client_ip(request)
    reason = "denied"
    if not snapshot.network_restriction_enabled:
        reason = "disabled"
    elif principal.authenticated and principal.role == "admin" and snapshot.network_admin_bypass:
        reason = "admin_bypass"
    elif (
        principal.authenticated
        and principal.role == "contributor"
        and snapshot.network_contributor_bypass
    ):
        reason = "contributor_bypass"
    elif ip and any(ip_address(ip) in ip_network(cidr) for cidr in snapshot.network_allowed_cidrs):
        reason = "allowlist"
    return {"ip": ip, "allowed": reason != "denied", "reason": reason}
