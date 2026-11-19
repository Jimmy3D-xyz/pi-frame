# Synced folders

[← Back to the README](../README.md)

Photo source → Synced folders. Connect a service, pick a folder, and the
frame keeps an album in step with it:

- Photos (including iPhone HEIC) and videos added to the folder are
  downloaded, resized or converted like uploads, and appear on the frame.
- Changed files are replaced.
- Files taken out of the folder are removed from the frame (you can turn this off).
- It checks every hour by default, and there's a Sync now button.
- It's one-way: the frame never writes to your folders. Photos you upload by
  hand into a synced album are left alone.
- Several folders, from different services, can be synced at once.

## OneDrive, Google Drive and Dropbox

1. Press **Sign in** and sign in as usual.
2. Your browser then goes to an address starting `http://localhost:53682/`
   or `http://127.0.0.1:53682/` that won't load. That's expected: it was
   meant for the Pi. Copy that address from the address bar.
3. Paste it into the frame and press **Finish**.

Or, on a computer with rclone, run `rclone authorize "onedrive"` (or
`"drive"`, `"dropbox"`) and paste the token it prints.

Google Drive access is read-only.

### Google Drive with your own API key (optional)

Google slows downloads for shared apps, which makes the first sync of a big
folder slow. To use your own free key:

1. At [console.cloud.google.com](https://console.cloud.google.com) create a
   project and enable the **Google Drive API**.
2. Set up the **OAuth consent screen**: External, add yourself as a test
   user, then **Publish app**. (Unpublished, Google makes sign-ins expire after 7 days.)
3. **Credentials → Create OAuth client ID → Desktop app**.
4. Paste the client ID and secret into "Use your own Google API key" on the
   frame, then sign in. Google warns that the app isn't verified: it's your
   own, so choose Advanced → continue.

## NAS or shared folder (SMB)

Enter the NAS's address (e.g. `192.168.1.20` or `mynas.local`), a username
and a password. Works with Synology, QNAP, Windows shared folders and
anything else that speaks SMB 2 or 3. It works without the internet, and
syncs are fast.

- A separate account with read-only access to the photos folder is a good
  idea (on a Synology: Control Panel → User, then read-only permission on the
  shared folder).
- Names like `mynas.local` are looked up by the frame and re-checked before
  each sync, so a NAS whose address changes is still found. If a name isn't
  found, use the IP address.
- The password is stored scrambled in the frame's private settings.

## rclone

Syncing uses [rclone](https://rclone.org) 1.67 or newer, which the installer
downloads from rclone.org. Older versions, including Debian's 1.60, can list
OneDrive folders but every download fails with "unauthenticated" since a
2024 Microsoft change. If the frame says sign-in has expired straight after
signing in, check the version with `rclone version`.

To install it by hand on a 64-bit Pi:

```sh
cd /tmp && curl -LO https://downloads.rclone.org/rclone-current-linux-arm64.zip
unzip -o rclone-current-linux-arm64.zip
sudo install -m 755 rclone-*-linux-arm64/rclone /usr/local/bin/rclone
```

Re-running the installer (`sudo ./deploy/install.sh`) does the same.
