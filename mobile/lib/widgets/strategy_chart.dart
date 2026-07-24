import 'package:flutter/material.dart';

import '../theme.dart';

/// Inline candlestick chart for a trade explanation. Draws:
///   - the OHLC bars the strategy was looking at
///   - the indicator overlay lines (EMA20/50/200) and BB envelope
///   - dashed horizontal lines at the entry / SL / TP levels
///   - a small arrow on the signal bar pointing the side
///
/// All in a single [CustomPainter] so the page doesn't need a chart
/// library. Width follows the parent; height is fixed to keep the
/// trade-explanation sheet compact.
class StrategyChart extends StatelessWidget {
  const StrategyChart({
    super.key,
    required this.bars,
    required this.overlays,
    required this.entry,
    required this.stop,
    required this.target,
    required this.side,
    required this.symbol,
    this.subplots = const [],
    this.height = 200,
  });

  final List<Map<String, dynamic>> bars;
  final List<Map<String, dynamic>> overlays;
  final List<Map<String, dynamic>> subplots;
  final double entry;
  final double stop;
  final double target;
  final String side; // 'BUY' | 'SELL'
  final String symbol;
  final double height;

  int get _decimals {
    final s = symbol.toUpperCase();
    if (s.contains('XAU') || s.contains('GOLD')) return 2;
    if (s.contains('OIL') || s.contains('WTI') || s.contains('BRENT')) return 2;
    if (s.endsWith('JPY') || s.endsWith('JPYM')) return 3;
    if (s.contains('BTC') || s.contains('ETH')) return 1;
    return 5;
  }

  @override
  Widget build(BuildContext context) {
    if (bars.length < 2) {
      return const SizedBox.shrink();
    }
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final muted = mutedColor(context);
    final totalHeight = height + subplots.length * 70 + (subplots.isNotEmpty ? 8.0 : 0);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          decoration: BoxDecoration(
            color: isDark ? kSurface2 : kLightSurface2,
            border: Border.all(color: isDark ? kEdge : kLightEdge),
            borderRadius: BorderRadius.circular(10),
          ),
          padding: const EdgeInsets.all(8),
          child: SizedBox(
            height: totalHeight,
            width: double.infinity,
            child: CustomPaint(
              painter: _StrategyChartPainter(
                bars: bars,
                overlays: overlays,
                subplots: subplots,
                entry: entry,
                stop: stop,
                target: target,
                isBuy: side == 'BUY',
                isDark: isDark,
                decimals: _decimals,
                priceHeight: height,
              ),
            ),
          ),
        ),
        const SizedBox(height: 6),
        Wrap(
          spacing: 12,
          runSpacing: 4,
          children: [
            for (final o in overlays.where((o) => o['name'] != null))
              _LegendChip(
                label: (o['name'] as String?) ?? '',
                color: _parseHex((o['color'] as String?) ?? '#8fa0aa'),
                muted: muted,
              ),
            _LegendChip(
              label: 'Entry',
              color: side == 'BUY'
                  ? (isDark ? kNeonGreen : kLightWin)
                  : (isDark ? kNeonRed : kLightLoss),
              muted: muted,
            ),
            _LegendChip(
              label: 'SL',
              color: isDark ? kNeonRed : kLightLoss,
              muted: muted,
            ),
            _LegendChip(
              label: 'TP',
              color: isDark ? kNeonGreen : kLightWin,
              muted: muted,
            ),
          ],
        ),
      ],
    );
  }
}

class _LegendChip extends StatelessWidget {
  const _LegendChip({required this.label, required this.color, required this.muted});
  final String label;
  final Color color;
  final Color muted;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(width: 12, height: 2, color: color),
        const SizedBox(width: 5),
        Text(label, style: TextStyle(color: muted, fontSize: 10)),
      ],
    );
  }
}

Color _parseHex(String hex) {
  final clean = hex.replaceAll('#', '');
  if (clean.length != 6) return const Color(0xFF8fa0aa);
  return Color(int.parse('FF$clean', radix: 16));
}

class _StrategyChartPainter extends CustomPainter {
  _StrategyChartPainter({
    required this.bars,
    required this.overlays,
    required this.subplots,
    required this.entry,
    required this.stop,
    required this.target,
    required this.isBuy,
    required this.isDark,
    required this.decimals,
    required this.priceHeight,
  });

  final List<Map<String, dynamic>> bars;
  final List<Map<String, dynamic>> overlays;
  final List<Map<String, dynamic>> subplots;
  final double entry;
  final double stop;
  final double target;
  final bool isBuy;
  final bool isDark;
  final int decimals;
  final double priceHeight;

  double? _num(dynamic v) {
    if (v == null) return null;
    if (v is num) return v.toDouble();
    return null;
  }

