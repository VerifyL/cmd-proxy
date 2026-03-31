#!/usr/bin/env python3
# cmd_proxy/server.py

import os
import sys
import json
import socket
import signal
import logging
import argparse
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor

DEFAULT_SOCKET_PATH = '/tmp/cmd-proxy/cmd-proxy.sock'
DEFAULT_TIMEOUT = 10
DEFAULT_BACKLOG = 128
DEFAULT_WORKERS = 4

# 内部默认白名单
DEFAULT_COMMANDS = {
    'mstpctl': {
        'sudo': True,
        'max_args': 10,
        'arg_patterns': r'^[a-zA-Z0-9_.-]+$'
    },
    'health': {
        'sudo': False,
        'max_args': 0,
        'arg_patterns': None,
        'virtual': True
    }
}

def parse_args():
    parser = argparse.ArgumentParser(description='Command Proxy Server')
    parser.add_argument('-c', '--config', help='YAML config file (optional)')
    parser.add_argument('-s', '--socket', default=DEFAULT_SOCKET_PATH,
                        help='Unix socket path')
    parser.add_argument('-t', '--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help='Default command timeout (seconds)')
    parser.add_argument('-w', '--workers', type=int, default=DEFAULT_WORKERS,
                        help='Thread pool size')
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug logging')
    return parser.parse_args()

def load_config(config_path):
    """加载 YAML 配置文件，如果不存在或解析失败则返回 None"""
    if not config_path or not os.path.exists(config_path):
        return None
    try:
        import yaml
        with open(config_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        logging.error("PyYAML not installed. Please install it: pip install pyyaml")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Failed to parse config file: {e}")
        sys.exit(1)

def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger('cmd-proxy')

class CommandProxy:
    def __init__(self, socket_path, allowed_cmds, default_timeout, workers, logger):
        self.socket_path = socket_path
        self.allowed_cmds = allowed_cmds
        self.default_timeout = default_timeout
        self.workers = workers
        self.logger = logger
        self.server_socket = None
        self.executor = None
        self.running = False

    def is_command_allowed(self, base_cmd, args):
        if base_cmd not in self.allowed_cmds:
            self.logger.warning(f"Command '{base_cmd}' not in whitelist")
            return False
        rule = self.allowed_cmds[base_cmd]
        max_args = rule.get('max_args', 0)
        if len(args) > max_args:
            self.logger.warning(f"Too many arguments for '{base_cmd}': {len(args)} > {max_args}")
            return False

        arg_patterns = rule.get('arg_patterns')
        if arg_patterns:
            if isinstance(arg_patterns, str):
                for arg in args:
                    if not re.match(arg_patterns, arg):
                        self.logger.warning(f"Argument '{arg}' does not match pattern {arg_patterns}")
                        return False
            elif isinstance(arg_patterns, list):
                if len(arg_patterns) != len(args):
                    self.logger.warning(f"Number of patterns does not match arguments")
                    return False
                for pat, arg in zip(arg_patterns, args):
                    if not re.match(pat, arg):
                        self.logger.warning(f"Argument '{arg}' does not match pattern {pat}")
                        return False
        return True

    def execute_command(self, base_cmd, args, timeout):
        rule = self.allowed_cmds.get(base_cmd, {})
        if rule.get('virtual', False):
            # 健康检查命令
            if base_cmd == 'health':
                return "ok", "", 0
            else:
                return "", f"Virtual command {base_cmd} not implemented", 1

        use_sudo = rule.get('sudo', True)
        full_cmd = []
        if use_sudo:
            full_cmd.append('sudo')
        full_cmd.append(base_cmd)
        full_cmd.extend(args)

        self.logger.debug(f"Executing: {' '.join(full_cmd)}")
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            self.logger.error(f"Command timeout after {timeout}s: {base_cmd} {args}")
            return "", f"Command timed out after {timeout} seconds", -1
        except Exception as e:
            self.logger.exception(f"Unexpected error executing {base_cmd}: {e}")
            return "", str(e), -1

    def handle_connection(self, conn, addr):
        try:
            data = b''
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b'\n'):
                    break
            if not data:
                return

            try:
                req = json.loads(data.decode())
            except json.JSONDecodeError:
                conn.sendall(b"Invalid JSON")
                self.logger.warning("Invalid JSON received")
                return

            if 'args' not in req or not isinstance(req['args'], list) or len(req['args']) == 0:
                conn.sendall(b"Invalid request: missing 'args' or args is not a non-empty list")
                return

            base_cmd = req['args'][0]
            cmd_args = req['args'][1:]
            timeout = req.get('timeout', self.default_timeout)

            if not self.is_command_allowed(base_cmd, cmd_args):
                conn.sendall(b"Command not allowed")
                return

            stdout, stderr, ret = self.execute_command(base_cmd, cmd_args, timeout)
            output = stdout if stdout else stderr
            conn.sendall(output.encode())
            self.logger.info(f"Command {base_cmd} {cmd_args} returned {ret} ({len(output)} bytes)")
        except Exception as e:
            self.logger.exception(f"Error handling connection: {e}")
            try:
                conn.sendall(f"Internal error: {e}".encode())
            except:
                pass
        finally:
            conn.close()

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(DEFAULT_BACKLOG)
        os.chmod(self.socket_path, 0o600)
        self.logger.info(f"Listening on {self.socket_path}")

        self.running = True
        self.executor = ThreadPoolExecutor(max_workers=self.workers)

        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        while self.running:
            try:
                conn, addr = self.server_socket.accept()
                self.executor.submit(self.handle_connection, conn, addr)
            except OSError as e:
                if self.running:
                    self.logger.exception(f"Accept error: {e}")
            except Exception as e:
                self.logger.exception(f"Unexpected error: {e}")

    def signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        if self.executor:
            self.executor.shutdown(wait=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        sys.exit(0)

def main():
    args = parse_args()
    logger = setup_logging(args.debug)

    # 加载配置文件（可选）
    config = load_config(args.config)
    if config and 'commands' in config:
        allowed_cmds = config['commands']
        logger.info(f"Loaded commands from config file: {args.config}")
    else:
        # 使用内部默认白名单
        allowed_cmds = DEFAULT_COMMANDS.copy()
        if args.config:
            logger.warning(f"Config file {args.config} has no 'commands' section, using default whitelist")
        else:
            logger.info("No config file provided, using default whitelist")

    server = CommandProxy(
        socket_path=args.socket,
        allowed_cmds=allowed_cmds,
        default_timeout=args.timeout,
        workers=args.workers,
        logger=logger
    )
    server.start()

if __name__ == '__main__':
    main()
