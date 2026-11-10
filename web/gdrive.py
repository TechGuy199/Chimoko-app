"""Google Drive for backups: connecting a business's Google account, and putting backup files into a folder in its Drive.

The app asks only for the drive.file permission: it can see and change the files and folders it made itself, nothing else in the
person's Drive. The server keeps the long-lived refresh token (encrypted with a key made from SECRET_KEY) and turns it into a
short-lived access token when it needs one. Set up once on the server with GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (a Google
Cloud OAuth client of the "Web application" type whose redirect URI is this app's /backup/drive/callback).
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = "openid email https://www.googleapis.com/auth/drive.file"
FOLDER_TYPE = "application/vnd.google-apps.folder"
TIMEOUT = 40


class DriveError(Exception):
    """Something Google refused or could not be reached. gone=True means the connection itself no longer works (access withdrawn)."""

    def __init__(self, message, gone=False, status=None):
        super().__init__(message)
        self.gone = gone
        self.status = status


def configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


# --- the refresh token at rest ---
def _fernet(secret_key):
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("drive-token:" + secret_key).encode("utf-8")).digest()))


def seal(secret_key, text):
    return _fernet(secret_key).encrypt(text.encode("utf-8")).decode("ascii")


def unseal(secret_key, sealed):
    """The token, or None when it cannot be read (SECRET_KEY changed): the business then connects again."""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(secret_key).decrypt(sealed.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, AttributeError):
        return None


# --- talking to Google ---
def _call(url, data=None, method=None, headers=None, raw=False):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            body = reply.read()
            return body if raw else (json.loads(body or b"{}") if body else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except ValueError:
            detail = {}
        error = detail.get("error")
        message = (error.get("message") if isinstance(error, dict) else detail.get("error_description") or error) or f"HTTP {e.code}"
        gone = error in ("invalid_grant", "unauthorized_client") or e.code == 401
        raise DriveError(f"Google said: {message}", gone=gone, status=e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DriveError(f"Google Drive could not be reached ({e}).") from None


def auth_url(redirect_uri, state):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
    })


def exchange(code, redirect_uri):
    """The tokens for the code Google sent back, and the Google account's email address."""
    tokens = _call(TOKEN_URL, urllib.parse.urlencode({
        "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode("ascii"), headers={"Content-Type": "application/x-www-form-urlencoded"})
    if "https://www.googleapis.com/auth/drive.file" not in tokens.get("scope", "").split():
        raise DriveError("Permission to save files in Google Drive was not given. Connect again and tick the box for Google Drive.")
    if not tokens.get("refresh_token"):
        raise DriveError("Google did not give the app lasting access. Connect again.")
    email = ""
    try:   # the id token came straight from Google over TLS, so its claims can be read without checking the signature again
        payload = tokens.get("id_token", "").split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
    except (IndexError, ValueError):
        pass
    return tokens, email


def refresh(refresh_token):
    """A fresh access token and how many seconds it lasts."""
    tokens = _call(TOKEN_URL, urllib.parse.urlencode({
        "refresh_token": refresh_token, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }).encode("ascii"), headers={"Content-Type": "application/x-www-form-urlencoded"})
    return tokens["access_token"], int(tokens.get("expires_in", 3600))


def revoke(token):
    try:
        _call(REVOKE_URL, urllib.parse.urlencode({"token": token}).encode("ascii"), headers={"Content-Type": "application/x-www-form-urlencoded"})
    except DriveError:
        pass   # already withdrawn, or Google unreachable: the app forgets the token either way


def _auth(access):
    return {"Authorization": "Bearer " + access}


def usable(access, file_id):
    """Whether a file or folder the app made is still there and not in the bin."""
    try:
        info = _call(f"{FILES_URL}/{file_id}?fields=id,trashed&supportsAllDrives=true", headers=_auth(access))
    except DriveError as e:
        if e.status == 404:
            return False
        raise
    return not info.get("trashed")


def make_folder(access, name, parent=None):
    meta = {"name": name, "mimeType": FOLDER_TYPE}
    if parent:
        meta["parents"] = [parent]
    return _call(f"{FILES_URL}?fields=id&supportsAllDrives=true", json.dumps(meta).encode("utf-8"), "POST",
                 dict(_auth(access), **{"Content-Type": "application/json"}))["id"]


def _multipart(meta, data, mimetype):
    boundary = "backup" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
        f"--{boundary}\r\nContent-Type: {mimetype}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/related; boundary={boundary}"


def put_file(access, name, data, mimetype, parent, file_id=None):
    """Saves a file into the folder: replaces the one made last time (Drive keeps the earlier version in its history), or makes it.
    Returns the file's id."""
    if file_id:
        body, kind = _multipart({"trashed": False}, data, mimetype)
        try:
            return _call(f"{UPLOAD_URL}/{file_id}?uploadType=multipart&fields=id&supportsAllDrives=true", body, "PATCH",
                         dict(_auth(access), **{"Content-Type": kind}))["id"]
        except DriveError as e:
            if e.status != 404:
                raise   # 404: it was deleted for good, so it is made again below
    body, kind = _multipart({"name": name, "parents": [parent]}, data, mimetype)
    return _call(f"{UPLOAD_URL}?uploadType=multipart&fields=id&supportsAllDrives=true", body, "POST",
                 dict(_auth(access), **{"Content-Type": kind}))["id"]


def folder_link(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}"


def now_seconds():
    return int(time.time())
