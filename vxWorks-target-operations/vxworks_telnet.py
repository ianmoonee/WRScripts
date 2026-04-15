"""
VxWorks Target Operations via Telnet

Connects to a VxWorks target's serial console over telnet,
reboots it via a local command (vlmTool), stops autoboot,
boots VxWorks, and runs a configured set of commands.
"""

import telnetlib
import json
import re
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

DEFAULT_TIMEOUT = 10
DEFAULT_REBOOT_TIMEOUT = 180
DEFAULT_PROVISIONER_PROMPT = "-> "
DEFAULT_VXWORKS_PROMPT = "=>"
DEFAULT_AUTOBOOT_PATTERN = "Hit any key to stop autoboot"
DEFAULT_BOOT_CMD = "run bootprov"
REBOOT_CMD_TEMPLATE = "vlmTool reboot -t {target}"
RESERVE_CMD_TEMPLATE = "vlmTool reserve -t {target}"
FORCE_RESERVE_CMD_TEMPLATE = "vlmTool reserve -t {target} -f"
RESERVE_NOTE_CMD_TEMPLATE = 'vlmTool reserveNote -t {target} -n "copying_images"'
UNRESERVE_CMD_TEMPLATE = "vlmTool unreserve -t {target}"


class log:
    @staticmethod
    def _print(level, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} [{level}] {msg}", flush=True)

    @staticmethod
    def info(msg): log._print("INFO", msg)
    @staticmethod
    def warning(msg): log._print("WARNING", msg)
    @staticmethod
    def error(msg): log._print("ERROR", msg)


