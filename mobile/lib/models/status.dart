class BotStatus {
  final bool running;
  final bool mt5Connected;
  final DateTime? lastHeartbeat;
  final int openPositions;
  // SYMBOLS env from the bot, in declared order. Dashboard uses the
  // first entry to default the Calendar/Regime symbol picker.
  final List<String> symbols;

  BotStatus({
    required this.running,
    required this.mt5Connected,
    required this.lastHeartbeat,
    required this.openPositions,
    this.symbols = const [],
  });

  factory BotStatus.fromJson(Map<String, dynamic> json) => BotStatus(
        running: json['running'] as bool,
        mt5Connected: json['mt5_connected'] as bool,
        lastHeartbeat: json['last_heartbeat'] == null
            ? null
            : DateTime.parse(json['last_heartbeat'] as String),
        openPositions: json['open_positions'] as int,
        symbols: ((json['symbols'] as List<dynamic>?) ?? const <dynamic>[])
            .map((e) => e as String)
            .toList(),
      );
}

class Account {
  final double balance;
  final double equity;
  final int openPositions;
  final double dailyPnl;
  final String? currency;

  Account({
    required this.balance,
    required this.equity,
    required this.openPositions,
    required this.dailyPnl,
    this.currency,
  });

  factory Account.fromJson(Map<String, dynamic> json) => Account(
        balance: (json['balance'] as num).toDouble(),
        equity: (json['equity'] as num).toDouble(),
        openPositions: json['open_positions'] as int,
        dailyPnl: (json['daily_pnl'] as num).toDouble(),
        currency: json['currency'] as String?,
      );
}

class Strategy {
  final String name;
  final bool enabled;
  final String mode; // 'execute' (bot trades) | 'signal' (alerts only)
  // Admin flag: when false, the strategy's trades stay on admin's
  // account only and operators don't get them via the EA copier or
  // Telegram fan-out.
  final bool userCopyable;

  Strategy({
    required this.name,
    required this.enabled,
    required this.mode,
    this.userCopyable = true,
  });

  factory Strategy.fromJson(Map<String, dynamic> json) => Strategy(
        name: json['name'] as String,
        enabled: json['enabled'] as bool,
        mode: (json['mode'] as String?) ?? 'execute',
        userCopyable: json['user_copyable'] as bool? ?? true,
      );

  bool get isSignalOnly => mode == 'signal';
}

class Trade {
  final int id;
  final String symbol;
  final String side;
  final double entryPrice;
  final double? exitPrice;
  // Nullable on non-admin operators when the EA hasn't reported a fill
  // for this broker_ticket yet — the API returns null and the UI shows
  // "—". This keeps admin's USD pnl from leaking into a ZAR operator's
  // view while still surfacing the trade metadata.
  final double? pnl;
  final DateTime openedAt;
  final DateTime? closedAt;

  Trade({
    required this.id,
    required this.symbol,
    required this.side,
    required this.entryPrice,
    required this.exitPrice,
    required this.pnl,
    required this.openedAt,
    required this.closedAt,
  });

  factory Trade.fromJson(Map<String, dynamic> json) => Trade(
        id: json['id'] as int,
        symbol: json['symbol'] as String,
        side: json['side'] as String,
        entryPrice: (json['entry_price'] as num).toDouble(),
        exitPrice: json['exit_price'] == null
            ? null
            : (json['exit_price'] as num).toDouble(),
        pnl: json['pnl'] == null ? null : (json['pnl'] as num).toDouble(),
        openedAt: DateTime.parse(json['opened_at'] as String),
        closedAt: json['closed_at'] == null
            ? null
            : DateTime.parse(json['closed_at'] as String),
      );
}

class PendingOrder {
  final int ticket;
  final String symbol;
  final String orderType; // buy_limit | sell_limit | buy_stop | sell_stop
  final double price;
  final double volume;
  final double? sl;
  final double? tp;
  final String? comment;
  final DateTime placedAt;

  PendingOrder({
    required this.ticket,
    required this.symbol,
    required this.orderType,
    required this.price,
    required this.volume,
    required this.sl,
    required this.tp,
    required this.comment,
    required this.placedAt,
  });

  factory PendingOrder.fromJson(Map<String, dynamic> json) => PendingOrder(
        ticket: json['ticket'] as int,
        symbol: json['symbol'] as String,
        orderType: json['order_type'] as String,
        price: (json['price'] as num).toDouble(),
        volume: (json['volume'] as num).toDouble(),
        sl: json['sl'] == null ? null : (json['sl'] as num).toDouble(),
        tp: json['tp'] == null ? null : (json['tp'] as num).toDouble(),
        comment: json['comment'] as String?,
        placedAt: DateTime.parse(json['placed_at'] as String),
      );

  bool get isBuy => orderType.startsWith('buy');
}
