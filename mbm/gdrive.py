"""Google Drive storage, with nothing but `requests` and `openssl`.

Two ways to sign in:

* **Google account (device sign-in).** You create an OAuth client of type
  "TVs and Limited Input devices" in your own Google Cloud project, paste its
  ID and secret here, then open google.com/device and type the code the UI
  shows. The scope is ``drive.file``: this app sees only the files and folder
  it created itself, nothing else in your Drive.
* **Service account.** Paste the JSON key and the ID of a folder in a Shared
  Drive the service account is a member of. (Service accounts have no
  storage of their own, so a folder in someone's My Drive will not work.)
"""

import base64
import json
import os
import subprocess
import tempfile
import threading
import time

try:
    import requests
except ImportError:  # pragma: no cover - the package depends on it
    requests = None

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPE_USER = "https://www.googleapis.com/auth/drive.file"
SCOPE_SA = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 16 * 1024 * 1024  # a multiple of 256 KiB, as resumable uploads require
COMMON = {"supportsAllDrives": "true"}

_tokens = {}  # destination id -> (access_token, expires_at)
_tok_lock = threading.Lock()


class DriveError(Exception):
    pass


def _need_requests():
    if requests is None:
        raise DriveError("python3-requests is not installed")


def _err(resp):
    try:
        e = resp.json().get("error")
        if isinstance(e, dict):
            return "%s (%s)" % (e.get("message"), resp.status_code)
        return "%s: %s" % (e, resp.json().get("error_description", "")) if e else resp.text[:200]
    except ValueError:
        return "HTTP %d %s" % (resp.status_code, resp.text[:200])


# --------------------------------------------------------------------------
# device sign-in
# --------------------------------------------------------------------------

def start_device_flow(client_id):
    _need_requests()
    r = requests.post(DEVICE_URL, data={"client_id": client_id, "scope": SCOPE_USER}, timeout=20)
    if r.status_code != 200:
        raise DriveError("Google refused the client ID: %s" % _err(r))
    return r.json()  # device_code, user_code, verification_url, expires_in, interval


def poll_device_flow(client_id, client_secret, device_code):
    """One poll. Returns ("pending"|"slow_down"|"ok"|"denied"|"expired", data)."""
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret, "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=20)
    if r.status_code == 200:
        return "ok", r.json()
    try:
        err = r.json().get("error")
    except ValueError:
        err = None
    if err == "authorization_pending":
        return "pending", None
    if err == "slow_down":
        return "slow_down", None
    if err == "access_denied":
        return "denied", None
    if err in ("expired_token", "invalid_grant"):
        return "expired", None
    raise DriveError(_err(r))


# --------------------------------------------------------------------------
# access tokens
# --------------------------------------------------------------------------

