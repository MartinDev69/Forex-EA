class CalendarEvent {
  final DateTime eventTime;
  final String currency;
  final String impact;
  final String title;
  final String? actual;
  final String? forecast;
  final String? previous;
  final String source;

  CalendarEvent({
    required this.eventTime,
    required this.currency,
    required this.impact,
    required this.title,
    required this.actual,
    required this.forecast,
    required this.previous,
    required this.source,
  });

  factory CalendarEvent.fromJson(Map<String, dynamic> json) => CalendarEvent(
        eventTime: DateTime.parse(json['event_time'] as String),
        currency: json['currency'] as String,
        impact: json['impact'] as String,
        title: json['title'] as String,
        actual: json['actual'] as String?,
        forecast: json['forecast'] as String?,
        previous: json['previous'] as String?,
        source: json['source'] as String,
      );
}

/// Mirror of the server's BlackoutStatusResponse. `blackout` true means the
/// RiskManager will currently reject a new trade on this symbol.
class BlackoutStatus {
  final String symbol;
  final bool blackout;
  final bool enabled;
  final int beforeMin;
  final int afterMin;
  final CalendarEvent? currentEvent;
  final CalendarEvent? nextEvent;
  final double? minutesUntilNext;

  BlackoutStatus({
    required this.symbol,
    required this.blackout,
    required this.enabled,
    required this.beforeMin,
    required this.afterMin,
    required this.currentEvent,
    required this.nextEvent,
    required this.minutesUntilNext,
  });

  factory BlackoutStatus.fromJson(Map<String, dynamic> json) => BlackoutStatus(
        symbol: json['symbol'] as String,
        blackout: json['blackout'] as bool,
        enabled: json['enabled'] as bool,
        beforeMin: json['before_min'] as int,
        afterMin: json['after_min'] as int,
        currentEvent: json['current_event'] == null
            ? null
            : CalendarEvent.fromJson(json['current_event'] as Map<String, dynamic>),
        nextEvent: json['next_event'] == null
            ? null
            : CalendarEvent.fromJson(json['next_event'] as Map<String, dynamic>),
        minutesUntilNext: json['minutes_until_next'] == null
            ? null
            : (json['minutes_until_next'] as num).toDouble(),
      );
}
