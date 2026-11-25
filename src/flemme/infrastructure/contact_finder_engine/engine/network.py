"""Connexions requests épinglées, sans modifier DNS, sockets ou pools globaux."""

import ipaddress
import socket

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError


def public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Accès à un réseau privé/local refusé.")
    return addresses


class PublicConnection:
    def _new_conn(self):
        try:
            addresses = public_addresses(self._dns_host, self.port)
            for family, kind, protocol, _, address in addresses:
                sock = socket.socket(family, kind, protocol)
                try:
                    sock.settimeout(self.timeout)
                    for option in self.socket_options or ():
                        sock.setsockopt(*option)
                    if self.source_address:
                        sock.bind(self.source_address)
                    # No second DNS lookup; Host, TLS verification and SNI keep self.host.
                    sock.connect(address)
                    peer = ipaddress.ip_address(sock.getpeername()[0])
                    if not peer.is_global or peer != ipaddress.ip_address(address[0]):
                        raise ValueError("Pair réseau privé/local ou différent de l'IP validée.")
                    return sock
                except OSError:
                    sock.close()
                except Exception:
                    sock.close()
                    raise
        except (OSError, ValueError):
            raise NewConnectionError(self, "Connexion publique refusée ou inaccessible.") from None
        raise NewConnectionError(self, "Connexion publique inaccessible.")


class PublicHTTPConnection(PublicConnection, HTTPConnection):
    pass


class PublicHTTPSConnection(PublicConnection, HTTPSConnection):
    pass


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": PublicHTTPPool,
            "https": PublicHTTPSPool,
        }
