// Box Inventory offline copy.
//
// When turned on for a device, the inventory and smaller copies of the photos are kept on the device,
// encrypted, so they can be browsed without a connection. Changes made offline wait in an encrypted
// outbox and are sent when the server is reachable again.
//
// Encryption: each device has its own ECDH key pair. Anything saved is encrypted to the public key
// (so syncing never needs a fingerprint); reading needs the private key, which is:
//   - "passkey" mode: wrapped with a key derived from the passkey's PRF output, so it only exists
//     after the passkey's fingerprint/face check (where the browser and passkey support PRF);
//   - "device" mode: a non-extractable key bound to this browser, plus a passkey check the page
//     verifies itself before showing anything.
// Loaded after the main script; uses its globals (S, api helpers, render functions, b64e/b64d…).
const Offline = (() => {
  const DB_NAME = "box-offline", MAX_DAYS = 30, THUMB = 800;
  const ECDH = { name: "ECDH", namedCurve: "P-256" };
  const enc = new TextEncoder(), dec = new TextDecoder();
  const urls = new Map();       // "p/<place>/<photo>" or "r/<thing>" -> object URL while unlocked offline
  let priv = null;              // private key while unlocked (memory only)
  let syncing = false;

  /* ---- IndexedDB: meta (settings and keys), data (encrypted copies), outbox (encrypted changes) ---- */
  const req = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const openDb = () => new Promise((res, rej) => {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = () => { for (const s of ["meta", "data", "outbox"]) r.result.createObjectStore(s); };
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  async function run(store, mode, fn) {
    const db = await openDb();
    try { return await req(fn(db.transaction(store, mode).objectStore(store))); } finally { db.close(); }
  }
  const get = (store, key) => run(store, "readonly", (s) => s.get(key));
  const put = (store, key, val) => run(store, "readwrite", (s) => s.put(val, key));
  const del = (store, key) => run(store, "readwrite", (s) => s.delete(key));
  const allKeys = (store) => run(store, "readonly", (s) => s.getAllKeys());
  const wipe = () => new Promise((res) => { const r = indexedDB.deleteDatabase(DB_NAME); r.onsuccess = r.onerror = r.onblocked = () => res(); });
  const meta = () => get("meta", "meta").catch(() => null);

  /* ---- encryption ---- */
  async function seal(pub, bytes) {
    const eph = await crypto.subtle.generateKey(ECDH, true, ["deriveKey"]);
    const k = await crypto.subtle.deriveKey({ name: "ECDH", public: pub }, eph.privateKey, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    return { epk: await crypto.subtle.exportKey("raw", eph.publicKey), iv, ct: await crypto.subtle.encrypt({ name: "AES-GCM", iv }, k, bytes) };
  }
  async function unseal(box) {
    const epk = await crypto.subtle.importKey("raw", box.epk, ECDH, false, []);
    const k = await crypto.subtle.deriveKey({ name: "ECDH", public: epk }, priv, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv: box.iv }, k, box.ct));
  }
  const sealJson = async (pub, obj) => seal(pub, enc.encode(JSON.stringify(obj)));
  const unsealJson = async (box) => JSON.parse(dec.decode(await unseal(box)));
  async function kekFromPrf(prf) {
    const base = await crypto.subtle.importKey("raw", prf, "HKDF", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: enc.encode("box-inventory offline v1"), info: enc.encode("wrap") },
      base, { name: "AES-GCM", length: 256 }, false, ["wrapKey", "unwrapKey"]);
  }

  /* ---- checking a passkey without the server (device mode) ---- */
  function derToRaw(der) {  // ECDSA signature: DER SEQUENCE{r,s} -> 64-byte r||s for WebCrypto
    const d = new Uint8Array(der); let i = 2;
    const int = () => { if (d[i++] !== 2) throw new Error("bad signature"); let n = d[i++]; let v = d.slice(i, i + n); i += n;
      while (v.length > 32 && v[0] === 0) v = v.slice(1); const out = new Uint8Array(32); out.set(v, 32 - v.length); return out; };
    if (d[0] !== 0x30) throw new Error("bad signature");
    if (d[1] & 0x80) i = 2 + (d[1] & 0x7f);
    const r = int(), s = int(), raw = new Uint8Array(64); raw.set(r); raw.set(s, 32); return raw;
  }
  async function verifyAssertion(cred, challenge, rpId, keys) {
    const r = cred.response, cd = JSON.parse(dec.decode(r.clientDataJSON));
    if (cd.type !== "webauthn.get" || cd.challenge !== b64e(challenge) || cd.origin !== location.origin) throw new Error("That passkey response doesn't match.");
    const ad = new Uint8Array(r.authenticatorData);
    const rpHash = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(rpId)));
    if (rpHash.some((b, i) => b !== ad[i])) throw new Error("That passkey belongs to another site.");
    if (!(ad[32] & 0x01) || !(ad[32] & 0x04)) throw new Error("The passkey needs your fingerprint, face or screen lock.");
    const k = keys.find((x) => x.id === cred.id);
    if (!k) throw new Error("That passkey isn't one of this household's.");
    const signed = new Uint8Array(ad.length + 32); signed.set(ad);
    signed.set(new Uint8Array(await crypto.subtle.digest("SHA-256", r.clientDataJSON)), ad.length);
    let ok;
    if (k.key.alg === -7) {
      const pub = await crypto.subtle.importKey("jwk", { kty: "EC", crv: "P-256", x: k.key.x, y: k.key.y }, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
      ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, pub, derToRaw(r.signature), signed);
    } else {
      const pub = await crypto.subtle.importKey("jwk", { kty: "RSA", n: k.key.n, e: k.key.e }, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
      ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", pub, r.signature, signed);
    }
    if (!ok) throw new Error("The passkey check failed.");
  }
  const passkeyGet = (opts) => navigator.credentials.get({ publicKey: { timeout: 120000, userVerification: "required", ...opts } });

  /* ---- turning it on ---- */
  async function enable() {
    const info = await api("GET", "/api/auth/passkeys/keys");
    const challenge = crypto.getRandomValues(new Uint8Array(32)), salt = crypto.getRandomValues(new Uint8Array(32));
    const cred = await passkeyGet({ challenge, rpId: info.rpId, allowCredentials: info.keys.map((k) => ({ type: "public-key", id: b64d(k.id) })),
      extensions: { prf: { eval: { first: salt } } } });
    const prf = cred.getClientExtensionResults().prf?.results?.first;
    let m;
    if (prf) {
      const pair = await crypto.subtle.generateKey(ECDH, true, ["deriveKey"]);
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const wrapped = await crypto.subtle.wrapKey("pkcs8", pair.privateKey, await kekFromPrf(prf), { name: "AES-GCM", iv });
      m = { mode: "passkey", pub: pair.publicKey, wrapped, iv, salt, credId: cred.id };
      priv = pair.privateKey;
    } else {
      await verifyAssertion(cred, challenge, info.rpId, info.keys);
      const pair = await crypto.subtle.generateKey(ECDH, false, ["deriveKey"]);  // private key can never be read out
      m = { mode: "device", pub: pair.publicKey, priv: pair.privateKey };
      priv = pair.privateKey;
    }
    await put("meta", "meta", { ...m, v: 1, origin: location.origin, rpId: info.rpId, keys: info.keys, lastSync: 0, enabledAt: Date.now() });
    await syncDown();
  }
  async function disable() { revokeUrls(); priv = null; await wipe(); }

  /* ---- keeping the copy up to date (online) ---- */
  async function shrinkBlob(blob, max) {
    const bmp = await createImageBitmap(blob);
    const s = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas"); c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height); bmp.close?.();
    return new Promise((r) => c.toBlob(r, "image/jpeg", 0.8));
  }
  async function syncDown() {
    const m = await meta(); if (!m || S.offline || syncing) return;
    syncing = true;
    try {
      const info = await api("GET", "/api/auth/passkeys/keys").catch(() => null);
      if (info) {
        if (m.mode === "passkey" && !info.keys.some((k) => k.id === m.credId)) {  // its passkey was removed: the copy goes too
          await disable(); return note("The passkey this device's offline copy was tied to was removed, so the copy was deleted.");
        }
        m.keys = info.keys;
      }
      const snapshot = { boxes: Object.fromEntries(S.boxes), rooms: S.rooms, things: S.things, at: Date.now() };
      await put("data", "inventory", await sealJson(m.pub, snapshot));
      const want = new Map();
      for (const b of S.boxes.values()) for (const p of b.photos || []) want.set(`p/${b.id}/${p}`, `/photos/${b.id}/${p}`);
      for (const t of Object.values(S.things)) if (t.hasRef) want.set(`r/${t.id}/${t.refAt || 0}`, `/refs/${t.id}.jpg?v=${t.refAt || 0}`);
      const have = new Set((await allKeys("data")).filter((k) => k !== "inventory"));
      for (const k of have) if (!want.has(k)) await del("data", k);
      for (const [k, src] of want) {
        if (have.has(k)) continue;
        const r = await fetch(src, { credentials: "same-origin" }); if (!r.ok) continue;
        const small = await shrinkBlob(await r.blob(), THUMB);
        await put("data", k, await seal(m.pub, new Uint8Array(await small.arrayBuffer())));
      }
      m.lastSync = Date.now(); m.photos = want.size;
      await put("meta", "meta", m);
      if (S.view === "settings") renderSettings();
    } catch (e) { console.warn("offline copy:", e); } finally { syncing = false; }
  }

  /* ---- unlocking and using the copy (offline) ---- */
  async function unlock() {
    const m = await meta(); if (!m) throw new Error("There's no offline copy on this device.");
    if (m.lastSync && Date.now() - m.lastSync > MAX_DAYS * 86400000) {
      await disable(); throw new Error(`The offline copy was more than ${MAX_DAYS} days old, so it was deleted. Connect to the server to use the app.`);
    }
    const challenge = crypto.getRandomValues(new Uint8Array(32));
    if (m.mode === "passkey") {
      const cred = await passkeyGet({ challenge, rpId: m.rpId, allowCredentials: [{ type: "public-key", id: b64d(m.credId) }], extensions: { prf: { eval: { first: m.salt } } } });
      const prf = cred.getClientExtensionResults().prf?.results?.first;
      if (!prf) throw new Error("This passkey can't unlock the offline copy here. Use the device and passkey it was set up with.");
      try { priv = await crypto.subtle.unwrapKey("pkcs8", m.wrapped, await kekFromPrf(prf), { name: "AES-GCM", iv: m.iv }, ECDH, false, ["deriveKey"]); }
      catch { throw new Error("That passkey couldn't unlock the offline copy."); }
    } else {
      const cred = await passkeyGet({ challenge, rpId: m.rpId, allowCredentials: m.keys.map((k) => ({ type: "public-key", id: b64d(k.id) })) });
      await verifyAssertion(cred, challenge, m.rpId, m.keys);
      priv = m.priv;
    }
    const snap = await unsealJson(await get("data", "inventory"));
    S.boxes = new Map(Object.entries(snap.boxes)); S.rooms = snap.rooms; S.things = snap.things; S.loaded = true;
    await applyOutbox();  // show changes made in an earlier offline session too
    revokeUrls();
    for (const k of await allKeys("data")) {
      if (k === "inventory") continue;
      const bytes = await unseal(await get("data", k)).catch(() => null);
      if (bytes) urls.set(k.startsWith("r/") ? k.split("/").slice(0, 2).join("/") : k, URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" })));
    }
    S.offline = true; S.offlineAt = snap.at;
  }
  function revokeUrls() { for (const u of urls.values()) URL.revokeObjectURL(u); urls.clear(); }
  const photoUrl = (placeId, name) => urls.get(`p/${placeId}/${name}`) || "";
  const refUrl = (thingId) => urls.get(`r/${thingId}`) || "";

  /* ---- changes made offline ---- */
  // The outbox holds numbered changes ("00000001"…) plus the photos they refer to ("photo:<name>").
  const opKeys = async () => (await allKeys("outbox").catch(() => [])).filter((k) => !String(k).startsWith("photo:")).sort();
  const isNetwork = (e) => !!e && (e.network || (e instanceof TypeError && /fetch|network|load failed/i.test(e.message)));
  let seq = 0;
  async function queue(op) {
    const m = await meta();
    seq = Math.max(seq, ...(await opKeys()).map(Number), 0) + 1;
    await put("outbox", String(seq).padStart(8, "0"), await sealJson(m.pub, op));
    banner();
  }
  async function readOutbox() {
    const out = [];
    for (const k of await opKeys()) out.push({ key: k, op: await unsealJson(await get("outbox", k)) });
    return out;
  }
  const pendingCount = async () => (await opKeys()).length;
  const offlineError = (msg) => Object.assign(new Error(msg), { offline: true });

  // Apply one change to the local copy (also used to replay the outbox after unlocking).
  function applyLocal(op) {
    const b = S.boxes.get(op.box);
    if (op.t === "boxField" && b) b[op.field] = op.value;
    if (op.t === "itemAdd" && b && !b.items.some((i) => i.id === op.item.id)) b.items.push(op.item);
    if (op.t === "itemDel" && b) b.items = b.items.filter((i) => i.id !== op.id);
    if (op.t === "itemEdit" && b) { const it = b.items.find((i) => i.id === op.id); if (it) Object.assign(it, op.fields); }
    if (op.t === "move") {
      const a = S.boxes.get(op.from), to = S.boxes.get(op.to);
      if (a && to) { const moving = a.items.filter((i) => op.items.includes(i.id)); a.items = a.items.filter((i) => !op.items.includes(i.id));
        moving.forEach((i) => { delete i.loc; to.items.push(i); }); }
    }
    if (op.t === "thing") {
      const t = S.things[op.id]; if (!t) return;
      const body = op.body, now = op.at;
      if (body.name) t.name = body.name;
      if ("note" in body) t.note = body.note;
      if ("home" in body) t.home = body.home;
      if (body.status && STATUS[body.status]) { t.status = body.status; t.statusAt = now; }
      const seenAt = body.backHome ? t.home : body.seen;
      if (seenAt && S.boxes.get(seenAt)) {
        t.lastSeen = { place: seenAt, time: now, source: "manual" };
        const rule = S.boxes.get(seenAt).setsStatus;
        if (rule) t.status = rule; else if (body.backHome) t.status = "clean";
      }
      if (body.counts) for (const [k, n] of Object.entries(body.counts)) { if (n > 0) t.counts[k] = n; else delete t.counts[k]; }
    }
    if (op.t === "photo" && b && !b.photos.includes(op.tmp)) b.photos.push(op.tmp);
  }
  async function applyOutbox() {
    for (const { op } of await readOutbox()) {
      applyLocal(op);
      if (op.t === "photo") {
        const blob = await get("outbox", "photo:" + op.tmp).then((x) => x && unseal(x)).catch(() => null);
        if (blob) urls.set(`p/${op.box}/${op.tmp}`, URL.createObjectURL(new Blob([blob], { type: "image/jpeg" })));
      }
    }
  }

  // The page's server calls while offline: reads come from the copy, supported changes are queued.
  async function offlineApi(method, path, body) {
    if (method === "GET" && path === "/api/boxes") return { boxes: Object.fromEntries(S.boxes), rooms: S.rooms, things: S.things };
    let m;
    if ((m = path.match(/^\/api\/boxes\/(\d+)$/)) && method === "PATCH") {
      const b = S.boxes.get(m[1]); if (!b) throw offlineError("That box isn't in the offline copy.");
      const ops = [];
      for (const f of ["name", "category", "location", "room", "setsStatus"]) if (f in body && body[f] !== b[f]) ops.push({ t: "boxField", box: b.id, field: f, base: b[f] ?? "", value: body[f] });
      if (Array.isArray(body.items)) {
        const old = new Map(b.items.map((i) => [i.id, i])), now = new Map(body.items.map((i) => [i.id, i]));
        for (const i of body.items) {
          const o = old.get(i.id);
          if (!o) ops.push({ t: "itemAdd", box: b.id, item: i });
          else if (o.name !== i.name || o.qty !== i.qty) ops.push({ t: "itemEdit", box: b.id, id: i.id, base: { name: o.name, qty: o.qty }, fields: { name: i.name, qty: i.qty, edited: true } });
        }
        for (const id of old.keys()) if (!now.has(id)) ops.push({ t: "itemDel", box: b.id, id });
      }
      for (const op of ops) { op.at = Date.now(); applyLocal(op); await queue(op); }
      b.updatedAt = Date.now();
      return b;
    }
    if ((m = path.match(/^\/api\/things\/(\w+)$/)) && method === "PATCH") {
      if (!S.things[m[1]]) throw offlineError("That item isn't in the offline copy.");
      const op = { t: "thing", id: m[1], body, at: Date.now() };
      applyLocal(op); await queue(op); return S.things[m[1]];
    }
    if (path === "/api/move" && method === "POST") {
      const op = { t: "move", from: body.from, to: body.to, items: body.items, at: Date.now() };
      applyLocal(op); await queue(op);
      return { from: S.boxes.get(body.from), to: S.boxes.get(body.to), moved: body.items.length, things: S.things };
    }
    if ((m = path.match(/^\/api\/boxes\/(\d+)\/photos$/)) && method === "PUT") {
      const b = S.boxes.get(m[1]); if (!b) throw offlineError("That box isn't in the offline copy.");
      const meta_ = await meta(), tmp = `offline-${Date.now()}-${Math.random().toString(36).slice(2, 6)}.jpg`;
      const bytes = new Uint8Array(await body.arrayBuffer());
      await put("outbox", "photo:" + tmp, await seal(meta_.pub, bytes));
      urls.set(`p/${b.id}/${tmp}`, URL.createObjectURL(body));
      const op = { t: "photo", box: b.id, tmp, at: Date.now() };
      applyLocal(op); await queue(op);
      return { photo: tmp, box: b };
    }
    if ((m = path.match(/^\/api\/boxes\/(\d+)\/scan$/)) && method === "POST") {
      await queue({ t: "scan", box: m[1], photos: body.photos || null, mode: body.mode, at: Date.now() });
      throw Object.assign(new Error("Saved. Claude will scan it when you're back online."), { offline: true, queued: true });
    }
    throw offlineError("That needs a connection to the server. You're using the offline copy.");
  }

  /* ---- sending offline changes when the server is back ---- */
  async function syncUp() {
    if (!(await pendingCount())) return { sent: 0, conflicts: [] };
    if (!priv) { const m = await meta(); if (m?.mode === "device") priv = m.priv; else throw new Error("Unlock the offline copy to send its changes."); }
    const live = await realApi("GET", "/api/boxes");  // the server's current state, to merge into
    const boxes = live.boxes, names = {}, conflicts = [];
    let sent = 0;
    for (const { key, op } of await readOutbox()) {
      const b = boxes[op.box];
      try {
        if (op.t === "boxField" && b) {
          if (b[op.field] === op.base || b[op.field] === op.value) Object.assign(boxes[op.box], await realApi("PATCH", `/api/boxes/${op.box}`, { [op.field]: op.value }));
          else conflicts.push({ kind: "field", box: op.box, field: op.field, mine: op.value, theirs: b[op.field] });
        }
        if ((op.t === "itemAdd" || op.t === "itemDel" || op.t === "itemEdit") && b) {
          let items = b.items.map((i) => ({ ...i }));
          if (op.t === "itemAdd" && !items.some((i) => i.id === op.item.id)) items.push(op.item);
          if (op.t === "itemDel") items = items.filter((i) => i.id !== op.id);
          if (op.t === "itemEdit") {
            const it = items.find((i) => i.id === op.id);
            if (it) for (const f of ["name", "qty"]) {
              if (it[f] === op.base[f] || it[f] === op.fields[f]) { it[f] = op.fields[f]; it.edited = true; }
              else conflicts.push({ kind: "item", box: op.box, id: op.id, field: f, mine: op.fields[f], theirs: it[f] });
            }
          }
          Object.assign(boxes[op.box], await realApi("PATCH", `/api/boxes/${op.box}`, { items }));
        }
        if (op.t === "thing") await realApi("PATCH", `/api/things/${op.id}`, op.body).catch(() => {});
        if (op.t === "move" && boxes[op.from] && boxes[op.to]) {
          const r = await realApi("POST", "/api/move", { from: op.from, to: op.to, items: op.items }).catch(() => null);
          if (r) { boxes[op.from] = r.from; boxes[op.to] = r.to; }
        }
        if (op.t === "photo" && b) {
          const sealed = await get("outbox", "photo:" + op.tmp);
          if (sealed) {
            const r = await realApi("PUT", `/api/boxes/${op.box}/photos`, new Blob([await unseal(sealed)], { type: "image/jpeg" }), "image/jpeg");
            names[op.tmp] = r.photo; boxes[op.box] = r.box;
            await del("outbox", "photo:" + op.tmp);
          }
        }
        if (op.t === "scan" && b) {
          const photos = op.photos ? op.photos.map((p) => names[p] || p).filter((p) => !p.startsWith("offline-")) : null;
          const r = await realApi("POST", `/api/boxes/${op.box}/scan`, { photos, mode: op.mode }).catch(() => null);
          if (r) boxes[op.box] = r.box;
        }
        await del("outbox", key); sent++;
      } catch (e) {
        if (isNetwork(e)) throw e;  // the connection dropped again: keep the rest for later
        console.warn("offline change failed:", op, e); await del("outbox", key);
      }
    }
    return { sent, conflicts };
  }

  /* ---- what the page shows ---- */
  async function banner() {
    const el = $("#offbar"); if (!el) return;
    if (!S.offline) { el.innerHTML = ""; return; }
    const n = await pendingCount();
    el.innerHTML = `<div class="banner"><span class="msg"><b>Offline.</b> Showing this device's encrypted copy from ${fmtWhen(S.offlineAt)}.
      ${n ? `${n} change${n === 1 ? "" : "s"} waiting to sync.` : ""}</span><button class="btn ghost" data-reconnect>Reconnect</button></div>`;
  }
  function note(msg) { $("#banner").innerHTML = `<div class="banner"><span class="msg">${esc(msg)}</span><button class="linkbtn" data-bclose>Close</button></div>`; }
  function showConflicts(list) {
    if (!list.length) return;
    S.conflicts = list;
    $("#banner").innerHTML = `<div class="banner" style="display:grid;gap:8px"><span class="msg"><b>Changed on another device too.</b> Pick which to keep:</span>
      ${list.map((c, i) => { const b = S.boxes.get(c.box); const what = c.kind === "field" ? `${placeShort(b)} ${c.field}` : `${placeShort(b)}: item ${c.field}`;
        return `<div class="btnrow"><span style="flex:1;min-width:0">${esc(what)}: yours <b>${esc(c.mine)}</b> · other device <b>${esc(c.theirs)}</b></span>
          <button class="btn ghost" data-keepmine="${i}">Keep yours</button><button class="linkbtn" data-keeptheirs="${i}">Keep theirs</button></div>`; }).join("")}</div>`;
  }
  document.addEventListener("click", async (e) => {
    const mine = e.target.closest("[data-keepmine]"), theirs = e.target.closest("[data-keeptheirs]");
    if (mine || theirs) {
      const c = S.conflicts[Number((mine || theirs).dataset.keepmine ?? (mine || theirs).dataset.keeptheirs)];
      if (mine && c) {
        if (c.kind === "field") keep(await api("PATCH", `/api/boxes/${c.box}`, { [c.field]: c.mine }));
        else { const b = S.boxes.get(c.box); keep(await api("PATCH", `/api/boxes/${c.box}`, { items: b.items.map((i) => i.id === c.id ? { ...i, [c.field]: c.mine, edited: true } : i) })); }
      }
      (mine || theirs).closest(".btnrow").remove();
      if (!$("#banner [data-keepmine]")) $("#banner").innerHTML = "";
    }
    if (e.target.closest("[data-reconnect]")) reconnect();
    if (e.target.closest("[data-gooffline]")) showUnlock();
  });

  // Settings → Offline
  async function section() {
    const m = await meta(), supported = !!(window.indexedDB && crypto.subtle && window.PublicKeyCredential && window.isSecureContext);
    if (!S.auth?.auth) return `<p class="hint">The offline copy is locked with a passkey, so it needs sign-in to be turned on.</p>`;
    if (!supported) return `<p class="hint">An offline copy needs the app's secure (https) address and a browser with passkeys.</p>`;
    if (!m) return `<p>Keep an encrypted copy of the inventory and photos on this device, to look things up without a connection.
      Changes you make offline sync when you're back.</p>
      <div class="btnrow"><button class="btn ghost" data-offon>Keep an offline copy on this device</button></div><p class="err" id="off-err"></p>`;
    const n = await pendingCount();
    return `<p><b>On.</b> ${m.lastSync ? `Updated ${fmtWhen(m.lastSync)}, ${m.photos || 0} photos.` : "Preparing…"}${n ? ` ${n} offline change${n === 1 ? "" : "s"} waiting.` : ""}</p>
      <p class="hint">${m.mode === "passkey" ? "Locked with your passkey: it can only be read after your fingerprint, face or screen lock."
        : "Locked to this browser with a key that can't be copied off the device, and opened with a passkey check. (Your browser or passkey doesn't support the stronger passkey encryption.)"}
        It's deleted automatically after ${MAX_DAYS} days without updating.</p>
      <div class="btnrow"><button class="btn ghost" data-offsync>Update now</button><button class="btn ghost danger" data-offoff>Turn off and delete the copy</button></div><p class="err" id="off-err"></p>`;
  }
  document.addEventListener("click", async (e) => {
    const err = (m) => { const x = $("#off-err"); if (x) x.textContent = m; };
    if (e.target.closest("[data-offon]")) { try { await enable(); renderSettings(); } catch (x) { err(passkeyError(x)); } }
    if (e.target.closest("[data-offsync]")) { await syncDown(); renderSettings(); }
    const off = e.target.closest("[data-offoff]");
    if (off) { if (armed !== off) return arm(off, "Tap again to delete it"); disarm(); await disable(); renderSettings(); }
  });

  /* ---- entering and leaving offline mode ---- */
  async function showUnlock(msg = "") {
    show("auth");
    const m = await meta();
    $("#authview").innerHTML = m ? `<div class="authcard"><h2>You're offline</h2>
        <p>The server can't be reached. This device has an encrypted copy from ${fmtWhen(m.lastSync)}.</p>
        <button class="btn tape" id="offunlock">Unlock with passkey</button>
        <p class="hint">Changes you make offline are sent when the server is reachable again. Use this device's own passkey: the QR-code option needs a connection.</p>
        <button class="linkbtn" data-reconnect>Try the server again</button><p class="err" id="a-err">${esc(msg)}</p></div>`
      : `<div class="authcard"><h2>Can't reach the server</h2><p>Check your connection (and Tailscale), then try again.</p>
        <button class="btn tape" data-reconnect>Try again</button><p class="err" id="a-err">${esc(msg)}</p></div>`;
    $("#offunlock")?.addEventListener("click", async () => {
      try { await unlock(); show("list"); render(); banner(); } catch (e) { $("#a-err").textContent = passkeyError(e); }
    });
  }
  async function reconnect() {
    try {
      let st;
      try { st = await (await fetch("/api/auth/state", { credentials: "same-origin", cache: "no-store" })).json(); }
      catch { throw Object.assign(new Error("offline"), { network: true }); }  // unreachable, or a proxy's error page
      S.auth = st;
      if (st.auth && !st.loggedIn) { S.offline = false; revokeUrls(); return showAuth("Back online. Sign in to send your offline changes.", true); }
      const r = await syncUp();
      S.offline = false; revokeUrls(); banner(); show("list"); await load();
      if (r.sent) note(`Back online: ${r.sent} offline change${r.sent === 1 ? "" : "s"} synced.`);
      showConflicts(r.conflicts);
    } catch (e) {
      const network = isNetwork(e);
      if (!network) console.warn("sync failed:", e);
      if (S.offline) return note(network ? "Still offline: the server can't be reached." : `Couldn't sync yet: ${e.message}`);
      showUnlock(network ? "" : e.message);
    }
  }
  addEventListener("online", () => { if (S.offline) reconnect(); });

  // After every successful load while online: send anything left from an offline session, then refresh the copy.
  async function afterLoad() {
    if (S.offline || !(await meta())) return;
    if (await pendingCount()) {
      try { const r = await syncUp(); if (r.sent) { note(`${r.sent} offline change${r.sent === 1 ? "" : "s"} synced.`); showConflicts(r.conflicts); return load(); } }
      catch (e) { return note(e.message); }
    }
    syncDown();
  }

  return { api: offlineApi, unlock, showUnlock, afterLoad, section, photoUrl, refUrl, has: async () => !!(await meta()), banner };
})();
