import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _prefsKey = 'antigreed.themeMode';
const _seedColor = Color(0xFF0E7C42);

final ValueNotifier<ThemeMode> themeMode = ValueNotifier(ThemeMode.dark);

Future<void> loadThemeMode() async {
  final prefs = await SharedPreferences.getInstance();
  final raw = prefs.getString(_prefsKey);
  themeMode.value = switch (raw) {
    'light' => ThemeMode.light,
    'dark' => ThemeMode.dark,
    _ => ThemeMode.dark,
  };
}

Future<void> setThemeMode(ThemeMode mode) async {
  themeMode.value = mode;
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(_prefsKey, mode == ThemeMode.light ? 'light' : 'dark');
}

Future<void> toggleThemeMode() =>
    setThemeMode(themeMode.value == ThemeMode.dark ? ThemeMode.light : ThemeMode.dark);

ThemeData darkTheme() => ThemeData(
      colorScheme: ColorScheme.fromSeed(
        seedColor: _seedColor,
        brightness: Brightness.dark,
      ),
      useMaterial3: true,
      scaffoldBackgroundColor: const Color(0xFF0B1220),
      cardTheme: const CardThemeData(
        elevation: 0,
        color: Color(0xFF13203A),
        margin: EdgeInsets.symmetric(vertical: 6, horizontal: 12),
      ),
    );

ThemeData lightTheme() => ThemeData(
      colorScheme: ColorScheme.fromSeed(
        seedColor: _seedColor,
        brightness: Brightness.light,
      ),
      useMaterial3: true,
      scaffoldBackgroundColor: const Color(0xFFF5F7FA),
      cardTheme: const CardThemeData(
        elevation: 0,
        color: Colors.white,
        margin: EdgeInsets.symmetric(vertical: 6, horizontal: 12),
      ),
    );
