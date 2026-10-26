# Native Android client

Four native screens: AI chat, home devices, health, and server/settings. The app
does not load a WebView and does not embed a server address. Enter your own
server and administrator credentials; the password is encrypted on the device
using Android Keystore. Mutating panel requests use the session CSRF token.
Android application backup is disabled to keep local account settings out of
device/cloud backups. Use HTTPS when connecting across untrusted networks.

Requires JDK 17+, Android SDK Platform 34 and build-tools 35.0.0. Install the
SDK from Google and accept its terms. The app supports Android 6.0+.

```sh
export ANDROID_SDK_ROOT=/path/to/android-sdk
bash apps/android/scripts/build.sh
```

This produces a locally signed **debug APK** in `apps/android/out/`. Debug
signing is for development. For your own release, supply a private keystore,
its alias and a password in the environment:

```sh
export ANDROID_KEYSTORE=/private/path/release.jks
export ANDROID_KEY_ALIAS=your-alias
export ANDROID_KEYSTORE_PASSWORD=your-private-password
bash apps/android/scripts/build.sh --release
```

Keep the same release signing key for updates. Keys and built APKs are ignored
by Git. The repository does not distribute an original deployer's APK or key.
`ops/release/publish_android_app.py` can install your reviewed APK into your
own hub's data directory; it is not part of the source publication process.

The server's health API has separate credentials and access rules. A hub login
alone does not grant access to an independently protected health service.
