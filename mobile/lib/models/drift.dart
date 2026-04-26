class DriftMetric {
  final String name;
  final double baseline;
  final double live;
  final double delta;
  final double deltaPct;

  DriftMetric({
    required this.name,
    required this.baseline,
    required this.live,
    required this.delta,
    required this.deltaPct,
  });

  factory DriftMetric.fromJson(Map<String, dynamic> json) => DriftMetric(
        name: json['name'] as String,
        baseline: (json['baseline'] as num).toDouble(),
        live: (json['live'] as num).toDouble(),
        delta: (json['delta'] as num).toDouble(),
        deltaPct: (json['delta_pct'] as num).toDouble(),
      );
}

class DriftBaseline {
  final int tradeCount;
  final double winRate;
  final double avgR;
  final double avgTradesPerDay;
  final String source;

  DriftBaseline({
    required this.tradeCount,
    required this.winRate,
    required this.avgR,
    required this.avgTradesPerDay,
    required this.source,
  });

  factory DriftBaseline.fromJson(Map<String, dynamic> json) => DriftBaseline(
        tradeCount: json['trade_count'] as int,
        winRate: (json['win_rate'] as num).toDouble(),
        avgR: (json['avg_r'] as num).toDouble(),
        avgTradesPerDay: (json['avg_trades_per_day'] as num).toDouble(),
        source: json['source'] as String,
      );
}

class DriftReport {
  final String strategy;
  final String symbol;
  final String status; // 'ok' | 'warn' | 'danger' | 'unknown'
  final int liveTradeCount;
  final DriftBaseline? baseline;
  final List<DriftMetric> metrics;
  final String note;

  DriftReport({
    required this.strategy,
    required this.symbol,
    required this.status,
    required this.liveTradeCount,
    required this.baseline,
    required this.metrics,
    required this.note,
  });

  factory DriftReport.fromJson(Map<String, dynamic> json) => DriftReport(
        strategy: json['strategy'] as String,
        symbol: json['symbol'] as String,
        status: json['status'] as String,
        liveTradeCount: json['live_trade_count'] as int,
        baseline: json['baseline'] == null
            ? null
            : DriftBaseline.fromJson(json['baseline'] as Map<String, dynamic>),
        metrics: (json['metrics'] as List<dynamic>)
            .map((e) => DriftMetric.fromJson(e as Map<String, dynamic>))
            .toList(),
        note: json['note'] as String,
      );
}

class DriftResponse {
  final List<DriftReport> reports;
  final int count;

  DriftResponse({required this.reports, required this.count});

  factory DriftResponse.fromJson(Map<String, dynamic> json) => DriftResponse(
        reports: (json['reports'] as List<dynamic>)
            .map((e) => DriftReport.fromJson(e as Map<String, dynamic>))
            .toList(),
        count: json['count'] as int,
      );
}
