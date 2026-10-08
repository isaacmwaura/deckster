# Changelog

## v0.7.0 — command, audio and Android architecture rebuild, 2026-10-03

- Share bounded command ownership across the HTTP desktop workspace and phone, with independent volume, media, hotkey, soundboard and Stop lanes. Ping remains responsive during slow unrelated work.
- Identify commands and return linked outcomes; perform Windows writes and readback on the COM owner, and reject stale observations. Reconnect clears pending actions without replaying clips or toggles.
- Reduce unchanged-state traffic, send separate meters/media/soundboard updates, cache endpoint discovery and library metadata, and suspend hidden phone/desktop rendering and polling.
- Bound chunk decoding and active playback with explicit admission errors; render multi-minute effects without truncation. Preallocate callback scratch, preserve independent Me output and expose audio progress/deadline diagnostics.
- Prepare an optional microphone-only processor boundary before clip mixing. The shipped processor is bypass; no denoiser model is selected.
- Serialize listener transitions, expose requested versus active mode/TLS and retain secure preferences after failure. Keep the desktop console available if the phone listener cannot start. Local readiness and diagnostics distinguish stalled subsystems from process liveness.
- Android owns foreground discovery, cancellable connection attempts and WebView release/recovery. Persist Mounted/Battery modes; Battery permits normal screen timeout. Candidate Android versionCode 18.
- Roll back failed durable configuration changes and preserve resource ownership during failed cleanup. Automated tests and isolated package checks do not establish Discord listener quality, physical POCO F3 acceptance or gaming/battery savings.

## v0.6.8 — phone, media and audio reliability review, 2026-10-02

- Keep dial touch targets stable during redraws, capture the active pointer, and cancel gestures and pending commands on interruption. Delayed volume echoes no longer immediately overwrite recent input, and reconnects keep one current socket.
- Reuse phone controls and redraw only changed content. Slow clients receive the latest complete state instead of replaying old volume snapshots.
- Move sound decoding, library work and device recovery off the phone server's event loop. Separate bounded control and soundboard command queues keep volume controls responsive during a slow sound load; Stop all invalidates pending playback.
- Preserve microphone + Others when optional local listening fails or changes. Audio callbacks avoid blocking clip locks and disk logging, preserve captured speech after clip errors, and expose interruption counts in Audio routing.
- Refresh late/reused-title media artwork, serve its actual image type, select the current Chrome media session, and bound artwork storage and native waits. A live Chrome check confirmed PNG artwork.
- Decode float WAV and compressed stereo clips consistently, avoid phase cancellation in mono effects, and invalidate changed clip files in the playback cache.
- Android source versionCode 17. Physical phone use and actual Discord listener reception remain acceptance checks.

## v0.6.7 — soundboard recovery and Discord delivery checks, 2026-10-01

- Recover stopped microphone/mixer/monitor streams automatically using the saved devices. Transient driver failures retry with a delay capped at 30 seconds; retries preserve settings and close partially opened streams.
- Reconnect returning devices without substituting another microphone or cable. A missing optional monitor no longer prevents sending sounds to Others.
- Audio routing includes a sound delivery check that identifies a short test signal at the cable's actual receiving microphone. Silence and unrelated audio cannot satisfy the check. Captured audio stays in memory and is never saved.
- Persistent Discord/game guidance explains Studio/Custom settings, Krisp, echo cancellation, automatic gain control, voice activity thresholds and push-to-talk. A successful cable check confirms delivery before the receiving app's filters; call reception still needs checking in Discord.
- Android versionCode 16; desktop and companion source version 0.6.7.

## v0.6.6 — compact workspace and responsive wiring, 2026-09-30

- Hide gesture controls move directly below the phone preview; long app lists scroll independently.
- Symmetric audio cards and rounded wires keep the full routing console visible on ordinary laptop windows, including 1251×834 and 1280×800.
- Lightweight audio signal polling every 70 ms and faster peak release follow short sounds without rebuilding the workspace.
- Start audio / Apply device changes labels clarify the selected-device action. Recommended settings remains the one-click option.
- Compatibility status is compact when supported and expands automatically for problems.
- Android versionCode 15; matching local EXE/APK builds.

