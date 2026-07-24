import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _prefsKey = 'antigreed.themeMode';

// Institutional slate. Two colour families with strictly separate jobs, so
// nothing on screen is coloured decoratively:
//
//   kAccent      UI chrome only — active nav, primary buttons, focus,
//                selected states, section eyebrows, links.
//   kWin/kLoss   SIGNAL only — P&L sign, buy/sell side, entry/SL/TP levels,
//                candle direction, and health states.
//
// Surfaces are flat: hairline borders, no glow, no phosphor shadows. Mirrors
// the --accent/--green/--red tokens in src/api/static/index.html.
const Color kAccent    = Color(0xFF38BDF8);     // cyan — chrome accent
const Color kWin       = Color(0xFF34D399);     // signal green
const Color kLoss      = Color(0xFFF87171);     // signal red
const Color kAmber     = Color(0xFFFBBF24);     // warn / signal-only mode
const Color kInk       = Color(0xFF0B1120);     // page background
const Color kSurface   = Color(0xFF111A2C);     // cards
const Color kSurface2  = Color(0xFF162034);     // pressed / inset
const Color kEdge      = Color(0xFF1E293B);     // hairline borders
const Color kText      = Color(0xFFE2E8F0);     // body text
const Color kMuted     = Color(0xFF64748B);     // labels, captions

/// Corner radius used across cards, inputs and buttons. Tight on purpose —
/// the softer 14px radius read consumer-app rather than trading terminal.
const double kRadius = 8;

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
    primary: kAccent,
    onPrimary: Color(0xFF06121F),
    secondary: kAccent,
    onSecondary: Color(0xFF06121F),
    error: kLoss,
    onError: Color(0xFF06121F),
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
        borderRadius: BorderRadius.all(Radius.circular(kRadius)),
      ),
      margin: EdgeInsets.symmetric(vertical: 5, horizontal: 12),
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: kInk,
      foregroundColor: kText,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: kInk,
      indicatorColor: kAccent.withValues(alpha: 0.14),
      surfaceTintColor: Colors.transparent,
      labelTextStyle: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return TextStyle(
          fontSize: 11,
          fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
          color: selected ? kAccent : kMuted,
          letterSpacing: 0.3,
        );
      }),
      iconTheme: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return IconThemeData(color: selected ? kAccent : kMuted, size: 22);
      }),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: kSurface2,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kEdge),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kEdge),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kAccent, width: 1.4),
      ),
      labelStyle: const TextStyle(color: kMuted),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: kAccent,
        foregroundColor: const Color(0xFF06121F),
        textStyle: const TextStyle(fontWeight: FontWeight.w600, letterSpacing: 0.2),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(kRadius)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: kAccent,
        side: const BorderSide(color: kEdge),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(kRadius)),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(foregroundColor: kAccent),
    ),
    dividerTheme: const DividerThemeData(color: kEdge, thickness: 1, space: 1),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: kSurface,
      contentTextStyle: TextStyle(color: kText),
      behavior: SnackBarBehavior.floating,
    ),
    // Body copy is sans; only figures opt into monospace (see TickerText and
    // the explicit fontFamily: 'monospace' spots). The old global monospace
    // made every label read as terminal output.
    textTheme: Typography.whiteMountainView.apply(
      bodyColor: kText,
      displayColor: kText,
    ),
  );
}

// Light-mode tokens — same slate system, inverted. Accent and win/loss shift
// to darker variants so they hold contrast on white.
const Color kLightAccent   = Color(0xFF0284C7);
const Color kLightInk      = Color(0xFFF1F5F9);   // page background
const Color kLightSurface  = Color(0xFFFFFFFF);   // cards
const Color kLightSurface2 = Color(0xFFF8FAFC);   // pressed / inset
const Color kLightEdge     = Color(0xFFE2E8F0);   // hairline borders
const Color kLightText     = Color(0xFF0F172A);   // body text
const Color kLightMuted    = Color(0xFF64748B);   // labels
const Color kLightWin      = Color(0xFF059669);
const Color kLightLoss     = Color(0xFFDC2626);

