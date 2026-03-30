import pytest
from cmd_proxy.server import CommandProxy

class DummyLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def exception(self, msg): pass

@pytest.fixture
def proxy():
    allowed = {
        'mstpctl': {
            'sudo': True,
            'max_args': 2,
            'arg_patterns': r'^[a-zA-Z0-9_.-]+$'
        },
        'ping': {
            'sudo': False,
            'max_args': 0,
            'virtual': True
        }
    }
    return CommandProxy('/tmp/test.sock', allowed, 10, 4, DummyLogger())

def test_is_command_allowed(proxy):
    assert proxy.is_command_allowed('mstpctl', ['showbridge', 'Bridge']) is True
    assert proxy.is_command_allowed('mstpctl', ['showbridge']) is True
    assert proxy.is_command_allowed('mstpctl', ['showbridge', 'Bridge', 'extra']) is False  # too many args
    assert proxy.is_command_allowed('invalid', []) is False

def test_execute_command_virtual(proxy):
    stdout, stderr, rc = proxy.execute_command('ping', [], 5)
    assert stdout == 'pong'
    assert rc == 0

def test_execute_command_real(monkeypatch, proxy):
    # Mock subprocess.run to avoid real execution
    import subprocess
    def mock_run(*args, **kwargs):
        class MockResult:
            stdout = 'mocked output'
            stderr = ''
            returncode = 0
        return MockResult()
    monkeypatch.setattr(subprocess, 'run', mock_run)
    stdout, stderr, rc = proxy.execute_command('mstpctl', ['showbridge', 'Bridge'], 5)
    assert stdout == 'mocked output'
    assert rc == 0