class VxWorksTelnet:

    def __init__(self, host, port, timeout=DEFAULT_TIMEOUT,
                 provisioner_prompt=DEFAULT_PROVISIONER_PROMPT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.provisioner_prompt = provisioner_prompt
        self.connection = None

    def connect(self, retries=12, retry_delay=10):
        # Kill any existing connections to this host:port before attempting
        self._kill_stale_connections(self.host, self.port)
        for attempt in range(1, retries + 1):
            try:
                self.connection = telnetlib.Telnet(self.host, self.port, self.timeout)
                log.info(f"Console connected to {self.host}:{self.port}")
                return
            except (ConnectionRefusedError, OSError) as e:
                if attempt < retries:
                    log.warning(f"Connection to {self.host}:{self.port} refused (attempt {attempt}/{retries}), "
                                f"retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise

    @staticmethod
    def reboot_target(reboot_cmd):
        """Reboot the target via local command (does not need telnet)."""
        log.info(f"Running local command: {reboot_cmd}")
        result = subprocess.run(reboot_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning(f"Reboot command exited with code {result.returncode}: {result.stderr.strip()}")
        else:
            log.info(f"Reboot command output: {result.stdout.strip()}")

    @staticmethod
    def reserve_target(target_name, retry_delay=1):
        """Reserve a target via vlmTool (does not need telnet).

        Behaviour depends on the FORCE_RESERVE environment variable:
          0        – never force-reserve; retry normal reserve forever
          1        – force-reserve immediately
          N (> 1)  – try normal reserve with retry_delay pauses, for up to
                     N seconds total, then fall back to force reserve
          unset    – same as 0
        """
        force_value = int(os.environ.get("FORCE_RESERVE", "0"))

        # FORCE_RESERVE=1 → force-reserve immediately
        if force_value == 1:
            reserve_cmd = FORCE_RESERVE_CMD_TEMPLATE.format(target=target_name)
            log.info(f"FORCE_RESERVE=1, force-reserving target {target_name}")
            log.info(f"Running local command: {reserve_cmd}")
            result = subprocess.run(reserve_cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to force-reserve target {target_name}: {result.stderr.strip()}")
            log.info(f"Reserve command output: {result.stdout.strip()}")
            return

        reserve_cmd = RESERVE_CMD_TEMPLATE.format(target=target_name)

        # FORCE_RESERVE=0 (or unset) → only normal reserve, never force, retry forever
        if force_value == 0:
            attempt = 0
            while True:
                attempt += 1
                log.info(f"Running local command: {reserve_cmd} (attempt {attempt})")
                result = subprocess.run(reserve_cmd, shell=True, capture_output=True, text=True)
                if result.returncode == 0:
                    log.info(f"Reserve command output: {result.stdout.strip()}")
                    return
                log.warning(f"Reserve attempt {attempt} failed: {result.stderr.strip()}")
                time.sleep(retry_delay)

        # FORCE_RESERVE=N (> 1) → retry normal reserve for up to N seconds, then force
        log.info(f"FORCE_RESERVE={force_value}, will retry normal reserve for up to {force_value}s before forcing")
        elapsed = 0
        attempt = 0
        while elapsed < force_value:
            attempt += 1
            log.info(f"Running local command: {reserve_cmd} (attempt {attempt}, {elapsed}s/{force_value}s elapsed)")
            result = subprocess.run(reserve_cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                log.info(f"Reserve command output: {result.stdout.strip()}")
                return
            log.warning(f"Reserve attempt {attempt} failed: {result.stderr.strip()}")
            if elapsed + retry_delay < force_value:
                time.sleep(retry_delay)
                elapsed += retry_delay
            else:
                break

        log.warning(f"Normal reserve failed after {elapsed}s, falling back to force reserve")
        force_cmd = FORCE_RESERVE_CMD_TEMPLATE.format(target=target_name)
        log.info(f"Running local command: {force_cmd}")
        result = subprocess.run(force_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to force-reserve target {target_name}: {result.stderr.strip()}")
        log.info(f"Reserve command output: {result.stdout.strip()}")

    @staticmethod
    def set_reserve_note(target_name):
        """Set a reserve note on a target via vlmTool."""
        note_cmd = RESERVE_NOTE_CMD_TEMPLATE.format(target=target_name)
        log.info(f"Running local command: {note_cmd}")
        result = subprocess.run(note_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning(f"Reserve note command exited with code {result.returncode}: {result.stderr.strip()}")
        else:
            log.info(f"Reserve note command output: {result.stdout.strip()}")

    @staticmethod
    def unreserve_target(target_name):
        """Unreserve a target via vlmTool (does not need telnet)."""
        unreserve_cmd = UNRESERVE_CMD_TEMPLATE.format(target=target_name)
        log.info(f"Running local command: {unreserve_cmd}")
        result = subprocess.run(unreserve_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning(f"Unreserve command exited with code {result.returncode}: {result.stderr.strip()}")
        else:
            log.info(f"Unreserve command output: {result.stdout.strip()}")

    def reboot_and_boot(self, reboot_timeout=DEFAULT_REBOOT_TIMEOUT,
                         vxworks_prompt=DEFAULT_VXWORKS_PROMPT,
                         autoboot_pattern=DEFAULT_AUTOBOOT_PATTERN,
                         boot_cmd=DEFAULT_BOOT_CMD):
        if not self.connection:
            raise ConnectionError("Not connected.")

        log.info(f"Waiting for autoboot pattern '{autoboot_pattern}'...")
        self._read_until_pattern(autoboot_pattern, reboot_timeout)
        log.info("Autoboot detected, sending Enter.")
        self.connection.write(b"\r\n")

        self._read_until(vxworks_prompt, reboot_timeout)
        log.info(f"VxWorks prompt '{vxworks_prompt}' received.")

        log.info(f">> {boot_cmd}")
        self.connection.write(boot_cmd.encode("ascii") + b"\r\n")

        # Consume the command echo (contains =>) before waiting for the real prompt
        time.sleep(1)
        try:
            self.connection.read_very_eager()
        except EOFError:
            pass

        prov_pat = re.escape(self.provisioner_prompt).encode("ascii")
        vx_pat = re.escape(vxworks_prompt).encode("ascii")
        combined = re.compile(b"(" + prov_pat + b"|" + vx_pat + b")")
        _, match, data = self.connection.expect([combined], reboot_timeout)
        output = data.decode("ascii", errors="replace")

        if match is None:
            raise TimeoutError(f"Timed out after boot command. Got:\n{self._last_lines(output)}")
        if vxworks_prompt in output and self.provisioner_prompt not in output:
            # Read any remaining buffered data for extra context
            try:
                extra = self.connection.read_very_eager().decode("ascii", errors="replace")
                output += extra
            except EOFError:
                pass
            raise RuntimeError(f"Boot failed — returned to VxWorks prompt.\n"
                               f"Last output:\n{self._last_lines(output)}")

        log.info(f"Provisioner prompt '{self.provisioner_prompt}' received. Waiting for boot to settle...")

        # Wait for boot-time output to finish, then send Enter for a clean prompt
        time.sleep(5)
        self.connection.read_very_eager()  # discard any remaining boot output
        self.connection.write(b"\r\n")
        self._read_until(self.provisioner_prompt, self.timeout)
        log.info("Target is ready.")

    def disconnect(self):
        if self.connection:
            # Kill our own socket's file descriptor to ensure a hard close
            try:
                sock = self.connection.get_socket()
                sock.shutdown(2)  # SHUT_RDWR
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
            log.info("Disconnected.")
            self._kill_stale_connections(self.host, self.port)

    @staticmethod
    def _kill_stale_connections(host, port):
        """Kill any processes still connected to host:port."""
        my_pid = os.getpid()
        pids_to_kill = set()

        # Try ss
        try:
            result = subprocess.run(
                ["ss", "-tnp", "dst", f"{host}:{port}"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "pid=" in line:
                    for part in line.split(","):
                        if part.startswith("pid="):
                            pid = int(part.split("=")[1].rstrip(")"))
                            if pid != my_pid:
                                pids_to_kill.add(pid)
        except Exception:
            pass

        # Try lsof as fallback/complement
        try:
            result = subprocess.run(
                ["lsof", "-i", f"@{host}:{port}", "-t"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid != my_pid:
                    pids_to_kill.add(pid)
        except Exception:
            pass

        for pid in pids_to_kill:
            log.info(f"Killing stale process (PID {pid}) connected to {host}:{port}")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run_command(self, command, wait_for=None, timeout=None):
        if not self.connection:
            raise ConnectionError("Not connected.")

        read_timeout = timeout or self.timeout
        self.connection.write(command.encode("ascii") + b"\r\n")

        if wait_for is None:
            return self._read_until(self.provisioner_prompt, read_timeout)
        return self._read_until_pattern(wait_for, read_timeout)

    def run_commands(self, commands):
        results = []
        for entry in commands:
            cmd = entry["cmd"]
            log.info(f">> {cmd}")
            output = self.run_command(cmd, entry.get("wait_for"), entry.get("timeout"))
            log.info(output)
            results.append(output)
        return results

    def _read_until(self, expected, timeout=None):
        t = timeout or self.timeout
        data = self.connection.read_until(expected.encode("ascii"), t)
        decoded = data.decode("ascii", errors="replace")
        if expected not in decoded:
            raise TimeoutError(f"Timed out waiting for '{expected}'. Got:\n{decoded}")
        return decoded

    def _read_until_pattern(self, pattern, timeout=None):
        t = timeout or self.timeout
        compiled = re.compile(pattern.encode("ascii"))
        _, match, data = self.connection.expect([compiled], t)
        decoded = data.decode("ascii", errors="replace")
        if match is None:
            raise TimeoutError(f"Timed out waiting for pattern '{pattern}'. Got:\n{decoded}")
        return decoded

    @staticmethod
    def _last_lines(text, n=10):
        lines = text.strip().splitlines()
        if len(lines) <= n:
            return text.strip()
        return "\n".join(["...(truncated)"] + lines[-n:])

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


def _expand_env_vars(text):
    """Replace ${VAR} placeholders with environment variable values."""
    def _replace(match):
        var = match.group(1)
        value = os.environ.get(var)
        if value is None:
            raise ValueError(f"Environment variable '{var}' is not set")
        return value
    return re.sub(r"\$\{(\w+)\}", _replace, text)


def load_config(commands_path, targets_path, component):
    with open(targets_path, "r") as f:
        targets_def = json.load(f)["targets"]
    with open(commands_path, "r") as f:
        components = json.load(f)["components"]
    if component not in components:
        available = ", ".join(components.keys())
        raise ValueError(f"Component '{component}' not found. Available: {available}")
    raw = components[component]

    # Expand entries where "target" is a list into one entry per target
    expanded = []
    for entry in raw["targets"]:
        targets = entry["target"]
        if isinstance(targets, list):
            for t in targets:
                expanded.append({"target": t, "commands": list(entry["commands"])})
        else:
            expanded.append(entry)
    raw["targets"] = expanded

    # Expand environment variables in command strings
    for entry in raw["targets"]:
        for cmd_entry in entry["commands"]:
            cmd_entry["cmd"] = _expand_env_vars(cmd_entry["cmd"])

    return targets_def, raw


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <commands.json> <targets.json> <component> [target1 target2 ...]")
        sys.exit(1)

    commands_path = sys.argv[1]
    targets_path = sys.argv[2]
    component = sys.argv[3]
    selected_targets = sys.argv[4:] if len(sys.argv) > 4 else None

    targets_def, commands_def = load_config(commands_path, targets_path, component)
    log.info(f"Running component: {component}")

    # Track currently reserved targets for cleanup on signal
    reserved_targets = set()

    def _cleanup_and_exit(signum, frame):
        sig_name = signal.Signals(signum).name
        log.warning(f"Received {sig_name}, cleaning up...")
        for t in list(reserved_targets):
            log.info(f"Unreserving target {t} due to {sig_name}...")
            try:
                VxWorksTelnet.unreserve_target(t)
            except Exception:
                pass
        os._exit(1)

    signal.signal(signal.SIGINT, _cleanup_and_exit)
    signal.signal(signal.SIGTERM, _cleanup_and_exit)

    # Collect entries to process, filtering by selected targets
    entries_to_process = []
    for entry in commands_def["targets"]:
        name = entry["target"]
        if selected_targets and name not in selected_targets:
            continue
        if name not in targets_def:
            log.error(f"Target '{name}' not found in {targets_path}")
            continue
        entries_to_process.append(entry)

    # Group entries by backplane (dict preserves insertion order in Python 3.7+)
    backplane_groups = {}
    for entry in entries_to_process:
        name = entry["target"]
        backplane = targets_def[name].get("backplane", name)
        if backplane not in backplane_groups:
            backplane_groups[backplane] = []
        backplane_groups[backplane].append(entry)

    # Process each backplane group: reserve once, run all targets, unreserve once
    for backplane, entries in backplane_groups.items():
        reserve_target_name = entries[0]["target"]
        try:
            VxWorksTelnet.reserve_target(reserve_target_name)
            reserved_targets.add(reserve_target_name)
            VxWorksTelnet.set_reserve_note(reserve_target_name)

            for entry in entries:
                name = entry["target"]
                target = targets_def[name]

                log.info(f"{'='*50}")
                log.info(f"Target: {name} ({target['host']})")
                log.info(f"{'='*50}")

                init_opts = {k: target[k] for k in ("timeout", "provisioner_prompt") if k in target}
                vx = VxWorksTelnet(host=target["host"], port=target["port"], **init_opts)

                # Reboot first (local command, no telnet needed)
                reboot_cmd = REBOOT_CMD_TEMPLATE.format(target=name)
                VxWorksTelnet.reboot_target(reboot_cmd)

                # Now connect — telnet server may take time to come back after reboot
                vx.connect()
                try:
                    vx.reboot_and_boot()
                    vx.run_commands(entry["commands"])
                    log.info(f"=== {name}: All commands completed ===")
                finally:
                    vx.disconnect()
                    time.sleep(5)

            VxWorksTelnet.unreserve_target(reserve_target_name)
            reserved_targets.discard(reserve_target_name)

        except (RuntimeError, TimeoutError, ConnectionError, OSError) as e:
            log.error(f"FAILED on backplane {backplane}: {e}")
            log.info(f"Unreserving target {reserve_target_name} (backplane {backplane}) due to failure...")
            try:
                VxWorksTelnet.unreserve_target(reserve_target_name)
                reserved_targets.discard(reserve_target_name)
            except Exception:
                pass
            os._exit(1)

    os._exit(0)
