class CorrelationPair {
  final String symbolA;
  final String symbolB;
  final double value;
  final int windowBars;
  final DateTime computedAt;

  CorrelationPair({
    required this.symbolA,
    required this.symbolB,
    required this.value,
    required this.windowBars,
    required this.computedAt,
  });

  factory CorrelationPair.fromJson(Map<String, dynamic> json) => CorrelationPair(
        symbolA: json['symbol_a'] as String,
        symbolB: json['symbol_b'] as String,
        value: (json['value'] as num).toDouble(),
        windowBars: json['window_bars'] as int,
        computedAt: DateTime.parse(json['computed_at'] as String),
      );
}

class CorrelationResponse {
  final List<CorrelationPair> pairs;
  final int count;

  CorrelationResponse({required this.pairs, required this.count});

  factory CorrelationResponse.fromJson(Map<String, dynamic> json) =>
      CorrelationResponse(
        pairs: (json['pairs'] as List<dynamic>)
            .map((e) => CorrelationPair.fromJson(e as Map<String, dynamic>))
            .toList(),
        count: json['count'] as int,
      );
}
