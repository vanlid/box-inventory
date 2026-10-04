# Box Inventory

Number your storage boxes, sort them into rooms, photograph what's inside, and Claude lists
the contents and remembers where each item is in the photo. Search any item later to see
which box (and room) it's in, with that part of the photo highlighted. It runs on a computer at home, and
photo scans use your Claude subscription through Claude Code, so you don't need an API key.

## Before you start (every option)

- **A Claude token.** If you already use `CLAUDE_CODE_OAUTH_TOKEN`, use that one.
  Otherwise create one with `claude setup-token`, or run `./save-token.sh` (Mac/Linux).
- **Claude Code**, except with Docker, where it's built into the image:
  - Mac/Linux: `curl -fsSL https://claude.ai/install.sh | bash`
  - Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`

Each installer copies the token from `CLAUDE_CODE_OAUTH_TOKEN` into `.claude-token`, a file
only your user can read, because background services don't see your shell's variables.

## Install (no git needed)

**Mac or Linux:**
```bash
curl -fsSL https://github.com/vanlid/box-inventory/releases/latest/download/install.sh | bash
```
**Windows (PowerShell):**
```powershell
irm https://github.com/vanlid/box-inventory/releases/latest/download/install.ps1 | iex
```
This puts the app in `~/box-inventory` (Windows: `%USERPROFILE%\box-inventory`), creates `.env`, and
installs it as a background service. Run the same line again to **update**: your `data/`, `.env`
and token are never touched. If it stops because the Claude token is missing, put it in `.env`
(see below) and run the line again.

### Docker / Docker Compose (any OS)

Uses the ready-made image `ghcr.io/vanlid/box-inventory` (Intel/AMD and ARM, e.g. Apple Silicon).
Claude Code is built in, so you only need Docker.

```bash
mkdir box-inventory && cd box-inventory
curl -fsSL -o compose.yaml https://github.com/vanlid/box-inventory/releases/latest/download/compose.yaml
curl -fsSL -o .env https://github.com/vanlid/box-inventory/releases/latest/download/env.example
# edit .env: at least CLAUDE_CODE_OAUTH_TOKEN=...
docker compose up -d
docker compose logs | grep "setup code"     # the code for your first passkey
```

- **Data:** your boxes, photos and passkeys live in `./data` next to `compose.yaml`.
- **Update:** `docker compose pull && docker compose up -d`.
- **Stop:** `docker compose down` (data is kept).
- **Start at boot:** make Docker start at boot (Docker Desktop: "Start when you sign in"). The container restarts by itself.
- **HTTPS for passkeys:** either run `tailscale serve --bg 8765` on the host (see "Sign-in with passkeys"),
  or use the built-in Tailscale add-on below.

#### Optional: Tailscale inside Docker

Gives the app its own device and HTTPS address on your tailnet, such as
`https://box-inventory.tail1234.ts.net`, without installing Tailscale on the computer. The address
belongs to the container, so you can move to another machine, restore a backup, and your passkeys
keep working.

1. In the Tailscale admin console: turn on **MagicDNS** and **HTTPS certificates** (DNS page), and
   create an auth key (**Settings → Keys → Generate auth key**).
2. Download the add-on next to `compose.yaml`:
   `curl -fsSL -O https://github.com/vanlid/box-inventory/releases/latest/download/compose.tailscale.yaml`
3. Add to `.env` (on Windows, use `;` instead of `:`):
   ```
   COMPOSE_FILE=compose.yaml:compose.tailscale.yaml
   TS_AUTHKEY=tskey-auth-...
   ```
4. `docker compose up -d`, then open `https://box-inventory.<your-tailnet>.ts.net` on your phone.
5. In the admin console, find the new **box-inventory** device and choose **Disable key expiry**,
   so it doesn't drop off your tailnet after 180 days.

Tailscale keeps its login in `./tailscale`. If Tailscale has a problem, the app keeps working on
your LAN port. Use `TS_HOSTNAME` in `.env` for a different device name.

#### Optional: reach it from anywhere without the Tailscale app (Funnel)

Tailscale Funnel puts the same `https://box-inventory.<your-tailnet>.ts.net` address on the
internet, so a phone without Tailscale can open it. Your passkeys and printed labels keep working.

