import os
import sys
import time
import subprocess
import threading
import readline
import platform
import socket
import json
import urllib.request
import urllib.error
import psutil
from datetime import datetime

# Default Configurations
MASTER_URL = os.environ.get("MASTER_URL", "http://localhost:5000")
SECRET_KEY = os.environ.get("SECRET_KEY", "botmaster-secret")
NODE_ID = os.environ.get("NODE_ID", f"Node-{socket.gethostname()}-{os.getpid()}")
HEARTBEAT_INTERVAL_SEC = 1.5
TARGET_SCRIPT = os.environ.get("TARGET_SCRIPT", "target_script.py")

# In-memory storage for local managed workers and logs
active_workers = {}
logs_buffer = []
logs_lock = threading.Lock()
local_worker_counter = 0

def log_system(msg):
    time_str = datetime.now().strftime("%H:%M:%S")
    entry = f"[{time_str}] [{NODE_ID}] {msg}"
    print(entry, flush=True)
    with logs_lock:
        logs_buffer.append(entry)
        if len(logs_buffer) > 100:
            logs_buffer.pop(0)

class NodeWorkerProcess:
    def __init__(self, local_id):
        self.local_id = local_id
        self.full_worker_id = f"{NODE_ID}:{local_id}"
        self.process = None
        self.thread = None
        self.pid = None
        self.status = "INITIALIZING"
        self.solves = 0
        self.fails = 0
        self.timeouts = 0
        self.restarts = 0
        self.solve_timestamps = []
        self.fail_timestamps = []
        self.timeout_timestamps = []
        self.should_run = True
        self.start_time = time.time()

    def start(self):
        self.should_run = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        while self.should_run:
            self.start_time = time.time()
            self.status = "RUNNING"
            log_system(f"[WORKER] Starting Python subprocess for {self.full_worker_id}...")

            try:
                # Spawn target script in unbuffered mode (-u)
                cmd = [sys.executable, "-u", TARGET_SCRIPT, self.full_worker_id]
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                self.pid = self.process.pid

                # Read output line by line
                for line in iter(self.process.stdout.readline, ''):
                    if not line:
                        break
                    clean_line = line.strip()
                    if clean_line:
                        self._parse_line(clean_line)

                self.process.wait()
            except Exception as e:
                log_system(f"[ERROR] Subprocess error in {self.full_worker_id}: {str(e)}")

            self.pid = None
            if not self.should_run:
                self.status = "STOPPED"
                log_system(f"[WORKER] {self.full_worker_id} stopped permanently.")
                break

            self.restarts += 1
            self.status = "CRASHED/RESTARTING"
            log_system(f"[WARNING] {self.full_worker_id} exited! Auto-restarting in 2s (Restart #{self.restarts})...")
            time.sleep(2)

    def _parse_line(self, line):
        time_str = datetime.now().strftime("%H:%M:%S")
        entry = f"[{time_str}] [{self.full_worker_id}] {line}"
        print(entry, flush=True)
        with logs_lock:
            logs_buffer.append(entry)
            if len(logs_buffer) > 100:
                logs_buffer.pop(0)

        if "Success!" in line or "Solved" in line:
            self.solves += 1
            self.solve_timestamps.append(time.time())
        elif "Fail!" in line or "womp womp" in line:
            self.fails += 1
            self.fail_timestamps.append(time.time())
        elif "Timeout" in line:
            self.timeouts += 1
            self.timeout_timestamps.append(time.time())
        elif "error" in line.lower() or "fail" in line.lower() or "exception" in line.lower():
            self.fails += 1
            self.fail_timestamps.append(time.time())

    def get_rate_per_min(self):
        # 5-minute rolling average solves per minute
        now = time.time()
        five_min_ago = now - 300.0
        self.solve_timestamps = [ts for ts in self.solve_timestamps if ts >= five_min_ago]
        uptime = self.get_uptime_seconds()
        window_minutes = min(max(uptime, 10.0), 300.0) / 60.0
        return float(len(self.solve_timestamps) / window_minutes)

    def get_fail_rate_per_min(self):
        # 5-minute rolling average fails per minute
        now = time.time()
        five_min_ago = now - 300.0
        self.fail_timestamps = [ts for ts in self.fail_timestamps if ts >= five_min_ago]
        uptime = self.get_uptime_seconds()
        window_minutes = min(max(uptime, 10.0), 300.0) / 60.0
        return float(len(self.fail_timestamps) / window_minutes)

    def get_timeout_rate_per_min(self):
        # 5-minute rolling average timeouts per minute
        now = time.time()
        five_min_ago = now - 300.0
        self.timeout_timestamps = [ts for ts in self.timeout_timestamps if ts >= five_min_ago]
        uptime = self.get_uptime_seconds()
        window_minutes = min(max(uptime, 10.0), 300.0) / 60.0
        return float(len(self.timeout_timestamps) / window_minutes)

    def get_uptime_seconds(self):
        if self.status == "RUNNING":
            return int(time.time() - self.start_time)
        return 0

    def format_uptime(self):
        sec = self.get_uptime_seconds()
        hours = sec // 3600
        minutes = (sec % 3600) // 60
        seconds = sec % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    def stop(self):
        self.should_run = False
        self.status = "STOPPING"
        if self.process and self.process.poll() is None:
            try:
                parent = psutil.Process(self.process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, Exception):
                        pass
                parent.kill()
            except (psutil.NoSuchProcess, Exception):
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.status = "STOPPED"

def spawn_local_worker():
    global local_worker_counter
    local_worker_counter += 1
    local_id = f"Worker-{local_worker_counter}"
    worker = NodeWorkerProcess(local_id)
    active_workers[worker.full_worker_id] = worker
    worker.start()
    return worker.full_worker_id

