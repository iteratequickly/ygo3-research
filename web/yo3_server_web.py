
import json
import os
import threading
import time
import urllib.request
import webbrowser
from urllib.parse import urlsplit

import server_launcher as server


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(HERE, "images")
INDEX_FILE = os.path.join(HERE, "index.html")

STATUS = {
    "gate": False,
    "front": False,
}

STATUS_LOCK = threading.Lock()


# --------------------------------------------------
# QUIET MODE
# --------------------------------------------------

def quiet_log_message(self, format, *args):
    pass


server.HTTPHandler.log_message = quiet_log_message


# --------------------------------------------------
# SERVER HELPERS
# --------------------------------------------------

def get_server_value(name, default=None):
    return getattr(server, name, default)


def get_status(name):
    with STATUS_LOCK:
        return bool(STATUS.get(name, False))


# --------------------------------------------------
# HTTP RESPONSES
# --------------------------------------------------

def send_response(handler, body, content_type):

    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, payload):

    body = json.dumps(payload).encode("utf-8")

    send_response(
        handler,
        body,
        "application/json; charset=utf-8"
    )


def serve_dashboard(handler):

    if not os.path.isfile(INDEX_FILE):
        handler.send_error(404, "Dashboard not found")
        return

    try:
        with open(INDEX_FILE, "rb") as file:
            body = file.read()

        send_response(
            handler,
            body,
            "text/html; charset=utf-8"
        )

    except OSError:
        handler.send_error(500, "Could not read dashboard")


def serve_image(handler, filename):

    allowed_images = {
        "logo.png": "image/png",
    }

    if filename not in allowed_images:
        handler.send_error(404, "Image not found")
        return

    image_path = os.path.join(IMAGE_DIR, filename)

    if not os.path.isfile(image_path):
        handler.send_error(404, "Image not found")
        return

    try:
        with open(image_path, "rb") as file:
            body = file.read()

        send_response(
            handler,
            body,
            allowed_images[filename]
        )

    except OSError:
        handler.send_error(500, "Could not read image")


# --------------------------------------------------
# DASHBOARD API
# --------------------------------------------------

def get_api_status():

    return {
        "browser": True,
        "gate": get_status("gate"),
        "front": get_status("front"),
        "http": True,

        "host": get_server_value("HOST", "127.0.0.1"),
        "gate_port": get_server_value("GATE_PORT", 5730),
        "front_port": get_server_value("FRONT_PORT", 1213),
        "http_port": get_server_value("HTTP_PORT", 80),
    }


# --------------------------------------------------
# DASHBOARD ROUTING
# --------------------------------------------------

original_do_get = server.HTTPHandler.do_GET


def dashboard_get(self):

    path = urlsplit(self.path).path

    if path in ("/", "/index.html"):
        serve_dashboard(self)
        return

    if path == "/api/status":
        send_json(self, get_api_status())
        return

    if path.startswith("/images/"):

        filename = path[len("/images/"):]

        if "/" in filename or "\\" in filename:
            self.send_error(404, "Invalid image path")
            return

        serve_image(self, filename)
        return

    original_do_get(self)


server.HTTPHandler.do_GET = dashboard_get


# --------------------------------------------------
# BROWSER LAUNCHER
# --------------------------------------------------

def dashboard_url():

    host = get_server_value("HOST", "127.0.0.1")
    port = get_server_value("HTTP_PORT", 80)

    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port_part = "" if port == 80 else f":{port}"

    return f"http://{host}{port_part}/"


def open_browser_when_ready():

    url = dashboard_url()
    status_url = url.rstrip("/") + "/api/status"

    for _ in range(120):

        try:
            with urllib.request.urlopen(
                status_url,
                timeout=2
            ) as response:

                if response.status == 200:
                    webbrowser.open_new(url)
                    return

        except Exception:
            pass

        time.sleep(0.5)


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    print("YO3 Server Started")

    # No TCP polling or dummy client connections.
    # These represent startup state, not live health checks.

    with STATUS_LOCK:
        STATUS["gate"] = True
        STATUS["front"] = True

    threading.Thread(
        target=open_browser_when_ready,
        daemon=True
    ).start()

    server.main()


if __name__ == "__main__":
    main()
