import 'package:flutter/material.dart';
import 'api/client.dart';
import 'api/config.dart';
import 'screens/home.dart';
import 'screens/lock.dart';
import 'screens/login.dart';
import 'services/quick_unlock.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await loadThemeMode();
  // Defensive: secure storage init can fail on rooted/jailbroken devices or
  // first launch after an OS upgrade. Treat any failure as "no quick unlock"
  // so the app falls back to the password screen instead of crashing.
  bool qu = false;
  try {
    qu = await QuickUnlock.instance.isEnabled();
  } catch (_) {
    qu = false;
  }
  runApp(AntiGreedApp(
    apiClient: ApiClient(baseUrl: apiBaseUrl),
    startWithQuickUnlock: qu,
  ));
}

/// How long the app can be in the background before we re-show the lock
/// screen on resume. Short enough that an unattended phone can't be picked
/// up and casually browsed; long enough that a quick switch to a 2FA app
/// or copy-paste from another tab doesn't force a re-unlock.
const Duration _autoLockAfter = Duration(seconds: 30);

class AntiGreedApp extends StatefulWidget {
  const AntiGreedApp({
    super.key,
    required this.apiClient,
    required this.startWithQuickUnlock,
  });

  final ApiClient apiClient;
  final bool startWithQuickUnlock;

  @override
  State<AntiGreedApp> createState() => _AntiGreedAppState();
}

class _AntiGreedAppState extends State<AntiGreedApp> with WidgetsBindingObserver {
  // 'lock'  → biometric + PIN screen (quick unlock is enabled)
  // 'login' → password screen (cold start or user opted out)
  // 'home'  → main app (token live)
  late String _route;
  DateTime? _backgroundedAt;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _route = widget.startWithQuickUnlock ? 'lock' : 'login';
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused || state == AppLifecycleState.hidden) {
      _backgroundedAt = DateTime.now();
    } else if (state == AppLifecycleState.resumed) {
      _maybeRelock();
    }
  }

  Future<void> _maybeRelock() async {
    final stamp = _backgroundedAt;
    _backgroundedAt = null;
    if (stamp == null) return;
    if (_route != 'home') return; // already on lock/login — nothing to do
    if (DateTime.now().difference(stamp) < _autoLockAfter) return;
    if (!await QuickUnlock.instance.isEnabled()) return;
    // Drop the live token so a stolen phone can't keep using it.
    widget.apiClient.logout();
    if (!mounted) return;
    setState(() => _route = 'lock');
  }

  void _toLogin() => setState(() => _route = 'login');
  void _toHome() => setState(() => _route = 'home');

  /// Plain sign-out: drop the JWT but keep the saved PIN/biometric so the
  /// next launch is a one-tap unlock instead of a full password retype.
  /// If quick unlock isn't set up, fall back to the password screen.
  Future<void> _signOut() async {
    widget.apiClient.logout();
    final hasQuickUnlock = await QuickUnlock.instance.isEnabled();
    if (!mounted) return;
    setState(() => _route = hasQuickUnlock ? 'lock' : 'login');
  }

  /// Full reset: drop the JWT AND wipe saved creds + PIN. Used for the
  /// explicit "Forget this device" action — useful when handing the phone
  /// to someone else or if the user wants to re-enroll a different PIN.
  Future<void> _forgetDevice() async {
    widget.apiClient.logout();
    await QuickUnlock.instance.disable();
    if (!mounted) return;
    setState(() => _route = 'login');
  }

  @override
  Widget build(BuildContext context) {
    Widget body;
    switch (_route) {
      case 'lock':
        body = LockScreen(
          apiClient: widget.apiClient,
          onUnlocked: _toHome,
          onUsePassword: _toLogin,
        );
        break;
      case 'home':
        body = HomeScreen(
          apiClient: widget.apiClient,
          onSignedOut: _signOut,
          onForgetDevice: _forgetDevice,
        );
        break;
      case 'login':
      default:
        body = LoginScreen(
          apiClient: widget.apiClient,
          onSignedIn: _toHome,
        );
    }
    return ValueListenableBuilder<ThemeMode>(
      valueListenable: themeMode,
      builder: (context, mode, _) => MaterialApp(
        title: 'AntiGreed',
        debugShowCheckedModeBanner: false,
        theme: lightTheme(),
        darkTheme: darkTheme(),
        themeMode: mode,
        home: body,
      ),
    );
  }
}
