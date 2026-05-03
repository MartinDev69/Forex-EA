import 'package:flutter/material.dart';

/// Cold-start splash. Logo fades and scales in, then settles into a slow
/// breathing pulse while the app finishes initialising. The caller controls
/// the minimum visible duration via the `onDone` callback — we never cut
/// the animation short before its first full beat.
class SplashScreen extends StatefulWidget {
  const SplashScreen({
    super.key,
    required this.onDone,
    this.minVisible = const Duration(milliseconds: 1400),
  });

  /// Fired after the entrance animation has finished AND `minVisible` has
  /// elapsed since mount. Lets the parent route on to lock / login.
  final VoidCallback onDone;
  final Duration minVisible;

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen>
    with TickerProviderStateMixin {
  late final AnimationController _entry;
  late final AnimationController _pulse;
  late final Animation<double> _fade;
  late final Animation<double> _scale;
  late final DateTime _mountedAt;

  @override
  void initState() {
    super.initState();
    _mountedAt = DateTime.now();

    // Entry: 0 → 1 over 700ms. Fade and scale up together for a confident
    // "snap in" rather than a slow drift. Curve is decelerate so the logo
    // looks like it's catching itself at the end.
    _entry = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 700),
    )..forward();
    _fade = CurvedAnimation(parent: _entry, curve: Curves.easeOut);
    _scale = Tween<double>(begin: 0.78, end: 1.0).animate(
      CurvedAnimation(parent: _entry, curve: Curves.easeOutBack),
    );

    // Subtle breathing pulse once the entry finishes — prevents the splash
    // from looking frozen if the parent takes longer than expected to
    // route away. Repeats with reverse so it never hard-snaps.
    _pulse = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1100),
    );
    _entry.addStatusListener((status) async {
      if (status == AnimationStatus.completed) {
        _pulse.repeat(reverse: true);
        // Wait until both the entry has finished AND minVisible has elapsed
        // since we mounted, whichever is later. Keeps a fast init from
        // flashing the splash and disappearing before the user registers it.
        final elapsed = DateTime.now().difference(_mountedAt);
        final remaining = widget.minVisible - elapsed;
        if (remaining > Duration.zero) {
          await Future.delayed(remaining);
        }
        if (mounted) widget.onDone();
      }
    });
  }

  @override
  void dispose() {
    _entry.dispose();
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(
        fit: StackFit.expand,
        children: [
          // Robot in starting blocks behind a dark scrim — gives the splash
          // a "ready to trade" feel rather than just a logo on black.
          Image.asset(
            'assets/img/robot-ready.jpg',
            fit: BoxFit.cover,
            color: Colors.black.withValues(alpha: 0.55),
            colorBlendMode: BlendMode.darken,
          ),
          Container(
            decoration: BoxDecoration(
              gradient: RadialGradient(
                center: Alignment.center,
                radius: 0.9,
                colors: [
                  Colors.transparent,
                  Colors.black.withValues(alpha: 0.65),
                ],
              ),
            ),
          ),
          Center(
            child: AnimatedBuilder(
              animation: Listenable.merge([_entry, _pulse]),
              builder: (_, _) {
                final pulseScale = 1.0 + (_pulse.value * 0.04);
                return Opacity(
                  opacity: _fade.value,
                  child: Transform.scale(
                    scale: _scale.value * pulseScale,
                    child: Image.asset(
                      'assets/antigreed-logo.png',
                      width: 240,
                      fit: BoxFit.contain,
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}
