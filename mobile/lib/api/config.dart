// Where the mobile app looks for the FastAPI backend.
//
// For local development:
//   - iOS simulator on the same Mac:     http://127.0.0.1:8000
//   - Android emulator on the same Mac:  http://10.0.2.2:8000
//   - Physical device on same Wi-Fi:     http://<your-mac-lan-ip>:8000
//
// Override at runtime with:
//   flutter run --dart-define=API_BASE_URL=http://192.168.1.10:8000
import 'dart:io' show Platform;

String get apiBaseUrl {
  const override = String.fromEnvironment('API_BASE_URL');
  if (override.isNotEmpty) return override;
  try {
    if (Platform.isAndroid) return 'http://10.0.2.2:8000';
  } catch (_) {
    // Platform unavailable on web — fall through.
  }
  return 'http://127.0.0.1:8000';
}
