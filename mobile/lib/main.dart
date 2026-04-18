import 'package:flutter/material.dart';
import 'api/client.dart';
import 'api/config.dart';
import 'screens/home.dart';

void main() {
  runApp(AntiGreedApp(apiClient: ApiClient(baseUrl: apiBaseUrl)));
}

class AntiGreedApp extends StatelessWidget {
  const AntiGreedApp({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AntiGreed',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF0E7C42),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
        scaffoldBackgroundColor: const Color(0xFF0B1220),
        cardTheme: const CardThemeData(
          elevation: 0,
          color: Color(0xFF13203A),
          margin: EdgeInsets.symmetric(vertical: 6, horizontal: 12),
        ),
      ),
      home: HomeScreen(apiClient: apiClient),
    );
  }
}
