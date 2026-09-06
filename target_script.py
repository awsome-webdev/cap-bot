import time
import json
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
import undetected_chromedriver as uc
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import sys
from pynput.keyboard import Key, Controller

keyboard = Controller()

def wait_for_console_log(driver, phrase: str, poll_interval: float = 0.5, timeout: float = None):
    start_time = time.time()
    while True:
        if timeout is not None and (time.time() - start_time) > timeout:
            return 'timeout'
            
        logs = driver.get_log('browser')
        for entry in logs:
            if phrase in entry.get('message', ''):
                return
                
        time.sleep(poll_interval)

if __name__ == '__main__':
    # Optional worker ID from command line arguments for process identification
    worker_id = sys.argv[1] if len(sys.argv) > 1 else "standalone"
    
    options = uc.ChromeOptions()
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    
    # Flags to keep JavaScript active when in background/off-screen
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = uc.Chrome(options=options, version_main=152)

    # Move window off-screen to positive 20000 pixels on x-axis (bypasses Windows negative clamping)
    #driver.set_window_position(20000, 0)

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

    count = 0
    fails = 0

    def solve():
        global count, fails
        
        # Ensure window stays off-screen even if driver.get tries to pull focus
        driver.set_window_position(20000, 0)
        
        driver.get('https://botme.idk.dunkirk.sh/captchas/cap-default?name=awsome-webdev')
        print('Loaded page', flush=True)
        time.sleep(2)
        
        ele = driver.find_element(By.ID, 'cap')
        ele.click()
        print('clicked element', flush=True)
        
        wa = wait_for_console_log(driver, "actually a good", timeout=10)
        if wa is None:
            count += 1
            print(f'Success! Solved {count} Captchas', flush=True)
        else:
            fails += 1
            print(f'Timeout ({fails} total fails)', flush=True)

    while True:
        solve()