def _sa_assertion(sa):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": sa["client_email"], "scope": SCOPE_SA, "aud": sa.get("token_uri", TOKEN_URL),
              "iat": now, "exp": now + 3600}

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")
    signing_input = b64(header) + b"." + b64(claims)
    fd, keyfile = tempfile.mkstemp(prefix="mbm-sa-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sa["private_key"])
        p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", keyfile], input=signing_input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        os.unlink(keyfile)
    if p.returncode != 0:
        raise DriveError("Could not sign with the service account key: %s" % p.stderr.decode()[:200])
    return (signing_input + b"." + base64.urlsafe_b64encode(p.stdout).rstrip(b"=")).decode()


def access_token(dest):
    _need_requests()
    with _tok_lock:
        tok = _tokens.get(dest["id"])
        if tok and tok[1] > time.time() + 60:
            return tok[0]
    if dest.get("auth") == "service_account":
        try:
            sa = json.loads(dest.get("sa_json") or "")
        except ValueError:
            raise DriveError("The service account key is not valid JSON")
        r = requests.post(TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": _sa_assertion(sa)}, timeout=20)
    else:
        if not dest.get("refresh_token"):
            raise DriveError("This Google Drive destination is not signed in yet")
        r = requests.post(TOKEN_URL, data={
            "client_id": dest.get("client_id"), "client_secret": dest.get("client_secret"),
            "refresh_token": dest["refresh_token"], "grant_type": "refresh_token"}, timeout=20)
    if r.status_code != 200:
        raise DriveError("Google sign-in failed: %s" % _err(r))
    data = r.json()
    with _tok_lock:
        _tokens[dest["id"]] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def forget_token(dest_id):
    with _tok_lock:
        _tokens.pop(dest_id, None)


class Drive:
    def __init__(self, dest):
        self.dest = dest

    def _h(self):
        return {"Authorization": "Bearer " + access_token(self.dest)}

    def _get(self, path, **params):
        params.update(COMMON)
        r = requests.get(API + path, headers=self._h(), params=params, timeout=60)
        if r.status_code != 200:
            raise DriveError(_err(r))
        return r.json()

    def about(self):
        try:
            return self._get("/about", fields="user(emailAddress,displayName)").get("user") or {}
        except DriveError:
            return {}

    def folder_id(self):
        """The folder backups live in: the configured ID, or ours found/created by name."""
        if self.dest.get("folder_id"):
            return self.dest["folder_id"]
        name = self.dest.get("folder_name") or "MariaDB Backups"
        q = "mimeType='%s' and name='%s' and trashed=false" % (FOLDER_MIME, name.replace("'", "\\'"))
        found = self._get("/files", q=q, fields="files(id,name)", spaces="drive").get("files")
        if found:
            return found[0]["id"]
        r = requests.post(API + "/files", headers=self._h(), params=COMMON,
                          json={"name": name, "mimeType": FOLDER_MIME}, timeout=30)
        if r.status_code != 200:
            raise DriveError("Could not create the folder: %s" % _err(r))
        return r.json()["id"]

    def list(self, folder):
        files, token = [], None
        while True:
            params = {"q": "'%s' in parents and trashed=false" % folder,
                      "fields": "nextPageToken,files(id,name,size,createdTime)",
                      "pageSize": 1000, "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self._get("/files", **params)
            files += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return files

    def find(self, folder, name):
        for f in self.list(folder):
            if f["name"] == name:
                return f
        return None

    def upload(self, folder, path, log=None, cancel=None):
        size = os.path.getsize(path)
        meta = {"name": os.path.basename(path), "parents": [folder]}
        params = dict(COMMON, uploadType="resumable")
        r = requests.post(UPLOAD, headers=dict(self._h(), **{
            "X-Upload-Content-Type": "application/x-tar", "X-Upload-Content-Length": str(size)}),
            params=params, json=meta, timeout=60)
        if r.status_code != 200:
            raise DriveError("Upload could not start: %s" % _err(r))
        session = r.headers["Location"]
        sent, last_pct = 0, -1
        with open(path, "rb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    requests.delete(session, timeout=10)
                    raise DriveError("Cancelled")
                f.seek(sent)
                chunk = f.read(CHUNK)
                rng = ("bytes %d-%d/%d" % (sent, sent + len(chunk) - 1, size)) if chunk else ("bytes */%d" % size)
                r = self._put(session, chunk, rng, size)
                if r.status_code in (200, 201):
                    return r.json()
                have = r.headers.get("Range")  # 308: what Google has so far, e.g. "bytes=0-1234"
                sent = int(have.split("-")[1]) + 1 if have else 0
                pct = int(sent * 100 / size) if size else 100
                if log and pct // 25 != last_pct // 25:
                    log("  uploaded %d%%" % pct)
                    last_pct = pct

    @staticmethod
    def _put(session, chunk, content_range, size):
        """PUT one chunk, retrying network errors and 5xx/429 with backoff.

        After a failure Google is asked how much it actually has, and that
        answer (a 308 with a Range header) is returned so the caller resumes
        from the right offset instead of resending blindly.
        """
        for attempt in range(6):
            try:
                r = requests.put(session, data=chunk, headers={"Content-Range": content_range}, timeout=300)
                if r.status_code in (200, 201, 308):
                    return r
                if r.status_code < 500 and r.status_code != 429:
                    raise DriveError("Upload failed: %s" % _err(r))
            except requests.RequestException:
                pass
            time.sleep(min(2 ** attempt, 30))
            try:
                q = requests.put(session, headers={"Content-Range": "bytes */%d" % size}, timeout=60)
                if q.status_code in (200, 201, 308):
                    return q
            except requests.RequestException:
                pass
        raise DriveError("Upload failed after several retries")

    def download(self, file_id, dest_path):
        r = requests.get(API + "/files/%s" % file_id, headers=self._h(),
                         params=dict(COMMON, alt="media"), stream=True, timeout=300)
        if r.status_code != 200:
            raise DriveError("Download failed: %s" % _err(r))
        tmp = dest_path + ".partial"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        os.replace(tmp, dest_path)

    def stream(self, file_id):
        r = requests.get(API + "/files/%s" % file_id, headers=self._h(),
                         params=dict(COMMON, alt="media"), stream=True, timeout=300)
        if r.status_code != 200:
            raise DriveError("Download failed: %s" % _err(r))
        return r

    def delete(self, file_id):
        r = requests.delete(API + "/files/%s" % file_id, headers=self._h(), params=COMMON, timeout=60)
        if r.status_code not in (200, 204, 404):
            raise DriveError("Delete failed: %s" % _err(r))
