"""Tracked items: things with a home place, a status, and where they were last seen.

A "place" is any record in db["boxes"]: a numbered box, or a spot such as a laundry basket or a
wardrobe shelf. A thing is either a single item (a hoodie) or a counted group of look-alikes
(12 black socks) whose counts are kept per place, so the ones nobody has seen stand out.
"""
import time
import uuid

STATUSES = ["clean", "in_use", "to_wash", "washing", "drying", "lent", "missing"]
MAX_HISTORY = 40


def now_ms():
    return int(time.time() * 1000)


def _place_ok(db, key):
    return key == "" or key in db["boxes"]


def add_history(t, **event):
    t["history"] = ([{"t": now_ms(), **event}] + t.get("history", []))[:MAX_HISTORY]


def set_status(t, status, source):
    if status in STATUSES and status != t.get("status"):
        t["status"], t["statusAt"] = status, now_ms()
        add_history(t, what="status", status=status, source=source)


def set_seen(db, t, place, source, photo=None, box=None, count=None):
    """Record a sighting (manual or confirmed from a photo). The place may set the status, e.g.
    anything seen in the laundry basket becomes "to wash"."""
    seen = {"place": place, "time": now_ms(), "source": source}
    if photo and box:
        seen.update(photo=photo, box=box)
    t["lastSeen"] = seen
    if t["kind"] == "group" and count is not None:
        t.setdefault("counts", {})[place] = max(0, min(int(count), 999))
    add_history(t, what="seen", place=place, source=source, **({"count": count} if count is not None else {}))
    rule = (db["boxes"].get(place) or {}).get("setsStatus")
    if rule:
        set_status(t, rule, "place")


def new_thing(db, req):
    """req: name, kind (single|group), total, note, home, status, fromItem {place, item}."""
    name = str(req.get("name", "")).strip()[:80]
    src = req.get("fromItem") or {}
    item = None
    if src:
        place = db["boxes"].get(str(src.get("place", "")))
        item = next((i for i in (place or {}).get("items", []) if i["id"] == src.get("item")), None)
        if not item:
            raise ValueError("That item no longer exists.")
        name = name or item["name"]
    if not name:
        raise ValueError("Give the item a name.")
    kind = "group" if req.get("kind") == "group" else "single"
    try:
        total = max(1, min(int(req.get("total") or (item or {}).get("qty") or 1), 999))
    except (TypeError, ValueError):
        total = 1
    home = str(req.get("home", "") if "home" in req else (src.get("place", "") if item else ""))
    if not _place_ok(db, home):
        home = ""
    codes = [int(t["code"][1:]) for t in db["things"].values() if t.get("code", "T0")[1:].isdigit()]
    t = {"id": uuid.uuid4().hex[:10], "code": f"T{max(codes + [0]) + 1}", "name": name, "kind": kind,
         "total": total if kind == "group" else 1, "note": str(req.get("note") or (item or {}).get("note", ""))[:160],
         "home": home, "status": req.get("status") if req.get("status") in STATUSES else "clean",
         "statusAt": now_ms(), "lastSeen": None, "counts": {}, "hasRef": False, "createdAt": now_ms(), "history": []}
    add_history(t, what="created")
    if item:
        t["from"] = {"place": str(src["place"]), "item": item["id"]}
        loc = item.get("loc") or {}
        set_seen(db, t, str(src["place"]), "manual", loc.get("photo"), loc.get("box"),
                 count=t["total"] if kind == "group" else None)
    db["things"][t["id"]] = t
    return t


def patch_thing(db, t, req):
    if "name" in req and str(req["name"]).strip():
        t["name"] = str(req["name"]).strip()[:80]
    if "note" in req:
        t["note"] = str(req["note"]).strip()[:160]
    if "total" in req and t["kind"] == "group":
        try:
            t["total"] = max(1, min(int(req["total"]), 999))
        except (TypeError, ValueError):
            pass
    if "home" in req and _place_ok(db, str(req["home"])) and req["home"] != t["home"]:
        t["home"] = str(req["home"])
        add_history(t, what="home", place=t["home"])
    if "seen" in req and str(req["seen"]) in db["boxes"]:  # "I put it here" / "it's here now"
        set_seen(db, t, str(req["seen"]), "manual", count=req.get("count"))
    if "counts" in req and t["kind"] == "group" and isinstance(req["counts"], dict):
        for place, n in req["counts"].items():
            if place in db["boxes"]:
                try:
                    n = max(0, min(int(n), 999))
                except (TypeError, ValueError):
                    continue
                if n:
                    t["counts"][place] = n
                else:
                    t["counts"].pop(place, None)
    if "status" in req:
        set_status(t, req["status"], "manual")
    if req.get("backHome") and t["home"]:
        set_seen(db, t, t["home"], "manual", count=t["total"] if t["kind"] == "group" else None)
        if not (db["boxes"].get(t["home"]) or {}).get("setsStatus"):
            set_status(t, "clean", "manual")  # putting it away usually means it's clean


def place_removed(db, key):
    for t in db["things"].values():
        if t["home"] == key:
            t["home"] = ""
        t.get("counts", {}).pop(key, None)
        if (t.get("lastSeen") or {}).get("place") == key:
            t["lastSeen"] = {**t["lastSeen"], "place": "", "photo": None, "box": None}


def photo_removed(db, key, photo):
    place = db["boxes"].get(key) or {}
    place["sightings"] = [s for s in place.get("sightings", []) if s["photo"] != photo]
    for t in db["things"].values():
        ls = t.get("lastSeen") or {}
        if ls.get("place") == key and ls.get("photo") == photo:
            ls.pop("photo", None)
            ls.pop("box", None)


def sightings_from_scan(db, key, found, photo_names):
    """found: [{code, photo (1-based), box, count, confidence}] from Claude. Stored as suggestions to confirm."""
    place = db["boxes"][key]
    by_code = {t["code"]: t for t in db["things"].values()}
    new = []
    for s in found if isinstance(found, list) else []:
        t = by_code.get(str((s or {}).get("code", "")).upper())
        n = s.get("photo") if isinstance(s, dict) else None
        if not t or not isinstance(n, int) or not 1 <= n <= len(photo_names):
            continue
        try:
            box = [max(0, min(1000, int(v))) for v in s.get("box", [])]
        except (TypeError, ValueError):
            box = []
        if len(box) != 4 or box[2] - box[0] < 5 or box[3] - box[1] < 5:
            box = None
        conf = s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "medium"
        try:
            count = max(1, min(int(s.get("count") or 1), 999))
        except (TypeError, ValueError):
            count = 1
        new.append({"id": uuid.uuid4().hex[:8], "thing": t["id"], "photo": photo_names[n - 1], "box": box,
                    "count": count, "confidence": conf, "time": now_ms()})
    seen_now = {s["thing"] for s in new}  # a newer photo replaces older open suggestions for the same thing
    place["sightings"] = ([s for s in place.get("sightings", []) if s["thing"] not in seen_now] + new)[-40:]
    return new


def resolve_sighting(db, key, sid, accept):
    place = db["boxes"][key]
    s = next((s for s in place.get("sightings", []) if s["id"] == sid), None)
    if not s:
        return False
    place["sightings"].remove(s)
    t = db["things"].get(s["thing"])
    if accept and t:
        set_seen(db, t, key, "photo", s["photo"], s["box"], count=s["count"] if t["kind"] == "group" else None)
    return True
