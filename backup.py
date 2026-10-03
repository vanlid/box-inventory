"""Backups of ./data to Google Drive or OneDrive, and restore from them.

Sign-in uses the OAuth *device code* flow: the app shows a short code, you approve it at
google.com/device or microsoft.com/devicelogin on any device. No redirect URLs are needed, so
it works on a fresh install before HTTPS is set up. Only the Python standard library is used.

Config (environment or the .env file next to server.py):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   OAuth client of type "TVs and Limited Input devices"
  MS_CLIENT_ID [, MS_TENANT=consumers]     Entra app registration with public client flows on
  BACKUP_KEEP=14                           how many backups to keep
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from base64 import urlsafe_b64decode
from pathlib import Path

ZIP_PREFIX = "box-inventory-"
CHUNK = 320 * 1024 * 32  # 10 MiB: a multiple of both Google's 256 KiB and OneDrive's 320 KiB
ALLOWED = re.compile(r"^(manifest\.json|inventory\.json|auth\.json|photos/\d+/[\w-]+\.jpg|refs/\w+\.jpg)$")
MAX_RESTORE = 20 * 1024 ** 3


class BackupError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never forward an Authorization header to another host
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, url, body=None, headers=None, form=None, js=None, timeout=60):
    """Returns (status, headers, bytes). HTTP errors are returned, not raised."""
    h = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if js is not None:
        body = json.dumps(js).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackupError(f"Couldn't reach {urllib.parse.urlsplit(url).hostname}: {getattr(e, 'reason', e)}")


def as_json(content):
    try:
        return json.loads(content or b"{}")
    except ValueError:
        return {}


def stream_to_file(url, headers, dest, progress=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with _opener.open(req, timeout=120) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                buf = r.read(1024 * 1024)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if done > MAX_RESTORE:
                    raise BackupError("That backup is too large.")
                if progress and total:
                    progress(f"Downloading {done * 100 // total}%")
    except urllib.error.HTTPError as e:
        raise BackupError(f"Download failed ({e.code}).")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackupError(f"Download failed: {getattr(e, 'reason', e)}")


# ---------- providers ----------

class Provider:
    key = label = ""

    def __init__(self, state, save):
        self.state, self._save = state, save  # state: {"refresh_token", "account", ...}
        self._token, self._exp = None, 0

    def token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        tok = self.refresh()
        self._token, self._exp = tok["access_token"], time.time() + int(tok.get("expires_in", 3600))
        return self._token

    def auth(self):
        return {"Authorization": "Bearer " + self.token()}

    def poll_result(self, code, data, declined):
        if code == 200 and data.get("access_token"):
            self._token, self._exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
            return data
        err = data.get("error")
        if err in ("authorization_pending", "slow_down"):
            return err
        if err in ("access_denied", declined):
            raise BackupError("Access was declined.")
        if err == "expired_token":
            raise BackupError("The code expired. Start again.")
        raise BackupError(data.get("error_description") or f"Sign-in failed ({err or code}).")

    def upload_chunks(self, url, path, headers, progress, done_codes, more_codes):
        total = os.path.getsize(path)
        with open(path, "rb") as f:
            start = 0
            while True:
                chunk = f.read(CHUNK)
                end = start + len(chunk) - 1
                h = {**headers, "Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{total}"}
                code, _, content = http("PUT", url, body=chunk, headers=h, timeout=300)
                if code in done_codes:
                    return as_json(content)
                if code not in more_codes:
                    raise BackupError(f"Upload failed ({code}): {content[:200].decode(errors='replace')}")
                start = end + 1
                progress(f"Uploading {start * 100 // max(total, 1)}%")


class Google(Provider):
    key, label = "google", "Google Drive"
    SCOPE = "https://www.googleapis.com/auth/drive.file openid email"
    FOLDER = "Box Inventory backups"
    API = "https://www.googleapis.com/drive/v3"

    def __init__(self, state, save, client_id, secret):
        super().__init__(state, save)
        self.cid, self.secret = client_id, secret

    def start(self):
        code, _, c = http("POST", "https://oauth2.googleapis.com/device/code", form={"client_id": self.cid, "scope": self.SCOPE})
        d = as_json(c)
        if code != 200:
            raise BackupError(d.get("error_description") or d.get("error") or f"Google refused ({code}). Check GOOGLE_CLIENT_ID.")
        return {"device_code": d["device_code"], "user_code": d["user_code"], "url": d["verification_url"],
                "interval": int(d.get("interval", 5)), "expires": time.time() + int(d.get("expires_in", 1800))}

    def poll(self, device_code):
        code, _, c = http("POST", "https://oauth2.googleapis.com/token", form={
            "client_id": self.cid, "client_secret": self.secret, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        r = self.poll_result(code, as_json(c), "access_denied")
        if isinstance(r, str):
            return r
        account = ""
        try:  # display only: the email claim from the ID token Google just sent us over TLS
            payload = r["id_token"].split(".")[1]
            account = json.loads(urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
        except (KeyError, IndexError, ValueError):
            pass
        if not r.get("refresh_token"):
            raise BackupError("Google didn't return a refresh token. Remove the app's access in your Google account and try again.")
        return {"refresh_token": r["refresh_token"], "account": account}

    def refresh(self):
        code, _, c = http("POST", "https://oauth2.googleapis.com/token", form={
            "client_id": self.cid, "client_secret": self.secret, "refresh_token": self.state["refresh_token"],
            "grant_type": "refresh_token"})
        d = as_json(c)
        if code != 200:
            raise BackupError("Google access expired or was removed. Disconnect and connect again." if d.get("error") == "invalid_grant"
                              else f"Google sign-in failed ({d.get('error') or code}).")
        return d

    def folder(self):
        q = f"name='{self.FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        code, _, c = http("GET", f"{self.API}/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}), headers=self.auth())
        files = as_json(c).get("files") if code == 200 else None
        if files:
            return files[0]["id"]
        code, _, c = http("POST", f"{self.API}/files?fields=id", headers=self.auth(),
                          js={"name": self.FOLDER, "mimeType": "application/vnd.google-apps.folder"})
        if code not in (200, 201):
            raise BackupError(f"Couldn't create the backup folder ({code}).")
        return as_json(c)["id"]

    def upload(self, path, name, progress):
        code, h, c = http("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id",
                          headers={**self.auth(), "X-Upload-Content-Type": "application/zip",
                                   "X-Upload-Content-Length": str(os.path.getsize(path))},
                          js={"name": name, "parents": [self.folder()], "mimeType": "application/zip"})
        if code != 200 or not h.get("Location"):
            raise BackupError(f"Google Drive refused the upload ({code}).")
        self.upload_chunks(h["Location"], path, {}, progress, (200, 201), (308,))

    def list(self):
        q = f"'{self.folder()}' in parents and trashed=false"
        code, _, c = http("GET", f"{self.API}/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name,size,createdTime)", "orderBy": "createdTime desc", "pageSize": 200}), headers=self.auth())
        if code != 200:
            raise BackupError(f"Couldn't list backups ({code}).")
        return [{"id": f["id"], "name": f["name"], "size": int(f.get("size", 0)), "created": f["createdTime"]}
                for f in as_json(c).get("files", []) if f["name"].startswith(ZIP_PREFIX)]

    def download(self, file_id, dest, progress):
        stream_to_file(f"{self.API}/files/{urllib.parse.quote(file_id)}?alt=media", self.auth(), dest, progress)

    def delete(self, file_id):
        http("DELETE", f"{self.API}/files/{urllib.parse.quote(file_id)}", headers=self.auth())


class OneDrive(Provider):
    key, label = "onedrive", "OneDrive"
    SCOPE = "Files.ReadWrite.AppFolder User.Read offline_access"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, state, save, client_id, tenant):
        super().__init__(state, save)
        self.cid, self.base = client_id, f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"

    def start(self):
        code, _, c = http("POST", f"{self.base}/devicecode", form={"client_id": self.cid, "scope": self.SCOPE})
        d = as_json(c)
        if code != 200:
            msg = re.split(r"\s*(?:\r\n|Trace ID:)", d.get("error_description", ""))[0]
            raise BackupError(msg or f"Microsoft refused ({code}). Check MS_CLIENT_ID.")
        return {"device_code": d["device_code"], "user_code": d["user_code"], "url": d["verification_uri"],
                "interval": int(d.get("interval", 5)), "expires": time.time() + int(d.get("expires_in", 900))}

    def poll(self, device_code):
        code, _, c = http("POST", f"{self.base}/token", form={
            "client_id": self.cid, "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        r = self.poll_result(code, as_json(c), "authorization_declined")
        if isinstance(r, str):
            return r
        code, _, me = http("GET", f"{self.GRAPH}/me", headers={"Authorization": "Bearer " + r["access_token"]})
        me = as_json(me) if code == 200 else {}
        return {"refresh_token": r["refresh_token"], "account": me.get("userPrincipalName") or me.get("displayName", "")}

    def refresh(self):
        code, _, c = http("POST", f"{self.base}/token", form={
            "client_id": self.cid, "grant_type": "refresh_token", "refresh_token": self.state["refresh_token"], "scope": self.SCOPE})
        d = as_json(c)
        if code != 200:
            raise BackupError("OneDrive access expired or was removed. Disconnect and connect again." if d.get("error") == "invalid_grant"
                              else f"Microsoft sign-in failed ({d.get('error') or code}).")
        if d.get("refresh_token") and d["refresh_token"] != self.state["refresh_token"]:
            self.state["refresh_token"] = d["refresh_token"]  # Microsoft rotates refresh tokens
            self._save()
        return d

    def upload(self, path, name, progress):
        code, _, c = http("POST", f"{self.GRAPH}/me/drive/special/approot:/{urllib.parse.quote(name)}:/createUploadSession",
                          headers=self.auth(), js={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        url = as_json(c).get("uploadUrl")
        if code != 200 or not url:
            raise BackupError(f"OneDrive refused the upload ({code}).")
        self.upload_chunks(url, path, {}, progress, (200, 201), (202,))  # the upload URL is pre-authorized

    def list(self):
        code, _, c = http("GET", f"{self.GRAPH}/me/drive/special/approot/children?$select=id,name,size,createdDateTime&$top=200",
                          headers=self.auth())
        if code != 200:
            raise BackupError(f"Couldn't list backups ({code}).")
        out = [{"id": f["id"], "name": f["name"], "size": int(f.get("size", 0)), "created": f["createdDateTime"]}
               for f in as_json(c).get("value", []) if f["name"].startswith(ZIP_PREFIX)]
        return sorted(out, key=lambda f: f["created"], reverse=True)

    def download(self, file_id, dest, progress):
        code, _, c = http("GET", f"{self.GRAPH}/me/drive/items/{urllib.parse.quote(file_id)}?$select=id,@microsoft.graph.downloadUrl",
                          headers=self.auth())
        url = as_json(c).get("@microsoft.graph.downloadUrl")
        if code != 200 or not url:
            raise BackupError(f"Couldn't find that backup ({code}).")
        stream_to_file(url, {}, dest, progress)  # pre-authorized URL: no token sent

    def delete(self, file_id):
        http("DELETE", f"{self.GRAPH}/me/drive/items/{urllib.parse.quote(file_id)}", headers=self.auth())


class LocalFolder(Provider):
    """For testing and for a NAS/USB disk: BACKUP_LOCAL_DIR=/path."""
    key, label = "local", "Local folder"

    def __init__(self, state, save, folder):
        super().__init__(state, save)
        self.dir = Path(folder)

    def start(self):
        return {"device_code": "local", "user_code": "", "url": "", "interval": 1, "expires": time.time() + 60}

    def poll(self, device_code):
        self.dir.mkdir(parents=True, exist_ok=True)
        return {"refresh_token": "", "account": str(self.dir)}

    def upload(self, path, name, progress):
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, self.dir / name)

    def list(self):
        files = [p for p in self.dir.glob(ZIP_PREFIX + "*.zip")] if self.dir.exists() else []
        return sorted(({"id": p.name, "name": p.name, "size": p.stat().st_size,
                        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))} for p in files),
                      key=lambda f: f["name"], reverse=True)

    def download(self, file_id, dest, progress):
        if "/" in file_id or "\\" in file_id:
            raise BackupError("Bad backup name.")
        shutil.copyfile(self.dir / file_id, dest)

    def delete(self, file_id):
        (self.dir / file_id).unlink(missing_ok=True)


# ---------- manager ----------

class Backups:
    def __init__(self, data_dir, data_lock, fix_auth):
        """fix_auth(auth_from_backup, origin, current_auth) -> auth dict to write after a restore."""
        self.data = Path(data_dir)
        self.file = self.data / "backup.json"
        self.data_lock, self.fix_auth = data_lock, fix_auth
        self.lock = threading.RLock()
        self.job = None          # {"kind": "backup"|"restore", "progress": str}
        self.pending = None      # device-code sign-in in progress
        self.st = json.loads(self.file.read_text("utf-8")) if self.file.exists() else {}
        self.keep = int(os.environ.get("BACKUP_KEEP", "14"))

    # config
    def available(self):
        out = []
        if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
            out.append({"key": "google", "label": "Google Drive"})
        if os.environ.get("MS_CLIENT_ID"):
            out.append({"key": "onedrive", "label": "OneDrive"})
        if os.environ.get("BACKUP_LOCAL_DIR"):
            out.append({"key": "local", "label": "Local folder"})
        return out

    def provider(self, key=None, state=None):
        key = key or self.st.get("provider")
        state = state if state is not None else self.st.setdefault("state", {})
        if key == "google":
            return Google(state, self.save, os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", ""))
        if key == "onedrive":
            return OneDrive(state, self.save, os.environ.get("MS_CLIENT_ID", ""), os.environ.get("MS_TENANT", "consumers"))
        if key == "local" and os.environ.get("BACKUP_LOCAL_DIR"):
            return LocalFolder(state, self.save, os.environ["BACKUP_LOCAL_DIR"])
        raise BackupError("No backup service connected.")

    def save(self):
        with self.lock:
            self.data.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st, indent=1), "utf-8")
            os.chmod(tmp, 0o600)  # holds the refresh token
            tmp.replace(self.file)

    def status(self):
        with self.lock:
            p = self.st.get("provider")
            return {"available": self.available(),
                    "connected": p and {"key": p, "label": {"google": "Google Drive", "onedrive": "OneDrive", "local": "Local folder"}.get(p, p),
                                        "account": self.st.get("state", {}).get("account", "")},
                    "last": self.st.get("last"), "error": self.st.get("error"), "job": self.job, "keep": self.keep,
                    "restored": self.st.get("restored"),
                    "pending": self.pending and {k: self.pending[k] for k in ("user_code", "url", "interval", "provider")}}

    # sign-in
    def connect_start(self, key):
        if key not in [a["key"] for a in self.available()]:
            raise BackupError("That service isn't configured on this server.")
        d = self.provider(key, {}).start()
        with self.lock:
            self.pending = {**d, "provider": key}
        return self.status()["pending"]

    def connect_poll(self):
        with self.lock:
            pend = self.pending
        if not pend:
            raise BackupError("No sign-in in progress.")
        if time.time() > pend["expires"]:
            self.pending = None
            raise BackupError("The code expired. Start again.")
        try:
            r = self.provider(pend["provider"], {}).poll(pend["device_code"])
        except BackupError:
            self.pending = None
            raise
        if isinstance(r, str):
            if r == "slow_down":
                pend["interval"] += 5
            return {"status": "pending", "interval": pend["interval"]}
        with self.lock:
            self.pending = None
            self.st = {"provider": pend["provider"], "state": r, "last": None, "error": None}
            self.save()
        return {"status": "connected"}

    def disconnect(self):
        with self.lock:
            if self.job:
                raise BackupError("Wait for the current backup or restore to finish.")
            self.st, self.pending = {}, None
            self.file.unlink(missing_ok=True)

    # jobs
    def _run(self, kind, fn):
        with self.lock:
            if self.job:
                raise BackupError("A backup or restore is already running.")
            self.job = {"kind": kind, "progress": "Starting…"}

        def go():
            try:
                fn(lambda msg: self.job.update(progress=msg))
                with self.lock:
                    self.st["error"] = None
            except Exception as e:  # report every failure in the UI instead of dying silently
                with self.lock:
                    self.st["error"] = {"kind": kind, "message": str(e) if isinstance(e, BackupError) else f"Unexpected error: {e}",
                                        "time": int(time.time() * 1000)}
                print(f"{kind} failed:", repr(e), flush=True)
            finally:
                with self.lock:
                    self.job = None
                    if self.st.get("provider"):
                        self.save()
        threading.Thread(target=go, daemon=True).start()

    def backup_now(self):
        self._run("backup", self._backup)

    def _backup(self, progress):
        p = self.provider()
        name = time.strftime(ZIP_PREFIX + "%Y%m%d-%H%M%S.zip")
        tmp = self.data / ".backup-upload.zip"
        progress("Packing…")
        with self.data_lock:  # consistent snapshot; jpgs are stored, not recompressed, so this is quick
            with zipfile.ZipFile(tmp, "w") as z:
                z.writestr("manifest.json", json.dumps({"app": "box-inventory", "format": 1, "created": int(time.time())}))
                if (self.data / "inventory.json").exists():
                    z.write(self.data / "inventory.json", "inventory.json", zipfile.ZIP_DEFLATED)
                if (self.data / "auth.json").exists():
                    auth = json.loads((self.data / "auth.json").read_text("utf-8"))
                    auth["sessions"] = {}  # sign-in cookies stay on this server
                    z.writestr("auth.json", json.dumps(auth), zipfile.ZIP_DEFLATED)
                for f in sorted((self.data / "photos").glob("*/*.jpg")):
                    z.write(f, f"photos/{f.parent.name}/{f.name}", zipfile.ZIP_STORED)
                for f in sorted((self.data / "refs").glob("*.jpg")):
                    z.write(f, f"refs/{f.name}", zipfile.ZIP_STORED)
        try:
            p.upload(tmp, name, progress)
            size = tmp.stat().st_size
        finally:
            tmp.unlink(missing_ok=True)
        with self.lock:
            self.st["last"] = {"name": name, "size": size, "time": int(time.time() * 1000)}
        progress("Removing old backups…")
        for old in p.list()[self.keep:]:
            p.delete(old["id"])

    def list(self):
        return self.provider().list()

    def restore(self, file_id, origin):
        self._run("restore", lambda progress: self._restore(file_id, origin, progress))

    def _restore(self, file_id, origin, progress):
        p = self.provider()
        tmp = self.data / ".restore-download.zip"
        try:
            p.download(file_id, tmp, progress)
            progress("Checking the backup…")
            try:
                z = zipfile.ZipFile(tmp)
            except zipfile.BadZipFile:
                raise BackupError("That file isn't a valid backup.")
            with z:
                names = z.namelist()
                bad = [n for n in names if not ALLOWED.match(n)]
                if bad or "inventory.json" not in names:
                    raise BackupError("That file isn't a Box Inventory backup.")
                if sum(i.file_size for i in z.infolist()) > MAX_RESTORE or z.testzip():
                    raise BackupError("That backup is damaged or too large.")
                inventory = json.loads(z.read("inventory.json"))
                if not isinstance(inventory.get("boxes"), dict):
                    raise BackupError("That backup's inventory is unreadable.")
                restored_auth = json.loads(z.read("auth.json")) if "auth.json" in names else {}
                progress("Restoring…")
                with self.data_lock:
                    keep = self.data / time.strftime("before-restore-%Y%m%d-%H%M%S")
                    keep.mkdir(parents=True)
                    current_auth = {}
                    for n in ("inventory.json", "auth.json", "photos", "refs"):
                        if (self.data / n).exists():
                            if n == "auth.json":
                                current_auth = json.loads((self.data / n).read_text("utf-8"))
                            shutil.move(str(self.data / n), str(keep / n))
                    for n in names:  # every name was checked against ALLOWED above: no path tricks possible
                        if n.startswith(("photos/", "refs/")):
                            (self.data / n).parent.mkdir(parents=True, exist_ok=True)
                            (self.data / n).write_bytes(z.read(n))
                    (self.data / "photos").mkdir(exist_ok=True)
                    (self.data / "inventory.json").write_bytes(z.read("inventory.json"))
                    auth = self.fix_auth(restored_auth, origin, current_auth)
                    tmp_auth = self.data / "auth.tmp"
                    tmp_auth.write_text(json.dumps(auth, indent=1), "utf-8")
                    os.chmod(tmp_auth, 0o600)
                    tmp_auth.replace(self.data / "auth.json")
            try:
                name = next((f["name"] for f in p.list() if f["id"] == file_id), file_id)
            except BackupError:
                name = file_id
            with self.lock:
                self.st["restored"] = {"name": name, "time": int(time.time() * 1000), "previous": keep.name,
                                       "boxes": len(inventory["boxes"])}
        finally:
            tmp.unlink(missing_ok=True)

    # daily schedule
    def has_boxes(self):
        """Never auto-backup an empty install: its backups would push real ones out of the kept set."""
        try:
            return bool(json.loads((self.data / "inventory.json").read_text("utf-8")).get("boxes"))
        except (OSError, ValueError):
            return False

    def changed_since(self, ms):
        files = [self.data / "inventory.json", self.data / "auth.json"]
        newest = max((f.stat().st_mtime for f in files if f.exists()), default=0)
        return newest * 1000 > ms

    def scheduler(self):
        while True:
            time.sleep(600)
            try:
                with self.lock:
                    due = (self.st.get("provider") and not self.job and not self.pending
                           and (not self.st.get("last") or time.time() * 1000 - self.st["last"]["time"] > 24 * 3600 * 1000)
                           and self.changed_since((self.st.get("last") or {}).get("time", 0)) and self.has_boxes()
                           and not (self.st.get("error") and time.time() * 1000 - self.st["error"]["time"] < 6 * 3600 * 1000))
                if due:
                    self.backup_now()
            except Exception as e:
                print("backup scheduler:", repr(e), flush=True)
