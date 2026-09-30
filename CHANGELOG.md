# Changelog

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
