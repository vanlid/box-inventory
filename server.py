#!/usr/bin/env python3
"""Box Inventory: a small home-network server.

Stores everything under ./data (inventory.json, auth.json, photos/<box>/<file>.jpg) and
identifies box contents by running the local `claude` CLI headless, so scans use the
Claude login or token on this machine (no API key). Sign-in uses passkeys (WebAuthn),
which browsers only allow over HTTPS (e.g. `tailscale serve`) or on localhost.

Run:  python3 server.py [--port 8765] [--log file]
Env:  PORT=8765  CLAUDE_BIN=/path/to/claude  CLAUDE_MODEL=sonnet  AUTH=off (disable sign-in)
      GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET, MS_CLIENT_ID: backups (see backup.py). Also read from .env.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import backup as BK
import things as TH
import webauthn as W

ROOT = Path(__file__).resolve().parent


def load_env_file():
    """Settings from .env next to this file (real environment variables win)."""
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text("utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#") and v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()
DATA = ROOT / "data"
PHOTOS = DATA / "photos"
REFS = DATA / "refs"  # reference pictures of tracked items, plus sheet.jpg with all of them
RECEIPTS = DATA / "receipts"  # receipt photos for insurance
DB_FILE = DATA / "inventory.json"
AUTH_FILE = DATA / "auth.json"
SETUP_FILE = DATA / "setup-code.txt"
TOKEN_FILE = ROOT / ".claude-token"
PORT = int(os.environ.get("PORT", "8765"))
MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
ASK_MODEL = os.environ.get("CLAUDE_ASK_MODEL", "haiku")  # plain-language questions: fast and cheap
AUTH_ON = os.environ.get("AUTH", "on").strip().lower() not in ("off", "0", "false", "no")
MAX_UPLOAD = 25 * 1024 * 1024
MAX_SCAN_PHOTOS = 8
SESSION_DAYS = 180
COOKIE = "bi_session"

CATEGORIES = ["Tech & cables", "Documents", "Food & supplements", "Cleaning & tools",
              "Health & personal care", "Office supplies", "Clothes & outdoor", "Kitchen",
              "Hobby", "Mixed / not sure"]

lock = threading.RLock()  # re-entrant: session checks happen inside locked sections
challenges = {}  # challenge -> (kind, expires, origin); in memory, valid 5 minutes


def oauth_token():
    """Long-lived token from `claude setup-token`, kept in .claude-token (or the env var)."""
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t:
            return t
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def find_claude():
    for c in [os.environ.get("CLAUDE_BIN"), shutil.which("claude"),
              str(Path.home() / ".local/bin/claude"), str(Path.home() / ".local/bin/claude.exe"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]:
        if c and Path(c).exists():
            return c
    return None


def now_ms():
    return int(time.time() * 1000)


# ---------- storage ----------

def write_json(path, data, private=False):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(path)  # atomic: a crash never leaves a half-written file


def load():
    db = json.loads(DB_FILE.read_text("utf-8")) if DB_FILE.exists() else {}
    db.setdefault("boxes", {})
    db.setdefault("rooms", [])
    db.setdefault("things", {})
    db.setdefault("settings", {"currency": "SEK"})
    for k, b in db["boxes"].items():  # places: numbered boxes, or spots like "laundry basket"
        b.setdefault("id", k)
        b.setdefault("kind", "box")
        b.setdefault("setsStatus", "")
        b.setdefault("sightings", [])
    return db


def save(db):
    write_json(DB_FILE, db)


def new_id():
    return uuid.uuid4().hex[:8]


def clean_loc(loc, photos):
    """An item's position: which photo and [left, top, right, bottom] in 0-1000 units."""
    if not isinstance(loc, dict) or loc.get("photo") not in photos:
        return None
    try:
        b = [max(0, min(1000, int(v))) for v in loc.get("box", [])]
    except (TypeError, ValueError):
        return None
    if len(b) != 4 or b[2] - b[0] < 5 or b[3] - b[1] < 5:
        return None
    return {"photo": loc["photo"], "box": b}


def clean_item(it, photos):
    if not isinstance(it, dict):
        return None
    name = str(it.get("name", "")).strip()[:120]
    if not name:
        return None
    try:
        qty = max(1, int(it.get("qty") or 1))
    except (TypeError, ValueError):
        qty = 1
    out = {"id": str(it.get("id") or new_id())[:16], "name": name, "qty": qty}
    if it.get("src") == "scan":  # listed by a scan (re-scans may refresh it) vs. added by hand
        out["src"] = "scan"
    if it.get("edited"):  # a scanned item you changed: re-scans keep it as you left it
        out["edited"] = True
    if it.get("scanName"):  # the name the scan gave it, so a renamed item isn't found "again"
        out["scanName"] = str(it["scanName"])[:120]
    if it.get("note"):
        out["note"] = str(it["note"]).strip()[:160]
    loc = clean_loc(it.get("loc"), photos)
    if loc:
        out["loc"] = loc
    return TH.clean_details(it, out)


# ---------- passkey accounts ----------

def load_auth():
    a = json.loads(AUTH_FILE.read_text("utf-8")) if AUTH_FILE.exists() else {}
    a.setdefault("rp", None)  # {"id": host, "origin": "https://host"}, fixed by the first passkey
    a.setdefault("userId", W.b64u_encode(secrets.token_bytes(16)))
    a.setdefault("credentials", [])
    a.setdefault("sessions", {})
    return a


def save_auth(a):
    t = time.time()
    a["sessions"] = {k: v for k, v in a["sessions"].items() if v["exp"] > t}
    write_json(AUTH_FILE, a, private=True)


def setup_code():
    """One-time code needed to register the first passkey, so a stranger can't claim the app first."""
    if SETUP_FILE.exists():
        return SETUP_FILE.read_text().strip()
    code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    DATA.mkdir(parents=True, exist_ok=True)
    SETUP_FILE.write_text(code + "\n")
    os.chmod(SETUP_FILE, 0o600)
    return code


