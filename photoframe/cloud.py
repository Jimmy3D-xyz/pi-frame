"""Cloud and network folder sync: OneDrive, Google Drive, Dropbox and NAS
(SMB network shares).

Link a folder to an album; the frame keeps the album in step with the
folder: new and changed photos/videos are downloaded (then resized/converted
exactly like uploads), and files removed from the folder are removed from the
album if you want. It's one-way: the frame never writes to OneDrive.

Talking to each service is done by rclone. Signing in to the cloud services
on a headless Pi uses rclone's own sign-in helper running on the Pi: the
browser on your phone/PC does the login, then you paste back the address it
lands on (a localhost link that doesn't load on your PC); the frame hands that
to rclone, which finishes the sign-in. A token from `rclone authorize` run on a
PC is accepted too. A NAS just needs its address, a username and a password.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .storage import IMAGE_EXTS, Storage, StorageError, _check_name, _safe_filename
from .video import VIDEO_EXTS

log = logging.getLogger(__name__)

REMOTE = "onedrive"  # the original (only) remote name; kept for older setups
AUTH_PORT = 53682

PROVIDERS = {
    "onedrive": {"label": "OneDrive", "backend": "onedrive", "oauth": True},
    "gdrive": {"label": "Google Drive", "backend": "drive", "oauth": True},
    "dropbox": {"label": "Dropbox", "backend": "dropbox", "oauth": True},
    "smb": {"label": "NAS", "backend": "smb", "oauth": False},
}
BATCH = 20  # files per download batch
SPARE_BYTES = 500 * 1024 * 1024
FIRST_SYNC_DELAY = 90  # seconds after start-up


class CloudError(RuntimeError):
    pass


ONEDRIVE_MIN_RCLONE = (1, 67, 0)  # older versions can't download from OneDrive since Microsoft's 2024 change


def rclone_version() -> tuple[int, int, int] | None:
    exe = shutil.which("rclone")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def _friendly(err: str, label: str = "OneDrive", nas: bool = False, host: str = "") -> str:
    """Turn rclone's error text into a plain message about the right service."""
    e = err.lower()
    where = f" at {host}" if host else ""
    if nas:
        if "logon is invalid" in e or "logon failure" in e or "access denied" in e or "status_logon_failure" in e:
            return "The NAS didn't accept that username or password."
        if "no such host" in e or "lookup " in e and "server misbehaving" in e:
            return (f"Couldn't find the NAS{where} on your network. Try its IP address (e.g. 192.168.1.20), "
                    "from your router's device list or the NAS's own settings.")
        if "refused" in e:
            return f"The NAS{where} refused the connection. Check its file sharing (SMB) is turned on."
        if "bad network name" in e or "bad_network_name" in e:
            return "That shared folder doesn't exist on the NAS (check the name, or type its path)."
        if "i/o timeout" in e or "no route" in e or "unreachable" in e or "dial tcp" in e or "timeout" in e:
            return f"Couldn't reach the NAS{where}. Check it's switched on and on the same network as the frame."
        if "directory not found" in e or "not found" in e or "object_name_not_found" in e:
            return "That folder can't be found on the NAS (was it moved, renamed or deleted?)."
    else:
        if "unauthenticated: unauthenticated" in e or "failed to open source object: unauthenticated" in e:
            v = rclone_version()
            if v and v < ONEDRIVE_MIN_RCLONE and label == "OneDrive":
                return (f"This frame's rclone ({'.'.join(map(str, v))}) is too old to download from OneDrive. "
                        "Update the frame, or install the current rclone (see the README), then press Sync now.")
            return f"{label} refused to hand over the files. Try Disconnect, then connect {label} again."
        if ("invalid_grant" in e or "invalidauthenticationtoken" in e or "couldn't fetch token" in e
                or "token expired" in e or "expired_access_token" in e):
            return f"{label} sign-in has expired or was revoked. Connect {label} again."
        if "directory not found" in e or "itemnotfound" in e or "not found" in e:
            return f"That {label} folder can't be found (was it moved, renamed or deleted?)."
        if "no such host" in e or "dial tcp" in e or "network is unreachable" in e or "timeout" in e:
            return f"Couldn't reach {label}. Is the frame online?"
    last = [line for line in err.strip().splitlines() if line.strip()]
    return (last[-1] if last else err)[-300:]


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def resolve_host(host: str) -> str | None:
    """Look a NAS name up with the Pi's own resolver, which understands .local
    names (via avahi) and the router's DNS. rclone's own lookup can't do .local."""
    import socket
    if _is_ip(host):
        return host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, 445, socket.AF_INET, socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return None
    return infos[0][4][0] if infos else None


