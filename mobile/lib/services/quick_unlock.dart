import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:local_auth/local_auth.dart';

/// Two-factor app unlock: an app-specific PIN that always works, plus an
/// optional biometric shortcut layered on top. Both unlock the same saved
/// AD-ID + password, which is then handed back to the API client to mint a
/// fresh JWT — we never persist the JWT, only the credentials, and only
/// inside platform-encrypted storage (iOS Keychain / Android Keystore).
class QuickUnlock {
  QuickUnlock._();
  static final QuickUnlock instance = QuickUnlock._();

  // Storage keys — namespaced so we can wipe/migrate without nuking other data.
  static const _kEnabled = 'qu_enabled_v2';
  static const _kUsername = 'qu_username';
  static const _kPassword = 'qu_password';
  static const _kPinHash = 'qu_pin_hash';
  static const _kPinSalt = 'qu_pin_salt';
  static const _kBiometricsOn = 'qu_biometrics_on';
  static const _kFailedAttempts = 'qu_failed_attempts';

  // 5 wrong PINs in a row → wipe everything. Forces the user back to a full
  // password sign-in. Lower than the typical 10 because the "attacker" here
  // already has physical access to an unlocked device.
  static const int _maxFailedAttempts = 5;

  final FlutterSecureStorage _storage = const FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock_this_device,
    ),
  );

  final LocalAuthentication _auth = LocalAuthentication();

  // ----- Capability ---------------------------------------------------------

  /// True if the device has a screen lock set (biometric or PIN/passcode).
  /// We require this to attempt biometric prompts at all.
  Future<bool> isDeviceSecure() async {
    try {
      return await _auth.isDeviceSupported();
    } catch (_) {
      return false;
    }
  }

  /// True if at least one biometric is enrolled (Face ID, Touch ID,
  /// fingerprint). When false, we still allow quick unlock — just PIN-only.
  Future<bool> hasBiometricEnrolled() async {
    try {
      if (!await _auth.isDeviceSupported()) return false;
      final list = await _auth.getAvailableBiometrics();
      return list.isNotEmpty;
    } catch (_) {
      return false;
    }
  }

  // ----- State --------------------------------------------------------------

  /// True only if the user has explicitly opted in AND we have a PIN saved.
  Future<bool> isEnabled() async {
    try {
      if (await _storage.read(key: _kEnabled) != '1') return false;
      final hash = await _storage.read(key: _kPinHash);
      final salt = await _storage.read(key: _kPinSalt);
      return hash != null && salt != null;
    } catch (_) {
      // Secure storage init can fail on rooted/jailbroken devices or on
      // first launch after an OS upgrade. Treat as "not enabled" so the
      // user falls back to the password screen instead of a crash.
      return false;
    }
  }

  /// Did the user opt to use biometrics in addition to the PIN?
  Future<bool> isBiometricsOn() async {
    if (!await isEnabled()) return false;
    return (await _storage.read(key: _kBiometricsOn)) == '1';
  }

  Future<int> failedAttempts() async {
    final s = await _storage.read(key: _kFailedAttempts);
    return int.tryParse(s ?? '') ?? 0;
  }

  Future<int> attemptsRemaining() async {
    return _maxFailedAttempts - await failedAttempts();
  }

  // ----- Enable / disable ---------------------------------------------------

  /// Persist creds + PIN + biometric preference. Caller is responsible for
  /// confirming the device supports biometric *before* passing
  /// `useBiometrics: true`.
  Future<void> enable({
    required String username,
    required String password,
    required String pin,
    required bool useBiometrics,
  }) async {
    _validatePin(pin);
    final salt = _randomSalt();
    final hash = _hashPin(pin, salt);
    await _storage.write(key: _kUsername, value: username);
    await _storage.write(key: _kPassword, value: password);
    await _storage.write(key: _kPinHash, value: hash);
    await _storage.write(key: _kPinSalt, value: salt);
    await _storage.write(key: _kBiometricsOn, value: useBiometrics ? '1' : '0');
    await _storage.write(key: _kFailedAttempts, value: '0');
    await _storage.write(key: _kEnabled, value: '1');
  }

  /// Wipe everything. Called on sign-out, on too many PIN failures, or when
  /// the saved creds no longer authenticate against the server.
  Future<void> disable() async {
    try {
      await _storage.deleteAll();
    } catch (_) {
      // best-effort — secure storage might already be unavailable
    }
  }

  // ----- Unlock paths -------------------------------------------------------

  /// Tries the system biometric/credential prompt. Returns saved creds on
  /// success, null on cancel or any failure. Does NOT count toward PIN
  /// lockout — biometric and PIN failures are separate.
  Future<({String username, String password})?> unlockWithBiometrics() async {
    if (!await isBiometricsOn()) return null;
    final ok = await _runBiometricPrompt(reason: 'Unlock AntiGreed');
    if (!ok) return null;
    return _readCreds();
  }

  /// Verify a user-supplied PIN. Returns saved creds on success.
  /// On failure, increments the lockout counter and wipes if the cap is hit.
  Future<UnlockResult> unlockWithPin(String pin) async {
    final salt = await _storage.read(key: _kPinSalt);
    final hash = await _storage.read(key: _kPinHash);
    if (salt == null || hash == null) {
      return UnlockResult.notEnabled();
    }
    if (_hashPin(pin, salt) == hash) {
      await _storage.write(key: _kFailedAttempts, value: '0');
      final creds = await _readCreds();
      if (creds == null) return UnlockResult.notEnabled();
      return UnlockResult.success(creds);
    }
    final next = (await failedAttempts()) + 1;
    if (next >= _maxFailedAttempts) {
      await disable();
      return UnlockResult.lockedOut();
    }
    await _storage.write(key: _kFailedAttempts, value: '$next');
    return UnlockResult.wrongPin(_maxFailedAttempts - next);
  }

  /// Run a biometric prompt without unlocking — used during enable() flow
  /// to *verify the device can authenticate this user* before we trust
  /// quick unlock with their creds.
  Future<bool> testBiometric() async {
    if (!await hasBiometricEnrolled()) return false;
    return _runBiometricPrompt(reason: 'Confirm to enable quick unlock');
  }

  // ----- Internals ----------------------------------------------------------

  Future<({String username, String password})?> _readCreds() async {
    final user = await _storage.read(key: _kUsername);
    final pass = await _storage.read(key: _kPassword);
    if (user == null || pass == null) return null;
    return (username: user, password: pass);
  }

  Future<bool> _runBiometricPrompt({required String reason}) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          // We have our own PIN as fallback, so reject device-credential
          // fallback here — any failure should drop the user to our PIN
          // screen, not let them in with the device passcode (which might
          // be different from their app PIN).
          biometricOnly: true,
          stickyAuth: true,
          useErrorDialogs: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }

  void _validatePin(String pin) {
    if (pin.length < 4 || pin.length > 6) {
      throw ArgumentError('PIN must be 4-6 digits');
    }
    if (!RegExp(r'^[0-9]+$').hasMatch(pin)) {
      throw ArgumentError('PIN must be digits only');
    }
  }

  String _randomSalt() {
    final r = Random.secure();
    final bytes = List<int>.generate(16, (_) => r.nextInt(256));
    return base64Url.encode(bytes);
  }

  String _hashPin(String pin, String salt) {
    final input = utf8.encode('$salt:$pin');
    return sha256.convert(input).toString();
  }
}

/// Tagged-union return for `unlockWithPin`. Lets the screen tell the user
/// "wrong PIN, 3 attempts left" vs "you've been locked out, sign in again."
sealed class UnlockResult {
  const UnlockResult();
  factory UnlockResult.success(({String username, String password}) creds) =
      UnlockSuccess;
  factory UnlockResult.wrongPin(int attemptsLeft) = UnlockWrongPin;
  factory UnlockResult.lockedOut() = UnlockLockedOut;
  factory UnlockResult.notEnabled() = UnlockNotEnabled;
}

class UnlockSuccess extends UnlockResult {
  const UnlockSuccess(this.creds);
  final ({String username, String password}) creds;
}

class UnlockWrongPin extends UnlockResult {
  const UnlockWrongPin(this.attemptsLeft);
  final int attemptsLeft;
}

class UnlockLockedOut extends UnlockResult {
  const UnlockLockedOut();
}

class UnlockNotEnabled extends UnlockResult {
  const UnlockNotEnabled();
}