## v0.6.5 — wiring and simple setup, 2026-09-30

- Audio-routing diagram follows the user's wiring sketch: microphone and sounds
  enter the mixer, with separate app-input, headphones and direct-mic connections.
  Measured signal animates along wires; inactive connections are dashed.
- Prominent Use recommended settings connects a matching route in one click.
  Compatibility checks run automatically on device and route changes.
- Missing VB-CABLE notices on Soundboard and Audio routing include the official
  download link and install/restart instructions. Settings explains external links.
- Connected phones/tablets report viewport dimensions automatically, including
  rotation. Portrait previews scale to the available window height.
- Mixer app names, status and Hide controls have separate grid cells.
- Packaged startup and local installation keep one current Desktop shortcut,
  removing only identifiable older Deckster links.
- Download-first guides with screenshots; Python and USB tools are bundled in
  the EXE. Python/pip instructions are in the developer section.
- Android versionCode 14. Public release publication is a separate operation.

## v0.6.4 — local installed review, 2026-09-30

- Audio-routing notices expire and stay on their originating page.
- Virtual mixer visual with measured voice, soundboard, mixed output, headphones
  and Windows direct-input bypass signals, plus activity-driven animation.
- Use mixer as Windows microphone selects the paired virtual cable capture end;
  apps with custom microphone selection still need their own input configured.
- Sample-rate support checks and actionable Windows format recommendations.
- Responsive routing and actual phone/tablet preview screen shapes and orientation.
- Installed on PC and Android (code 13), preserving user settings and app data.
  Public packaged release remains v0.5.3.

## v0.6.3 — current source, 2026-09-30

This source update includes the desktop and phone work developed since v0.5.3.
The latest packaged GitHub release is still v0.5.3; v0.6.3 downloads have not
been published as a GitHub release.

### Desktop and phone interface

- Dedicated desktop workspace with the actual phone HTML/CSS/JS renderer.
- Separate Page layout and Phone workspace for page placement and app management.
- Movable Mixer, page swaps, empty-center validation and saved app visibility/order.
- Capped preview sizing, smaller Mixer readouts and translucent page chevrons.
- Deliberate page gestures and a held-app gesture that retains drag ownership.
- Full-draft Discard next to Save, covering page/app/Hide/pad layout edits.

### Soundboard and audio

- 16 removable CC0 starter sounds and 12 assignable pads; WAV/MP3/OGG/FLAC imports.
- Click-to-audition library tray, cross-frame drops, desktop hold-to-lift pad swaps,
  and returning a pad to the tray to unassign it.
- Stable pointer ownership during live updates; Escape/blur/resize cancellation.
- Integrated clip name/icon/volume and Others/Me route editing, deletion and restore.
- Phone hold-to-edit, visible per-clip Save, restart-on-repeat, different-pad
  polyphony and clip-duration playback indicators.
- Clear microphone + sounds → virtual cable → game/call input flow, with a local
  listening branch. More explicit playback/stream error reporting and traces.
- Presentation-only defaults reset that preserves imports, audio routes and pairing.

### Transport and validation

- Dedicated numeric loopback desktop listener with Host/Origin validation and no
  phone socket. Removed the unsupported browser certificate-exception flag;
  phone pinned TLS remains independent.
- Mock review mode skips the live USB watcher, preventing test tunnel changes.
- 136 Python tests, desktop/phone browser interaction suites, Windows and Android
  builds, and an isolated packaged startup check passed.

Physical phone gestures and actual desktop dragging still require hands-on
acceptance. Intermittent sound delivery to a real game/call remains under
investigation; controlled cable captures and automated checks do not establish
that every receiving application reliably plays every sound.

## v0.5.3 — packaged release

Guided virtual-cable audio routing, receiving-microphone recommendations and
explicit connection controls. This is the latest existing GitHub binary release.