def managed_stem(link_id: str, rel_path: str) -> str:
    """Stable local file name for a synced file: readable, and unique per
    folder + path, so later syncs can find (and replace or remove) it."""
    name = _safe_filename(Path(rel_path).name)[:40]
    digest = hashlib.sha1(f"{link_id}\0{rel_path}".encode()).hexdigest()[:6]
    return f"{name}_{digest}"


def get_drive(access_token: str) -> dict:
    """Ask Microsoft Graph which drive this account has (rclone needs its id)."""
    req = urllib.request.Request("https://graph.microsoft.com/v1.0/me/drive",
                                 headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def google_user(access_token: str) -> dict:
    req = urllib.request.Request("https://www.googleapis.com/drive/v3/about?fields=user",
                                 headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("user", {})


def dropbox_user(access_token: str) -> dict:
    req = urllib.request.Request("https://api.dropboxapi.com/2/users/get_current_account", data=b"null",
                                 headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


class OneDrive:
    def __init__(self, data_dir: Path, storage: Storage, frame=None):
        self.dir = data_dir / "cloud"
        self.dir.mkdir(exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.conf = self.dir / "rclone.conf"
        self.state_path = self.dir / "state.json"
        self.storage = storage
        self.frame = frame
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._auth_proc: subprocess.Popen | None = None
        self._auth_out: list[str] = []
        self._stop = threading.Event()
        self.progress: dict = {}  # link id -> {"done", "total", "current"}
        self._auth_provider = "onedrive"
        self._auth_client: dict = {}
        self.state = {"accounts": [], "links": []}
        try:
            self.state.update(json.loads(self.state_path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
        # Older versions had a single OneDrive account and no per-link remote.
        old = self.state.pop("account", None)
        if old and not any(a["remote"] == REMOTE for a in self.state["accounts"]):
            self.state["accounts"].append({"remote": REMOTE, "provider": "onedrive", **old})
        for link in self.state["links"]:
            link.setdefault("remote", REMOTE)

    # ---------------------------------------------------------------- basics
    @staticmethod
    def available() -> bool:
        return shutil.which("rclone") is not None

    def _read_conf(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        if self.conf.exists():
            cp.read(self.conf)
        return cp

    def _write_conf(self, cp: configparser.ConfigParser) -> None:
        tmp = self.conf.with_suffix(".tmp")
        with open(tmp, "w") as f:
            cp.write(f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.conf)

    def _set_remote(self, remote: str, values: dict) -> None:
        with self._lock:
            cp = self._read_conf()
            if cp.has_section(remote):
                cp.remove_section(remote)
            cp.add_section(remote)
            for k, v in values.items():
                if v not in (None, ""):
                    cp.set(remote, k, str(v))
            self._write_conf(cp)

    def remotes(self) -> list[str]:
        return self._read_conf().sections()

    def remote_connected(self, remote: str) -> bool:
        return remote in self.remotes()

    @property
    def connected(self) -> bool:
        return bool(self.remotes())

    def _account(self, remote: str) -> dict | None:
        return next((a for a in self.state["accounts"] if a["remote"] == remote), None)

    def _upsert_account(self, remote: str, provider: str, name: str, **extra) -> dict:
        with self._lock:
            acct = {"remote": remote, "provider": provider, "name": name, "since": time.time(), **extra}
            self.state["accounts"] = [a for a in self.state["accounts"] if a["remote"] != remote] + [acct]
            self._save()
        return acct

    def _save(self) -> None:
        with self._lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            os.replace(tmp, self.state_path)

    def _rclone(self, *args: str, timeout: float = 300, remote: str | None = None) -> str:
        cmd = ["rclone", "--config", str(self.conf), "--low-level-retries", "3", "--retries", "2", *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise CloudError("OneDrive didn't answer in time") from e
        if r.returncode != 0:
            raw = (r.stderr or r.stdout).strip()
            log.warning("rclone %s failed: %s", args[0] if args else "", raw[-600:])  # full detail for diagnostics
            acct = self._account(remote) if remote else None
            nas = bool(acct and acct["provider"] == "smb") or bool(remote and remote.startswith("nas"))
            raise CloudError(_friendly(raw, self._label(remote) if remote else "OneDrive", nas,
                                       (acct or {}).get("host", "") if nas else ""))
        return r.stdout

    @staticmethod
    def _norm(folder: str) -> str:
        folder = (folder or "").strip()
        return folder.rstrip("/") if folder not in ("", "/") else ""

    def _remote(self, folder: str, remote: str = REMOTE) -> str:
        return f"{remote}:{self._norm(folder)}"

    # ---------------------------------------------------------------- sign-in
    def start_auth(self, provider: str = "onedrive", client_id: str = "", client_secret: str = "") -> str:
        """Start rclone's sign-in helper on the Pi; return the service's login URL."""
        if not self.available():
            raise CloudError("rclone isn't installed (re-run install.sh)")
        info = PROVIDERS.get(provider)
        if not info or not info["oauth"]:
            raise CloudError("unknown service")
        self.cancel_auth()
        self._auth_out = []
        self._auth_provider = provider
        self._auth_client = {"client_id": client_id.strip(), "client_secret": client_secret.strip()}
        args = ["rclone", "--config", os.devnull, "authorize", info["backend"]]
        if provider == "gdrive":
            # read-only access; optionally the user's own Google API key (faster)
            blob = {"scope": "drive.readonly"}
            if client_id.strip():
                blob.update(client_id=client_id.strip(), client_secret=client_secret.strip())
            args.append(_b64(blob))
        self._auth_proc = subprocess.Popen(
            args + ["--auth-no-open-browser"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        threading.Thread(target=self._read_auth_output, daemon=True).start()
        deadline = time.time() + 20
        local = None
        while time.time() < deadline and local is None:
            for line in list(self._auth_out):
                m = re.search(r"(https?://\S+)", line)
                if m and "/auth?state=" in m.group(1):
                    local = m.group(1)
                    break
            if self._auth_proc.poll() is not None:
                break
            time.sleep(0.1)
        if local is None:
            self.cancel_auth()
            raise CloudError("couldn't start the OneDrive sign-in: " + " ".join(self._auth_out)[-300:])
        # rclone's local page just redirects to Microsoft; hand the browser the destination.
        req = urllib.request.Request(local)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307) and e.headers.get("Location"):
                return e.headers["Location"]
            raise CloudError(f"sign-in helper answered {e.code}") from e
        raise CloudError("sign-in helper didn't give a login address")

    def _read_auth_output(self) -> None:
        proc = self._auth_proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self._auth_out.append(line.rstrip("\n"))

    def cancel_auth(self) -> None:
        if self._auth_proc and self._auth_proc.poll() is None:
            self._auth_proc.terminate()
            try:
                self._auth_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._auth_proc.kill()
        self._auth_proc = None

    def finish_auth(self, pasted: str, provider: str | None = None) -> dict:
        """Accept either the localhost address the browser landed on, or a
        token from `rclone authorize` run on a PC."""
        provider = provider or self._auth_provider
        pasted = (pasted or "").strip()
        token = None
        if "access_token" in pasted:
            m = re.search(r"\{.*\}", pasted, re.S)
            token = m.group(0) if m else None
        else:
            q = urllib.parse.urlparse(pasted).query if "?" in pasted else pasted
            params = urllib.parse.parse_qs(q)
            if "error" in params:
                raise CloudError("Microsoft said: " + params.get("error_description", params["error"])[0])
            if "code" not in params or "state" not in params:
                raise CloudError("That doesn't look like the address from after signing in. It should start "
                                 f"with http://localhost:{AUTH_PORT}/ or http://127.0.0.1:{AUTH_PORT}/")
            if not self._auth_proc or self._auth_proc.poll() is not None:
                raise CloudError("The sign-in timed out. Press 'Sign in with Microsoft' again.")
            url = f"http://127.0.0.1:{AUTH_PORT}/?" + urllib.parse.urlencode(
                {"code": params["code"][0], "state": params["state"][0]})
            try:
                urllib.request.urlopen(url, timeout=30).read()
            except Exception as e:  # noqa: BLE001
                raise CloudError(f"the sign-in helper didn't accept the code ({e})") from e
            deadline = time.time() + 45
            while time.time() < deadline:
                text = "\n".join(self._auth_out)
                m = re.search(r"--->\s*(\{.*?\})\s*<---", text, re.S)
                if m:
                    token = m.group(1)
                    break
                if self._auth_proc.poll() is not None and "<---" not in text:
                    break
                time.sleep(0.2)
            self.cancel_auth()
            if not token:
                raise CloudError("sign-in didn't complete: " + _friendly("\n".join(self._auth_out[-5:])))
        if not token:
            raise CloudError("couldn't find a sign-in token in what was pasted")
        try:
            tok = json.loads(token)
            access = tok["access_token"]
        except (json.JSONDecodeError, KeyError) as e:
            raise CloudError("the sign-in token wasn't readable") from e
        return self._save_oauth(provider, tok, access)

    def _save_oauth(self, provider: str, tok: dict, access: str) -> dict:
        label = PROVIDERS[provider]["label"]
        token_json = json.dumps(tok, separators=(",", ":"))
        try:
            if provider == "onedrive":
                drive = get_drive(access)
                owner = (drive.get("owner") or {}).get("user") or {}
                values = {"type": "onedrive", "token": token_json, "drive_id": drive["id"],
                          "drive_type": drive.get("driveType", "personal")}
                name, extra = owner.get("displayName") or label, {"drive_type": drive.get("driveType")}
            elif provider == "gdrive":
                user = google_user(access)
                values = {"type": "drive", "scope": "drive.readonly", "token": token_json, **self._auth_client}
                name, extra = user.get("displayName") or user.get("emailAddress") or label, {
                    "email": user.get("emailAddress"), "own_key": bool(self._auth_client.get("client_id"))}
            elif provider == "dropbox":
                user = dropbox_user(access)
                values = {"type": "dropbox", "token": token_json}
                name, extra = (user.get("name") or {}).get("display_name") or label, {"email": user.get("email")}
            else:
                raise CloudError("unknown service")
        except CloudError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CloudError(f"signed in, but couldn't open your {label} ({e})") from e
        self._set_remote(provider, values)
        acct = self._upsert_account(provider, provider, name, **extra)
        log.info("%s connected (%s)", label, name)
        return acct

    # ---------------------------------------------------------------- NAS
    def add_nas(self, host: str, user: str, password: str, domain: str = "", label: str = "") -> dict:
        """Connect an SMB network share (Synology, QNAP, Windows, …)."""
        host = (host or "").strip().removeprefix("smb://").removeprefix("\\\\").strip("/\\")
        if not host or any(c in host for c in " /\\"):
            raise CloudError("enter the NAS's name or address, e.g. 192.168.1.20 or mynas.local")
        if not user:
            raise CloudError("enter a username for the NAS")
        ip = resolve_host(host)
        if ip is None:
            raise CloudError(f"Couldn't find a NAS called '{host}' on your network. Try its IP address "
                             "(e.g. 192.168.1.20), from your router's device list or the NAS's own settings.")
        existing = [a["remote"] for a in self.state["accounts"] if a["provider"] == "smb"]
        remote = "nas" if "nas" not in existing else next(f"nas{i}" for i in range(2, 100) if f"nas{i}" not in existing)
        try:
            obscured = subprocess.run(["rclone", "obscure", password or ""], capture_output=True, text=True,
                                      timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            raise CloudError(f"couldn't store the password ({e})") from e
        self._set_remote(remote, {"type": "smb", "host": ip, "user": user.strip(), "pass": obscured,
                                  "domain": (domain or "").strip()})
        try:
            try:
                self._rclone("--contimeout", "10s", "lsjson", "--dirs-only", "--no-mimetype", "--no-modtime",
                             f"{remote}:", timeout=45)
            except CloudError as e:
                # word the error as a NAS one (no account exists yet for _rclone to look at)
                raise CloudError(str(e) if "NAS" in str(e) else _friendly(str(e), "NAS", True, host)) from e
        except CloudError:
            with self._lock:
                cp = self._read_conf()
                cp.remove_section(remote)
                self._write_conf(cp)
            raise
        acct = self._upsert_account(remote, "smb", (label or "").strip() or f"NAS ({host})", host=host, ip=ip,
                                    user=user.strip())
        log.info("NAS %s connected as %s", host, user)
        return acct

    def disconnect(self, remote: str = REMOTE) -> None:
        self.cancel_auth()
        with self._lock:
            cp = self._read_conf()
            if cp.has_section(remote):
                cp.remove_section(remote)
                self._write_conf(cp)
            acct = self._account(remote)
            self.state["accounts"] = [a for a in self.state["accounts"] if a["remote"] != remote]
            self._save()
        what = PROVIDERS.get((acct or {}).get("provider", ""), {}).get("label", remote)
        log.info("%s disconnected (synced albums are kept)", what)

    def _refresh_nas(self, remote: str) -> None:
        acct = self._account(remote)
        if not acct or acct["provider"] != "smb" or _is_ip(acct.get("host", "")):
            return
        ip = resolve_host(acct["host"])
        if not ip:
            return  # keep the last known address
        with self._lock:
            cp = self._read_conf()
            if cp.has_section(remote) and cp.get(remote, "host", fallback="") != ip:
                cp.set(remote, "host", ip)
                self._write_conf(cp)
                log.info("NAS %s is now at %s", acct["host"], ip)

    # ---------------------------------------------------------------- browsing
    def list_folders(self, folder: str = "", remote: str = REMOTE) -> list[dict]:
        self._refresh_nas(remote)
        out = self._rclone("lsjson", "--dirs-only", "--no-mimetype", "--no-modtime", self._remote(folder, remote),
                           timeout=60, remote=remote)
        items = json.loads(out or "[]")
        base = self._norm(folder)
        return sorted(({"name": i["Name"], "path": f"{base}/{i['Name']}" if base else i["Name"]} for i in items),
                      key=lambda d: d["name"].lower())

    def count_media(self, folder: str, subfolders: bool, remote: str = REMOTE) -> dict:
        files = self._list_files(folder, subfolders, remote)
        vids = sum(1 for f in files if Path(f["Path"]).suffix.lower() in VIDEO_EXTS)
        return {"photos": len(files) - vids, "videos": vids, "bytes": sum(f["Size"] for f in files)}

    def _list_files(self, folder: str, subfolders: bool, remote: str = REMOTE) -> list[dict]:
        args = ["lsjson", "--files-only", "--no-mimetype"]
        args += ["-R"] if subfolders else ["--max-depth", "1"]
        self._refresh_nas(remote)
        out = self._rclone(*args, self._remote(folder, remote), timeout=600, remote=remote)
        wanted = IMAGE_EXTS | VIDEO_EXTS
        return [f for f in json.loads(out or "[]") if Path(f["Path"]).suffix.lower() in wanted]

    def _label(self, remote: str) -> str:
        acct = self._account(remote)
        if acct:
            return PROVIDERS.get(acct["provider"], {}).get("label", remote)
        return {"onedrive": "OneDrive", "gdrive": "Google Drive", "dropbox": "Dropbox"}.get(remote, "NAS" if remote.startswith("nas") else remote)

    # ---------------------------------------------------------------- links
    def links(self) -> list[dict]:
        return self.state["links"]

    def _link(self, link_id: str) -> dict:
        for link in self.state["links"]:
            if link["id"] == link_id:
                return link
        raise CloudError("unknown synced folder")

    def add_link(self, folder: str, album: str, subfolders: bool = True, mirror: bool = True,
                 every_hours: int = 1, remote: str = REMOTE) -> dict:
        if not self.remote_connected(remote):
            raise CloudError("connect that service first")
        try:
            album = _check_name(album, "album")
        except StorageError as e:
            raise CloudError(f"{e} (letters, numbers, spaces and - . ( ) & ' only)") from e
        if any(l["album"] == album for l in self.state["links"]):
            raise CloudError(f"the album '{album}' is already synced with another folder")
        self.storage.create_album(album)
        link = {"id": hashlib.sha1(f"{folder}\0{album}\0{time.time()}".encode()).hexdigest()[:10],
                "remote": remote, "folder": self._norm(folder), "album": album, "subfolders": bool(subfolders),
                "mirror": bool(mirror), "every_hours": max(1, int(every_hours)),
                "last_sync": None, "last_ok": None, "last_result": "Not synced yet", "files": 0}
        with self._lock:
            self.state["links"].append(link)
            self._save()
        log.info("%s folder '%s' linked to album '%s'", self._label(remote), link["folder"] or "(root)", album)
        threading.Thread(target=self.sync, args=(link["id"],), daemon=True).start()
        return link

    def update_link(self, link_id: str, **changes) -> dict:
        with self._lock:
            link = self._link(link_id)
            for k in ("subfolders", "mirror"):
                if k in changes:
                    link[k] = bool(changes[k])
            if "every_hours" in changes:
                link["every_hours"] = max(1, int(changes["every_hours"]))
            self._save()
        return link

    def remove_link(self, link_id: str, delete_album: bool = False) -> None:
        with self._lock:
            link = self._link(link_id)
            self.state["links"].remove(link)
            self._save()
        (self.dir / f"manifest-{link_id}.json").unlink(missing_ok=True)
        if delete_album:
            try:
                self.storage.delete_album(link["album"])
            except StorageError:
                pass
        log.info("%s folder '%s' unlinked%s", self._label(link.get("remote", REMOTE)), link["folder"],
                 " and its album deleted" if delete_album else "")

    # ---------------------------------------------------------------- sync
    def _manifest(self, link_id: str) -> dict:
        try:
            return json.loads((self.dir / f"manifest-{link_id}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_manifest(self, link_id: str, manifest: dict) -> None:
        p = self.dir / f"manifest-{link_id}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest))
        os.replace(tmp, p)

    def sync(self, link_id: str) -> dict:
        """Bring one album in line with its OneDrive folder."""
        if not self._sync_lock.acquire(blocking=False):
            return {"skipped": "another sync is running"}
        try:
            return self._sync(link_id)
        finally:
            self._sync_lock.release()
            self.progress.pop(link_id, None)

    def _sync(self, link_id: str) -> dict:
        link = self._link(link_id)
        album = link["album"]
        remote = link.get("remote", REMOTE)
        label = self._label(remote)
        added = updated = removed = failed = 0
        errors: list[str] = []
        self.progress[link_id] = {"done": 0, "total": 0, "current": "Listing the folder…"}
        try:
            if not self.remote_connected(remote):
                raise CloudError(f"{label} isn't connected")
            if not (self.storage.root / album).is_dir():
                self.storage.create_album(album)
            files = self._list_files(link["folder"], link["subfolders"], remote)
            listing = {f["Path"]: f for f in files}
            manifest = self._manifest(link_id)

            # removed from OneDrive (or out of scope now)
            if link["mirror"]:
                for path in [p for p in manifest if p not in listing]:
                    self.storage.delete_stem(album, managed_stem(link_id, path))
                    manifest.pop(path)
                    removed += 1
                if removed:
                    self._save_manifest(link_id, manifest)

            todo = [f for p, f in listing.items()
                    if p not in manifest or manifest[p]["size"] != f["Size"] or manifest[p]["mtime"] != f["ModTime"]]
            self.progress[link_id] = {"done": 0, "total": len(todo), "current": ""}
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                need = sum(f["Size"] for f in batch) + SPARE_BYTES
                if shutil.disk_usage(self.storage.root).free < need:
                    raise CloudError("The SD card is nearly full, so the sync stopped. Free some space or "
                                     "remove photos from the OneDrive folder.")
                with tempfile.TemporaryDirectory(dir=self.storage.root.parent / "tmp"
                                                 if (self.storage.root.parent / "tmp").is_dir() else None) as tmp:
                    listfile = Path(tmp) / "files.txt"
                    listfile.write_text("\n".join(f["Path"] for f in batch) + "\n")
                    dl = Path(tmp) / "dl"
                    self._rclone("copy", "--files-from", str(listfile), "--no-traverse",
                                 self._remote(link["folder"], remote), str(dl), timeout=3600, remote=remote)
                    for f in batch:
                        path = f["Path"]
                        self.progress[link_id]["current"] = Path(path).name
                        local = dl / path
                        try:
                            if not local.exists():
                                raise CloudError("download didn't arrive")
                            is_new = path not in manifest
                            self._import(link_id, album, path, local)
                            manifest[path] = {"size": f["Size"], "mtime": f["ModTime"]}
                            added += is_new
                            updated += not is_new
                        except Exception as e:  # noqa: BLE001
                            failed += 1
                            errors.append(f"{Path(path).name}: {e}")
                            log.warning("%s: couldn't add %s: %s", label, path, e)
                        self.progress[link_id]["done"] += 1
                    self._save_manifest(link_id, manifest)
                if added or updated:
                    self._notify()
            parts = []
            if added:
                parts.append(f"added {added}")
            if updated:
                parts.append(f"updated {updated}")
            if removed:
                parts.append(f"removed {removed}")
            if failed:
                parts.append(f"{failed} couldn't be added")
            summary = (", ".join(parts) if parts else "no changes").capitalize() + f" · {len(listing)} in the folder"
            ok = failed == 0
            result = {"added": added, "updated": updated, "removed": removed, "failed": failed,
                      "files": len(listing), "errors": errors[:10]}
        except Exception as e:  # noqa: BLE001
            summary = str(e) if isinstance(e, CloudError) else _friendly(str(e))
            ok = False
            result = {"error": summary, "added": added, "updated": updated, "removed": removed}
            log.warning("%s sync of '%s' failed: %s", label, link["folder"], summary)
        with self._lock:
            try:
                link = self._link(link_id)
            except CloudError:
                return result  # unlinked while syncing
            link.update(last_sync=time.time(), last_ok=ok, last_result=summary,
                        files=result.get("files", link.get("files", 0)), errors=result.get("errors", []))
            self._save()
        if ok:
            log.info("%s sync of '%s': %s", label, link["folder"] or "(root)", summary)
        if removed:
            self._notify()
        return result

    def _import(self, link_id: str, album: str, rel_path: str, local: Path) -> None:
        stem = managed_stem(link_id, rel_path)
        ext = local.suffix.lower()
        if ext in VIDEO_EXTS:
            jobs = getattr(self.frame, "jobs", None)
            if jobs is None:
                raise CloudError("videos need ffmpeg on the frame")
            dest = self.storage.incoming_path(album, local.name)
            shutil.move(str(local), dest)
            jobs.add(album, dest, stem=stem)
        else:
            from . import render

            img = render.open_image(local)
            self.storage.delete_stem(album, stem)
            self.storage.add(album, local.name, img, stem=stem)

    def _notify(self) -> None:
        if self.frame is not None:
            try:
                self.frame.photos_added()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- schedule
    def run(self) -> None:
        started = time.time()
        while not self._stop.wait(30):
            if time.time() - started < FIRST_SYNC_DELAY or not self.connected:
                continue
            remotes = set(self.remotes())
            wifi = getattr(self.frame, "wifi", None)
            if wifi is not None and wifi.status.mode != "online":
                continue
            for link in list(self.state["links"]):
                if link.get("remote", REMOTE) not in remotes:
                    continue
                due = (link.get("last_sync") or 0) + link["every_hours"] * 3600
                if time.time() >= due:
                    self.sync(link["id"])

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        remotes = set(self.remotes())
        accounts = [{**a, "label": PROVIDERS.get(a["provider"], {}).get("label", a["provider"]),
                     "connected": a["remote"] in remotes} for a in self.state["accounts"]]
        # a config written by hand (or by an older version) without account details
        for r in remotes - {a["remote"] for a in accounts}:
            prov = "onedrive" if r == REMOTE else "gdrive" if r == "gdrive" else "dropbox" if r == "dropbox" else "smb"
            accounts.append({"remote": r, "provider": prov, "name": PROVIDERS[prov]["label"],
                             "label": PROVIDERS[prov]["label"], "connected": True})
        links = []
        for link in self.state["links"]:
            remote = link.get("remote", REMOTE)
            nxt = (link.get("last_sync") or 0) + link["every_hours"] * 3600
            acct = self._account(remote)
            source = acct["name"] if acct and acct["provider"] == "smb" else self._label(remote)
            links.append({**link, "remote": remote, "source": source,
                          "connected": remote in remotes, "progress": self.progress.get(link["id"]),
                          "next_sync": max(nxt, time.time()) if remote in remotes else None})
        onedrive = next((a for a in accounts if a["remote"] == REMOTE), None)
        v = rclone_version()
        return {"available": self.available(), "connected": bool(remotes),
                "rclone_version": ".".join(map(str, v)) if v else None,
                "rclone_old_for_onedrive": bool(v and v < ONEDRIVE_MIN_RCLONE),
                "providers": [{"id": k, **v} for k, v in PROVIDERS.items()],
                "accounts": accounts, "links": links,
                "account": onedrive,  # older pages/tests
                "signing_in": bool(self._auth_proc and self._auth_proc.poll() is None),
                "signing_in_to": self._auth_provider if (self._auth_proc and self._auth_proc.poll() is None) else None}


CloudSync = OneDrive  # the class now covers every service


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
