import os
import sys
import time
import subprocess
import threading
import queue
import re
import psutil
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, render_template, jsonify, request

# Create Flask Application
app = Flask(__name__)

# Configurable Shared Secret Key for Cluster Nodes
SECRET_KEY = os.environ.get("SECRET_KEY", "botmaster-secret")

# Target Python script to manage
TARGET_SCRIPT = "target_script.py"

class WorkerProcess:
    def __init__(self, worker_id, manager):
        self.worker_id = worker_id
        self.manager = manager
        self.process = None
        self.thread = None
        self.pid = None
        self.should_run = True
        self.status = "INITIALIZING"
        self.start_time = None
        
        # Statistics
        self.solves = 0
        self.fails = 0
        self.restarts = 0
        
        # Timestamps of successful solves for rate calculation
        self.solve_timestamps = deque(maxlen=1000)
        self.fail_timestamps = deque(maxlen=1000)

    def start(self):
        self.should_run = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        while self.should_run:
            self.start_time = time.time()
            self.status = "RUNNING"
            self.manager.log(f"[SYSTEM] Starting Worker {self.worker_id}...")

            try:
                # Spawn target script as a subprocess with unbuffered output
                cmd = [sys.executable, "-u", TARGET_SCRIPT, self.worker_id]
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                self.pid = self.process.pid
                self.manager.log(f"[SYSTEM] Worker {self.worker_id} started with PID {self.pid}")

                # Read output line by line
                for line in iter(self.process.stdout.readline, ''):
                    if not line:
                        break
                    clean_line = line.strip()
                    if clean_line:
                        self._parse_output(clean_line)

                self.process.wait()
            except Exception as e:
                self.manager.log(f"[ERROR] Worker {self.worker_id} encountered error: {str(e)}")

            self.pid = None
            if not self.should_run:
                self.status = "STOPPED"
                self.manager.log(f"[SYSTEM] Worker {self.worker_id} stopped permanently.")
                break

            # If process crashed or exited unexpectedly, auto-restart
            self.restarts += 1
            self.status = "CRASHED/RESTARTING"
            self.manager.log(f"[WARNING] Worker {self.worker_id} exited/crashed! Auto-restarting (Restart #{self.restarts})...")
            time.sleep(2) # Brief cooldown before restart

    def _parse_output(self, line):
        # Format log line with worker prefix
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp_str}] [{self.worker_id}] {line}"
        self.manager.log(log_entry)

        # Regex match success output: "Success! Solved X Captchas"
        if "Success!" in line or "Solved" in line:
            self.solves += 1
            self.solve_timestamps.append(time.time())
        # Regex match fail output: "Timeout (X total fails)"
        elif "Timeout" in line or "fail" in line.lower():
            self.fails += 1
            self.fail_timestamps.append(time.time())

    def get_rate_per_min(self):
        """Calculates solves per minute based on solves in the last 60 seconds."""
        now = time.time()
        one_min_ago = now - 60.0
        recent_solves = sum(1 for ts in self.solve_timestamps if ts >= one_min_ago)
        return float(recent_solves)

    def get_uptime_seconds(self):
        if self.start_time and self.status == "RUNNING":
            return int(time.time() - self.start_time)
        return 0

    def stop(self):
        """Stops the worker process and aggressively kills child process trees (Chrome/ChromeDriver)."""
        self.should_run = False
        self.status = "STOPPING"
        if self.process and self.process.poll() is None:
            try:
                parent = psutil.Process(self.process.pid)
                # Kill child processes (Chrome, Chromedriver) first
                children = parent.children(recursive=True)
                for child in children:
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

