// Where the mobile app looks for the FastAPI backend.
//
// Default points at the production VPS. For local development, override with:
//   flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000      (iOS sim)
//   flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000       (Android emu)
//   flutter run --dart-define=API_BASE_URL=http://<mac-lan-ip>:8000   (real device on LAN)

const _prodApiBaseUrl = 'http://163.5.178.251:8000';

String get apiBaseUrl {
  const override = String.fromEnvironment('API_BASE_URL');
  if (override.isNotEmpty) return override;
  return _prodApiBaseUrl;
}

/// Semantic version of the app. Keep in sync with `version:` in
/// pubspec.yaml. Shown in the dashboard footer.
const appVersion = '1.0.0';

/// Build sub-tag — bumped on every visible UI change so we can verify
/// at a glance which APK is actually installed on a phone. Shown next
/// to the version in the dashboard footer.
const appBuildTag = 'b12';
