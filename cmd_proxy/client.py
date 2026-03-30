# cmd_proxy/client.py

import socket
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = '/tmp/cmd-proxy.sock'
DEFAULT_TIMEOUT = 10

class CommandProxyError(Exception):
    pass

class CommandProxyClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, timeout: int = DEFAULT_TIMEOUT):
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock = None
        self._connect()

    def _connect(self):
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect(self.socket_path)
        except Exception as e:
            raise CommandProxyError(f"Failed to connect to proxy at {self.socket_path}: {e}")

    def _reconnect(self):
        self.close()
        self._connect()

    def send(self, args: List[str], timeout: Optional[int] = None) -> str:
        req = json.dumps({'args': args, 'timeout': timeout or self.timeout})
        try:
            self._sock.sendall(req.encode() + b'\n')
            data = b''
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.decode()
        except (socket.error, socket.timeout) as e:
            logger.warning(f"Proxy connection error: {e}, reconnecting...")
            self._reconnect()
            self._sock.sendall(req.encode() + b'\n')
            data = b''
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.decode()
        except Exception as e:
            raise CommandProxyError(f"Request failed: {e}")

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

def execute(args: List[str], socket_path: str = DEFAULT_SOCKET_PATH, timeout: int = DEFAULT_TIMEOUT) -> str:
    with CommandProxyClient(socket_path, timeout) as client:
        return client.send(args, timeout)
