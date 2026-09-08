# Deckster — Android app

A thin **native WebView shell** around the web control surface in [`../web/`](../web).
It gives the phone a home-screen app, guaranteed fullscreen, a hard landscape lock,
keep-awake, native QR-scan pairing, mDNS auto-discovery, and a pinned secure
connection — things a plain browser tab can't.

## Connection

The app tries **USB first**, then offers Wi-Fi on a native Connect screen:

- **USB (primary, secure):** with the PC agent running and the phone on USB, the app
  loads `http://localhost:<port>/` via `adb reverse` (a loopback origin — off-network).
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

## Status

Implemented: WebView shell (fullscreen, landscape lock, keep-awake, auto-reconnect);
a **Compose Connect screen**; **USB-first** connection; **mDNS/NSD auto-discovery** of
the PC on Wi-Fi; native **CameraX + ML Kit QR-scan** pairing; and **cert-pinned TLS**
(accepts the agent's self-signed cert only when it matches the fingerprint from mDNS).

Remaining: a real launcher icon (currently the Android system placeholder).
