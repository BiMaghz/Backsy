import logging
import socket
import urllib3.util.connection as urllib3_cn

logger = logging.getLogger(__name__)

_configured = False

def _ipv6_works(host: str, port: int, timeout: float) -> bool:
    try:
        addr_info = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
        if not addr_info:
            return False
        family, socktype, proto, _canonname, sockaddr = addr_info[0]
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False

def configure_ip_family(host: str = "api.telegram.org", port: int = 443, timeout: float = 5) -> None:
    """
    Called once at startup. If IPv6 to `host` is unusable (a common VPS problem), force
    IPv4 for the whole process; otherwise every connection first waits out an IPv6
    timeout. The setting is process-wide on purpose (requests and boto3 share urllib3),
    which is why it lives here and not in a destination's constructor.
    """
    global _configured
    if _configured:
        return
    _configured = True

    logger.info(f"Testing network connectivity to {host}...")
    if _ipv6_works(host, port, timeout):
        logger.info("IPv6 is healthy. Using default network settings.")
        return

    logger.warning(f"IPv6 to {host} failed or timed out. Forcing IPv4 to improve speed.")
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
