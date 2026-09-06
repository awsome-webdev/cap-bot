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
        self.worker_counter = 0
        self.logs = deque(maxlen=200)
        self.lock = threading.Lock()
        
        # Historical rate metrics tracking (for Chart.js)
        self.history_timestamps = deque(maxlen=30)
        self.history_rates = deque(maxlen=30)
        
        # Start background monitor thread for aggregate rate history
        self.monitor_thread = threading.Thread(target=self._monitor_history, daemon=True)
        self.monitor_thread.start()

    def log(self, message):
        with self.lock:
            self.logs.append(message)
            print(message)

    def spawn_worker(self):
        with self.lock:
            self.worker_counter += 1
            worker_id = f"Worker-{self.worker_counter}"
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
        """Removes and kills a worker asynchronously without blocking API threads or holding locks."""
        with self.lock:
            worker = self.workers.pop(worker_id, None)
        
        if worker:
            threading.Thread(target=worker.stop, daemon=True).start()
            self.log(f"[SYSTEM] Terminated worker {worker_id}")
            return True
        return False

    def restart_worker(self, worker_id):
        """Triggers process restart asynchronously without holding locks."""
        with self.lock:
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
        """Stops all active worker processes and cleans up process trees."""
        with self.lock:
            workers_to_stop = list(self.workers.values())
            self.workers.clear()

        def _do_stop_all():
            threads = [threading.Thread(target=w.stop, daemon=True) for w in workers_to_stop]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.log("[SYSTEM] Stopped all workers.")

        if sync:
            _do_stop_all()
        else:
            threading.Thread(target=_do_stop_all, daemon=True).start()
        return True

    def _monitor_history(self):
        """Periodically samples cluster performance for charts."""
        while True:
            time.sleep(3)
            now_str = datetime.now().strftime("%H:%M:%S")
            overall_rpm = self.get_overall_rate_per_min()
            
            with self.lock:
                self.history_timestamps.append(now_str)
                self.history_rates.append(round(overall_rpm, 2))

    def get_overall_rate_per_min(self):
        with self.lock:
            return sum(w.get_rate_per_min() for w in self.workers.values())

    def get_dashboard_data(self):
        with self.lock:
            total_solved = sum(w.solves for w in self.workers.values())
            total_fails = sum(w.fails for w in self.workers.values())
            total_restarts = sum(w.restarts for w in self.workers.values())
            active_count = sum(1 for w in self.workers.values() if w.status == "RUNNING")
            
            # Calculate solved in last minute
            now = time.time()
            one_min_ago = now - 60.0
            solved_last_minute = sum(
                sum(1 for ts in w.solve_timestamps if ts >= one_min_ago)
                for w in self.workers.values()
            )

            overall_rate_min = sum(w.get_rate_per_min() for w in self.workers.values())
            overall_rate_sec = overall_rate_min / 60.0

            total_attempts = total_solved + total_fails
            fail_rate_pct = (total_fails / total_attempts * 100.0) if total_attempts > 0 else 0.0

            # Worker details
            worker_list = []
            for w_id, w in list(self.workers.items()):
                w_attempts = w.solves + w.fails
                w_success_rate = (w.solves / w_attempts * 100.0) if w_attempts > 0 else 100.0
                uptime_sec = w.get_uptime_seconds()
                uptime_formatted = str(timedelta(seconds=uptime_sec)) if uptime_sec > 0 else "0:00:00"

                worker_list.append({
                    "id": w.worker_id,
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

            return {
                "total_solved": total_solved,
                "total_fails": total_fails,
                "total_restarts": total_restarts,
                "solved_last_minute": solved_last_minute,
                "overall_rate_per_min": round(overall_rate_min, 1),
                "overall_rate_per_sec": round(overall_rate_sec, 2),
                "fail_rate_percent": round(fail_rate_pct, 1),
                "active_workers_count": active_count,
                "total_workers_count": len(self.workers),
                "workers": worker_list,
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
    w_id = manager.spawn_worker()
    return jsonify({"success": True, "worker_id": w_id, "message": f"Spawned {w_id}"})

@app.route("/api/workers/spawn_bulk", methods=["POST"])
def api_spawn_bulk():
    data = request.json or {}
    count = int(data.get("count", 1))
    spawned = manager.spawn_bulk(count)
    return jsonify({"success": True, "spawned": spawned, "message": f"Spawned {len(spawned)} workers"})

@app.route("/api/workers/<worker_id>/kill", methods=["POST"])
def api_kill_worker(worker_id):
    success = manager.kill_worker(worker_id)
    return jsonify({"success": success, "message": f"Killed {worker_id}" if success else "Worker not found"})

@app.route("/api/workers/<worker_id>/restart", methods=["POST"])
def api_restart_worker(worker_id):
    success = manager.restart_worker(worker_id)
    return jsonify({"success": success, "message": f"Restarting {worker_id}" if success else "Worker not found"})

@app.route("/api/workers/stop_all", methods=["POST"])
def api_stop_all():
    manager.stop_all(sync=False)
    return jsonify({"success": True, "message": "All workers stopped"})

if __name__ == "__main__":
    print("=" * 65)
    print("      CAPTCHA BOTMASTER ORCHESTRATOR & MONITOR CONTROL CENTER")
    print("=" * 65)
    print("Starting Web Interface at: http://localhost:5000")
    print("Press Ctrl+C to terminate manager and all child subprocesses.")
    print("=" * 65)

    # Spawn 1 initial worker process on startup
    manager.spawn_worker()

    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        # Cleanup workers on shutdown
        manager.stop_all(sync=True)
