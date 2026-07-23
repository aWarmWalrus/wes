"""Unit tests for the host registry (hosts.yaml + wes_hosts.py).

Guards the single source of truth: the file's shape (so a typo fails CI, not
production), the URL builder, and the graceful fallback when the registry can't
be read (which keeps the voice loop alive if PyYAML is missing on the Pi).
"""
import os

import wes_hosts


class TestRegistryFile:
    """The committed hosts.yaml must define the machines the code relies on."""

    def test_pc_and_pi_present_with_ips(self):
        wes_hosts.load(force=True)
        assert wes_hosts.ip("pc") == "10.0.0.91"
        assert wes_hosts.ip("pi") == "10.0.0.79"

    def test_required_service_ports_defined(self):
        assert wes_hosts.port("pc", "server") == 8080
        assert wes_hosts.port("pc", "windows_exporter") == 9182
        assert wes_hosts.port("pc", "gpu_exporter") == 9835
        assert wes_hosts.port("pi", "pi_state") == 8090
        assert wes_hosts.port("pi", "prometheus") == 9090


class TestUrl:
    def test_builds_url_with_path(self):
        assert wes_hosts.url("pi", "pi_state", "/scene") == "http://10.0.0.79:8090/scene"
        assert wes_hosts.url("pc", "server") == "http://10.0.0.91:8080"

    def test_unknown_host_or_port_returns_default(self):
        assert wes_hosts.url("nope", "server", default="fb") == "fb"
        assert wes_hosts.url("pc", "no_such_port", default="fb") == "fb"
        assert wes_hosts.port("pc", "no_such_port", default=1) == 1


class TestFallback:
    def test_missing_registry_degrades_to_defaults(self, monkeypatch, tmp_path):
        # point at a nonexistent file and clear the cache -> everything falls
        # back so a service is never bricked by a bad/absent registry
        monkeypatch.setattr(wes_hosts, "_PATH", str(tmp_path / "gone.yaml"))
        monkeypatch.setattr(wes_hosts, "_cache", None)
        assert wes_hosts.load(force=True) == {}
        assert wes_hosts.url("pi", "pi_state", default="http://x:1") == "http://x:1"
        assert wes_hosts.ip("pc", default="?") == "?"
        wes_hosts.load(force=True)  # restore real cache for other tests

    def test_no_yaml_module_degrades(self, monkeypatch):
        monkeypatch.setattr(wes_hosts, "yaml", None)
        monkeypatch.setattr(wes_hosts, "_cache", None)
        assert wes_hosts.load(force=True) == {}
        wes_hosts.load(force=True)


class TestSummary:
    def test_summary_is_llm_readable(self):
        wes_hosts.load(force=True)
        s = wes_hosts.summary()
        assert "10.0.0.91" in s and "10.0.0.79" in s
        assert "walrus-pi" in s          # alias surfaced
        assert "server 8080" in s        # a port the user might ask about
        assert "pc" in s and "pi" in s