class OrchestratorManager:
    def __init__(self):
        self.workers = {}
        self.remote_nodes = {}
        self.node_command_queues = {}
        self.worker_counter = 0
        self.logs = deque(maxlen=200)
        # CRITICAL FIX: Use RLock (Reentrant Lock) to prevent deadlocks when calling lock-protected methods internally
        self.lock = threading.RLock()
        
        # Historical rate metrics tracking (for Chart.js)
        self.history_timestamps = deque(maxlen=30)
        self.history_rates = deque(maxlen=30)
        
        # Start background monitor thread for aggregate rate history and node timeouts
        self.monitor_thread = threading.Thread(target=self._monitor_history, daemon=True)
        self.monitor_thread.start()

    def log(self, message):
        with self.lock:
            self.logs.append(message)
            print(message)

    def spawn_worker(self):
        with self.lock:
            self.worker_counter += 1
            worker_id = f"Local-Worker-{self.worker_counter}"
            worker = WorkerProcess(worker_id, self)
            self.workers[worker_id] = worker
            worker.start()
            return worker_id

    def spawn_bulk(self, count):
        spawned = []
        for _ in range(count):
            spawned.append(self.spawn_worker())
        return spawned

    def kill_worker(self, worker_id):
        """Removes and kills a local or remote worker."""
        with self.lock:
            # Check if it's a remote worker (e.g. Node-XXX:Worker-1)
            if ":" in worker_id:
                node_id = worker_id.split(":")[0]
                if node_id in self.node_command_queues:
                    self.node_command_queues[node_id].append({"action": "KILL", "target": worker_id})
                    self.log(f"[SYSTEM] Queued KILL command for remote worker {worker_id} on {node_id}")
                    return True

            worker = self.workers.pop(worker_id, None)
        
        if worker:
            threading.Thread(target=worker.stop, daemon=True).start()
            self.log(f"[SYSTEM] Terminated worker {worker_id}")
            return True
        return False

    def restart_worker(self, worker_id):
        """Triggers worker restart locally or queues command for remote node."""
        with self.lock:
            if ":" in worker_id:
                node_id = worker_id.split(":")[0]
                if node_id in self.node_command_queues:
                    self.node_command_queues[node_id].append({"action": "RESTART", "target": worker_id})
                    self.log(f"[SYSTEM] Queued RESTART command for remote worker {worker_id} on {node_id}")
                    return True

            worker = self.workers.get(worker_id)
        
        if worker:
            def _restart_proc():
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
                self.log(f"[SYSTEM] Manually triggered restart for {worker_id}")

            threading.Thread(target=_restart_proc, daemon=True).start()
            return True
        return False

    def stop_all(self, sync=False):
        """Stops all active local and remote worker processes."""
        with self.lock:
            workers_to_stop = list(self.workers.values())
            self.workers.clear()

            # Broadcast STOP_ALL to all registered remote nodes
            for node_id, cmd_queue in self.node_command_queues.items():
                cmd_queue.append({"action": "STOP_ALL"})

        def _do_stop_all():
            threads = [threading.Thread(target=w.stop, daemon=True) for w in workers_to_stop]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.log("[SYSTEM] Stopped all local & remote workers.")

        if sync:
            _do_stop_all()
        else:
            threading.Thread(target=_do_stop_all, daemon=True).start()
        return True

    def process_node_heartbeat(self, node_data):
        node_id = node_data.get("node_id")
        provided_secret = node_data.get("secret_key")

        if provided_secret != SECRET_KEY:
            return {"success": False, "error": "Invalid secret key"}, 403

        with self.lock:
            # Register or update remote node status
            self.remote_nodes[node_id] = {
                "last_seen": time.time(),
                "hostname": node_data.get("hostname", "Unknown"),
                "platform": node_data.get("platform", "Unknown"),
                "cpu_count": node_data.get("cpu_count", 0),
                "active_workers_count": node_data.get("active_workers_count", 0),
                "total_solved": node_data.get("total_solved", 0),
                "total_fails": node_data.get("total_fails", 0),
                "total_restarts": node_data.get("total_restarts", 0),
                "workers": node_data.get("workers", [])
            }

            if node_id not in self.node_command_queues:
                self.node_command_queues[node_id] = []

            # Append logs forwarded by node
            node_logs = node_data.get("logs", [])
            for l in node_logs:
                self.logs.append(l)

            # Retrieve queued commands for this node
            commands = list(self.node_command_queues[node_id])
            self.node_command_queues[node_id].clear()

        return {"success": True, "commands": commands}, 200

    def spawn_node_worker(self, node_id, count=1):
        with self.lock:
            if node_id in self.node_command_queues:
                self.node_command_queues[node_id].append({"action": "SPAWN", "count": count})
                self.log(f"[SYSTEM] Queued SPAWN ({count}) command for Node {node_id}")
                return True
            return False

    def _monitor_history(self):
        """Periodically samples cluster performance and removes timed-out nodes."""
        while True:
            time.sleep(3)
            now_str = datetime.now().strftime("%H:%M:%S")
            overall_rpm = self.get_overall_rate_per_min()
            
            with self.lock:
                self.history_timestamps.append(now_str)
                self.history_rates.append(round(overall_rpm, 2))

                # Prune nodes inactive for > 10 seconds
                now = time.time()
                dead_nodes = [nid for nid, nd in list(self.remote_nodes.items()) if now - nd["last_seen"] > 10]
                for d_node in dead_nodes:
                    del self.remote_nodes[d_node]
                    if d_node in self.node_command_queues:
                        del self.node_command_queues[d_node]
                    self.log(f"[WARNING] Node {d_node} timed out (disconnected).")

    def get_overall_rate_per_min(self):
        with self.lock:
            local_rpm = sum(w.get_rate_per_min() for w in list(self.workers.values()))
            remote_rpm = sum(
                sum(w.get("rate_per_min", 0) for w in node.get("workers", []))
                for node in list(self.remote_nodes.values())
            )
            return local_rpm + remote_rpm

    def get_dashboard_data(self):
        with self.lock:
            local_solved = sum(w.solves for w in list(self.workers.values()))
            local_fails = sum(w.fails for w in list(self.workers.values()))
            local_restarts = sum(w.restarts for w in list(self.workers.values()))
            local_active = sum(1 for w in list(self.workers.values()) if w.status == "RUNNING")

            remote_solved = sum(nd["total_solved"] for nd in list(self.remote_nodes.values()))
            remote_fails = sum(nd["total_fails"] for nd in list(self.remote_nodes.values()))
            remote_restarts = sum(nd["total_restarts"] for nd in list(self.remote_nodes.values()))
            remote_active = sum(nd["active_workers_count"] for nd in list(self.remote_nodes.values()))

            total_solved = local_solved + remote_solved
            total_fails = local_fails + remote_fails
            total_restarts = local_restarts + remote_restarts
            active_count = local_active + remote_active

            # Solved last minute across local + remote
            now = time.time()
            one_min_ago = now - 60.0
            solved_last_minute = sum(
                sum(1 for ts in w.solve_timestamps if ts >= one_min_ago)
                for w in list(self.workers.values())
            ) + sum(
                sum(w.get("rate_per_min", 0) for w in node.get("workers", []))
                for node in list(self.remote_nodes.values())
            )

            overall_rate_min = self.get_overall_rate_per_min()
            overall_rate_sec = overall_rate_min / 60.0

            total_attempts = total_solved + total_fails
            fail_rate_pct = (total_fails / total_attempts * 100.0) if total_attempts > 0 else 0.0

            # Aggregated Worker details (Local + Remote)
            combined_workers = []
            for w_id, w in list(self.workers.items()):
                w_attempts = w.solves + w.fails
                w_success_rate = (w.solves / w_attempts * 100.0) if w_attempts > 0 else 100.0
                uptime_sec = w.get_uptime_seconds()
                uptime_formatted = str(timedelta(seconds=uptime_sec)) if uptime_sec > 0 else "0:00:00"

                combined_workers.append({
                    "id": w.worker_id,
                    "node_id": "Master (Local)",
                    "pid": w.pid,
                    "status": w.status,
                    "solves": w.solves,
                    "fails": w.fails,
                    "restarts": w.restarts,
                    "rate_per_min": w.get_rate_per_min(),
                    "success_rate": round(w_success_rate, 1),
                    "uptime_seconds": uptime_sec,
                    "uptime_formatted": uptime_formatted
                })

            for node_id, node in list(self.remote_nodes.items()):
                for rw in node.get("workers", []):
                    rw_copy = dict(rw)
                    rw_copy["node_id"] = node_id
                    combined_workers.append(rw_copy)

            # Remote Node summaries
            nodes_summary = [
                {
                    "node_id": nid,
                    "hostname": nd["hostname"],
                    "platform": nd["platform"],
                    "cpu_count": nd["cpu_count"],
                    "active_workers": nd["active_workers_count"],
                    "solves": nd["total_solved"],
                    "fails": nd["total_fails"],
                    "last_seen": round(now - nd["last_seen"], 1)
                }
                for nid, nd in list(self.remote_nodes.items())
            ]

            return {
                "total_solved": total_solved,
                "total_fails": total_fails,
                "total_restarts": total_restarts,
                "solved_last_minute": solved_last_minute,
                "overall_rate_per_min": round(overall_rate_min, 1),
                "overall_rate_per_sec": round(overall_rate_sec, 2),
                "fail_rate_percent": round(fail_rate_pct, 1),
                "active_workers_count": active_count,
                "total_workers_count": len(combined_workers),
                "workers": combined_workers,
                "nodes": nodes_summary,
                "history_labels": list(self.history_timestamps),
                "history_rates": list(self.history_rates),
                "logs": list(self.logs)
            }

