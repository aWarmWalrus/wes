"""Shared accessor for the WES host registry (hosts.yaml at the repo root).

The single source of truth for machine identities, IPs, and service ports.
Imported by pc/wes_server.py, pc/wes_discord.py, and pi/wes_client.py — each
adds the repo root to sys.path first. Every lookup takes a `default` so a
missing or malformed registry degrades to the caller's old hard-coded value
rather than bricking a service; a unit test guards the file's shape.

    import wes_hosts
    wes_hosts.url("pi", "pi_state", "/scene")   # -> http://10.0.0.79:8090/scene
    wes_hosts.summary()                          # human/LLM-readable table
"""
import os

try:
    import yaml
except ImportError:  # pragma: no cover - only on a venv without PyYAML
    yaml = None

_PATH = os.environ.get(
    "WES_HOSTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hosts.yaml"))
_cache = None


def load(force=False):
    """The parsed registry dict, cached. Returns {} if unreadable (no yaml,
    missing file, parse error) so callers fall back to their defaults."""
    global _cache
    if _cache is not None and not force:
        return _cache
    if yaml is None:
        return {}
    try:
        with open(_PATH, encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        _cache = {}
    return _cache


def host(name):
    """One host's record ({} if absent)."""
    return load().get("hosts", {}).get(name, {}) or {}


def ip(name, default=None):
    return host(name).get("ip", default)


def port(name, port_key, default=None):
    return (host(name).get("ports") or {}).get(port_key, default)


def url(name, port_key, path="", default=None):
    """http://<ip>:<port><path>, or `default` if the host/port isn't found."""
    h = host(name)
    p = (h.get("ports") or {}).get(port_key)
    if not h.get("ip") or p is None:
        return default
    return f"http://{h['ip']}:{p}{path}"


def summary():
    """Compact human/LLM-readable registry — the body of the lookup_hosts tool."""
    out = []
    for name, h in (load().get("hosts", {}) or {}).items():
        aka = h.get("aliases") or []
        label = name + (f" (aka {', '.join(aka)})" if aka else "")
        ports = ", ".join(f"{k} {v}" for k, v in (h.get("ports") or {}).items())
        line = f"{label}: {h.get('ip', '?')} — {h.get('role', '').strip()}"
        if h.get("hostname"):
            line += f" — hostname {h['hostname']}"
        if ports:
            line += f" — ports: {ports}"
        out.append(line)
    return "\n".join(out) if out else "(host registry unavailable)"