1. Create your first passkey on the tailnet address **before** turning Funnel on. Until a passkey
   exists, the setup code is all that stands between a stranger and your app.
2. Allow Funnel in the Tailscale admin console (**Access controls**). The default policy doesn't,
   even though it allows all connections. Add this section after the `"grants": [...]` list (or
   just the `{...}` line if a `"nodeAttrs"` section already exists), then save:
   ```
   "nodeAttrs": [
       {"target": ["autogroup:member"], "attr": ["funnel"]},
   ],
   ```
3. Add `TS_FUNNEL=true` to `.env` and run `docker compose up -d --force-recreate tailscale`.
4. Check that Tailscale allows it. This prints `funnel` lines; no output means step 2 is missing:
   `docker compose exec tailscale tailscale status --json | grep -i funnel`

   Don't rely on `tailscale funnel status` or the admin console badge: they say "Funnel on" even
   when the policy doesn't allow it, and the address then never reaches the internet.

The app is now public. Passkeys still guard everything, but **never combine Funnel with
`AUTH=off`**: that puts your whole inventory on the internet without a sign-in. Expect bots to
probe the address. To make it private again, remove `TS_FUNNEL` and run
`docker compose up -d --force-recreate tailscale`.

### What goes in `.env`

| Setting | Needed? | What it is |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | **Yes** | Your Claude token, from `claude setup-token`. Used for photo scans. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | For Google Drive backups | See "Backups" below. |
| `MS_CLIENT_ID` | For OneDrive backups | See "Backups" below. |
| `MS_TENANT` | Rarely | `consumers` (default) for personal Microsoft accounts, `common` to also allow work accounts. |
| `BACKUP_LOCAL_DIR` | No | Back up to folders too, e.g. a NAS share or USB disk. Several are separated by `:` (Windows: `;`). |
| `BACKUP_KEEP` | No | How many backups to keep in each place (default 14). |
| `PORT` | No | Default 8765. |
| `CLAUDE_MODEL` | No | Model for scans (default `sonnet`). |
| `CLAUDE_ASK_MODEL` | No | Model for plain-language questions (default `haiku`). |
| `AUTH=off` | No | Turns passkey sign-in off. Only on a network you trust. |

Restart the app after changing `.env` (run the install line again, or `docker compose up -d`).

## Install it as an app on your phone or computer

Open the app at its https address (see "Sign-in with passkeys"), then:

| Device / browser | How | Share photos into a box |
|---|---|---|
| Android, Chrome | menu → **Install app** (or Settings → App → Install) | Yes: gallery/camera → Share → Box Inventory |
| Android, Edge / Firefox | menu → **Add to phone** / **Install** | Chrome only |
| iPhone / iPad, Safari | Share → **Add to Home Screen** | No (iOS doesn't allow it) |
| Windows / Mac / Linux, Chrome or Edge | install icon in the address bar | Yes |
| Mac, Safari | File → **Add to Dock** | No |
| Firefox on desktop | Works in a tab; installing depends on the version and system | No |

The installed app opens full-screen like a normal app, and passkeys work in it. When photos are
shared into it, it asks which box or spot they belong to, then uploads and scans them as usual.

## Ask in plain language

Type anything in the search box. Exact matches show at once; below them, **Ask** (or Enter when
nothing matches) sends the question to Claude with a text version of your inventory: boxes,
rooms, items and notes, and tracked items with their status and where they were last seen. It
understands any language and vague wording ("sockor", "something to charge my laptop", "what did I
lend out?"), answers briefly, and shows the matching boxes and items as tappable cards. It uses a
fast model (`CLAUDE_ASK_MODEL`, default `haiku`), runs without any tools, and can only point to
boxes and items that exist.

**Voice:** tap the microphone in the search box and say the question ("var ligger mitt pass?"). It's
written into the search box and asked right away. Works in Chrome, Edge and Safari; the language
follows the browser or can be set under Settings → App. In Chrome and Edge, the browser sends the
audio to its speech service (Google or Microsoft) to turn it into text.

## Insurance details and export

Tap **Details** on any item (or open a tracked item) to add its **value**, **purchase date**,
**serial number** and a **receipt photo**. Settings → **Insurance and export** sets the currency and
downloads everything as a **spreadsheet (CSV)**, or opens a **printable report** grouped by room with
totals and optional photos (print it, or save it as a PDF). Receipts are included in backups.

## Lending things out

Set a tracked item to **Lent out** to note who borrowed it and when it's due back. The Tracked tab's
**Lent out** filter lists them, overdue items show in red, and the app reminds you when something is
overdue. **Add reminder to calendar** downloads a calendar event with an alert, so your phone's own
calendar reminds you on the day. **It's back** puts it home again; the history remembers who had it.

## Offline copy (per device)

Settings → **Offline → Keep an offline copy on this device** stores the inventory and smaller copies
of the photos on that phone or computer, **encrypted**, so you can look things up without a
connection (in the basement, or when the server is down). Open the app offline and it asks for your
passkey to unlock the copy.

- **Passkey-locked** where the browser and passkey support the PRF extension (current Chrome,
  Safari on iOS 18 / macOS 15, recent Firefox, with passkeys in Google Password Manager or iCloud
  Keychain): the copy can only be decrypted after your fingerprint, face or screen lock.
  Passkeys created with an older version of the app don't have this. Add a new passkey to get it.
- Otherwise **device-locked**: a key that can't be copied off the device, plus a passkey check the
  app verifies itself before showing anything.
- **Changes made offline** (editing boxes and items, moving items, tracked-item status, adding photos
  to existing boxes) wait in an encrypted outbox and are sent when the server is back. Item edits are
  merged with whatever changed on the server meanwhile; if the same thing was changed differently on
  another device, the app asks which to keep. Photos taken offline are scanned once they're uploaded.
  New boxes need a connection.
- The copy refreshes itself whenever the app is online, is deleted after 30 days without refreshing,
  and is deleted if the passkey it's locked to is removed. Unlocking offline needs a passkey that's on
  that device (the QR-code option needs a connection).

## QR labels

Settings → **Print QR labels** (or **Print label** on a box) prints stickers with the box number,
name, room and a QR code. Point the phone camera at a label and the app opens that box. Spots and
tracked items (a suitcase, a bag) can have labels too.

Sizes: A4 sheets of 24 (70 × 37 mm, e.g. Avery 3474), A4 sheets of 8 large labels (105 × 74 mm),
or a 62 mm label-printer roll. In the print dialog, set margins to **None** and scale to **100%**.
The QR codes use a compact uppercase form of the address, which keeps them simple enough to scan
from about 10× their width (30 cm for the small labels, 70 cm for the large ones).
The QR codes point to the app's secure (https) address, so print them from there.

## Tracking clothes and things that move around

Besides numbered **boxes**, add **spots**: places that aren't boxes, like a wardrobe shelf, the
laundry basket, the drying rack or a chair. Spots don't use up box numbers.

Tap **Track** next to anything in a box's contents (or **Tracked → + Track item**) to follow it:

- **Home**: where it belongs. Items seen somewhere else show up under **Away from home**.
- **Status**: clean, in use, to wash, washing, drying, lent out, missing. The **Laundry** tab moves
  things along: to wash → washing → drying → clean → put away.
- **Last seen**: photograph any place and Claude also looks for your tracked items there, using
  their pictures. It suggests sightings under **Seen here?** and you confirm them.
- A spot can set the status: make the laundry basket set "To wash", so anything confirmed there is
  marked for washing.
- **Look-alikes** such as socks: track a group with a total ("black sport socks, 12"). Counts are
  kept per place, and the app shows how many are unaccounted for.

Recognition works best on distinctive items (a patterned sock, a specific hoodie). Identical items
can't be told apart, which is why groups are counted instead.

## Sign-in with passkeys (HTTPS via Tailscale)

The app is protected by passkeys (fingerprint, face or screen lock). Browsers only allow passkeys
on HTTPS, so give the server a secure address with [Tailscale](https://tailscale.com) (free):

1. Install Tailscale on the server and on your phone, signed in to the same account.
2. In the Tailscale admin console, turn on **MagicDNS** and **HTTPS certificates** (DNS page).
3. On the server: `tailscale serve --bg 8765`. It prints an address like `https://mac-mini.tail1234.ts.net`.
4. Open that address on your phone. Enter the **setup code** (the installer prints it; it's also
   in `data/setup-code.txt` and the log) and create your first passkey.

**More devices or people:** on the new device, tap *Sign in*, choose *Use a phone or tablet*, and
scan the QR code with your phone. Once signed in, it offers to create a passkey for that device.
Manage passkeys under **Settings**. The app works from anywhere your phone is on Tailscale.

To run without sign-in (e.g. plain HTTP on a trusted network), set `AUTH=off`.

## Backups to Google Drive or OneDrive

Settings → **Backups** connects one or more places to back up to: Google Drive, OneDrive, and local
folders, with **several accounts per service** if you like (yours and a family member's Google Drive, say).
The app makes a backup once a day when something changed (and whenever you tap **Back up now**),
uploads it to every connected place, and keeps the 14 newest in each. If one place fails, the others
still get the backup, and the error shows next to that place. Each backup is one zip with your rooms,
boxes, photos and passkeys. Sign-in tokens and device sign-in cookies are not included.

- Google Drive: a **Box Inventory backups** folder. The app can only see files it created.
- OneDrive: the app's own folder, **Apps/<your app name>**.

You connect by entering a short code at google.com/device or microsoft.com/devicelogin, so no
web addresses need registering and it works on a fresh install before HTTPS is set up.

### One-time: create your Google client (about 5 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project ("Box Inventory").
2. **APIs & Services → Library**: enable **Google Drive API**.
3. **Google Auth Platform** (OAuth consent screen): user type **External**, fill in app name and your email.
   Under **Audience**, click **Publish app**. While it's in "Testing", Google ends access after 7 days.
4. **Clients → Create client**, type **TVs and Limited Input devices**. Put the client ID and secret in `.env`:
   `GOOGLE_CLIENT_ID=…` and `GOOGLE_CLIENT_SECRET=…`
5. Restart the app. When you connect, Google may warn that the app isn't verified. It's your own app, so continue.

### One-time: create your Microsoft app (about 5 minutes)

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → **App registrations → New registration**.
   Name it, choose **Personal Microsoft accounts only**, leave the redirect URI empty. (If you're asked to
   create a free Azure account first, do that; this doesn't cost anything.)
2. **Authentication → Advanced settings**: set **Allow public client flows** to **Yes**, then Save.
3. **API permissions → Add → Microsoft Graph → Delegated**: add **Files.ReadWrite.AppFolder**,
   **User.Read** and **offline_access**.
4. Copy the **Application (client) ID** into `.env` as `MS_CLIENT_ID=…` and restart the app.

### Restore on a fresh install

Install as usual, open the app, enter the setup code, and tap **Restoring from a backup instead?**
Connect your drive and pick a backup.

- Same HTTPS address as before (same Tailscale account and machine name): your passkeys work
  right away. Just sign in.
- New address (another machine name or Tailscale account): passkeys only work on the address they
  were made for, so the app sets them aside and you create a new passkey with the same setup code.
  Your Tailscale login is never part of a backup. You choose the account on each machine.

What was on the server before a restore is moved to `data/before-restore-<date>/`. Delete it once
you're happy. You can also restore from Settings at any time.

## Run from a git checkout instead

| Where | Command (run in this folder) | Runs as |
|---|---|---|
| **Docker** (any OS) | `cp .env.example .env`, add the token, then `docker compose up -d --build` | container, restarts with Docker |
| **Mac** | `chmod +x *.sh && ./install-mac.sh` | LaunchAgent, starts at login |
| **Linux** | `chmod +x *.sh && ./install-linux.sh` | systemd user service, starts at boot |
| **Windows** | `powershell -ExecutionPolicy Bypass -File .\install-windows.ps1` | Scheduled Task, starts at login |

Each installer checks the token with a quick Claude call, starts the service, and prints the
address to open on your phone (e.g. `http://mac-mini.local:8765`). "Add to Home Screen"
on the phone makes it feel like an app.

To remove the service: `./install-mac.sh uninstall`, `./install-linux.sh uninstall`,
`.\install-windows.ps1 -Uninstall`, or `docker compose down`. Your data is kept.

### Platform notes

- **Mac:** the service starts when you log in. For a headless Mac mini, turn on
  System Settings → Users & Groups → Automatic login so it comes back after updates or power cuts.
- **Linux:** the installer turns on "linger" so the service runs without you logged in.
  If that's not allowed, it prints the `sudo loginctl enable-linger` command to run.
- **Windows:** run the installer from an *administrator* PowerShell to open the firewall
  port automatically; otherwise it prints the one command to run. Your Wi-Fi must be set to
  "Private network". The task starts when you log in; the log is `data\server.log`.
- **Docker:** set Docker to start at boot (Docker Desktop: "Start when you sign in").
  To update Claude Code in the image: `docker compose build --pull --no-cache && docker compose up -d`.

## Quick test without installing

`python3 server.py` (Windows: `py server.py`), then open `http://localhost:8765`.

## Where things are

- `data/inventory.json`: rooms, boxes and items, including where each item is in its photo (readable JSON)
- `data/auth.json`: registered passkeys (public keys only) and sign-in sessions
- `data/photos/<place id>/`: the photos
- `data/refs/`: reference pictures of tracked items, and `sheet.jpg` with all of them for scans
- `data/backup.json`: which drive is connected, and its sign-in token (readable only by you)
- Besides the built-in drive backups, copying the `data/` folder is a complete backup too.

## Notes

- With `AUTH=off`, anyone who can reach the port can use the app.
- **Select** on a box's contents lets you move items to another box or spot, or delete several at once.
  Tracked items moved this way take their home and last-seen place along.
- **Re-scan all photos** refreshes only what the scan listed and you never touched: items you added by
  hand, edited or track are kept as they are.
- `.claude-token` and `.env` hold keys to your Claude subscription and backup apps. Don't share them or put them in git.
- Scans don't use any of your other Claude Code setup (plugins, MCP servers, settings). Each scan
  runs with only the Read tool, looks at the photos and returns a list.
- Scans use the `sonnet` model (`CLAUDE_MODEL` to change it) and look at the 8 newest photos of a box at most.

## Local models (optional)

Instead of Claude, photo scans and/or questions can use a model running on your own hardware through
[Ollama](https://ollama.com). The model is loaded only while it's working and unloaded after
`OLLAMA_KEEP_ALIVE` (default 5 minutes), so it doesn't hold memory between scans. Local vision models
list items well but are less accurate than Claude, especially at locating items in the photo; each
box's **Re-scan** has a "…with Claude" option for when you want the better result.

- **Mac (Apple Silicon):** install the Ollama Mac app, which uses the GPU and unified memory. Docker
  on macOS can't use the Apple GPU, so don't run Ollama in Docker there. In `.env`:
  `SCAN_ENGINE=local` and, if the app runs in Docker, `OLLAMA_URL=http://host.docker.internal:11434`.
- **PC with an NVIDIA GPU:** add `compose.ollama.yaml`, which runs Ollama in Docker with GPU access.
  In `.env`: `COMPOSE_FILE=compose.yaml:compose.ollama.yaml` (Windows: `;`) and `SCAN_ENGINE=local`.
- **Download the model:** Settings → **Scanning** checks Ollama and has a **Download model** button
  (or run `ollama pull qwen2.5vl:7b`). Other vision models work too, e.g. `qwen3-vl`, `gemma3`; set
  `OLLAMA_MODEL`. `ASK_ENGINE=local` uses `OLLAMA_ASK_MODEL` (default: the same model) for questions.

## Releases and updates (for maintainers)

- Push a tag like `v1.2.0` to publish a release with the download bundles (`.github/workflows/release.yml`).
- The Docker image `ghcr.io/vanlid/box-inventory` is built for Intel/AMD and ARM on every push to `main`,
  every tag, and every Monday, so base-image security fixes and new Claude Code versions are picked up.
  Each build is scanned with Trivy; if it finds fixable high or critical vulnerabilities, the run fails
  and GitHub emails you (`.github/workflows/image.yml`).
- Dependabot opens weekly pull requests for base-image and workflow updates. Merging one rebuilds the image.