# Instantiate Orchestrator Manager
manager = OrchestratorManager()

# Web Routes
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(manager.get_dashboard_data())

@app.route("/api/workers/spawn", methods=["POST"])
def api_spawn_worker():
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")

    if node_id and node_id != "Master (Local)":
        success = manager.spawn_node_worker(node_id, count=1)
        return jsonify({"success": success, "message": f"Queued spawn on {node_id}" if success else "Node not found"})
    else:
        w_id = manager.spawn_worker()
        return jsonify({"success": True, "worker_id": w_id, "message": f"Spawned {w_id} on Master"})

@app.route("/api/workers/spawn_bulk", methods=["POST"])
def api_spawn_bulk():
    data = request.get_json(silent=True) or {}
    count = int(data.get("count", 1))
    node_id = data.get("node_id")

    if node_id and node_id != "Master (Local)":
        success = manager.spawn_node_worker(node_id, count=count)
        return jsonify({"success": success, "message": f"Queued {count} spawns on {node_id}" if success else "Node not found"})
    else:
        spawned = manager.spawn_bulk(count)
        return jsonify({"success": True, "spawned": spawned, "message": f"Spawned {len(spawned)} workers on Master"})

@app.route("/api/workers/<path:worker_id>/kill", methods=["POST"])
def api_kill_worker(worker_id):
    success = manager.kill_worker(worker_id)
    return jsonify({"success": success, "message": f"Killed worker {worker_id}" if success else "Worker not found"})

