# Beta Testing

Beta releases are intended for deliberate testing of the camera and dashboard
behavior before a stable release.

## Install a beta

Every prerelease includes a HACS-compatible `comelit_intercom.zip` asset.

### Through HACS

1. Open the Comelit Intercom repository in HACS.
2. Enable prerelease or beta updates from the repository menu.
3. Redownload the repository and select the desired beta version.
4. Restart Home Assistant.
5. Confirm the loaded version before testing the intercom.

The exact menu labels can vary between HACS versions. Disable prerelease updates
again if the installation should return to the stable channel after testing.

### Manually

1. Download `comelit_intercom.zip` from the selected
   [GitHub prerelease](https://github.com/Jordi-14/ha-component-comelit-intercom/releases).
2. Create a full Home Assistant backup.
3. Replace the contents of `custom_components/comelit_intercom` with the files
   from the ZIP.
4. Restart Home Assistant and confirm the loaded version.

Do not mix files from different releases.

## Test checklist

- The integration exposes one `Live feed` camera plus the original per-door
  opening buttons.
- The old custom intercom card and its Lovelace resource are removed.
- Earlier event, audio, and manual Start/Stop entities are removed from the
  integration's entity registry on startup; existing door entities and their
  entity IDs remain unchanged.
- A built-in picture entity card with `camera_view: auto` shows a current still
  without leaving a video session active.
- Repeated still requests within 15 seconds reuse the cached JPEG.
- Clicking the camera opens low-latency live video without any separate
  Start/Stop controls.
- After closing one live view and allowing a dashboard still to refresh,
  clicking the camera again replaces the still with live video rather than
  remaining on the cached poster.
- Closing a WebRTC viewer releases the panel automatically; HLS fallback is
  released by its safety timeout.
- Open the camera in two browsers, close one, and verify the other keeps playing.
- Ring the doorbell while the camera is idle and while it is playing; the ring
  must not start, stop, restart, or answer the video session.
- Test both a browser and the Home Assistant Companion app.
- Verify every configured door still opens from its existing button entity.
- Opening the Comelit app during an HA live view releases or interrupts the HA
  stream without leaving a stuck video session.

Keep the entrance visible and the official Comelit app available while testing
session takeover behavior.

## Report a beta problem

Include the beta version, Home Assistant version, Comelit model and firmware,
whether testing in a browser or Companion app, and the expected and observed
behavior. Review logs and diagnostics before publishing them; do not share
tokens, local IP addresses, backups, databases, or packet captures publicly.

## Roll back

Restore the full Home Assistant backup created before testing, or reinstall the
previous integration version in HACS and restart Home Assistant. A full backup
is preferred because it also preserves config-entry state.
