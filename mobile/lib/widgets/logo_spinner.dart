import 'package:flutter/material.dart';

/// Drop-in replacement for CircularProgressIndicator. Pulses the AntiGreed
/// logo at the requested size. Use whenever the app is fetching/initialising
/// and wants to look on-brand instead of generic.
class LogoSpinner extends StatefulWidget {
  const LogoSpinner({super.key, this.size = 64, this.label});

  final double size;
  final String? label;

  @override
  State<LogoSpinner> createState() => _LogoSpinnerState();
}

class _LogoSpinnerState extends State<LogoSpinner>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        AnimatedBuilder(
          animation: _ctrl,
          builder: (_, _) {
            // Pulse: scale 0.92 → 1.0 with a slight opacity dim at min so
            // the logo "breathes" rather than just shrinking.
            final v = _ctrl.value;
            return Opacity(
              opacity: 0.7 + (v * 0.3),
              child: Transform.scale(
                scale: 0.92 + (v * 0.08),
                child: Image.asset(
                  'assets/antigreed-logo.png',
                  width: widget.size,
                  fit: BoxFit.contain,
                ),
              ),
            );
          },
        ),
        if (widget.label != null) ...[
          const SizedBox(height: 8),
          Text(
            widget.label!,
            style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
          ),
        ],
      ],
    );
  }
}