def kill_local_worker(full_worker_id):
    if full_worker_id in active_workers:
        worker = active_workers.pop(full_worker_id)
        worker.stop()
        log_system(f"Terminated local worker {full_worker_id}")
        return True
    return False

def restart_local_worker(full_worker_id):
    if full_worker_id in active_workers:
        worker = active_workers[full_worker_id]
        if worker.process and worker.process.poll() is None:
            try:
                parent = psutil.Process(worker.process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                parent.kill()
            except Exception:
                try:
                    worker.process.kill()
                except Exception:
                    pass
        log_system(f"Triggered restart for local worker {full_worker_id}")
        return True
    return False

def stop_all_local_workers():
    workers = list(active_workers.values())
    active_workers.clear()
    for w in workers:
        w.stop()
    log_system("Stopped all local workers.")

def send_heartbeat():
    global logs_buffer, MASTER_URL
    worker_list = []
    node_solves = 0
    node_fails = 0
    node_timeouts = 0
    node_restarts = 0

    for w_id, w in list(active_workers.items()):
        node_solves += w.solves
        node_fails += w.fails
        node_timeouts += getattr(w, 'timeouts', 0)
        node_restarts += w.restarts
        attempts = w.solves + w.fails + getattr(w, 'timeouts', 0)
        success_rate = (w.solves / attempts * 100.0) if attempts > 0 else 100.0

        worker_list.append({
            "id": w.full_worker_id,
            "pid": w.pid,
            "status": w.status,
            "solves": w.solves,
            "fails": w.fails,
            "timeouts": getattr(w, 'timeouts', 0),
            "restarts": w.restarts,
            "rate_per_min": w.get_rate_per_min(),
            "fail_rate_per_min": w.get_fail_rate_per_min(),
            "timeout_rate_per_min": w.get_timeout_rate_per_min(),
            "success_rate": round(success_rate, 1),
            "uptime_seconds": w.get_uptime_seconds(),
            "uptime_formatted": w.format_uptime()
        })

    with logs_lock:
        outgoing_logs = list(logs_buffer)
        logs_buffer.clear()

    payload = {
        "node_id": NODE_ID,
        "secret_key": SECRET_KEY,
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "cpu_count": os.cpu_count() or 1,
        "active_workers_count": sum(1 for w in active_workers.values() if w.status == "RUNNING"),
        "total_solved": node_solves,
        "total_fails": node_fails,
        "total_timeouts": node_timeouts,
        "total_restarts": node_restarts,
        "workers": worker_list,
        "logs": outgoing_logs
    }

    url = f"{MASTER_URL.rstrip('/')}/api/node/heartbeat"
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json", "User-Agent": "BotmasterWorkerNode/3.0"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status == 200:
                resp_body = json.loads(resp.read().decode('utf-8'))
                commands = resp_body.get("commands", [])
                execute_commands(commands)
            else:
                log_system(f"[WARNING] Heartbeat rejected by master (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        # If master redirected HTTP -> HTTPS, automatically upgrade MASTER_URL to https://
        if e.code in (301, 302, 307, 308) and MASTER_URL.startswith("http://"):
            new_location = e.headers.get("Location")
            if new_location and new_location.startswith("https://"):
                log_system(f"[NOTICE] Redirected to HTTPS. Upgrading MASTER_URL to https://...")
                MASTER_URL = "https://" + MASTER_URL[len("http://"):]
            else:
                log_system(f"[ERROR] HTTP Redirect {e.code}: {e.reason}")
        else:
            log_system(f"[ERROR] Master responded with HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        log_system(f"[ERROR] Unable to reach master at {MASTER_URL}: {e.reason}")
    except Exception as e:
        log_system(f"[ERROR] Heartbeat error: {str(e)}")

def execute_commands(commands):
    for cmd in commands:
        action = cmd.get("action")
        target = cmd.get("target")
        log_system(f"[COMMAND RECEIVED] Action: {action} (Target: {target or 'N/A'})")

        if action == "SPAWN":
            count = cmd.get("count", 1)
            for _ in range(count):
                spawn_local_worker()
        elif action == "KILL":
            kill_local_worker(target)
        elif action == "RESTART":
            restart_local_worker(target)
        elif action == "STOP_ALL":
            stop_all_local_workers()

def setup_interactive_config():
    global MASTER_URL, SECRET_KEY

    # If MASTER_URL is explicitly set or if running non-interactively (systemd / no tty), skip prompt
    if "MASTER_URL" in os.environ or not sys.stdin.isatty():
        return

    print("=================================================")
    print("   CAPTCHA BOTMASTER DISTRIBUTED WORKER NODE     ")
    print("=================================================")
    print(f"Node ID    : {NODE_ID}")
    print(f"Default URL: {MASTER_URL}")
    print("=================================================")

    try:
        url_input = input(f"Enter Master Dashboard Domain/URL [default: {MASTER_URL}]: ").strip()
        if url_input:
            if not url_input.startswith("http://") and not url_input.startswith("https://"):
                url_input = f"https://{url_input}"
            MASTER_URL = url_input.rstrip("/")

        key_input = input(f"Enter Secret Key [default: {SECRET_KEY}]: ").strip()
        if key_input:
            SECRET_KEY = key_input
    except (KeyboardInterrupt, EOFError):
        pass

def main():
    setup_interactive_config()
    log_system(f"Connecting Node to Master Control Center at {MASTER_URL}...")

    # Spawn 1 initial worker process
    spawn_local_worker()

    try:
        while True:
            send_heartbeat()
            time.sleep(HEARTBEAT_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nShutting down node workers...")
        stop_all_local_workers()
        sys.exit(0)

if __name__ == "__main__":
    main()