  @override
  void paint(Canvas canvas, Size size) {
    final n = bars.length;
    if (n < 2) return;

    const padL = 4.0, padR = 60.0, padT = 6.0, padB = 12.0;
    final innerW = size.width - padL - padR;
    // Constrain candles to the priceHeight; subplots get the rest below.
    final innerH = priceHeight - padT - padB;
    final step = innerW / n;
    final candleW = (step * 0.6).clamp(1.0, 14.0);

    // Figure y range from bars + overlays + levels.
    final ys = <double>[];
    for (final b in bars) {
      final h = _num(b['h']); final l = _num(b['l']);
      if (h != null) ys.add(h);
      if (l != null) ys.add(l);
    }
    for (final o in overlays) {
      final kind = o['kind'] as String?;
      if (kind == 'line') {
        final vals = (o['values'] as List?) ?? const [];
        for (final v in vals) {
          final d = _num(v);
          if (d != null) ys.add(d);
        }
      } else if (kind == 'band') {
        for (final key in ['upper', 'lower']) {
          final vals = (o[key] as List?) ?? const [];
          for (final v in vals) {
            final d = _num(v);
            if (d != null) ys.add(d);
          }
        }
      }
    }
    for (final v in [entry, stop, target]) {
      ys.add(v);
    }
    if (ys.isEmpty) return;
    final yMin = ys.reduce((a, b) => a < b ? a : b);
    final yMax = ys.reduce((a, b) => a > b ? a : b);
    final yPad = ((yMax - yMin) * 0.04).abs().clamp(1e-9, double.infinity);
    final yTop = yMax + yPad;
    final yBot = yMin - yPad;
    final yRange = (yTop - yBot).abs().clamp(1e-9, double.infinity);

    double xAt(int i) => padL + i * step + step / 2;
    double yAt(double v) => padT + (yTop - v) / yRange * innerH;

    // BB envelope as a translucent fill first so candles draw on top.
    for (final o in overlays) {
      if (o['kind'] != 'band') continue;
      final color = _parseHex((o['color'] as String?) ?? '#8fa0aa');
      final upper = ((o['upper'] as List?) ?? const []).map(_num).toList();
      final lower = ((o['lower'] as List?) ?? const []).map(_num).toList();
      final path = Path();
      bool started = false;
      for (var i = 0; i < n && i < upper.length; i++) {
        final v = upper[i];
        if (v == null) continue;
        final p = Offset(xAt(i), yAt(v));
        if (!started) {
          path.moveTo(p.dx, p.dy);
          started = true;
        } else {
          path.lineTo(p.dx, p.dy);
        }
      }
      for (var i = (lower.length < n ? lower.length : n) - 1; i >= 0; i--) {
        final v = lower[i];
        if (v == null) continue;
        final p = Offset(xAt(i), yAt(v));
        path.lineTo(p.dx, p.dy);
      }
      path.close();
      canvas.drawPath(
        path,
        Paint()..color = color.withValues(alpha: 0.05),
      );
      _drawDashed(canvas, upper, color, xAt, yAt);
      _drawDashed(canvas, lower, color, xAt, yAt);
    }

    // Candles
    for (var i = 0; i < n; i++) {
      final b = bars[i];
      final o = _num(b['o']); final h = _num(b['h']);
      final l = _num(b['l']); final c = _num(b['c']);
      if (o == null || h == null || l == null || c == null) continue;
      final up = c >= o;
      final color = up
          ? (isDark ? kNeonGreen : kLightWin)
          : (isDark ? kNeonRed : kLightLoss);
      final x = xAt(i);
      final wickPaint = Paint()
        ..color = color.withValues(alpha: 0.85)
        ..strokeWidth = 1;
      canvas.drawLine(Offset(x, yAt(h)), Offset(x, yAt(l)), wickPaint);
      final bodyTop = yAt(o > c ? o : c);
      final bodyBot = yAt(o > c ? c : o);
      final bodyH = (bodyBot - bodyTop).abs().clamp(1.0, double.infinity);
      canvas.drawRect(
        Rect.fromLTWH(x - candleW / 2, bodyTop, candleW, bodyH),
        Paint()..color = color.withValues(alpha: 0.85),
      );
    }

    // Line overlays on top of candles
    for (final o in overlays) {
      if (o['kind'] != 'line') continue;
      final color = _parseHex((o['color'] as String?) ?? '#8fa0aa');
      final vals = ((o['values'] as List?) ?? const []).map(_num).toList();
      _drawLine(canvas, vals, color, xAt, yAt);
    }

    // Entry / SL / TP horizontal lines + label badges on the right.
    final entryColor = isBuy
        ? (isDark ? kNeonGreen : kLightWin)
        : (isDark ? kNeonRed : kLightLoss);
    _drawLevel(canvas, size, entry, entryColor, 'ENTRY ${entry.toStringAsFixed(decimals)}',
        padR, yAt);
    _drawLevel(canvas, size, stop, isDark ? kNeonRed : kLightLoss,
        'SL ${stop.toStringAsFixed(decimals)}', padR, yAt);
    _drawLevel(canvas, size, target, isDark ? kNeonGreen : kLightWin,
        'TP ${target.toStringAsFixed(decimals)}', padR, yAt);

    // Signal arrow on the latest bar.
    final sigX = xAt(n - 1);
    final sigY = yAt(entry);
    final arrowPaint = Paint()..color = entryColor;
    final p = Path();
    if (isBuy) {
      p.moveTo(sigX, sigY + 14);
      p.lineTo(sigX - 5, sigY + 22);
      p.lineTo(sigX + 5, sigY + 22);
    } else {
      p.moveTo(sigX, sigY - 14);
      p.lineTo(sigX - 5, sigY - 22);
      p.lineTo(sigX + 5, sigY - 22);
    }
    p.close();
    canvas.drawPath(p, arrowPaint);

    // Subplot panes
    if (subplots.isNotEmpty) {
      const subH = 70.0, subGap = 8.0;
      final paneTop0 = priceHeight + 2.0;
      for (var idx = 0; idx < subplots.length; idx++) {
        final sp = subplots[idx];
        final top = paneTop0 + idx * (subH + subGap);
        final yMin = (sp['y_min'] as num?)?.toDouble() ?? 0.0;
        final yMax = (sp['y_max'] as num?)?.toDouble() ?? 100.0;
        final yRange = (yMax - yMin).abs().clamp(1e-9, double.infinity);
        double sy(double v) => top + (yMax - v) / yRange * subH;

        // Pane separator
        canvas.drawLine(
          Offset(padL, top),
          Offset(size.width - padR, top),
          Paint()..color = (isDark ? kEdge : kLightEdge).withValues(alpha: 0.5)..strokeWidth = 1,
        );

        // Title
        final mutedC = isDark ? kMuted : kLightMuted;
        final titlePainter = TextPainter(
          text: TextSpan(
            text: (sp['name'] as String?) ?? '',
            style: TextStyle(
              fontFamily: 'monospace', fontSize: 9,
              color: mutedC,
              letterSpacing: 1.5,
            ),
          ),
          textDirection: TextDirection.ltr,
        )..layout();
        titlePainter.paint(canvas, Offset(padL + 4, top + 2));

        // Guide lines (OB/OS/threshold)
        for (final g in (sp['guides'] as List? ?? const [])) {
          final gy = sy((g['y'] as num).toDouble());
          final gColor = _parseHex((g['color'] as String?) ?? '#8fa0aa')
              .withValues(alpha: 0.5);
          final paint = Paint()..color = gColor..strokeWidth = 1;
          var gx = padL;
          while (gx < size.width - padR) {
            canvas.drawLine(Offset(gx, gy), Offset(gx + 3, gy), paint);
            gx += 6;
          }
          final lbl = TextPainter(
            text: TextSpan(
              text: '${g['label']} ${g['y']}',
              style: TextStyle(
                fontFamily: 'monospace', fontSize: 9,
                color: gColor.withValues(alpha: 0.9),
              ),
            ),
            textDirection: TextDirection.ltr,
          )..layout();
          lbl.paint(canvas, Offset(size.width - padR - lbl.width - 2, gy - 11));
        }

        final kind = sp['kind'] as String?;
        if (kind == 'line') {
          final color = _parseHex((sp['color'] as String?) ?? '#22ee88');
          final vals = ((sp['values'] as List?) ?? const []).map(_num).toList();
          _drawLine(canvas, vals, color, xAt, sy);
        } else if (kind == 'double_line') {
          final pColor = _parseHex((sp['primary_color'] as String?) ?? '#22ee88');
          final sColor = _parseHex((sp['secondary_color'] as String?) ?? '#ffc73a');
          final tColor = _parseHex((sp['tertiary_color'] as String?) ?? '#ff3355');
          final pVals = ((sp['primary'] as List?) ?? const []).map(_num).toList();
          final sVals = ((sp['secondary'] as List?) ?? const []).map(_num).toList();
          _drawLine(canvas, pVals, pColor, xAt, sy);
          _drawLine(canvas, sVals, sColor, xAt, sy);
          if (sp['tertiary'] != null) {
            final tVals = (sp['tertiary'] as List).map(_num).toList();
            _drawLine(canvas, tVals, tColor, xAt, sy);
          }
        } else if (kind == 'macd') {
          final hist = ((sp['histogram'] as List?) ?? const []).map(_num).toList();
          final zeroY = sy(0);
          for (var i = 0; i < hist.length; i++) {
            final v = hist[i];
            if (v == null) continue;
            final x = xAt(i);
            final y0 = zeroY, y1 = sy(v);
            final ttop = y0 < y1 ? y0 : y1;
            final h2 = (y1 - y0).abs().clamp(1.0, double.infinity);
            final hcolor = (v >= 0
                ? (isDark ? kNeonGreen : kLightWin)
                : (isDark ? kNeonRed : kLightLoss))
                .withValues(alpha: 0.5);
            canvas.drawRect(
              Rect.fromLTWH(x - candleW / 2, ttop, candleW, h2),
              Paint()..color = hcolor,
            );
          }
          // Zero line
          canvas.drawLine(
            Offset(padL, zeroY), Offset(size.width - padR, zeroY),
            Paint()..color = (isDark ? kMuted : kLightMuted).withValues(alpha: 0.5)
              ..strokeWidth = 1,
          );
          final mColor = _parseHex((sp['color'] as String?) ?? '#22ee88');
          final sColor = _parseHex((sp['signal_color'] as String?) ?? '#ff3355');
          final mVals = ((sp['macd'] as List?) ?? const []).map(_num).toList();
          final sVals = ((sp['signal'] as List?) ?? const []).map(_num).toList();
          _drawLine(canvas, mVals, mColor, xAt, sy);
          _drawLine(canvas, sVals, sColor, xAt, sy);
        }
      }
    }
  }