@app.route("/api/workers/<path:worker_id>/restart", methods=["POST"])
def api_restart_worker(worker_id):
    success = manager.restart_worker(worker_id)
    return jsonify({"success": success, "message": f"Restarting worker {worker_id}" if success else "Worker not found"})

@app.route("/api/workers/stop_all", methods=["POST"])
def api_stop_all():
    manager.stop_all(sync=False)
    return jsonify({"success": True, "message": "All local and remote workers stopped"})

# Remote Node Heartbeat API Endpoint
@app.route("/api/node/heartbeat", methods=["POST"])
def api_node_heartbeat():
    data = request.get_json(silent=True) or {}
    res, status_code = manager.process_node_heartbeat(data)
    return jsonify(res), status_code

if __name__ == "__main__":
    print("=" * 65)
    print("      CAPTCHA BOTMASTER ORCHESTRATOR & MONITOR CONTROL CENTER")
    print("=" * 65)
    print("Starting Web Interface at: http://0.0.0.0:5000")
    print("Press Ctrl+C to terminate manager and all child subprocesses.")
    print("=" * 65)

    # Spawn 1 initial worker process locally on startup
    manager.spawn_worker()

    try:
        # ENABLE THREADED MODE TO PREVENT HTTP LOCKUPS
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
    finally:
        # Cleanup workers on shutdown
        manager.stop_all(sync=True)