ThemeData lightTheme() {
  final scheme = const ColorScheme.light(
    primary: kLightAccent,
    onPrimary: Colors.white,
    secondary: kLightAccent,
    onSecondary: Colors.white,
    error: kLightLoss,
    onError: Colors.white,
    surface: kLightSurface,
    onSurface: kLightText,
    surfaceContainerHighest: kLightSurface2,
    outline: kLightEdge,
  );
  return ThemeData(
    colorScheme: scheme,
    brightness: Brightness.light,
    useMaterial3: true,
    scaffoldBackgroundColor: kLightInk,
    canvasColor: kLightInk,
    cardTheme: const CardThemeData(
      elevation: 0,
      color: kLightSurface,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: kLightEdge, width: 1),
        borderRadius: BorderRadius.all(Radius.circular(kRadius)),
      ),
      margin: EdgeInsets.symmetric(vertical: 5, horizontal: 12),
    ),
    appBarTheme: const AppBarTheme(
      backgroundColor: kLightInk,
      foregroundColor: kLightText,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: kLightInk,
      indicatorColor: kLightAccent.withValues(alpha: 0.12),
      surfaceTintColor: Colors.transparent,
      labelTextStyle: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return TextStyle(
          fontSize: 11,
          fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
          color: selected ? kLightAccent : kLightMuted,
          letterSpacing: 0.3,
        );
      }),
      iconTheme: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return IconThemeData(
          color: selected ? kLightAccent : kLightMuted,
          size: 22,
        );
      }),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: kLightSurface,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kLightEdge),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kLightEdge),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(kRadius),
        borderSide: const BorderSide(color: kLightAccent, width: 1.4),
      ),
      labelStyle: const TextStyle(color: kLightMuted),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: kLightAccent,
        foregroundColor: Colors.white,
        textStyle: const TextStyle(fontWeight: FontWeight.w600, letterSpacing: 0.2),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(kRadius)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: kLightAccent,
        side: const BorderSide(color: kLightEdge),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(kRadius)),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(foregroundColor: kLightAccent),
    ),
    dividerTheme: const DividerThemeData(color: kLightEdge, thickness: 1, space: 1),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: kLightSurface,
      contentTextStyle: TextStyle(color: kLightText),
      behavior: SnackBarBehavior.floating,
    ),
  );
}

/// Drop-in for the BIG trading numbers — balance, equity, P&L, lot size.
/// Monospace with tabular figures so columns of numbers align, and flat in
/// both themes: the number carries meaning through colour, never a glow.
class TickerText extends StatelessWidget {
  const TickerText(
    this.text, {
    super.key,
    this.tone = TickerTone.neutral,
    this.size = 26,
    this.weight = FontWeight.w600,
  });

  final String text;
  final TickerTone tone;
  final double size;
  final FontWeight weight;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final color = switch (tone) {
      TickerTone.win => isDark ? kWin : kLightWin,
      TickerTone.loss => isDark ? kLoss : kLightLoss,
      TickerTone.neutral => isDark ? kText : kLightText,
    };
    return Text(
      text,
      style: TextStyle(
        color: color,
        fontSize: size,
        fontWeight: weight,
        fontFamily: 'monospace',
        letterSpacing: -0.5,
        fontFeatures: const [FontFeature.tabularFigures()],
      ),
    );
  }
}

enum TickerTone { neutral, win, loss }

/// Card surface for the "live" panels (Account, Status, Regime, Open
/// positions). Flat in both themes — a hairline border does the separating,
/// tinted by [tone] when the panel should hint at win/loss.
BoxDecoration panelDecoration(BuildContext context, {TickerTone tone = TickerTone.neutral}) {
  final isDark = Theme.of(context).brightness == Brightness.dark;
  final Color border = switch (tone) {
    TickerTone.win => (isDark ? kWin : kLightWin).withValues(alpha: 0.30),
    TickerTone.loss => (isDark ? kLoss : kLightLoss).withValues(alpha: 0.30),
    TickerTone.neutral => isDark ? kEdge : kLightEdge,
  };
  return BoxDecoration(
    color: isDark ? kSurface : kLightSurface,
    borderRadius: BorderRadius.circular(kRadius),
    border: Border.all(color: border),
  );
}

/// Theme-aware muted text color — same intent as `kMuted` on dark and
/// `kLightMuted` on light. Used for tiny KPI labels.
Color mutedColor(BuildContext context) =>
    Theme.of(context).brightness == Brightness.dark ? kMuted : kLightMuted;

/// Theme-aware chrome accent — active states, eyebrows, selected chips.
Color accentColor(BuildContext context) =>
    Theme.of(context).brightness == Brightness.dark ? kAccent : kLightAccent;
