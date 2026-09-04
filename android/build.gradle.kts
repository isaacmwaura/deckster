// Root build file. Plugin versions are pinned here and applied per-module.
// Open this `android/` folder in Android Studio and let it sync + generate the
// Gradle wrapper (gradlew) — the wrapper jar is intentionally not committed.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
