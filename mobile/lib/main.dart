import 'package:flutter/material.dart';
import 'api/client.dart';
import 'api/config.dart';
import 'screens/home.dart';
import 'screens/login.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await loadThemeMode();
  runApp(AntiGreedApp(apiClient: ApiClient(baseUrl: apiBaseUrl)));
}

class AntiGreedApp extends StatefulWidget {
  const AntiGreedApp({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<AntiGreedApp> createState() => _AntiGreedAppState();
}

class _AntiGreedAppState extends State<AntiGreedApp> {
  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<ThemeMode>(
      valueListenable: themeMode,
      builder: (context, mode, _) => MaterialApp(
        title: 'AntiGreed',
        debugShowCheckedModeBanner: false,
        theme: lightTheme(),
        darkTheme: darkTheme(),
        themeMode: mode,
        home: widget.apiClient.token == null
            ? LoginScreen(
                apiClient: widget.apiClient,
                onSignedIn: () => setState(() {}),
              )
            : HomeScreen(
                apiClient: widget.apiClient,
                onSignedOut: () => setState(() {}),
              ),
      ),
    );
  }
}