def new_challenge(kind, origin):
    t = time.time()
    for c in [c for c, v in challenges.items() if v[1] < t]:
        del challenges[c]
    c = W.b64u_encode(secrets.token_bytes(32))
    challenges[c] = (kind, t + 300, origin)
    return c


def take_challenge(credential, kind, origin):
    """Find and consume the challenge echoed in clientDataJSON (each one works once)."""
    try:
        c = json.loads(W.b64u_decode(credential["response"]["clientDataJSON"]))["challenge"]
    except (KeyError, TypeError, ValueError):
        raise W.WebAuthnError("malformed passkey data")
    v = challenges.pop(c, None) if isinstance(c, str) else None
    if not v or v[0] != kind or v[1] < time.time() or v[2] != origin:
        raise W.WebAuthnError("This sign-in request expired. Try again.")
    return c


def fix_auth_after_restore(restored, origin, current):
    """Passkeys only work on the address they were made for. Keep them when restoring on that same
    address; otherwise drop them and reopen setup so the first new passkey can be created."""
    a = {"rp": restored.get("rp"), "userId": restored.get("userId") or current.get("userId") or W.b64u_encode(secrets.token_bytes(16)),
         "credentials": restored.get("credentials") or [], "sessions": {}}
    if not AUTH_ON:
        return a
    if a["rp"] and a["credentials"] and origin == a["rp"].get("origin"):
        ids = {c["id"] for c in a["credentials"]}
        a["sessions"] = {k: v for k, v in (current.get("sessions") or {}).items() if v.get("cred") in ids}
        SETUP_FILE.unlink(missing_ok=True)
    else:
        a.update(rp=None, credentials=[])
        setup_code()
    return a


BACKUPS = BK.Backups(DATA, lock, fix_auth_after_restore)


# ---------- insurance export ----------

CURRENCIES = ["SEK", "EUR", "USD", "NOK", "DKK", "GBP", "CHF"]


def export_csv(db):
    """All items and tracked items as a spreadsheet: one row each, with insurance details."""
    import csv
    import io
    rooms = {r["id"]: r["name"] for r in db["rooms"]}
    cur = db["settings"].get("currency", "SEK")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Room", "Place", "Item", "Quantity", "Note", f"Value each ({cur})", f"Total value ({cur})", "Purchased",
                "Serial number", "Receipt", "Tracked status", "Lent to", "Due back"])
    things_by_item = {(t.get("from") or {}).get("item"): t for t in db["things"].values() if t.get("from")}
    lent = lambda t, f: t.get(f, "") if t.get("status") == "lent" else ""
    for b in sorted(db["boxes"].values(), key=lambda b: (rooms.get(b.get("room"), "~"), b["kind"] == "spot", b.get("number") or 0)):
        place = (f'Box {b["number"]:02d}' if b["kind"] == "box" else "Spot") + (f' {b["name"]}' if b.get("name") else "")
        for i in b["items"]:
            t = things_by_item.get(i["id"]) or {}
            v = i.get("value", t.get("value"))
            w.writerow([rooms.get(b.get("room"), ""), place, i["name"], i["qty"], i.get("note", ""),
                        v if v is not None else "", round(v * i["qty"], 2) if v is not None else "",
                        i.get("purchased", t.get("purchased", "")), i.get("serial", t.get("serial", "")),
                        "yes" if i.get("receipt") or t.get("receipt") else "", t.get("status", "").replace("_", " "),
                        lent(t, "lentTo"), lent(t, "dueBack")])
    for t in db["things"].values():
        if t.get("from"):
            continue  # already listed with its box item
        home = db["boxes"].get(t.get("home")) or {}
        v = t.get("value")
        w.writerow([rooms.get(home.get("room"), ""), home.get("name") or "", t["name"], t.get("total", 1), t.get("note", ""),
                    v if v is not None else "", round(v * t.get("total", 1), 2) if v is not None else "", t.get("purchased", ""),
                    t.get("serial", ""), "yes" if t.get("receipt") else "", t["status"].replace("_", " "), lent(t, "lentTo"), lent(t, "dueBack")])
    return out.getvalue()


# ---------- Claude scan ----------

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestedName": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "sightings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Tracked item code, e.g. T3"},
                "photo": {"type": "integer"},
                "box": {"type": "array", "items": {"type": "integer"}},
                "count": {"type": "integer", "description": "How many of it you see (for groups)"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["code", "photo", "box", "confidence"]}},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "qty": {"type": "integer"},
                "note": {"type": "string"},
                "photo": {"type": "integer", "description": "Number of the photo where the item is most visible"},
                "box": {"type": "array", "items": {"type": "integer"},
                        "description": "[left, top, right, bottom] in that photo, each 0-1000 of the image width/height"},
            },
            "required": ["name", "qty", "photo", "box"]}},
    },
    "required": ["items"],
}