  void _drawLine(
    Canvas canvas,
    List<double?> values,
    Color color,
    double Function(int) xAt,
    double Function(double) yAt,
  ) {
    final path = Path();
    bool started = false;
    for (var i = 0; i < values.length; i++) {
      final v = values[i];
      if (v == null) {
        started = false;
        continue;
      }
      final p = Offset(xAt(i), yAt(v));
      if (!started) {
        path.moveTo(p.dx, p.dy);
        started = true;
      } else {
        path.lineTo(p.dx, p.dy);
      }
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = color
        ..strokeWidth = 1.5
        ..style = PaintingStyle.stroke
        ..strokeJoin = StrokeJoin.round,
    );
  }

  void _drawDashed(
    Canvas canvas,
    List<double?> values,
    Color color,
    double Function(int) xAt,
    double Function(double) yAt,
  ) {
    final paint = Paint()
      ..color = color.withValues(alpha: 0.7)
      ..strokeWidth = 1
      ..style = PaintingStyle.stroke;
    Offset? prev;
    for (var i = 0; i < values.length; i++) {
      final v = values[i];
      if (v == null) {
        prev = null;
        continue;
      }
      final p = Offset(xAt(i), yAt(v));
      if (prev != null) {
        // Cheap dash: 4px on / 3px off. We just draw alternating segments.
        final dx = p.dx - prev.dx, dy = p.dy - prev.dy;
        final dist = (dx * dx + dy * dy);
        if (dist > 0) {
          canvas.drawLine(prev, p, paint);
        }
      }
      prev = p;
    }
  }

