import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _prefsKey = 'antigreed.themeMode';

// Trading-floor LED palette — pure black canvas, neon green for primary
// action / wins, neon red for loss, subtle phosphor-grid card surface.
const Color kNeonGreen = Color(0xFF22EE88);
const Color kNeonRed   = Color(0xFFFF3355);
const Color kAmber     = Color(0xFFFFC73A);
const Color kInk       = Color(0xFF000000);     // page background
const Color kSurface   = Color(0xFF0A0C12);     // cards
const Color kSurface2  = Color(0xFF050608);     // pressed / inset
const Color kEdge      = Color(0xFF1A1E2A);     // hairline borders
const Color kText      = Color(0xFFE6EFE9);     // body text (slight green tint)
const Color kMuted     = Color(0xFF7C8693);     // labels, captions

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

ThemeData darkTheme() {
  final scheme = const ColorScheme.dark(
    primary: kNeonGreen,
    onPrimary: Colors.black,
    secondary: kNeonGreen,
    onSecondary: Colors.black,
    error: kNeonRed,
    onError: Colors.black,
    surface: kSurface,
    onSurface: kText,
    surfaceContainerHighest: kSurface2,
    outline: kEdge,
  );
  return ThemeData(
    colorScheme: scheme,
    brightness: Brightness.dark,
    useMaterial3: true,
    scaffoldBackgroundColor: kInk,
    canvasColor: kInk,
    cardTheme: const CardThemeData(
      elevation: 0,
      color: kSurface,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: kEdge, width: 1),
        borderRadius: BorderRadius.all(Radius.circular(14)),
      ),
      margin: EdgeInsets.symmetric(vertical: 6, horizontal: 12),
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: kInk,
      foregroundColor: kText,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: kInk,
      indicatorColor: kNeonGreen.withValues(alpha: 0.16),
      surfaceTintColor: Colors.transparent,
      labelTextStyle: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return TextStyle(
          fontSize: 11,
          fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
          color: selected ? kNeonGreen : kMuted,
          letterSpacing: 0.5,
        );
      }),
      iconTheme: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return IconThemeData(color: selected ? kNeonGreen : kMuted, size: 22);
      }),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: kSurface,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: kEdge),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: kEdge),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: kNeonGreen, width: 1.4),
      ),
      labelStyle: const TextStyle(color: kMuted),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: kNeonGreen,
        foregroundColor: Colors.black,
        textStyle: const TextStyle(fontWeight: FontWeight.w700, letterSpacing: 0.4),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: kNeonGreen,
        side: const BorderSide(color: kEdge),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(foregroundColor: kNeonGreen),
    ),
    dividerTheme: const DividerThemeData(color: kEdge, thickness: 1, space: 1),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: kSurface,
      contentTextStyle: TextStyle(color: kText),
      behavior: SnackBarBehavior.floating,
    ),
    textTheme: Typography.whiteMountainView.apply(
      bodyColor: kText,
      displayColor: kText,
      fontFamily: 'monospace',
    ),
  );
}

ThemeData lightTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: const Color(0xFF0E7C42),
    brightness: Brightness.light,
  );
  return ThemeData(
    colorScheme: scheme,
    useMaterial3: true,
    scaffoldBackgroundColor: const Color(0xFFF5F7FA),
    cardTheme: const CardThemeData(
      elevation: 0,
      color: Colors.white,
      margin: EdgeInsets.symmetric(vertical: 6, horizontal: 12),
    ),
  );
}

/// Drop-in for the BIG trading numbers — balance, equity, P&L, lot size.
/// Adds a phosphor glow on dark mode, prints clean on light. Default tone is
/// neutral (off-white); pass [tone] for win/loss coloring.
class TickerText extends StatelessWidget {
  const TickerText(
    this.text, {
    super.key,
    this.tone = TickerTone.neutral,
    this.size = 28,
    this.weight = FontWeight.w700,
  });

  final String text;
  final TickerTone tone;
  final double size;
  final FontWeight weight;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final color = switch (tone) {
      TickerTone.win => kNeonGreen,
      TickerTone.loss => kNeonRed,
      TickerTone.neutral => isDark ? kText : Colors.black87,
    };
    final shadows = isDark
        ? <Shadow>[
            Shadow(color: color.withValues(alpha: 0.55), blurRadius: 8),
            Shadow(color: color.withValues(alpha: 0.30), blurRadius: 22),
          ]
        : const <Shadow>[];
    return Text(
      text,
      style: TextStyle(
        color: color,
        fontSize: size,
        fontWeight: weight,
        letterSpacing: 0.5,
        fontFeatures: const [FontFeature.tabularFigures()],
        shadows: shadows,
      ),
    );
  }
}

enum TickerTone { neutral, win, loss }

/// Decorative card surface that picks up the trading-floor neon glow on
/// dark mode and prints flat on light. Use instead of a raw Card for the
/// "live" panels (Account, Status, Regime, Open positions).
BoxDecoration glowPanel({Color glow = kNeonGreen}) {
  return BoxDecoration(
    color: kSurface,
    borderRadius: BorderRadius.circular(14),
    border: Border.all(color: glow.withValues(alpha: 0.16)),
    boxShadow: [
      BoxShadow(
        color: glow.withValues(alpha: 0.18),
        blurRadius: 24,
        spreadRadius: -8,
      ),
    ],
  );
}
