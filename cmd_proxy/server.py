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
import shlex
import time
import threading

DEFAULT_SOCKET_PATH = '/tmp/cmd-proxy/cmd-proxy.sock'
DEFAULT_TIMEOUT = 20
DEFAULT_BACKLOG = 128
DEFAULT_WORKERS = 4

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
            if base_cmd == 'health':
                return "ok", "", 0
            else:
                return "", f"Virtual command {base_cmd} not implemented", 1

        use_sudo = rule.get('sudo', True)
        cmd_list = [base_cmd] + args
        need_shell = any(c in '|&;<>' for arg in cmd_list for c in arg)

        if base_cmd == 'config':
            if 'reload' in args or 'reboot' in args:
                action = 'reload' if 'reload' in args else 'reboot'
                self.logger.warning(f"Destructive command 'config {action}' triggered. Running in background.")
                
                log_dir = "/var/log"
                pid_file = f'{log_dir}/.config_{action}.pid'
                if os.path.exists(pid_file):
                    try:
                        with open(pid_file, 'r') as f:
                            old_pid = int(f.read().strip())
                        os.kill(old_pid, 0)
                        self.logger.warning(f"Another config {action} already running (PID {old_pid})")
                        return f"Another config {action} is already in progress. Please wait.", "", 1
                    except (ProcessLookupError, FileNotFoundError, ValueError):
                        os.unlink(pid_file)

                log_file = f"{log_dir}/config_{action}.log"

                cmd_list = ['sudo', 'config'] + args
                if '-y' not in cmd_list and '--yes' not in cmd_list:
                    cmd_list.append('-y')
                full_cmd = ' '.join(shlex.quote(part) for part in cmd_list)
                full_cmd += f' > {log_file} 2>&1'

                wrapper_cmd = f'echo $$ > {pid_file} && {full_cmd}; rm -f {pid_file}'
                self.logger.debug(f"Background command: {wrapper_cmd}")

                try:
                    subprocess.Popen(
                        wrapper_cmd,
                        shell=True,
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=None,
                        stderr=None
                    )
                    if action == 'reboot':
                        msg = "Config reboot triggered. System will reboot."
                        self.logger.info(msg)
                        return msg, "", 0

                    if os.path.exists(pid_file):
                        with open(pid_file, 'r') as f:
                            try:
                                pid = int(f.read().strip())
                                os.kill(pid, 0)
                                msg = f"Config reload triggered in background. Monitor log: {log_file}"
                                self.logger.info(msg)
                                return msg, "", 0
                            except (ValueError, ProcessLookupError, OSError):
                                pass
                    return "", "Config reload failed to start", 1
                except Exception as e:
                    self.logger.exception(f"Failed to start config {action}")
                    return "", f"Failed to start config {action}: {str(e)}", -1

        start_time = time.time()
        try:
            if need_shell:
                shell_metachars = set('|&;<>')
                escaped_parts = []
                for arg in cmd_list:
                    if len(arg) == 1 and arg in shell_metachars:
                        escaped_parts.append(arg)
                    else:
                        escaped_parts.append(shlex.quote(arg))
                full_cmd_str = " ".join(escaped_parts)
                if use_sudo:
                    full_cmd_str = "sudo " + full_cmd_str
                self.logger.debug(f"Executing (shell mode): {full_cmd_str}")
                result = subprocess.run(
                    full_cmd_str,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False
                )
            else:
                final_cmd = []
                if use_sudo:
                    final_cmd.append('sudo')
                final_cmd.extend(cmd_list)
                self.logger.debug(f"Executing (list mode): {final_cmd}")
                result = subprocess.run(
                    final_cmd,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False
                )
            elapsed = time.time() - start_time
            self.logger.debug(f"Command completed in {elapsed:.3f}s, returncode={result.returncode}")
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            self.logger.error(f"Command timeout after {timeout}s: {base_cmd} {args}")
            return "", f"Command timed out after {timeout} seconds", -1
        except Exception as e:
            self.logger.exception(f"Unexpected error executing {base_cmd}: {e}")
            return "", str(e), -1

    def execute_command_stream(self, base_cmd, args, timeout, conn):
        """流式执行命令，将输出逐行发送到 conn，最后发送结束标记"""
        rule = self.allowed_cmds.get(base_cmd, {})
        if rule.get('virtual', False):
            if base_cmd == 'health':
                conn.sendall(b"ok\n__END__\n")
            else:
                conn.sendall(f"Virtual command {base_cmd} not implemented\n__END__\n".encode())
            return

        use_sudo = rule.get('sudo', True)
        cmd_list = [base_cmd] + args
        need_shell = any(c in '|&;<>' for arg in cmd_list for c in arg)

        if base_cmd == 'config':
            if 'reload' in args or 'reboot' in args:
                stdout, stderr, ret = self.execute_command(base_cmd, args, timeout)
                output = stdout if stdout else stderr
                conn.sendall(output.encode())
                conn.sendall(b"__END__\n")
                return
            
        try:
            if need_shell:
                shell_metachars = set('|&;<>')
                escaped_parts = []
                for arg in cmd_list:
                    if len(arg) == 1 and arg in shell_metachars:
                        escaped_parts.append(arg)
                    else:
                        escaped_parts.append(shlex.quote(arg))
                full_cmd_str = " ".join(escaped_parts)
                if use_sudo:
                    full_cmd_str = "sudo " + full_cmd_str
                self.logger.debug(f"Stream executing (shell mode): {full_cmd_str}")
                proc = subprocess.Popen(
                    full_cmd_str,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
            else:
                final_cmd = []
                if use_sudo:
                    final_cmd.append('sudo')
                final_cmd.extend(cmd_list)
                self.logger.debug(f"Stream executing (list mode): {final_cmd}")
                proc = subprocess.Popen(
                    final_cmd,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )

            # 设置超时控制（使用 timer）
            timer = threading.Timer(timeout, proc.terminate)
            timer.start()
            try:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    try:
                        conn.sendall(line.encode())
                    except (BrokenPipeError, socket.error):
                        self.logger.debug("Client disconnected, stopping stream")
                        proc.terminate()
                        break
                proc.wait()
            finally:
                timer.cancel()
            try:
                conn.sendall(b"__END__\n")
            except (BrokenPipeError, socket.error):
                # 客户端可能已断开，忽略
                self.logger.debug("Client disconnected while sending END marker")
            self.logger.debug(f"Stream command finished with returncode {proc.returncode}")
        except Exception as e:
            self.logger.exception(f"Unexpected error streaming {base_cmd}: {e}")
            try:
                conn.sendall(f"Internal error: {str(e)}\n__END__\n".encode())
            except:
                pass

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
            stream = req.get('stream', False)

            if not self.is_command_allowed(base_cmd, cmd_args):
                conn.sendall(b"Command not allowed")
                return

            if stream:
                self.execute_command_stream(base_cmd, cmd_args, timeout, conn)
            else:
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
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except:
                pass
            conn.close()

    def start(self):
        socket_dir = os.path.dirname(self.socket_path)
        if socket_dir:
            try:
                os.makedirs(socket_dir, mode=0o775, exist_ok=True)
                self.logger.info(f"Created/verified socket directory: {socket_dir} (mode: 0o775)")
                os.chmod(socket_dir, 0o775)
            except Exception as e:
                self.logger.error(f"Failed to create socket directory {socket_dir}: {e}")
                sys.exit(1)

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
                self.logger.info(f"Removed existing socket file: {self.socket_path}")
            except Exception as e:
                self.logger.error(f"Failed to remove existing socket file: {e}")
                sys.exit(1)

        try:
            self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(self.socket_path)
            self.server_socket.listen(DEFAULT_BACKLOG)
            os.chmod(self.socket_path, 0o666)
            self.logger.info(f"Successfully started server, listening on {self.socket_path} (socket mode: 0o666)")
        except Exception as e:
            self.logger.error(f"Failed to start server: {e}")
            sys.exit(1)

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
            try:
                self.server_socket.close()
            except Exception as e:
                self.logger.warning(f"Failed to close server socket: {e}")
        if self.executor:
            self.executor.shutdown(wait=True)
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
                self.logger.info(f"Removed socket file: {self.socket_path}")
            except Exception as e:
                self.logger.warning(f"Failed to remove socket file: {e}")
        sys.exit(0)

def main():
    args = parse_args()
    logger = setup_logging(args.debug)

    config = load_config(args.config)
    if config and 'commands' in config:
        allowed_cmds = config['commands']
        logger.info(f"Loaded commands from config file: {args.config}")
    else:
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