def claude_env():
    # Drop variables inherited from a parent Claude Code session so the CLI uses this machine's own login.
    env = {k: v for k, v in os.environ.items() if not (k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"))}
    token = oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def run_claude(prompt, schema, model, tools=(), dirs=(), timeout=600):
    claude = find_claude()
    if not claude:
        raise RuntimeError("The claude command wasn't found on this computer. Install Claude Code and log in once.")
    cmd = [claude, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
           "--tools", ",".join(tools)] + (["--allowedTools", ",".join(tools)] if tools else [])
    for d in dirs:
        cmd += ["--add-dir", str(d)]
    cmd += ["--model", model, "--no-session-persistence", "--strict-mcp-config"]
    r = subprocess.run(cmd, cwd=str(PHOTOS), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL, env=claude_env())
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Claude didn't answer. " + (r.stderr or r.stdout).strip()[:300])
    if out.get("is_error"):
        raise RuntimeError("Claude reported an error: " + str(out.get("result", ""))[:300])
    data = out.get("structured_output")
    if data is None:  # fall back to parsing the text answer
        m = re.search(r"\{.*\}", out.get("result", ""), re.S)
        data = json.loads(m.group(0)) if m else {}
    return data


ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Short answer in the language of the question"},
        "matches": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "Place id, e.g. P3"},
                "item": {"type": "string", "description": "Item id inside that place, e.g. I1a2b3c4d, if it's an item"},
                "thing": {"type": "string", "description": "Tracked item id, e.g. T9f8e7d6c5b, if it's a tracked item"},
                "why": {"type": "string", "description": "A few words on why it matches"},
            },
            "required": ["why"]}},
    },
    "required": ["answer", "matches"],
}


def inventory_text(db):
    """A compact, id-tagged text version of the inventory for Claude to search."""
    rooms = {r["id"]: r["name"] for r in db["rooms"]}
    lines = []
    for b in sorted(db["boxes"].values(), key=lambda b: (b["kind"] == "spot", b.get("number") or 0, b.get("name") or "")):
        label = f'Box {b["number"]:02d}' if b["kind"] == "box" else "Spot"
        bits = [f'P{b["id"]} {label} "{b.get("name") or ""}"']
        if rooms.get(b.get("room")):
            bits.append(f'room: {rooms[b["room"]]}')
        if b.get("location"):
            bits.append(f'at: {b["location"]}')
        lines.append(" | ".join(bits))
        for i in b["items"]:
            lines.append(f'  I{i["id"]} {i["qty"]}x {i["name"]}' + (f' ({i["note"]})' if i.get("note") else ""))
    for t in db["things"].values():
        ls = t.get("lastSeen") or {}
        lines.append(f'T{t["id"]} tracked "{t["name"]}"' + (f' ({t["note"]})' if t.get("note") else "")
                     + f' | status: {t["status"].replace("_", " ")} | home: P{t["home"] or "-"} | last seen: P{ls.get("place") or "-"}'
                     + (f' | group of {t["total"]}, counts: ' + ", ".join(f"P{k}={n}" for k, n in t["counts"].items()) if t["kind"] == "group" else ""))
    return "\n".join(lines)


def ask(question, db):
    prompt = f"""You help someone find things in their home inventory. Answer their question using ONLY the inventory below.
Understand any language (often Swedish or English), synonyms, translations, plurals and vague descriptions:
"sockor" means socks, "something to charge my laptop" matches chargers and USB-C cables.
Answer briefly in the language of the question. List the best matches (at most 12) using the ids shown:
"place" (like P3) and, for an item, also "item" (like I1a2b3c4d); for a tracked item use "thing" (like T9f8e7d6c5b) and the place it was last seen.
If nothing matches, say so and suggest what to search for instead. Never invent ids.

Inventory:
{inventory_text(db)}

Question: {question}"""
    return run_claude(prompt, ASK_SCHEMA, ASK_MODEL, timeout=180)


def scan(box, photo_paths, known, tracked=()):
    files = "\n".join(f"Photo {i}: {p}" for i, p in enumerate(photo_paths, 1))
    known_txt = ("Items already recorded for this box. Don't list these again, unless you clearly see more of them "
                 "or one has no position yet:\n" + "\n".join(f"- {i['qty']}x {i['name']}" for i in known) + "\n\n") if known else ""
    title = f' ("{box["name"]}")' if box.get("name") else ""
    where = (f"numbered storage box {box['number']}{title}" if box.get("kind", "box") == "box"
             else f'the place "{box.get("name") or "unnamed spot"}" (a spot in a room, not a box)')
    sheet = REFS / "sheet.jpg"
    track_txt = ""
    if tracked:
        lines = "\n".join(f"- {t['code']}: {t['name']}" + (f" ({t['note']})" if t.get("note") else "")
                          + (f" [group of {t['total']} look-alikes: count how many you see]" if t["kind"] == "group" else "")
                          + ("" if t.get("hasRef") else " [no picture, match by description]") for t in tracked)
        track_txt = f"""
The owner also tracks these specific items, to know where each one is:
{lines}
{f"Their reference pictures are tiles labelled with the code in this sheet (Read it too): {sheet}" if sheet.exists() and any(t.get("hasRef") for t in tracked) else ""}
In "sightings", report each tracked item you can see in the photos, with the photo number, a tight box
around it, and confidence. Only report it when it looks like that same item (colour, pattern, print), not
just the same kind of thing; use "low" when unsure. For groups, put how many you see in "count".
Still list everything in "items" as usual, tracked or not.
"""
    prompt = f"""You are cataloguing the contents of {where} so the owner can search for things later.
Use the Read tool to look at each of these photos:
{files}

{known_txt}List every distinct physical item you can see inside or on the box.
- Combine identical items into one entry with a quantity.
- Be specific and searchable, in English: "USB-C cable", "HDMI cable", "Krill oil capsules", "Insurance letter".
- Put readable brand/label text (original language), colour or size in "note".
- Ignore the box itself, the floor and the background.
- If something is unclear, give your best plain description (e.g. "Small black charger").
- For each item give "photo" (the photo number where it's most visible) and "box": a tight rectangle
  [left, top, right, bottom] around it in that photo, measured from the top-left corner, each from 0 to 1000
  as a fraction of the image width (left/right) and height (top/bottom). For a group of identical items, cover the group.
Also suggest a short 2-4 word name for the box and pick a category.
{track_txt}"""
    return run_claude(prompt, SCHEMA, MODEL, tools=["Read"], dirs=[PHOTOS, REFS])


# ---------- HTTP ----------

