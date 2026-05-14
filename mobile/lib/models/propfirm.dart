class PropFirmStatus {
  PropFirmStatus({
    required this.enabled,
    required this.initialized,
    this.preset,
    this.initialBalance,
    this.currentEquity,
    this.peakEquity,
    this.profitAmount,
    this.profitTargetAmount,
    this.profitTargetPct,
    this.profitRemainingAmount,
    this.dailyStartEquity,
    this.dailyLossAmount,
    this.dailyLossLimitAmount,
    this.dailyLossPct,
    this.maxDailyLossPct,
    this.totalDrawdownAmount,
    this.totalDrawdownLimitAmount,
    this.totalDrawdownPct,
    this.maxTotalDrawdownPct,
    this.drawdownFromPeak,
    this.tradingDaysCount,
    this.minTradingDays,
    this.killedToday,
    this.killedPermanently,
    this.killedReason,
    this.maxLotSize,
    this.requireStopLoss,
    this.updatedAt,
  });

  final bool enabled;
  final bool initialized;
  final String? preset;
  final double? initialBalance;
  final double? currentEquity;
  final double? peakEquity;
  final double? profitAmount;
  final double? profitTargetAmount;
  final double? profitTargetPct;
  final double? profitRemainingAmount;
  final double? dailyStartEquity;
  final double? dailyLossAmount;
  final double? dailyLossLimitAmount;
  final double? dailyLossPct;
  final double? maxDailyLossPct;
  final double? totalDrawdownAmount;
  final double? totalDrawdownLimitAmount;
  final double? totalDrawdownPct;
  final double? maxTotalDrawdownPct;
  final bool? drawdownFromPeak;
  final int? tradingDaysCount;
  final int? minTradingDays;
  final bool? killedToday;
  final bool? killedPermanently;
  final double? maxLotSize;
  final bool? requireStopLoss;
  final String? killedReason;
  final String? updatedAt;

  factory PropFirmStatus.fromJson(Map<String, dynamic> json) {
    double? d(String k) => (json[k] as num?)?.toDouble();
    int? i(String k) => json[k] as int?;
    bool? b(String k) => json[k] as bool?;
    return PropFirmStatus(
      enabled: json['enabled'] as bool? ?? false,
      initialized: json['initialized'] as bool? ?? false,
      preset: json['preset'] as String?,
      initialBalance: d('initial_balance'),
      currentEquity: d('current_equity'),
      peakEquity: d('peak_equity'),
      profitAmount: d('profit_amount'),
      profitTargetAmount: d('profit_target_amount'),
      profitTargetPct: d('profit_target_pct'),
      profitRemainingAmount: d('profit_remaining_amount'),
      dailyStartEquity: d('daily_start_equity'),
      dailyLossAmount: d('daily_loss_amount'),
      dailyLossLimitAmount: d('daily_loss_limit_amount'),
      dailyLossPct: d('daily_loss_pct'),
      maxDailyLossPct: d('max_daily_loss_pct'),
      totalDrawdownAmount: d('total_drawdown_amount'),
      totalDrawdownLimitAmount: d('total_drawdown_limit_amount'),
      totalDrawdownPct: d('total_drawdown_pct'),
      maxTotalDrawdownPct: d('max_total_drawdown_pct'),
      drawdownFromPeak: b('drawdown_from_peak'),
      tradingDaysCount: i('trading_days_count'),
      minTradingDays: i('min_trading_days'),
      killedToday: b('killed_today'),
      killedPermanently: b('killed_permanently'),
      killedReason: json['killed_reason'] as String?,
      maxLotSize: d('max_lot_size'),
      requireStopLoss: b('require_stop_loss'),
      updatedAt: json['updated_at'] as String?,
    );
  }
}
