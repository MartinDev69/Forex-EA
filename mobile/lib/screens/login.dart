import 'package:flutter/material.dart';
import '../api/client.dart';
import '../services/quick_unlock.dart';
import 'pin_setup.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.apiClient, required this.onSignedIn});
  final ApiClient apiClient;
  final VoidCallback onSignedIn;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _user = TextEditingController();
  final _pass = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _user.dispose();
    _pass.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();
    if (_user.text.isEmpty || _pass.text.isEmpty) {
      setState(() => _error = 'Enter your AD-ID and password.');
      return;
    }
    setState(() { _busy = true; _error = null; });
    try {
      final username = _user.text.trim();
      final password = _pass.text;
      await widget.apiClient.login(username, password);
      // Sign-in succeeded. Offer quick unlock as an optional follow-up,
      // but never block the user from getting to the dashboard. If they
      // skip, dismiss, or any step fails, sign-in still completes.
      await _maybeOfferQuickUnlock(username: username, password: password);
      if (!mounted) return;
      widget.onSignedIn();
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.detail);
    } catch (_) {
      if (!mounted) return;
      setState(() => _error = 'Login failed. Check the API URL and try again.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _maybeOfferQuickUnlock({
    required String username,
    required String password,
  }) async {
    // Already set up on this device → just keep saved creds in sync (in
    // case the password changed server-side) and skip the setup dialog.
    if (await QuickUnlock.instance.isEnabled()) {
      await QuickUnlock.instance.refreshCredentials(
        username: username,
        password: password,
      );
      return;
    }
    if (!await QuickUnlock.instance.isDeviceSecure()) return;
    if (!mounted) return;
    final wantsIt = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Set up quick unlock?'),
        content: const Text(
          'Skip the password on the next launch by unlocking with a 4–6 digit '
          'PIN, optionally backed by Face ID / fingerprint.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Not now'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Set up'),
          ),
        ],
      ),
    );
    if (wantsIt != true) return;

    if (!mounted) return;
    final pin = await showPinSetup(context);
    if (pin == null) return; // user cancelled

    bool useBiometrics = false;
    if (await QuickUnlock.instance.hasBiometricEnrolled()) {
      if (!mounted) return;
      final wantBio = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Enable biometrics too?'),
          content: const Text(
            'Use Face ID / fingerprint as a faster shortcut. Your PIN still '
            'works as a fallback any time.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('PIN only'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('Enable'),
            ),
          ],
        ),
      );
      if (wantBio == true) {
        // Verify the prompt actually works on this device before we trust
        // it for unlock — otherwise the user might enable biometrics they
        // can't actually pass and get stuck on the lock screen.
        useBiometrics = await QuickUnlock.instance.testBiometric();
        if (!useBiometrics && mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text(
              'Biometric check didn\'t pass — saved with PIN only.',
            )),
          );
        }
      }
    }

    await QuickUnlock.instance.enable(
      username: username,
      password: password,
      pin: pin,
      useBiometrics: useBiometrics,
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Quick unlock enabled.')),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(
        fit: StackFit.expand,
        children: [
          // Iridescent robot hero behind a vignette — matches the design
          // and reads as a trading product, not a generic form.
          Image.asset(
            'assets/img/robot-iridescent.jpg',
            fit: BoxFit.cover,
            color: Colors.black.withValues(alpha: 0.55),
            colorBlendMode: BlendMode.darken,
          ),
          Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [
                  Color(0xCC000000),
                  Color(0xEE000000),
                  Color(0xFF000000),
                ],
              ),
            ),
          ),
          SafeArea(
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
                    'Control plane',
                    style: TextStyle(
                      color: Colors.grey.shade400,
                      letterSpacing: 3,
                      fontSize: 11,
                    ),
                  ),
                  const SizedBox(height: 32),
                  TextField(
                    controller: _user,
                    autocorrect: false,
                    enableSuggestions: false,
                    textInputAction: TextInputAction.next,
                    decoration: const InputDecoration(
                      labelText: 'AD-ID',
                      border: OutlineInputBorder(),
                      prefixIcon: Icon(Icons.badge_outlined),
                    ),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    controller: _pass,
                    obscureText: true,
                    textInputAction: TextInputAction.done,
                    onSubmitted: (_) => _submit(),
                    decoration: const InputDecoration(
                      labelText: 'Password',
                      border: OutlineInputBorder(),
                      prefixIcon: Icon(Icons.lock_outline),
                    ),
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
                  ],
                  const SizedBox(height: 20),
                  SizedBox(
                    width: double.infinity,
                    height: 48,
                    child: FilledButton(
                      onPressed: _busy ? null : _submit,
                      child: _busy
                          ? const SizedBox(
                              width: 20, height: 20,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Sign in'),
                    ),
                  ),
                  const SizedBox(height: 24),
                  Text(
                    'Sign in with the AD-ID assigned to you by the admin and the '
                    'password you set from the email link.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
                  ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
