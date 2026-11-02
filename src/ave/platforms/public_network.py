"""HTTPS sockets pinned to checked public DNS answers, retaining TLS host identity.

Every new socket resolves once, validates the entire answer set, and connects to
numeric sockaddr values only. urllib3 still owns SNI and certificate verification
against the original hostname. Proxies and automatic retries are disabled.
"""
import ipaddress
import socket
import time
import queue
import threading
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from requests.adapters import HTTPAdapter
import requests

_resolvers = threading.BoundedSemaphore(4)


def resolve(host, port, timeout=5):
    deadline=time.monotonic()+timeout
    if not _resolvers.acquire(timeout=min(1,timeout)):
        raise socket.timeout('DNS resolver busy')
    result = queue.Queue(maxsize=1)
    def run():
        try:result.put((socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM), None))
        except Exception as exc:result.put((None, exc))
        finally:_resolvers.release()
    threading.Thread(target=run, daemon=True).start()
    try:rows, error = result.get(timeout=max(.001,deadline-time.monotonic()))
    except queue.Empty:raise socket.timeout('DNS resolution timed out') from None
    if error:raise error
    return rows


def public_addresses(host, port, timeout=5):
    rows = resolve(host, port, timeout)
    if not rows:
        raise ValueError('Public DNS resolution returned no addresses')
    for family, kind, protocol, _, address in rows:
        ip = ipaddress.ip_address(address[0])
        if (family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM
                or not ip.is_global or ip.is_multicast or ip.is_reserved
                or getattr(ip, 'ipv4_mapped', None) or getattr(ip, 'sixtofour', None)
                or getattr(ip, 'teredo', None) or (family == socket.AF_INET6 and address[3])):
            raise ValueError('Connection requires exclusively public DNS addresses')
    return rows


class PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        deadline = time.monotonic() + float(self.timeout)
        rows = public_addresses(self._dns_host, self.port, float(self.timeout))
        error = None
        for family, kind, protocol, _, address in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('Public connection budget exhausted')
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(remaining)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                # Numeric address from the validated resolution: no second DNS lookup.
                sock.connect(address)
                return sock
            except OSError as exc:
                error = exc
                sock.close()
        raise error or OSError('No public address connected')


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicHTTPSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme, 'https': PublicHTTPSPool}

    def proxy_manager_for(self, *args, **kwargs):
        raise ValueError('Provider verification does not use proxies')


def session():
    transport = requests.Session()
    transport.trust_env = False
    transport.mount('https://', PublicHTTPSAdapter(max_retries=0))
    return transport
