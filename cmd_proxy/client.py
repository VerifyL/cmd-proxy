# cmd_proxy/client.py

import socket
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = '/tmp/cmd-proxy/cmd-proxy.sock'
DEFAULT_TIMEOUT = 20

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

    def send(self, args: List[str], timeout: Optional[int] = None, stream_callback=None) -> Optional[str]:
        """
        Send command to proxy.
        If stream_callback is provided, it will be called with each chunk of output (as str)
        as it is received. The final accumulated string is still returned (or None if no data).
        If stream_callback is not provided, behaves as before: returns the complete output.
        """
        req = json.dumps({'args': args, 'timeout': timeout or self.timeout, 'stream': stream_callback is not None})
        try:
            self._sock.sendall(req.encode() + b'\n')
            data = b''
            if stream_callback:
                # 流式模式：逐块读取，遇到结束标记停止
                while True:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    if chunk == b'__END__\n':
                        break
                    data += chunk
                    stream_callback(chunk.decode())
                return None
            else:
                # 一次性模式：读取全部数据
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
            if stream_callback:
                while True:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    if chunk == b'__END__\n':
                        break
                    data += chunk
                    stream_callback(chunk.decode())
                return None
            else:
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

def execute(args: List[str], socket_path: str = DEFAULT_SOCKET_PATH, timeout: int = DEFAULT_TIMEOUT, stream_callback=None) -> Optional[str]:
    with CommandProxyClient(socket_path, timeout) as client:
        return client.send(args, timeout, stream_callback)
