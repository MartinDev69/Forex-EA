import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/client.dart';
import '../services/quick_unlock.dart';

/// Shown on launch (or on resume from background) when the user has quick
/// unlock enabled. Two paths to in: biometric prompt, or 4–6 digit PIN.
/// Either one re-authenticates with the API and lands on home.
class LockScreen extends StatefulWidget {
  const LockScreen({
    super.key,
    required this.apiClient,
    required this.onUnlocked,
    required this.onUsePassword,
  });

  final ApiClient apiClient;
  final VoidCallback onUnlocked;
  final VoidCallback onUsePassword;

  @override
  State<LockScreen> createState() => _LockScreenState();
}

class _LockScreenState extends State<LockScreen> {
  final _pin = TextEditingController();
  final _focus = FocusNode();
  bool _busy = false;
  bool _biometricAvailable = false;
  int _attemptsLeft = 5;
  String? _error;
  String? _info;

  @override
  void initState() {
    super.initState();
    _bootstrap();
  }

  @override
  void dispose() {
    _pin.dispose();
    _focus.dispose();
    super.dispose();
  }

  Future<void> _bootstrap() async {
    final left = await QuickUnlock.instance.attemptsRemaining();
    final bio = await QuickUnlock.instance.isBiometricsOn();
    if (!mounted) return;
    setState(() {
      _attemptsLeft = left;
      _biometricAvailable = bio;
    });
    if (bio) {
      // Auto-fire biometric on launch — saves a tap when the user opens
      // the app expecting Face ID to greet them. If it fails or the user
      // cancels, the PIN field is right there as the fallback.
      _tryBiometric();
    } else {
      _focus.requestFocus();
    }
  }

  Future<void> _tryBiometric() async {
    if (_busy) return;
    setState(() { _busy = true; _info = null; _error = null; });
    final creds = await QuickUnlock.instance.unlockWithBiometrics();
    if (!mounted) return;
    if (creds == null) {
      setState(() {
        _busy = false;
        _info = 'Biometric cancelled. Enter your PIN to unlock.';
      });
      _focus.requestFocus();
      return;
    }
    await _signInWith(creds);
  }

  Future<void> _trySubmitPin(String value) async {
    if (_busy) return;
    if (value.length < 4) return;
    setState(() { _busy = true; _info = null; _error = null; });
    final result = await QuickUnlock.instance.unlockWithPin(value);
    if (!mounted) return;
    switch (result) {
      case UnlockSuccess(:final creds):
        await _signInWith(creds);
      case UnlockWrongPin(:final attemptsLeft):
        HapticFeedback.heavyImpact();
        _pin.clear();
        setState(() {
          _busy = false;
          _attemptsLeft = attemptsLeft;
          _error = attemptsLeft == 1
              ? 'Wrong PIN — 1 attempt left.'
              : 'Wrong PIN — $attemptsLeft attempts left.';
        });
        _focus.requestFocus();
      case UnlockLockedOut():
        HapticFeedback.heavyImpact();
        setState(() {
          _busy = false;
          _error = 'Too many wrong attempts — quick unlock disabled. '
              'Sign in with your password.';
        });
      case UnlockNotEnabled():
        widget.onUsePassword();
    }
  }

  Future<void> _signInWith(({String username, String password}) creds) async {
    try {
      await widget.apiClient.login(creds.username, creds.password);
      if (!mounted) return;
      widget.onUnlocked();
    } on ApiException catch (e) {
      // Saved creds rejected by the server (admin reset password,
      // AUTH_SECRET rotated, account deleted). Wipe and bounce.
      await QuickUnlock.instance.disable();
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = 'Saved sign-in is no longer valid: ${e.detail}';
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = 'Couldn\'t reach the server. Check your connection.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Image.asset(
                    'assets/antigreed-logo.png',
                    height: 140,
                    fit: BoxFit.contain,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Locked',
                    style: TextStyle(
                      color: Colors.grey.shade400,
                      letterSpacing: 3,
                      fontSize: 11,
                    ),
                  ),
                  const SizedBox(height: 32),
                  TextField(
                    controller: _pin,
                    focusNode: _focus,
                    obscureText: true,
                    keyboardType: TextInputType.number,
                    enabled: !_busy,
                    maxLength: 6,
                    inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                    style: const TextStyle(
                      fontSize: 28,
                      letterSpacing: 16,
                      fontFeatures: [FontFeature.tabularFigures()],
                    ),
                    textAlign: TextAlign.center,
                    decoration: const InputDecoration(
                      border: OutlineInputBorder(),
                      counterText: '',
                      hintText: 'PIN',
                    ),
                    onChanged: (v) {
                      // Auto-submit when the user types the most common length.
                      if (v.length == 4) _trySubmitPin(v);
                    },
                    onSubmitted: _trySubmitPin,
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: Colors.red.shade900.withValues(alpha: 0.3),
                        border: Border.all(color: Colors.red.shade700),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Text(
                        _error!,
                        style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                      ),
                    ),
                  ] else if (_info != null) ...[
                    const SizedBox(height: 12),
                    Text(
                      _info!,
                      style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                    ),
                  ] else if (_attemptsLeft < 5) ...[
                    const SizedBox(height: 12),
                    Text(
                      '$_attemptsLeft attempts left',
                      style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
                    ),
                  ],
                  const SizedBox(height: 16),
                  if (_biometricAvailable)
                    SizedBox(
                      width: double.infinity,
                      height: 48,
                      child: OutlinedButton.icon(
                        onPressed: _busy ? null : _tryBiometric,
                        icon: const Icon(Icons.fingerprint),
                        label: const Text('Use biometrics'),
                      ),
                    ),
                  const SizedBox(height: 8),
                  TextButton(
                    onPressed: _busy ? null : widget.onUsePassword,
                    child: const Text('Sign in with password instead'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
