"""Unit tests for the host registry (hosts.yaml + wes_hosts.py).

Guards the single source of truth: the file's shape (so a typo fails CI, not
production), the URL builder, and the graceful fallback when the registry can't
be read (which keeps the server answering if PyYAML goes missing from the venv).

The registry described two machines until 2026-09-02; the Pi was repurposed and
its entry removed (archive/pi/README.md). The multi-host assertions below became
single-host ones rather than being deleted, because the FALLBACK behaviour they
protect — an unknown host degrading to the caller's default instead of raising —
is exactly what a future second machine would depend on.
"""
import wes_hosts


class TestRegistryFile:
    """The committed hosts.yaml must define the machine the code relies on."""

    def test_pc_present_with_ip(self):
        wes_hosts.load(force=True)
        assert wes_hosts.ip("pc") == "DESKTOP-R2PFF9T.local"

    def test_required_service_ports_defined(self):
        assert wes_hosts.port("pc", "server") == 8080
        assert wes_hosts.port("pc", "windows_exporter") == 9182
        assert wes_hosts.port("pc", "gpu_exporter") == 9835
        assert wes_hosts.port("pc", "ollama") == 11434
        assert wes_hosts.port("pc", "prometheus") == 9090

    def test_grafana_is_not_on_3000(self):
        """Open WebUI owns 3000 on this machine (moved there 2026-09-02 after it
        silently shadowed the WES server on 8080). Grafana moved off the Pi onto
        this box and must not collide with it — a port clash here is the kind of
        failure that looks like a healthy service serving the wrong thing."""
        assert wes_hosts.port("pc", "grafana") == 3001

    def test_retired_pi_is_gone(self):
        """The Pi tier is archived. Anything still asking for it must get the
        caller's default, never a stale 10.0.0.79 that now belongs to whatever
        the hardware was repurposed into."""
        assert wes_hosts.host("pi") == {}
        assert wes_hosts.ip("pi", default="none") == "none"


class TestUrl:
    def test_builds_url_with_path(self):
        assert (wes_hosts.url("pc", "prometheus", "/api/v1/alerts")
                == "http://DESKTOP-R2PFF9T.local:9090/api/v1/alerts")
        assert wes_hosts.url("pc", "server") == "http://DESKTOP-R2PFF9T.local:8080"

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
        assert wes_hosts.url("pc", "server", default="http://x:1") == "http://x:1"
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
        assert "DESKTOP-R2PFF9T.local" in s
        assert "server 8080" in s        # a port the user might ask about
        assert "grafana 3001" in s
        assert "pc" in s