  void _drawLevel(
    Canvas canvas,
    Size size,
    double value,
    Color color,
    String label,
    double padR,
    double Function(double) yAt,
  ) {
    final y = yAt(value);
    final paint = Paint()
      ..color = color.withValues(alpha: 0.9)
      ..strokeWidth = 1;
    // Dashed horizontal: short segments across the chart width.
    const dash = 5.0, gap = 4.0;
    var x = 4.0;
    final endX = size.width - padR - 2;
    while (x < endX) {
      final x2 = (x + dash).clamp(0.0, endX);
      canvas.drawLine(Offset(x, y), Offset(x2, y), paint);
      x += dash + gap;
    }
    // Label badge on the right gutter
    final badgeW = 56.0, badgeH = 14.0;
    final rect = Rect.fromLTWH(size.width - padR + 2, y - badgeH / 2, badgeW, badgeH);
    canvas.drawRRect(
      RRect.fromRectAndRadius(rect, const Radius.circular(3)),
      Paint()..color = color.withValues(alpha: 0.9),
    );
    final tp = TextPainter(
      text: TextSpan(
        text: label,
        style: const TextStyle(
          color: Colors.black,
          fontSize: 9,
          fontWeight: FontWeight.w700,
          fontFamily: 'monospace',
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout(maxWidth: badgeW - 4);
    tp.paint(canvas, Offset(rect.left + 3, rect.top + (badgeH - tp.height) / 2));
  }

  @override
  bool shouldRepaint(covariant _StrategyChartPainter old) =>
      old.bars != bars || old.overlays != overlays
          || old.entry != entry || old.stop != stop || old.target != target;
}
