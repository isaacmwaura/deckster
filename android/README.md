# Deckster — Android app

A thin **native WebView shell** around the web control surface in [`../web/`](../web).
It gives the phone a home-screen app, guaranteed fullscreen, a hard landscape lock,
keep-awake, native QR-scan pairing, mDNS auto-discovery, and a pinned secure
connection — things a plain browser tab can't. The [public manual](../docs/manual.html) covers setup and the shared phone interface.

## Connection

The app tries **USB first**, then offers Wi-Fi on a native Connect screen:

- **USB (primary, secure):** with the PC agent running and the phone on USB, the app
  loads `http(s)://localhost:<port>/` via `adb reverse` (a loopback origin — off-network).
  Requires USB debugging enabled on the phone.
- **Wi-Fi (alternative):** the Connect screen lists PCs **auto-discovered** on the LAN
  (mDNS `_streamctl._tcp`); or **Scan QR** (native camera reads the PC's QR and pairs in
  one step); or type the PC's `http(s)://<lan-ip>:<port>/`. Pair with the 6-digit code.
- **Secure (TLS):** when the agent serves HTTPS, the app **pins** its certificate
  fingerprint (carried in the mDNS record) — encrypted with no warning, nothing to
  install on the phone.

## Build & install

This is a standard Android Studio project. It is **not** built by the Python repo's
tooling and can't be compiled on a machine without the Android SDK.

1. Open the `android/` folder in **Android Studio** (Giraffe+). Let it sync Gradle and
   generate the Gradle wrapper.
2. Build a debug APK: **Build → Build APK(s)**, or from the terminal once the wrapper
   exists: `./gradlew assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`.
3. Sideload: `adb install -r app-debug.apk`, or copy the APK to the phone and open it
   (allow "install from this source"). Unsigned is expected — this is open source.

## Current interface (v0.6.3 / versionCode 12)

The PC serves the current Mixer, Soundboard, Devices and Media pages; the APK
hosts that same renderer. Arrange pages and app visibility in the PC workspace.
Soundboard has 12 assignable pads, 16 CC0 starter sounds, Others/Me routes,
clip-duration feedback and a 1.5-second hold-to-edit gesture. Desktop pad holds
reorder sounds; physical phone holds continue to open their clip settings.
Page navigation uses layout-relative chevrons and deliberate swipes.

Upgrade an installed app with `adb install -r` to preserve its native pairing
and connection data. The PC's sound library and saved layout remain on the PC.
A browser's localStorage is per origin; native token storage bridges USB and
Wi-Fi reconnection. Phone pinned TLS is independent of the desktop loopback UI.

The latest existing GitHub binary release is v0.5.3. These changes are in the
v0.6.3 source; see [CHANGELOG.md](../CHANGELOG.md) before choosing a download.

## Status

Implemented: WebView shell (fullscreen, landscape lock, keep-awake, auto-reconnect);
a **Compose Connect screen**; **USB-first** connection; **mDNS/NSD auto-discovery** of
the PC on Wi-Fi; native **CameraX + ML Kit QR-scan** pairing; and **cert-pinned TLS**
(accepts the agent's self-signed cert only when it matches the fingerprint from mDNS).

Launcher icons are included at all Android densities. Physical touch/navigation
and real game/call sound delivery still need hands-on validation.
