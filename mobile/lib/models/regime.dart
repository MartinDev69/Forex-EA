/// Mirror of the server's RegimeResponse. `trend`/`volatility` are lowercase
/// strings matching the Python enum values (trend_up, trend_down, range,
/// unknown / low, normal, high, unknown).
class Regime {
  final String symbol;
  final String trend;
  final String volatility;
  final String label;
  final double? adx;
  final double? plusDi;
  final double? minusDi;
  final double? atr;
  final double? atrPct;
  final DateTime? timestamp;
  final DateTime? storedAt;

  Regime({
    required this.symbol,
    required this.trend,
    required this.volatility,
    required this.label,
    required this.adx,
    required this.plusDi,
    required this.minusDi,
    required this.atr,
    required this.atrPct,
    required this.timestamp,
    required this.storedAt,
  });

  bool get isKnown => trend != 'unknown';

  factory Regime.fromJson(Map<String, dynamic> json) {
    DateTime? parse(dynamic v) => v == null ? null : DateTime.parse(v as String);
    return Regime(
      symbol: json['symbol'] as String,
      trend: json['trend'] as String,
      volatility: json['volatility'] as String,
      label: json['label'] as String,
      adx: (json['adx'] as num?)?.toDouble(),
      plusDi: (json['plus_di'] as num?)?.toDouble(),
      minusDi: (json['minus_di'] as num?)?.toDouble(),
      atr: (json['atr'] as num?)?.toDouble(),
      atrPct: (json['atr_pct'] as num?)?.toDouble(),
      timestamp: parse(json['timestamp']),
      storedAt: parse(json['stored_at']),
    );
  }

  /// Placeholder used by the dashboard while the symbol picker is empty
  /// (first launch). Renders as the "waiting for classification" state.
  factory Regime.unknown(String symbol) => Regime(
        symbol: symbol,
        trend: 'unknown',
        volatility: 'unknown',
        label: 'unknown',
        adx: null,
        plusDi: null,
        minusDi: null,
        atr: null,
        atrPct: null,
        timestamp: null,
        storedAt: null,
      );
}
