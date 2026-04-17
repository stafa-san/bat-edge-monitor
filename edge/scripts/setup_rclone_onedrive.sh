#!/usr/bin/env bash
# One-time rclone + UC OneDrive setup for the bat-edge-monitor Pi.
#
# This script is intentionally *interactive* — rclone config can't be
# scripted end-to-end because OAuth requires a browser round-trip. Read
# each step and run the commands yourself. The steps are deliberately
# short so you can copy/paste one at a time.
#
# Audience: Mustapha or Dr. Johnson, running this once per Pi.
#
# Preconditions:
#   * You're SSH'd into the Pi as user `pi` (change the mount path in
#     edge/docker-compose.yml if your user is different).
#   * bat-edge-monitor repo is checked out to ~/bat-edge-monitor (or a
#     path you know).
#   * The sync-service image has been rebuilt at least once since the
#     D3 commit — that image already has the rclone binary baked in.
#
# Time budget: ~5 minutes if you already have UC OneDrive credentials
# handy in your password manager; ~15 if you need to dig them up.

set -euo pipefail

echo ""
echo "================================================================"
echo " rclone + UC OneDrive setup — bat-edge-monitor Pi"
echo "================================================================"
echo ""
echo "Before running: open this script in an editor so you can follow"
echo "along. The rclone config step is interactive and this script"
echo "cannot automate it."
echo ""

# -----------------------------------------------------------------------------
# Step 1 — Install rclone on the Pi host.
#
# The sync-service container ships with rclone, but running `rclone config`
# inside the container is awkward (no browser, no persistent home). We run
# it on the host and bind-mount the resulting config into the container.
# -----------------------------------------------------------------------------

echo "--- Step 1: install rclone on the host ---"
echo "$ sudo apt update && sudo apt install -y rclone"
echo ""

# -----------------------------------------------------------------------------
# Step 2 — Run `rclone config` and create the OneDrive remote.
#
# You'll be prompted for a lot of defaults. The ones that matter:
#   n) New remote
#   name>        onedrive
#   Storage>     onedrive             (the exact name may vary by rclone
#                                      version; pick "Microsoft OneDrive")
#   client_id>   (leave blank — rclone uses its own default)
#   client_secret> (leave blank)
#   Edit advanced config? n
#   Use auto config? y                 (opens a browser window on a
#                                      machine you can click through; if
#                                      running headless, say "n" and
#                                      follow the headless-auth flow)
#   <browser auth as zakarimn@mail.uc.edu>
#   Your choice?  1)  OneDrive Personal or Business
#                 Pick the one that matches the UC account (usually
#                 "OneDrive Business").
#   Is this OK? y
#   Keep this "onedrive" remote? y
#   Quit config? q
# -----------------------------------------------------------------------------

echo "--- Step 2: configure the OneDrive remote ---"
echo "$ rclone config"
echo ""
echo "   Use the exact remote name: onedrive"
echo "   Authenticate as:            zakarimn@mail.uc.edu"
echo "   Drive type:                 OneDrive Business"
echo ""

# -----------------------------------------------------------------------------
# Step 3 — Sanity-check the remote from the Pi shell.
# -----------------------------------------------------------------------------

echo "--- Step 3: list the OneDrive root to confirm auth worked ---"
echo "$ rclone lsd onedrive:"
echo ""
echo "   You should see your OneDrive top-level folders (including"
echo "   'Calls for Mustapha' if Dr. Johnson has shared it with you)."
echo ""

# -----------------------------------------------------------------------------
# Step 4 — Optional: pre-create the target folder so first sync has less
# to do. rclone will create missing folders on first copy anyway, so this
# is just a tidy-ness move.
# -----------------------------------------------------------------------------

echo "--- Step 4 (optional): create the target folder ---"
echo "$ rclone mkdir 'onedrive:Bat Recordings from pi01'"
echo ""
echo "   Change 'pi01' if this Pi has a different site ID (the"
echo "   ONEDRIVE_REMOTE_BASE_PATH env var controls this)."
echo ""

# -----------------------------------------------------------------------------
# Step 5 — Lock down the config file.
#
# The container mounts /home/pi/.config/rclone read-only, so this is
# defense in depth. Still worth setting 600 in case the file gets
# inspected from another process.
# -----------------------------------------------------------------------------

echo "--- Step 5: lock the config file ---"
echo "$ chmod 600 ~/.config/rclone/rclone.conf"
echo ""

# -----------------------------------------------------------------------------
# Step 6 — Restart sync-service so the new mount is visible. (If the
# container was already running before Step 2, it needs to be restarted
# to pick up the newly-created config directory.)
# -----------------------------------------------------------------------------

echo "--- Step 6: restart sync-service ---"
echo "$ cd ~/bat-edge-monitor/edge && docker compose restart sync-service"
echo ""
echo "   Then confirm rclone is reachable from inside the container:"
echo "$ docker compose exec sync-service rclone version"
echo "$ docker compose exec sync-service rclone lsd onedrive:"
echo ""

# -----------------------------------------------------------------------------
# Step 7 — Flip the feature flag.
#
# Until now, everything is dormant — the sync loop reads
# ENABLE_ONEDRIVE_SYNC=false and returns immediately. Flip the flag,
# restart sync-service, and tier-1 WAVs start flowing to OneDrive at the
# interval set by ONEDRIVE_SYNC_INTERVAL_MINUTES (default 60 min).
# -----------------------------------------------------------------------------

echo "--- Step 7: enable OneDrive sync ---"
echo "$ cd ~/bat-edge-monitor/edge"
echo "$ echo 'ENABLE_ONEDRIVE_SYNC=true' >> .env"
echo "$ docker compose up -d sync-service"
echo ""
echo "   Watch the logs for the first cycle:"
echo "$ docker compose logs -f sync-service | grep -i onedrive"
echo ""

# -----------------------------------------------------------------------------
# Troubleshooting.
# -----------------------------------------------------------------------------

echo "--- Troubleshooting ---"
echo ""
echo "  * 'rclone binary not found' in sync-service logs:"
echo "      rebuild the image with"
echo "        docker compose build --no-cache sync-service"
echo "      and check that edge/sync-service/Dockerfile's apt install"
echo "      line is present."
echo ""
echo "  * OAuth token expired (error 401):"
echo "      re-run 'rclone config reconnect onedrive:' on the host."
echo ""
echo "  * Files not uploading even though everything else is fine:"
echo "      confirm candidates exist with"
echo "        docker compose exec db psql -U postgres -d soundscape \\"
echo "          -c \"SELECT count(*) FROM bat_detections WHERE storage_tier=1 \\"
echo "              AND audio_path IS NOT NULL AND remote_audio_path IS NULL;\""
echo ""
echo "Done. Remember: Pi-side rclone OAuth is per-Pi, not per-repo."
echo "If you add a second Pi, run this whole script again on it."
