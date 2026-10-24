import os
import subprocess
import sys
import time
import urllib.request

import certifi

port = os.environ.get("PORT", "10000")
env = {**os.environ, "BACKEND_URL": f"http://127.0.0.1:{port}", "SSL_CERT_FILE": certifi.where()}

server = subprocess.Popen([sys.executable, "server.py"], env=env)
for _ in range(30):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        break
    except OSError:
        time.sleep(1)

agent = subprocess.Popen([sys.executable, "agent.py"], env=env)

while server.poll() is None and agent.poll() is None:
    time.sleep(5)
sys.exit(1)
