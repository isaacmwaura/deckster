# Deckster — Android development

To install and connect Deckster, follow the combined
[PC and phone setup guide](../README.md#install-on-your-pc-and-phone).
The instructions here are for building the Android app from source.

A thin **native WebView shell** around the web control surface in [`../web/`](../web).
It gives the phone a home-screen app, guaranteed fullscreen, a hard landscape lock,
mounted/battery screen modes, native QR-scan pairing, mDNS auto-discovery, and a pinned secure
connection — things a plain browser tab can't. The [public manual](../docs/manual.html) covers setup; the [architecture guide](../docs/architecture.md#android-lifecycle-and-persistence) explains native and shared-renderer ownership.

## Connection

The app tries **USB first**, then offers Wi-Fi on a native Connect screen:

- **USB (primary, secure):** with the PC agent running and the phone on USB, the app
  loads `http(s)://localhost:<port>/` via `adb reverse` (a loopback origin — off-network).
  Requires USB debugging enabled on the phone.
- **Wi-Fi (alternative):** the Connect screen lists PCs **auto-discovered** on the LAN
  (mDNS `_streamctl._tcp`); or **Scan QR** (native camera reads the PC's QR and pairs in
  one step); or type the PC's `http(s)://<lan-ip>:<port>/`. Pair with the 6-digit code.
- **Secure (TLS):** when the agent serves HTTPS, the app **pins** its certificate
   fingerprint (from QR/discovery or the saved PC) — encrypted with no warning, nothing to
  install on the phone.

## Build & install

To use Deckster, download the companion APK from the same release as the PC
EXE. The instructions below are for developers. The packaged PC app includes
Python and USB tools. The shared phone page reports its viewport dimensions
automatically to the desktop preview; users do not enter a screen size.

This is a standard Android Studio project. It is **not** built by the Python repo's
tooling and can't be compiled on a machine without the Android SDK.

1. Open the `android/` folder in **Android Studio** with JDK 17 and SDK 34. Sync
   Gradle using the committed wrapper.
2. Build a debug APK: **Build → Build APK(s)**, or run
   `./gradlew :app:assembleDebug` (`.\gradlew.bat :app:assembleDebug` in PowerShell)
   → `app/build/outputs/apk/debug/app-debug.apk`.
3. Sideload: `adb install -r app-debug.apk`, or copy the APK to the phone and open it
   (allow "install from this source"). Debug APKs are debug-signed; a production
   release needs an explicitly configured release signing key.

## Current interface (v0.7.0 / versionCode 18)

The PC serves the current Mixer, Soundboard, Devices and Media pages; the APK
hosts that same renderer. Arrange pages and app visibility in the PC workspace.
Soundboard has 12 assignable pads, 16 CC0 starter sounds, Others/Me routes,
clip-duration feedback and a 1.5-second hold-to-edit gesture. Desktop pad holds
reorder sounds; physical phone holds continue to open their clip settings.
Page navigation uses layout-relative chevrons and deliberate swipes.

Mounted mode remains the default and keeps the screen on only while the app is
in the foreground. Choose Battery on the Connect screen or in the phone top bar
to use Android's normal screen timeout. The choice persists across restarts and
USB/Wi-Fi handoffs. Backgrounding cancels discovery and connection probes, closes
the phone socket and suspends meters/rendering; return obtains current state
without replaying previous controls. Saver mode retains only the latest state
and stops the device meter frame loop until wake. A failed WebView renderer is
released and can be recreated with Refresh on the native error screen.

Upgrade an installed app with `adb install -r` to preserve its native pairing
and connection data. The PC's sound library and saved layout remain on the PC.
A browser's localStorage is per origin; native token storage bridges USB and
Wi-Fi reconnection. Phone pinned TLS is independent of the desktop loopback UI.

For app downloads and installation, use the combined
[PC and phone setup guide](../README.md#install-on-your-pc-and-phone).
See [CHANGELOG.md](../CHANGELOG.md) for changes and validation limits.

## Status

Implemented: WebView shell (fullscreen, landscape lock, foreground mounted/battery policy,
explicit renderer release/recovery and auto-reconnect);
a **Compose Connect screen**; **USB-first** connection; **mDNS/NSD auto-discovery** of
the PC on Wi-Fi; native **CameraX + ML Kit QR-scan** pairing; and **cert-pinned TLS**
(accepts the agent's self-signed cert only when it matches the selected/saved pin).

Launcher icons are included at all Android densities. Physical touch/navigation
and real game/call sound delivery still need hands-on validation.
