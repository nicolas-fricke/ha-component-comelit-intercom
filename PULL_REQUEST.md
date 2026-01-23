# Peer-aware multi-door (Door 1/2) support

## How Comelit handles multiple doors/cameras (peer/stream binding)

Some Comelit entrance units can present multiple camera/door “views” in the mobile app (e.g., swiping left/right).

In the setup this change targets, the intercom does **not** expose a “door #” selector in the control protocol. Instead, opening the door is tied to the **currently active peer/stream**:

- default peer (`cam1`) → door-open triggers **Door 1**
- after switching to `cam2` peer → door-open triggers **Door 2**

The `cam2` peer association persists only briefly (~20s on the tested setup) and then falls back to `cam1`.

This behavior is implemented based on observed network communication and device behavior.

Important limitation: the protocol does **not** provide a reliable way to query “which peer/camera is currently active”, so the integration uses a small persisted state to avoid unnecessary peer switches and to decide when to wait for fallback.

## Summary of changes

- Adds setup + options configuration for:
  - `Entrance unit address (call_target)` (example: `SB100001`)
  - `Number of doors` (`door_count`, default `1`, currently max `2`)
  - `Door 2 reuse window` (`reset_interval`, default `15s`): reuse cached `cam2` peer for repeated Door 2 requests (safety margin)
  - `Door delay` (`door_delay`, default `0s`): optional delay between selecting the `cam2` peer and sending door-open
- Implements peer-aware door opening via `PeerDoorOpener` (`custom_components/comelit_intercom/peer_door_opener.py`):
  - Door 2: start a call, wait for video, restart the call (`RELEASE + START40`), wait for video again → then send the door-open-only sequence.
  - Door 1 after Door 2: wait for the automatic fallback window instead of explicitly switching back.
- UI:
  - If `door_count == 1` (default): the integration behaves as before (buttons from the device’s address-book).
  - If `door_count > 1`: the integration exposes `Door 1..N` button entities (currently `Door 1..2`).

## How to test (manual)

1. In Home Assistant, add the “Comelit Intercom” integration as usual.
2. In the second setup step (“Door Settings”), set:
   - `Entrance unit address (call_target)` to the VIP address you use in the mobile app (e.g. `SB100001`)
   - `Number of doors` to `2`
   - `Door 2 reuse window` to `15s` (default; decrease if your peer falls back earlier)
   - `Door delay` to `0s` (increase to `1–2s` if the wrong door opens)
3. Restart Home Assistant (or reload the integration).
4. Verify `button.comelit_intercom_door_1` and `button.comelit_intercom_door_2` exist.
5. Press:
   - `Door 2` → should open door 2 (peer switch + door-open-only).
   - Immediately press `Door 2` again (within the reuse window) → should still open door 2 without re-switch.
   - Press `Door 1` shortly after `Door 2` → integration should wait for fallback, then open door 1.

If door 2 opens door 1 instead:
- Increase `Door delay` to `1–4s`.
- Decrease `Door 2 reuse window` (if the peer falls back earlier than expected on your setup).

## Compatibility & configuration notes

- Existing setups continue to work without changes (`door_count` defaults to `1`).
- Multi-door support is **experimental** and depends on system-specific behavior:
  - the peer fallback window varies by installation,
  - there is no protocol feedback that confirms which door is currently active.
- The implementation stores state in Home Assistant’s storage to cache peer selection timestamps. If the stored state is wrong, the integration may open the wrong door.

## Safety warning (READ THIS)

This feature can physically open a door.

Because the protocol does **not** let us verify which door is currently active, the multi-door implementation is **not 100% failsafe** and may open the wrong door in some situations.

Use at your own risk. Test carefully in your environment before relying on it for automations.
