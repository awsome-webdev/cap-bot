import subprocess
import sys
import time

class ProcessManager:
    def __init__(self):
        self.processes = {}

    def start_script(self, name, script_path, *args):
        """Starts a script in the background and keeps track of it."""
        cmd = [sys.executable, "-u", script_path, *args]
        
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.processes[name] = proc
        print(f"Started '{name}' (PID: {proc.pid})")

    def check_status(self):
        """Checks which processes are still running."""
        for name, proc in self.processes.items():
            poll = proc.poll()
            if poll is None:
                print(f"'{name}' is still running.")
            else:
                print(f"'{name}' finished with exit code {poll}.")

    def stop_script(self, name):
        """Terminates a specific process."""
        if name in self.processes:
            proc = self.processes[name]
            if proc.poll() is None:  # Still running
                proc.terminate()    # Graceful stop (SIGTERM)
                # proc.kill()       # Forceful stop (SIGKILL) if needed
                print(f"Stopped '{name}'.")

    def get_output(self, name):
        """Reads remaining stdout and stderr from a completed or running process."""
        if name in self.processes:
            proc = self.processes[name]
            stdout, stderr = proc.communicate() # Blocks until finished
            return stdout, stderr
        return None, None

if __name__ == "__main__":
    manager = ProcessManager()
    
    # Start processes
    manager.start_script("worker1", "child_long.py")
    
    # Do work in the manager script
    time.sleep(1.5)
    manager.check_status()
    
    # Retrieve final output
    stdout, stderr = manager.get_output("worker1")
    print("\n--- Final Output ---")
    print(stdout)
