"""Read-only Pi state endpoint (Tier 1).

A tiny stdlib HTTP server the PC's Claude tool handlers call to read live Pi
state — thermals, memory, load, disk, uptime, Bluetooth — and whitelisted logs.
Read-only: no endpoint executes arbitrary commands.

  GET /state              -> JSON system status
  GET /logs?service=&n=   -> recent journal/dmesg lines (whitelisted services)
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE_PORT = 8090

# Whitelisted log sources -> how to fetch them. Nothing here runs user input.
LOG_SOURCES = {
    "bluetooth": ["sudo", "-n", "journalctl", "-u", "bluetooth", "--no-pager"],
    "wireplumber": ["journalctl", "--user", "-u", "wireplumber", "--no-pager"],
    "pipewire": ["journalctl", "--user", "-u", "pipewire", "--no-pager"],
    "kernel": ["sudo", "-n", "dmesg"],
}


def _run(cmd, timeout=8):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


def system_state():
    """Collect a snapshot of live Pi state. Each field is best-effort."""
    state = {}

    # Temperature + throttling (Pi-specific).
    t = _run(["vcgencmd", "measure_temp"])  # e.g. "temp=51.6'C"
    try:
        state["temp_c"] = float(t.split("=")[1].split("'")[0])
    except Exception:  # noqa: BLE001
        state["temp_c"] = None
    thr = _run(["vcgencmd", "get_throttled"])  # e.g. "throttled=0x0"
    state["throttled"] = thr.split("=")[-1] if "=" in thr else thr
    state["throttled_ok"] = state["throttled"] in ("0x0", "")

    # Load average + CPU count.
    try:
        state["load_avg"] = [round(x, 2) for x in os.getloadavg()]
        state["cpu_count"] = os.cpu_count()
    except Exception:  # noqa: BLE001
        pass

    # Memory (MB) from /proc/meminfo.
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k.strip()] = int(v.strip().split()[0])  # kB
        state["mem_total_mb"] = round(mem["MemTotal"] / 1024)
        state["mem_available_mb"] = round(mem["MemAvailable"] / 1024)
        state["mem_used_pct"] = round(
            100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
        )
    except Exception:  # noqa: BLE001
        pass

    # Disk (root filesystem, GB).
    try:
        du = shutil.disk_usage("/")
        state["disk_total_gb"] = round(du.total / 1e9, 1)
        state["disk_free_gb"] = round(du.free / 1e9, 1)
        state["disk_used_pct"] = round(100 * du.used / du.total)
    except Exception:  # noqa: BLE001
        pass

    # Uptime.
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        d, rem = divmod(int(secs), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        state["uptime"] = f"{d}d {h}h {m}m"
    except Exception:  # noqa: BLE001
        pass

    # Bluetooth speaker.
    info = _run(["bluetoothctl", "info", "28:FA:19:2F:0F:76"])
    state["bt_speaker_connected"] = "Connected: yes" in info

    return state


_HERE = os.path.dirname(os.path.abspath(__file__))
HAILO_DETECT = os.path.join(_HERE, "hailo_detect.py")
HAILO_FACES = os.path.join(_HERE, "hailo_faces.py")
CAPTURE_FRAME = os.path.join(_HERE, "capture_frame.py")


def capture_jpeg():
    """Return one JPEG frame (bytes) from the camera, or b'' on failure."""
    try:
        r = subprocess.run(
            ["/usr/bin/python3", CAPTURE_FRAME],
            capture_output=True, timeout=15,
        )
        return r.stdout if r.returncode == 0 else b""
    except Exception:  # noqa: BLE001
        return b""


def look():
    """Capture a frame + run on-device object detection (Hailo, via system python3)."""
    try:
        r = subprocess.run(
            ["/usr/bin/python3", HAILO_DETECT],
            capture_output=True, text=True, timeout=15,
        )
        out = r.stdout.strip()
        if out:
            return json.loads(out)
        return {"error": "no detector output", "stderr": r.stderr[-300:]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def scene():
    """Capture a frame + recognize faces (Hailo, system python3).

    Returns {"faces": [{name, similarity, position, clothing}], "jpeg_b64": ...}
    — the same shape the client's wake-word prefetch sends the PC."""
    try:
        r = subprocess.run(
            ["/usr/bin/python3", HAILO_FACES, "scene"],
            capture_output=True, text=True, timeout=25,
        )
        out = r.stdout.strip()
        if out:
            return json.loads(out)
        return {"error": "no scene output", "stderr": r.stderr[-300:]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def read_log(service, lines):
    """Return recent lines from a whitelisted log source."""
    cmd = LOG_SOURCES.get(service)
    if cmd is None:
        return f"(unknown service {service!r}; allowed: {', '.join(LOG_SOURCES)})"
    lines = max(1, min(int(lines or 20), 50))
    out = _run(cmd + ["-n", str(lines)] if service != "kernel" else cmd, timeout=10)
    if service == "kernel":  # dmesg has no -n; tail it here
        out = "\n".join(out.splitlines()[-lines:])
    return out or "(no output)"


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type):
        code = 200 if data else 500
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/state":
            self._send(system_state())
        elif parsed.path == "/look":
            self._send(look())
        elif parsed.path == "/frame":
            self._send_bytes(capture_jpeg(), "image/jpeg")
        elif parsed.path == "/scene":
            self._send(scene())
        elif parsed.path == "/logs":
            q = parse_qs(parsed.query)
            service = (q.get("service") or [""])[0]
            n = (q.get("n") or ["20"])[0]
            self._send({"service": service, "log": read_log(service, n)})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):  # quiet
        pass


def start_state_server(port=STATE_PORT):
    """Start the read-only state server in a daemon thread."""
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        start_state_server()
        print(f"state server on :{STATE_PORT}", flush=True)
        while True:
            time.sleep(3600)
    else:
        print(json.dumps(system_state(), indent=2))
