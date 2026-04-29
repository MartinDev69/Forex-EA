import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

/// Stores the user's AD-ID and password in platform-encrypted storage
/// (iOS Keychain / Android Keystore-backed EncryptedSharedPreferences) so we
/// can re-issue a fresh token after a biometric/PIN unlock without making the
/// user retype their password every launch.
class QuickUnlock {
  QuickUnlock._();
  static final QuickUnlock instance = QuickUnlock._();

  static const _kEnabled = 'qu_enabled';
  static const _kUsername = 'qu_username';
  static const _kPassword = 'qu_password';

  // Use EncryptedSharedPreferences on Android — survives backup, faster than
  // the AndroidKeyStore-only path, still backed by a Keystore-protected key.
  final FlutterSecureStorage _storage = const FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock_this_device,
    ),
  );

  final LocalAuthentication _auth = LocalAuthentication();

  /// True only if the user has explicitly opted in AND we have creds saved.
  Future<bool> isEnabled() async {
    final flag = await _storage.read(key: _kEnabled);
    if (flag != '1') return false;
    final user = await _storage.read(key: _kUsername);
    final pass = await _storage.read(key: _kPassword);
    return user != null && pass != null;
  }

  /// True if the device has biometric hardware OR a device PIN/passcode set.
  /// We accept either — local_auth will fall back to device credential when
  /// biometrics aren't enrolled.
  Future<bool> isAvailable() async {
    try {
      final supported = await _auth.isDeviceSupported();
      if (!supported) return false;
      final canCheck = await _auth.canCheckBiometrics;
      // canCheckBiometrics is false on devices with only a PIN — but we still
      // want to allow that path, so isDeviceSupported is the real gate.
      return canCheck || supported;
    } catch (_) {
      return false;
    }
  }

  /// Persist creds and turn the flag on. Caller is responsible for confirming
  /// the user actually wants this (don't enable silently after every login).
  Future<void> enable({required String username, required String password}) async {
    await _storage.write(key: _kUsername, value: username);
    await _storage.write(key: _kPassword, value: password);
    await _storage.write(key: _kEnabled, value: '1');
  }

  /// Wipe everything. Called on sign-out, after a 401 (creds rotated server
  /// side), or when the user disables quick unlock from settings.
  Future<void> disable() async {
    await _storage.delete(key: _kEnabled);
    await _storage.delete(key: _kUsername);
    await _storage.delete(key: _kPassword);
  }

  /// Read saved creds AFTER a successful unlock prompt. Returns null on
  /// cancel, fail, or if quick unlock isn't enabled.
  Future<({String username, String password})?> unlockAndRead() async {
    final ok = await _prompt();
    if (!ok) return null;
    final user = await _storage.read(key: _kUsername);
    final pass = await _storage.read(key: _kPassword);
    if (user == null || pass == null) return null;
    return (username: user, password: pass);
  }

  Future<bool> _prompt() async {
    try {
      return await _auth.authenticate(
        localizedReason: 'Unlock AntiGreed',
        options: const AuthenticationOptions(
          // false = allow PIN/passcode fallback when biometrics fail or
          // aren't enrolled. Matches what the user expects from "PIN or
          // biometrics".
          biometricOnly: false,
          stickyAuth: true,
          // Keep the system-modal nature — don't show our own UI behind it.
          useErrorDialogs: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }
}