STATIC = ROOT / "static"  # installable-app files: manifest, service worker, icons (no secrets, served without sign-in)
STATIC_TYPES = {".webmanifest": "application/manifest+json", ".js": "text/javascript; charset=utf-8", ".png": "image/png",
                ".ico": "image/x-icon"}
PUBLIC = {"/", "/manifest.webmanifest", "/sw.js", "/favicon.ico", "/api/auth/state", "/api/auth/login/options", "/api/auth/login/verify", "/api/auth/logout",
          "/api/auth/register/options", "/api/auth/register/verify"}  # register/* check permission themselves


class H(BaseHTTPRequestHandler):
    server_version = "BoxInventory/2"

    def log_message(self, fmt, *a):
        print(time.strftime("%H:%M:%S"), self.command, self.path.split("?")[0], "-", fmt % a if a else "")

    def send(self, code, body=b"", ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k == "Cache-Control" for k, _ in headers):
            self.send_header("Cache-Control", "private, max-age=86400" if ctype == "image/jpeg" else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg, **extra):
        self.send(code, {"error": msg, **extra})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("too large")
        return self.rfile.read(n)

    def json_body(self):
        try:
            v = json.loads(self.body() or b"{}")
            return v if isinstance(v, dict) else None
        except ValueError:
            return None

    # --- sessions ---
    def session(self):
        """(token_hash, session) for a valid sign-in cookie, else (None, None)."""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                h = hashlib.sha256(v.encode()).hexdigest()
                with lock:
                    s = load_auth()["sessions"].get(h)
                if s and s["exp"] > time.time():
                    return h, s
        return None, None

    def start_session(self, a, cred_id):
        token = secrets.token_urlsafe(32)
        a["sessions"][hashlib.sha256(token.encode()).hexdigest()] = {"cred": cred_id, "exp": time.time() + SESSION_DAYS * 86400}
        return ("Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Strict")

    def origin(self):
        return (self.headers.get("Origin") or "").rstrip("/")

    def guard(self, path):
        """True if the request may proceed; otherwise sends 401/403 and returns False."""
        if (not AUTH_ON or path in PUBLIC or path.startswith(("/api/backup/", "/icons/", "/vendor/", "/app/"))  # backup routes check access themselves
                or re.fullmatch(r"/[bBtT]/\w+", path)):  # label links open the app page, which then asks to sign in
            return True
        if self.command != "GET":
            with lock:
                rp = load_auth()["rp"]
            if rp and self.origin() and self.origin() != rp["origin"]:
                self.fail(403, f"Open the app at {rp['origin']}")
                return False
        if self.session()[1]:
            return True
        self.fail(401, "Sign in first.", signIn=True)
        return False

    def dispatch(self):
        p = self.path.split("?")[0].rstrip("/") or "/"
        if not self.guard(p):
            return
        try:
            if p.startswith("/api/auth/"):
                return self.auth_route(p)
            if p.startswith("/api/backup/"):
                return self.backup_route(p)
            return self.app_route(p)
        except W.WebAuthnError as e:
            return self.fail(400, str(e) if "expired" in str(e) else "That passkey couldn't be verified. Try again.")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch

    # --- passkey sign-in ---
    def auth_route(self, p):
        cmd = self.command
        if p == "/api/auth/state" and cmd == "GET":
            with lock:
                a = load_auth()
            return self.send(200, {"auth": AUTH_ON, "loggedIn": bool(self.session()[1]),
                                   "hasPasskeys": bool(a["credentials"]), "origin": a["rp"] and a["rp"]["origin"]})
        if p == "/api/auth/logout" and cmd == "POST":
            h, _ = self.session()
            if h:
                with lock:
                    a = load_auth()
                    a["sessions"].pop(h, None)
                    save_auth(a)
            return self.send(200, {"ok": True}, headers=[("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")])

        # Passkey management: cookie-protected, and GET requests carry no Origin header.
        if p == "/api/auth/passkeys" and cmd == "GET":
            if not self.session()[1]:
                return self.fail(401, "Sign in first.", signIn=True)
            _, s = self.session()
            with lock:
                a = load_auth()
            return self.send(200, [{"id": c["id"], "name": c["name"], "created": c["created"], "lastUsed": c["lastUsed"],
                                    "current": c["id"] == s["cred"]} for c in a["credentials"]])
        if p == "/api/auth/passkeys/keys" and cmd == "GET":
            # Public keys (not secret) so a device can check a passkey itself while offline.
            _, s = self.session()
            if not s:
                return self.fail(401, "Sign in first.", signIn=True)
            with lock:
                a = load_auth()
            return self.send(200, {"rpId": a["rp"] and a["rp"]["id"], "current": s["cred"],
                                   "keys": [{"id": c["id"], "key": c["key"]} for c in a["credentials"]]})
        m = re.fullmatch(r"/api/auth/passkeys/([\w-]+)", p)
        if m and cmd in ("PATCH", "DELETE"):
            if not self.session()[1]:
                return self.fail(401, "Sign in first.", signIn=True)
            req = self.json_body() if cmd == "PATCH" else {}
            with lock:
                a = load_auth()
                c = next((c for c in a["credentials"] if c["id"] == m.group(1)), None)
                if not c:
                    return self.fail(404, "Passkey not found")
                if cmd == "PATCH":
                    c["name"] = str((req or {}).get("name") or c["name"]).strip()[:40]
                else:
                    if len(a["credentials"]) == 1:
                        return self.fail(409, "This is the only passkey. Add another one first, or you'd be locked out.")
                    a["credentials"].remove(c)
                    a["sessions"] = {k: v for k, v in a["sessions"].items() if v["cred"] != c["id"]}
                save_auth(a)
            return self.send(200, {"ok": True})
        if not AUTH_ON:
            return self.fail(404, "Sign-in is turned off on this server.")
        origin = self.origin()
        rp_id = urlsplit(origin).hostname if origin else None
        if not rp_id:
            return self.fail(400, "Open the app in a browser.")

        if p in ("/api/auth/register/options", "/api/auth/register/verify") and cmd == "POST":
            req = self.json_body()
            if req is None:
                return self.fail(400, "Bad request")
            with lock:
                a = load_auth()
                first = not a["credentials"]
                if first:
                    code = re.sub(r"\D", "", str(req.get("setupCode", "")))
                    if not secrets.compare_digest(code, re.sub(r"\D", "", setup_code())):
                        return self.fail(403, "That setup code isn't right. It's in data/setup-code.txt on the server.")
                elif not self.session()[1]:
                    return self.fail(401, "Sign in first, then add a passkey for this device.", signIn=True)
                elif origin != a["rp"]["origin"]:
                    return self.fail(403, f"Open the app at {a['rp']['origin']}")
                if p.endswith("/options"):
                    save_auth(a)  # persists the userId on first use
                    return self.send(200, {
                        "challenge": new_challenge("reg", origin),
                        "rp": {"id": rp_id, "name": "Box Inventory"},
                        "user": {"id": a["userId"], "name": "box-inventory", "displayName": "Box Inventory"},
                        "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
                        "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                                   "userVerification": "preferred"},
                        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in a["credentials"]],
                        "attestation": "none", "timeout": 180000})
                cred = req.get("credential") or {}
                ch = take_challenge(cred, "reg", origin)
                r = cred.get("response") or {}
                res = W.verify_registration(r.get("clientDataJSON", ""), r.get("attestationObject", ""), ch, origin, rp_id)
                if any(c["id"] == res["id"] for c in a["credentials"]):
                    return self.fail(409, "This passkey is already registered.")
                a["credentials"].append({"id": res["id"], "key": res["key"], "signCount": res["signCount"],
                                         "name": str(req.get("name") or "Passkey").strip()[:40] or "Passkey",
                                         "created": now_ms(), "lastUsed": now_ms()})
                headers = []
                if first:
                    a["rp"] = {"id": rp_id, "origin": origin}
                    SETUP_FILE.unlink(missing_ok=True)
                    headers.append(self.start_session(a, res["id"]))
                save_auth(a)
            return self.send(200, {"ok": True}, headers=headers)

        if p == "/api/auth/login/options" and cmd == "POST":
            with lock:
                a = load_auth()
            if not a["credentials"]:
                return self.fail(409, "No passkeys yet. Set up the app first.")
            if origin != a["rp"]["origin"]:
                return self.fail(403, f"Open the app at {a['rp']['origin']}")
            return self.send(200, {"challenge": new_challenge("login", origin), "rpId": rp_id,
                                   "allowCredentials": [], "userVerification": "preferred", "timeout": 180000})

        if p == "/api/auth/login/verify" and cmd == "POST":
            req = self.json_body() or {}
            cred = req.get("credential") or {}
            with lock:
                a = load_auth()
                if not a["rp"] or origin != a["rp"]["origin"]:
                    return self.fail(403, "Open the app at its secure address.")
                ch = take_challenge(cred, "login", origin)
                stored = next((c for c in a["credentials"] if c["id"] == cred.get("id")), None)
                if not stored:
                    return self.fail(401, "This passkey isn't registered here. Use another one, or ask for it to be added.")
                r = cred.get("response") or {}
                count = W.verify_assertion(r.get("clientDataJSON", ""), r.get("authenticatorData", ""),
                                           r.get("signature", ""), ch, origin, a["rp"]["id"], stored["key"])
                if count and stored["signCount"] and count <= stored["signCount"]:
                    return self.fail(401, "This passkey looks cloned, so it was refused.")
                stored["signCount"] = count
                stored["lastUsed"] = now_ms()
                cookie = self.start_session(a, stored["id"])
                save_auth(a)
            return self.send(200, {"ok": True, "name": stored["name"]}, headers=[cookie])

        return self.fail(404, "Not found")

    # --- backups ---
    def backup_allowed(self):
        """Signed in, or (fresh install, no passkeys yet) holding the setup code: needed to restore."""
        if not AUTH_ON:
            return True
        with lock:
            a = load_auth()
        if self.command != "GET" and a["rp"] and self.origin() and self.origin() != a["rp"]["origin"]:
            return False
        if self.session()[1]:
            return True
        code = re.sub(r"\D", "", self.headers.get("X-Setup-Code") or "")
        return not a["credentials"] and bool(code) and secrets.compare_digest(code, re.sub(r"\D", "", setup_code()))

    def backup_route(self, p):
        cmd = self.command
        if not self.backup_allowed():
            return self.fail(401, "Sign in first, or enter the setup code to restore.", signIn=True)
        req = (self.json_body() or {}) if cmd == "POST" else {}
        try:
            if p == "/api/backup/status" and cmd == "GET":
                return self.send(200, BACKUPS.status())
            if p == "/api/backup/connect" and cmd == "POST":
                return self.send(200, BACKUPS.connect_start(str(req.get("provider", ""))))
            if p == "/api/backup/connect/poll" and cmd == "POST":
                return self.send(200, BACKUPS.connect_poll())
            if p == "/api/backup/disconnect" and cmd == "POST":
                BACKUPS.disconnect(str(req.get("provider", "")))
                return self.send(200, BACKUPS.status())
            if p == "/api/backup/run" and cmd == "POST":
                BACKUPS.backup_now()
                return self.send(200, BACKUPS.status())
            if p == "/api/backup/list" and cmd == "GET":
                return self.send(200, BACKUPS.list())
            if p == "/api/backup/restore" and cmd == "POST":
                if not req.get("id") or not req.get("provider"):
                    return self.fail(400, "Pick a backup to restore.")
                BACKUPS.restore(str(req["provider"]), str(req["id"]), self.origin() or None)
                return self.send(200, BACKUPS.status())
        except BK.BackupError as e:
            return self.fail(400, str(e))
        return self.fail(404, "Not found")

    # --- inventory ---
    def app_route(self, p):
        cmd = self.command
        if (p == "/" or re.fullmatch(r"/[bBtT]/\w+", p)) and cmd == "GET":  # /b/7 and /t/… are QR label links (any case)
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/(manifest\.webmanifest|sw\.js|favicon\.ico|icons/[\w-]+\.png|(?:vendor|app)/[\w-]+\.js)", p)
        if m and cmd == "GET":
            f = STATIC / m.group(1)
            if not f.is_file():
                return self.fail(404, "Not found")
            return self.send(200, f.read_bytes(), STATIC_TYPES[f.suffix], headers=[("Cache-Control", "no-cache")])
        m = re.fullmatch(r"/photos/(\d+)/([\w-]+\.jpg)", p)
        if m and cmd == "GET":
            f = PHOTOS / m.group(1) / m.group(2)
            return self.send(200, f.read_bytes(), "image/jpeg") if f.exists() else self.fail(404, "Photo not found")
        m = re.fullmatch(r"/receipts/(\w+)\.jpg", p)
        if m and cmd == "GET":
            f = RECEIPTS / f"{m.group(1)}.jpg"
            return self.send(200, f.read_bytes(), "image/jpeg") if f.exists() else self.fail(404, "No receipt")
        if p == "/api/receipts" and cmd == "PUT":
            rid = uuid.uuid4().hex[:16]
            return self.save_jpeg(RECEIPTS / f"{rid}.jpg", dir_=RECEIPTS, extra={"id": rid})
        if p == "/api/settings" and cmd == "PATCH":
            req = self.json_body() or {}
            with lock:
                db = load()
                if str(req.get("currency", "")).upper() in CURRENCIES:
                    db["settings"]["currency"] = str(req["currency"]).upper()
                save(db)
            return self.send(200, db["settings"])
        if p == "/api/export.csv" and cmd == "GET":
            with lock:
                db = load()
            return self.send(200, export_csv(db).encode("utf-8-sig"), "text/csv; charset=utf-8",
                             headers=[("Content-Disposition", f'attachment; filename="box-inventory-{time.strftime("%Y-%m-%d")}.csv"')])
        m = re.fullmatch(r"/refs/(\w+)\.jpg", p)
        if m and cmd == "GET":
            f = REFS / f"{m.group(1)}.jpg"
            return self.send(200, f.read_bytes(), "image/jpeg") if f.exists() else self.fail(404, "No picture")
        if p.startswith("/api/things") or p == "/api/refsheet":
            return self.things_route(p)
        if p == "/api/boxes" and cmd == "GET":
            with lock:
                return self.send(200, load())
        if p == "/api/boxes" and cmd == "POST":
            req = self.json_body() or {}
            with lock:
                db = load()
                key = str(max([int(k) for k in db["boxes"]] + [0]) + 1)
                kind = "spot" if req.get("kind") == "spot" else "box"
                # Box numbers are for the labels on real boxes, so spots don't use them up.
                number = max([b["number"] or 0 for b in db["boxes"].values() if b["kind"] == "box"] + [0]) + 1 if kind == "box" else None
                room = req.get("room") if any(r["id"] == req.get("room") for r in db["rooms"]) else ""
                db["boxes"][key] = {"id": key, "kind": kind, "number": number, "name": str(req.get("name", "")).strip()[:80],
                                    "category": "", "location": "", "room": room, "setsStatus": "", "sightings": [],
                                    "items": [], "photos": [], "createdAt": now_ms(), "updatedAt": now_ms()}
                save(db)
                return self.send(200, db["boxes"][key])

        if p == "/api/rooms" and cmd == "POST":
            name = str((self.json_body() or {}).get("name", "")).strip()[:40]
            if not name:
                return self.fail(400, "Give the room a name.")
            with lock:
                db = load()
                room = next((r for r in db["rooms"] if r["name"].lower() == name.lower()), None)
                if not room:
                    room = {"id": new_id(), "name": name}
                    db["rooms"].append(room)
                    save(db)
            return self.send(200, room)
        m = re.fullmatch(r"/api/rooms/(\w+)", p)
        if m and cmd in ("PATCH", "DELETE"):
            req = self.json_body() if cmd == "PATCH" else {}
            with lock:
                db = load()
                room = next((r for r in db["rooms"] if r["id"] == m.group(1)), None)
                if not room:
                    return self.fail(404, "Room not found")
                if cmd == "PATCH":
                    name = str((req or {}).get("name", "")).strip()[:40]
                    if name:
                        room["name"] = name
                else:
                    db["rooms"].remove(room)
                    for b in db["boxes"].values():
                        if b.get("room") == room["id"]:
                            b["room"] = ""
                save(db)
            return self.send(200, {"rooms": db["rooms"]})

        if p == "/api/move" and cmd == "POST":
            return self.move_items()
        if p == "/api/ask" and cmd == "POST":
            q = str((self.json_body() or {}).get("q", "")).strip()[:300]
            if not q:
                return self.fail(400, "Ask a question.")
            with lock:
                db = load()
            try:
                res = ask(q, db)
            except subprocess.TimeoutExpired:
                return self.fail(504, "Claude took too long to answer. Try again.")
            except Exception as e:
                return self.fail(502, str(e))
            matches = []  # keep only ids that really exist
            for m in res.get("matches", []) if isinstance(res, dict) else []:
                if not isinstance(m, dict):
                    continue
                place, item, thing = (str(m.get(k) or "").lstrip("PIT") for k in ("place", "item", "thing"))
                t = db["things"].get(thing)
                if t and not place:
                    place = (t.get("lastSeen") or {}).get("place") or t.get("home") or ""
                b = db["boxes"].get(place)
                if not b and not t:
                    continue
                if item and not (b and any(i["id"] == item for i in b["items"])):
                    item = ""
                matches.append({"place": place if b else "", "item": item, "thing": thing if t else "", "why": str(m.get("why", ""))[:160]})
            return self.send(200, {"answer": str(res.get("answer", ""))[:1200], "matches": matches[:12]})
        m = re.fullmatch(r"/api/boxes/(\d+)(/photos(?:/([\w.-]+))?|/scan|/sightings)?", p)
        if not m:
            return self.fail(404, "Not found")
        key, sub, photo = m.groups()
        if sub == "/scan" and cmd == "POST":
            return self.do_scan(key)
        if sub == "/photos" and cmd == "PUT":
            return self.add_photo(key)
        if sub == "/sightings" and cmd == "POST":  # confirm or dismiss "seen here" suggestions
            req = self.json_body() or {}
            with lock:
                db = load()
                place = db["boxes"].get(key)
                if not place:
                    return self.fail(404, "Place not found")
                ids = [s["id"] for s in place["sightings"]] if req.get("all") else [str(req.get("id", ""))]
                for sid in ids:
                    TH.resolve_sighting(db, key, sid, bool(req.get("accept")))
                save(db)
                return self.send(200, {"box": place, "things": db["things"]})
        if sub is None and cmd == "PATCH":
            req = self.json_body()
            if req is None:
                return self.fail(400, "Bad request")
            with lock:
                db = load()
                box = db["boxes"].get(key)
                if not box:
                    return self.fail(404, "Box not found")
                for f in ("name", "category", "location"):
                    if f in req:
                        box[f] = str(req[f]).strip()[:80]
                if "room" in req and (req["room"] == "" or any(r["id"] == req["room"] for r in db["rooms"])):
                    box["room"] = req["room"]
                if "setsStatus" in req and req["setsStatus"] in [""] + TH.STATUSES:
                    box["setsStatus"] = req["setsStatus"]
                if "items" in req and isinstance(req["items"], list):
                    box["items"] = [x for x in (clean_item(i, box["photos"]) for i in req["items"]) if x]
                box["updatedAt"] = now_ms()
                save(db)
            return self.send(200, box)
        if cmd == "DELETE" and (sub is None or photo):
            with lock:
                db = load()
                box = db["boxes"].get(key)
                if not box:
                    return self.fail(404, "Box not found")
                if photo:
                    if photo in box["photos"]:
                        box["photos"].remove(photo)
                        (PHOTOS / key / photo).unlink(missing_ok=True)
                        for it in box["items"]:
                            if it.get("loc", {}).get("photo") == photo:
                                del it["loc"]
                        TH.photo_removed(db, key, photo)
                    box["updatedAt"] = now_ms()
                    save(db)
                    return self.send(200, box)
                del db["boxes"][key]
                TH.place_removed(db, key)
                shutil.rmtree(PHOTOS / key, ignore_errors=True)
                save(db)
            return self.send(200, {"deleted": int(key)})
        return self.fail(404, "Not found")

    def move_items(self):
        """Move items to another box or spot. Their photo crops stay behind (the photo shows the old place);
        tracked items made from them move their home and last-seen along."""
        req = self.json_body() or {}
        src, dst, ids = str(req.get("from", "")), str(req.get("to", "")), req.get("items")
        if not isinstance(ids, list) or not ids:
            return self.fail(400, "Pick the items to move.")
        with lock:
            db = load()
            a, b = db["boxes"].get(src), db["boxes"].get(dst)
            if not a or not b:
                return self.fail(404, "That box or spot doesn't exist.")
            if src == dst:
                return self.fail(400, "That's the same place.")
            moving = [i for i in a["items"] if i["id"] in ids]
            a["items"] = [i for i in a["items"] if i["id"] not in ids]
            for i in moving:
                i.pop("loc", None)
                b["items"].append(i)
                for t in db["things"].values():
                    if (t.get("from") or {}).get("place") == src and t["from"]["item"] == i["id"]:
                        t["from"]["place"] = dst
                        if t.get("home") == src:
                            t["home"] = dst
                            TH.add_history(t, what="home", place=dst)
                        TH.set_seen(db, t, dst, "manual")
            a["updatedAt"] = b["updatedAt"] = now_ms()
            save(db)
        return self.send(200, {"from": a, "to": b, "moved": len(moving), "things": db["things"]})

    def add_photo(self, key):
        try:
            data = self.body()
        except ValueError:
            return self.fail(413, "Photo is too large (max 25 MB).")
        if not data.startswith(b"\xff\xd8"):
            return self.fail(415, "Only JPEG photos are accepted.")
        with lock:
            db = load()
            box = db["boxes"].get(key)
            if not box:
                return self.fail(404, "Box not found")
            name = f"{int(time.time())}-{new_id()}.jpg"
            (PHOTOS / key).mkdir(parents=True, exist_ok=True)
            (PHOTOS / key / name).write_bytes(data)
            box["photos"].append(name)
            box["updatedAt"] = now_ms()
            save(db)
        self.send(200, {"photo": name, "box": box})

    def do_scan(self, key):
        req = self.json_body()
        if req is None:
            return self.fail(400, "Bad request")
        mode = "replace" if req.get("mode") == "replace" else "add"
        with lock:
            box = load()["boxes"].get(key)
        if not box:
            return self.fail(404, "Box not found")
        names = req.get("photos") or box["photos"]
        names = [n for n in names if n in box["photos"]][-MAX_SCAN_PHOTOS:]
        if not names:
            return self.fail(400, "This box has no photos to scan.")
        try:
            with lock:
                tracked = list(load()["things"].values())
            res = scan(box, [str(PHOTOS / key / n) for n in names], box["items"] if mode == "add" else [], tracked)
        except subprocess.TimeoutExpired:
            return self.fail(504, "The scan took too long. Try fewer photos at a time.")
        except Exception as e:
            return self.fail(502, str(e))
        found = []
        for it in res.get("items", []) if isinstance(res, dict) else []:
            if isinstance(it, dict):
                n = it.get("photo")
                if isinstance(n, int) and 1 <= n <= len(names):  # map "Photo 2" back to its file
                    it = {**it, "loc": {"photo": names[n - 1], "box": it.get("box")}}
                x = clean_item({**it, "src": "scan", "edited": False, "scanName": it.get("name")}, names)
                if x:
                    found.append(x)
        with lock:  # re-read: the box may have been edited while Claude was looking
            db = load()
            box = db["boxes"].get(key)
            if not box:
                return self.fail(404, "Box was deleted during the scan.")
            before = [dict(i) for i in box["items"]]
            if mode == "add":
                by_name = {n.lower(): i for i in box["items"] for n in (i["name"], i.get("scanName")) if n}
                added = []
                for i in found:
                    old = by_name.get(i["name"].lower())
                    if not old:
                        added.append(i)
                    elif "loc" not in old and "loc" in i:
                        old["loc"] = i["loc"]  # now we know where an earlier item is
                box["items"] = box["items"] + added
            else:
                # Keep what you added or edited (and anything tracked); refresh only untouched scanned items.
                linked = {t["from"]["item"] for t in db["things"].values() if (t.get("from") or {}).get("place") == key}
                kept = [i for i in box["items"] if i.get("src") != "scan" or i.get("edited") or i["id"] in linked]
                by_name = {n.lower(): i for i in kept for n in (i["name"], i.get("scanName")) if n}
                added = []
                for i in found:
                    old = by_name.get(i["name"].lower())
                    if old:
                        if "loc" in i:
                            old["loc"] = i["loc"]  # fresh position in the new photos
                    else:
                        added.append(i)
                box["items"] = kept + added
            if not box["name"] and res.get("suggestedName"):
                box["name"] = str(res["suggestedName"])[:60]
            if not box["category"] and res.get("category") in CATEGORIES and box["kind"] == "box":
                box["category"] = res["category"]
            seen = TH.sightings_from_scan(db, key, res.get("sightings"), names)
            box["updatedAt"] = now_ms()
            save(db)
        self.send(200, {"box": box, "added": [i["id"] for i in added], "before": before, "sightings": len(seen)})

    # --- tracked items ---
    def things_route(self, p):
        cmd = self.command
        if p == "/api/refsheet" and cmd == "PUT":  # one picture with every tracked item, made by the page
            return self.save_jpeg(REFS / "sheet.jpg")
        if p == "/api/things" and cmd == "POST":
            with lock:
                db = load()
                try:
                    t = TH.new_thing(db, self.json_body() or {})
                except ValueError as e:
                    return self.fail(400, str(e))
                save(db)
            return self.send(200, t)
        m = re.fullmatch(r"/api/things/(\w+)(/ref)?", p)
        if not m:
            return self.fail(404, "Not found")
        tid, ref = m.groups()
        if ref and cmd == "PUT":
            r = self.save_jpeg(REFS / f"{tid}.jpg", check=lambda db: tid in db["things"])
            with lock:
                db = load()
                if tid in db["things"] and (REFS / f"{tid}.jpg").exists():
                    db["things"][tid]["hasRef"] = True
                    db["things"][tid]["refAt"] = now_ms()
                    save(db)
            return r
        with lock:
            db = load()
            t = db["things"].get(tid)
            if not t:
                return self.fail(404, "Item not found")
            if cmd == "PATCH":
                TH.patch_thing(db, t, self.json_body() or {})
                save(db)
                return self.send(200, t)
            if cmd == "DELETE":
                del db["things"][tid]
                for b in db["boxes"].values():
                    b["sightings"] = [s for s in b.get("sightings", []) if s["thing"] != tid]
                (REFS / f"{tid}.jpg").unlink(missing_ok=True)
                save(db)
                return self.send(200, {"deleted": tid})
        return self.fail(404, "Not found")

    def save_jpeg(self, path, check=None, dir_=None, extra=None):
        try:
            data = self.body()
        except ValueError:
            return self.fail(413, "Picture is too large.")
        if not data.startswith(b"\xff\xd8"):
            return self.fail(415, "Only JPEG pictures are accepted.")
        with lock:
            if check and not check(load()):
                return self.fail(404, "Item not found")
            (dir_ or REFS).mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return self.send(200, {"ok": True, **(extra or {})})


