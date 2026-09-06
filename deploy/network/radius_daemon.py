"""Validate a replacement before interrupting the working RADIUS daemon."""
import subprocess


def replace_daemon(process, has_listeners):
    if has_listeners:
        try:
            checked = subprocess.run(['freeradius', '-XC'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except subprocess.TimeoutExpired:
            return process, False
        if checked.returncode:
            return process, False
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if not has_listeners:
        return None, True
    return subprocess.Popen(['freeradius', '-f', '-l', 'stdout']), True
