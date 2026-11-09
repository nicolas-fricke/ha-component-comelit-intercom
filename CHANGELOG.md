# Changelog

## 1.4.3

- Publish the tested camera and transport changes on the main branch.

## 1.4.2

- Serialize reconnects and ignore stale disconnect callbacks so concurrent
  refreshes cannot evict each other's ICONA connection.
- Give the proven legacy door sequence exclusive ownership of single-client
  panels, then always restore push notifications and the persistent transport.
- Resume an active live camera view after door control without racing viewer
  closure, integration shutdown, or another video negotiation.
- Prevent in-flight reconnect and door tasks from recreating sockets, VIP
  listeners, or keepalives after the config entry unloads.
- Preserve the local door-open event when the command succeeds but transport
  restoration fails, while still reporting the restoration problem.
- Treat ordinary socket loss and known protocol cleanup/retransmit traffic as
  expected diagnostics while retaining warnings for notification renewals.
- Add regression coverage for concurrent reconnects, stale callbacks, door
  success and failure, shutdown races, live-video recovery, and socket loss.

## 1.4.1

- Remove the play-button overlay from still previews for an unobstructed image.
- Show only the local capture time in the top-left corner.
- Preserve the last capture time while refreshing, temporarily unavailable, or
  disabled instead of replacing it with status text.

## 1.4.0

- Keep the integration camera-focused: doorbell rings no longer answer or
  control video sessions, and no call or audio controls are exposed.
- Track simultaneous WebRTC viewers independently so closing one card cannot
  stop another viewer's feed.
- Fix stale dashboard tasks that could start live video after previews were
  disabled or the card was hidden.
- Restore pre-camera door entity identities and migrate the short-lived v1.3
  beta IDs without changing entity IDs.
- Accept singleton door and actuator address books returned by older firmware.
- Add stable device identification, a configurable protocol port,
  reauthentication, and IP-address reconfiguration.
- Use Home Assistant config-entry runtime data and create the camera even when
  the device exposes no door controls.
- Harden automatic token extraction against oversized downloads and archive
  expansion.
- Align dependencies, typing, tests, and CI with Home Assistant 2026.5 and
  Python 3.14.

## 1.3.0

- Add an `Automatic still previews` switch, disabled by default.
- Add a 0–180 minute `Still preview interval` setting in 0.5-minute steps.
- Add a privacy-aware dashboard card that only refreshes while visible.
- Treat an interval of 0 as live video while the card is visible.
- Show the local capture time on every still and open live video when clicked.
- Preserve the released 1.2.0 camera, door, and media-session architecture.