if __name__ == "__main__":
    if "--log" in sys.argv:  # for windowless runs (Windows pythonw), where there is no console to print to
        sys.stdout = sys.stderr = open(sys.argv[sys.argv.index("--log") + 1], "a", buffering=1, encoding="utf-8")
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    PHOTOS.mkdir(parents=True, exist_ok=True)
    REFS.mkdir(parents=True, exist_ok=True)
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    c = find_claude()
    claude_auth = ("token from .claude-token" if TOKEN_FILE.exists() and TOKEN_FILE.read_text().strip() else "token from CLAUDE_CODE_OAUTH_TOKEN") if oauth_token() else "this computer's Claude login"
    print(f"Box Inventory on http://0.0.0.0:{PORT}  (claude: {c or 'NOT FOUND - scans will fail'}; auth: {claude_auth})", flush=True)
    if AUTH_ON and not load_auth()["credentials"]:
        print(f"First-time setup code for your first passkey: {setup_code()}  (also in data/setup-code.txt)", flush=True)
    elif not AUTH_ON:
        print("Sign-in is OFF (AUTH=off): anyone who can reach this port can use the app.", flush=True)
    if BACKUPS.available():
        print("Backups available to: " + ", ".join(a["label"] for a in BACKUPS.available()), flush=True)
    threading.Thread(target=BACKUPS.scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
