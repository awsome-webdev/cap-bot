import time
import json
import sys
import os
import shutil
import glob
import tempfile
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
import undetected_chromedriver as uc
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from pynput.keyboard import Key, Controller

keyboard = Controller()

def sanitize_worker_id(worker_id):
    """Sanitize worker_id string for filesystem safety."""
    return "".join(c for c in str(worker_id) if c.isalnum() or c in ("-", "_", ":")).replace(":", "_")

def clean_worker_temp_files(worker_id):
    """Purge temporary Chrome profiles and python temp folders to prevent Errno 28."""
    clean_id = sanitize_worker_id(worker_id)
    profile_dir = f"/tmp/uc_profile_{clean_id}"
    tmp_dir = f"/tmp/uc_tmp_{clean_id}"

    for path in (profile_dir, tmp_dir):
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

    # Clean leftover /tmp/tmp* folders older than 2 minutes
    try:
        now = time.time()
        for path in glob.glob("/tmp/tmp*"):
            if os.path.isdir(path) and (now - os.path.getmtime(path) > 120):
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

def wait_for_console_log(driver, phrase: str, poll_interval: float = 0.5, timeout: float = None):
    start_time = time.time()
    consecutive_errors = 0
    while True:
        if timeout is not None and (time.time() - start_time) > timeout:
            return 'timeout'
            
        try:
            logs = driver.get_log('browser')
            consecutive_errors = 0
            for entry in logs:
                if phrase in entry.get('message', ''):
                    return None
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors > 3:
                raise e
            time.sleep(1)
            continue
                
        time.sleep(poll_interval)

def create_driver(worker_id):
    clean_id = sanitize_worker_id(worker_id)
    
    # Stagger startup slightly to prevent ChromeDriver patching race condition across simultaneous workers
    time.sleep(random.uniform(0.5, 2.5))
    
    clean_worker_temp_files(worker_id)
    
    profile_dir = f"/tmp/uc_profile_{clean_id}"
    worker_tmp_dir = f"/tmp/uc_tmp_{clean_id}"
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(worker_tmp_dir, exist_ok=True)

    # Set Python tempfile directory to this worker's isolated directory
    tempfile.tempdir = worker_tmp_dir

    options = uc.ChromeOptions()
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    options.add_argument(f"--user-data-dir={profile_dir}")
    
    # Background & Performance Flags
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    
    # Memory & Disk optimization flags to prevent Errno 28 (No space left on device)
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-in-process-stack-traces")
    options.add_argument("--log-level=3")
    options.add_argument("--disk-cache-size=1048576")
    options.add_argument("--media-cache-size=1048576")

    driver = uc.Chrome(
        options=options,
        version_main=152,
        user_data_dir=profile_dir
    )

    # Spoof Navigator & JS Leaks
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'mimeTypes', { get: () => [1, 2, 3, 4] });
            
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
            
            window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            
            Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
            Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
        """
    })

    # Spoof Visibility and Focus
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
            Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
            Document.prototype.hasFocus = function() { return true; };

            window.addEventListener('blur', (e) => { e.stopImmediatePropagation(); }, true);
            window.addEventListener('visibilitychange', (e) => { e.stopImmediatePropagation(); }, true);
            window.dispatchEvent(new Event('focus'));
        """
    })

    return driver

if __name__ == '__main__':
    worker_id = sys.argv[1] if len(sys.argv) > 1 else "standalone"
    count = 0
    fails = 0

    driver = None

    def solve(driver_instance):
        global count, fails
        
        # Ensure window stays off-screen even if driver.get tries to pull focus
        driver_instance.set_window_position(20000, 0)
        
        driver_instance.get('https://botme.idk.dunkirk.sh/captchas/cap-default?name=awsome-webdev')
        print('Loaded page', flush=True)
        time.sleep(2)
        
        ele = driver_instance.find_element(By.ID, 'cap')
        ele.click()
        print('clicked element', flush=True)
        
        wa = wait_for_console_log(driver_instance, "actually a good", timeout=10)
        if wa is None:
            count += 1
            print(f'Success! Solved {count} Captchas', flush=True)
        else:
            fails += 1
            print(f'Timeout ({fails} total fails)', flush=True)

    try:
        while True:
            try:
                if driver is None:
                    print('Initializing Chrome browser...', flush=True)
                    driver = create_driver(worker_id)

                solve(driver)

            except (WebDriverException, Exception) as e:
                fails += 1
                print(f"Browser connection error ({type(e).__name__}): {e}", flush=True)
                print("Cleaning up dead Chrome instance and re-initializing in 3s...", flush=True)
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
                clean_worker_temp_files(worker_id)
                time.sleep(3)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        clean_worker_temp_files(worker_id)
