import pytest
import socket
import json
import threading
import time
from cmd_proxy.client import CommandProxyClient, execute, CommandProxyError

# 简单模拟服务端
class MockServer:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(socket_path)
        self.server.listen(1)
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        while self.running:
            try:
                conn, _ = self.server.accept()
                data = conn.recv(4096)
                if data:
                    req = json.loads(data.decode())
                    args = req.get('args', [])
                    # 模拟回显：返回参数列表的 JSON
                    response = json.dumps({'args': args})
                    conn.sendall(response.encode())
                conn.close()
            except:
                pass

    def stop(self):
        self.running = False
        self.server.close()
        self.thread.join()

@pytest.fixture
def mock_server(tmp_path):
    sock_path = str(tmp_path / 'test.sock')
    server = MockServer(sock_path)
    yield sock_path
    server.stop()

def test_execute(mock_server):
    output = execute(['ping', 'pong'], socket_path=mock_server)
    assert output == '{"args": ["ping", "pong"]}'

def test_client_send(mock_server):
    client = CommandProxyClient(socket_path=mock_server)
    output = client.send(['hello', 'world'])
    assert output == '{"args": ["hello", "world"]}'
    client.close()

def test_invalid_socket():
    with pytest.raises(CommandProxyError) as exc:
        execute(['test'], socket_path='/nonexistent.sock')
    assert 'Failed to connect' in str(exc.value)