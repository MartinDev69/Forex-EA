import 'package:flutter/material.dart';
import '../api/client.dart';
import '../services/quick_unlock.dart';

/// Shown on launch when the user has previously enabled quick unlock.
/// On tap → biometric/PIN prompt → silent re-login → home. On failure or
/// "Use password instead" → bounce back to the regular login screen.
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
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    // Auto-fire the prompt as soon as the screen mounts so the user lands
    // straight on Face ID / fingerprint without an extra tap.
    WidgetsBinding.instance.addPostFrameCallback((_) => _attempt());
  }

  Future<void> _attempt() async {
    if (_busy) return;
    setState(() { _busy = true; _error = null; });
    try {
      final creds = await QuickUnlock.instance.unlockAndRead();
      if (creds == null) {
        if (!mounted) return;
        setState(() => _error = 'Unlock cancelled. Tap to try again or sign in with your password.');
        return;
      }
      await widget.apiClient.login(creds.username, creds.password);
      if (!mounted) return;
      widget.onUnlocked();
    } on ApiException catch (e) {
      // Stored creds no longer valid (admin reset password, account deleted,
      // server rotated AUTH_SECRET). Disable quick unlock and bounce to login.
      await QuickUnlock.instance.disable();
      if (!mounted) return;
      setState(() => _error = 'Saved sign-in is no longer valid: ${e.detail}');
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Could not reach the server. Check the API URL and try again.');
    } finally {
      if (mounted) setState(() => _busy = false);
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
                    height: 160,
                    fit: BoxFit.contain,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Quick unlock',
                    style: TextStyle(
                      color: Colors.grey.shade400,
                      letterSpacing: 3,
                      fontSize: 11,
                    ),
                  ),
                  const SizedBox(height: 32),
                  if (_error != null) ...[
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
                    const SizedBox(height: 16),
                  ],
                  SizedBox(
                    width: double.infinity,
                    height: 48,
                    child: FilledButton.icon(
                      onPressed: _busy ? null : _attempt,
                      icon: const Icon(Icons.fingerprint),
                      label: _busy
                          ? const SizedBox(
                              width: 20, height: 20,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Unlock with biometrics or PIN'),
                    ),
                  ),
                  const SizedBox(height: 12),
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
