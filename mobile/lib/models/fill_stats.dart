class FillSymbolStats {
  final String symbol;
  final int fillCount;
  final int rejectedCount;
  final double avgSlippagePips;
  final double maxSlippagePips;
  final double avgLatencyMs;
  final double p95LatencyMs;

  FillSymbolStats({
    required this.symbol,
    required this.fillCount,
    required this.rejectedCount,
    required this.avgSlippagePips,
    required this.maxSlippagePips,
    required this.avgLatencyMs,
    required this.p95LatencyMs,
  });

  factory FillSymbolStats.fromJson(Map<String, dynamic> json) => FillSymbolStats(
        symbol: json['symbol'] as String,
        fillCount: json['fill_count'] as int,
        rejectedCount: json['rejected_count'] as int,
        avgSlippagePips: (json['avg_slippage_pips'] as num).toDouble(),
        maxSlippagePips: (json['max_slippage_pips'] as num).toDouble(),
        avgLatencyMs: (json['avg_latency_ms'] as num).toDouble(),
        p95LatencyMs: (json['p95_latency_ms'] as num).toDouble(),
      );
}

class FillStatsResponse {
  final List<FillSymbolStats> symbols;
  final int windowHours;

  FillStatsResponse({required this.symbols, required this.windowHours});

  factory FillStatsResponse.fromJson(Map<String, dynamic> json) => FillStatsResponse(
        symbols: (json['symbols'] as List<dynamic>)
            .map((e) => FillSymbolStats.fromJson(e as Map<String, dynamic>))
            .toList(),
        windowHours: json['window_hours'] as int,
      );
}
