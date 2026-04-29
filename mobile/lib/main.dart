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
  // Probe quick-unlock state once at startup so the first frame routes
  // straight to lock or login without an intermediate spinner.
  final qu = await QuickUnlock.instance.isEnabled();
  runApp(AntiGreedApp(
    apiClient: ApiClient(baseUrl: apiBaseUrl),
    startWithQuickUnlock: qu,
  ));
}

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

class _AntiGreedAppState extends State<AntiGreedApp> {
  // null     → no token yet, decide between lock and login
  // 'lock'   → show LockScreen (quick unlock available)
  // 'login'  → show LoginScreen (cold sign-in)
  // 'home'   → show HomeScreen (token live)
  late String _route;

  @override
  void initState() {
    super.initState();
    _route = widget.startWithQuickUnlock ? 'lock' : 'login';
  }

  void _toLogin() => setState(() => _route = 'login');
  void _toHome() => setState(() => _route = 'home');

  Future<void> _signOut() async {
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
          onSignedOut: () => _signOut(),
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
