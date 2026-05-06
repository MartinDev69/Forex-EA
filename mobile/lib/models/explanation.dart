class TradeExplanation {
  final int tradeId;
  final String strategy;
  final String symbol;
  final String side;
  final double signalPrice;
  final double signalStop;
  final double signalTarget;
  final double riskReward;
  final double stopDistancePips;
  final double lotSize;
  final double accountBalance;
  final String openedAt;
  final String? regimeTrend;
  final String? regimeVolatility;
  final String? regimeLabel;
  final double? regimeAdx;
  final double? regimeAtrPct;
  final String? allocatorRole;
  final double? allocatorWeight;
  final bool? mlFilterPassed;
  final String notes;
  // Indicator snapshot — what the strategy "saw". Free-form per
  // strategy: e.g. {'rsi': 28.5, 'ema_fast': 1.0852}.
  final Map<String, dynamic> indicators;

  TradeExplanation({
    required this.tradeId,
    required this.strategy,
    required this.symbol,
    required this.side,
    required this.signalPrice,
    required this.signalStop,
    required this.signalTarget,
    required this.riskReward,
    required this.stopDistancePips,
    required this.lotSize,
    required this.accountBalance,
    required this.openedAt,
    this.regimeTrend,
    this.regimeVolatility,
    this.regimeLabel,
    this.regimeAdx,
    this.regimeAtrPct,
    this.allocatorRole,
    this.allocatorWeight,
    this.mlFilterPassed,
    this.notes = '',
    this.indicators = const {},
  });

  factory TradeExplanation.fromJson(Map<String, dynamic> json) => TradeExplanation(
        tradeId: json['trade_id'] as int,
        strategy: json['strategy'] as String,
        symbol: json['symbol'] as String,
        side: json['side'] as String,
        signalPrice: (json['signal_price'] as num).toDouble(),
        signalStop: (json['signal_stop'] as num).toDouble(),
        signalTarget: (json['signal_target'] as num).toDouble(),
        riskReward: (json['risk_reward'] as num).toDouble(),
        stopDistancePips: (json['stop_distance_pips'] as num).toDouble(),
        lotSize: (json['lot_size'] as num).toDouble(),
        accountBalance: (json['account_balance'] as num).toDouble(),
        openedAt: json['opened_at'] as String,
        regimeTrend: json['regime_trend'] as String?,
        regimeVolatility: json['regime_volatility'] as String?,
        regimeLabel: json['regime_label'] as String?,
        regimeAdx: (json['regime_adx'] as num?)?.toDouble(),
        regimeAtrPct: (json['regime_atr_pct'] as num?)?.toDouble(),
        allocatorRole: json['allocator_role'] as String?,
        allocatorWeight: (json['allocator_weight'] as num?)?.toDouble(),
        mlFilterPassed: json['ml_filter_passed'] as bool?,
        notes: (json['notes'] as String?) ?? '',
        indicators: (json['indicators'] as Map<String, dynamic>?) ?? const {},
      );
}
