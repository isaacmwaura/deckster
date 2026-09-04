# Deckster — Android app

A thin native **WebView shell** around the web control surface in [`../web/`](../web).
It gives the phone a home-screen app, fullscreen, a landscape lock, keep-awake, native
QR-scan pairing, mDNS auto-discovery, and a pinned secure connection.

## Connection

The app tries **USB first**, then offers Wi-Fi on a native Connect screen:

- **USB (primary, secure):** with the PC agent running and USB debugging enabled, the
  app loads `http://localhost:<port>/` via `adb reverse` (a loopback origin — off-network).
- **Wi-Fi (alternative):** the Connect screen lists PCs **auto-discovered** on the LAN
  (mDNS `_streamctl._tcp`); or **Scan QR** (native camera reads the PC's QR and pairs in
  one step); or type the PC's `http(s)://<lan-ip>:<port>/`. Pair with the 6-digit code.
- If the agent can't be reached the app shows a native **"Can't reach your PC"** screen
  with Refresh + Back (not a browser error page).

## Build & install

Standard Android Studio project (Android SDK platform-34, JDK 17+).

```bash
./gradlew :app:assembleDebug   # -> app/build/outputs/apk/debug/app-debug.apk
```

Sideload: `adb install -r app-debug.apk`, or copy the APK to the phone and open it
(allow "install from this source"). Unsigned is expected — this is open source.
