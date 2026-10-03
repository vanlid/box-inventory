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

Settings → **Backups** connects a drive. The app makes a backup once a day when something changed
(and whenever you tap **Back up now**) and keeps the 14 newest. Each backup is one zip with your
rooms, boxes, photos and passkeys. Your drive's sign-in token and the device sign-in cookies are
not included.

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

## Pick one way to run it

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
- Boxes scanned before item positions existed get them with **Re-scan all photos** (this rebuilds that box's list).
- `.claude-token` and `.env` hold keys to your Claude subscription and backup apps. Don't share them or put them in git.
- Scans don't use any of your other Claude Code setup (plugins, MCP servers, settings). Each scan
  runs with only the Read tool, looks at the photos and returns a list.
- Scans use the `sonnet` model (`CLAUDE_MODEL` to change it) and look at the 8 newest photos of a box at most.
