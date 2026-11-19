import logging
import time

import pytest

from photoframe import health
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (1920, 1080)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    return frame, create_app(frame).test_client()


def test_decode_throttled():
    ok = health.decode_throttled(0)
    assert not any(ok["flags"].values()) and ok["notes"] == []
    bad = health.decode_throttled(0x50005)  # classic weak-power-supply reading
    f = bad["flags"]
    assert f["under_voltage_now"] and f["throttled_now"] and f["under_voltage_since_boot"] and f["throttled_since_boot"]
    assert not f["freq_capped_now"] and bad["raw"] == "0x50005"
    past = health.decode_throttled(0x50000)["flags"]
    assert past["under_voltage_since_boot"] and not past["under_voltage_now"]


def test_summary_levels():
    base = {"network": {"mode": "online", "band": "5 GHz", "link": {"last_ok": True}, "rejoins": []},
            "power": {"cpu_temp_c": 52.0, "throttled": health.decode_throttled(0)},
            "storage": {"free": 20e9, "root_read_only": False, "kernel_errors": []},
            "library": {"jobs": []}}
    s = {x["area"]: x for x in health.summarise(base)}
    assert all(x["level"] == "ok" for x in s.values())
    assert s["Network"]["text"] == "Connected on 5 GHz"

    worse = dict(base, power={"cpu_temp_c": 60.0, "throttled": health.decode_throttled(0x50000)},
                 storage={"free": 20e9, "root_read_only": True, "kernel_errors": ["EXT4-fs error"]},
                 network={"mode": "online", "band": "2.4 GHz", "link": {"last_ok": False}, "rejoins": [{}]})
    s = {x["area"]: x for x in health.summarise(worse)}
    assert s["Power"]["level"] == "warn" and "power supply" in s["Power"]["text"]
    assert s["SD card"]["level"] == "bad" and "read-only" in s["SD card"]["text"]
    assert s["Network"]["level"] == "warn"
    assert health.summarise(dict(base, network={"mode": "setup"}))[0]["level"] == "bad"


def test_sd_error_pattern():
    lines = [
        "[ 6676.048354] EXT4-fs (mmcblk0p2): failed to convert unwritten extents",
        "[ 6676.054900] EXT4-fs error (device mmcblk0p2) in ext4_reserve_inode_write:6359: IO failure",
        "[ 6676.065766] EXT4-fs error (device mmcblk0p2) in ext4_reserve_inode_write:6359: Journal has aborted",
        "[    2.1] mmc0: new ultra high speed DDR50 SDHC card at address aaaa",
        "[   20.0] brcmfmac: brcmf_c_process_clm_blob: no clm_blob available",
    ]
    hits = [l for l in lines if health.SD_ERROR_RE.search(l)]
    assert len(hits) == 2  # the real errors from your SD card incident; not normal boot messages


def test_maintenance_endpoint(env):
    frame, client = env
    h = client.get("/api/maintenance").get_json()
    for key in ("system", "power", "storage", "network", "display", "slideshow", "library", "summary"):
        assert key in h
    assert h["network"]["mode"] == "disabled"  # no NetworkManager in tests
    assert h["display"]["size"] == [1920, 1080]
    assert h["storage"]["free"] > 0 and h["system"]["hostname"]
    assert any(x["area"] == "SD card" for x in h["summary"])


def test_activity_log_and_filter(env):
    frame, client = env
    logging.getLogger("photoframe.frame").info("scheduled rotation: test entry")
    logging.getLogger("photoframe.network").warning("link check: test problem")
    ev = client.get("/api/maintenance/events").get_json()
    msgs = [e["msg"] for e in ev]
    assert "link check: test problem" in msgs and "scheduled rotation: test entry" in msgs
    assert msgs.index("link check: test problem") < msgs.index("scheduled rotation: test entry")  # newest first
    only = client.get("/api/maintenance/events?warnings=1").get_json()
    assert all(e["level"] in ("warning", "error") for e in only)
    assert any(e["msg"] == "link check: test problem" for e in only)


def test_diagnostics_have_no_secrets(env):
    frame, client = env
    frame.cfg.update({"access_token": "TOKEN-12345", "openai_api_key": "sk-SECRET", "http_password": "hunter2",
                      "device_name": "LCD frame"})
    r = client.get("/api/maintenance/diagnostics", headers={
        "Authorization": "Basic " + __import__("base64").b64encode(b"x:hunter2").decode()})
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    text = r.get_data(as_text=True)
    assert "== summary" in text and "LCD frame" in text
    for secret in ("TOKEN-12345", "sk-SECRET", "hunter2"):
        assert secret not in text


def test_restart_and_network_check(env, monkeypatch):
    frame, client = env
    exited = []
    monkeypatch.setattr("photoframe.web.os._exit", lambda code: exited.append(code))
    assert client.post("/api/maintenance/restart").status_code == 200
    time.sleep(1.3)
    assert exited == [0]
    assert client.post("/api/maintenance/check-network").status_code == 404  # no Wi-Fi manager here

    from photoframe.network import WifiStatus

    class FakeWifi:
        status = WifiStatus(mode="online")

        def check_now(self):
            return [{"target": "Router", "host": "192.168.1.1", "ok": True, "ms": 3}]

    frame.wifi = FakeWifi()
    r = client.post("/api/maintenance/check-network").get_json()
    assert r["results"][0]["ok"] is True


def test_read_only_check_uses_system_view(tmp_path):
    # What the sandboxed service sees for itself (ProtectSystem=strict): / is ro.
    # That must NOT be reported as a failing card.
    own = tmp_path / "self_mounts"
    own.write_text("/dev/mmcblk0p2 / ext4 ro,relatime 0 0\n"
                   "/dev/mmcblk0p2 /var/lib/pi-photoframe ext4 rw,relatime 0 0\n")
    system = tmp_path / "init_mounts"
    system.write_text("/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n/dev/mmcblk0p1 /boot/firmware vfat rw 0 0\n")
    assert health.root_read_only(str(system)) is False
    failed = tmp_path / "failed"
    failed.write_text("/dev/mmcblk0p2 / ext4 ro,noatime 0 0\n")  # kernel remounted it after errors
    assert health.root_read_only(str(failed)) is True
    assert health.root_read_only(str(tmp_path / "missing")) is None


def test_data_writable_and_summary(tmp_path):
    assert health.data_writable(tmp_path) is True
    base = {"network": {"mode": "online", "link": {"last_ok": True}, "rejoins": []}, "power": {},
            "storage": {"free": 20e9, "root_read_only": None, "data_writable": True, "kernel_errors": []}, "library": {"jobs": []}}
    sd = {x["area"]: x for x in health.summarise(base)}["SD card"]
    assert sd["level"] == "ok"
    base["storage"]["data_writable"] = False
    sd = {x["area"]: x for x in health.summarise(base)}["SD card"]
    assert sd["level"] == "bad" and "can't save" in sd["text"]
