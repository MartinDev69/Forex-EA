class BotStatus {
  final bool running;
  final bool mt5Connected;
  final DateTime? lastHeartbeat;
  final int openPositions;

  BotStatus({
    required this.running,
    required this.mt5Connected,
    required this.lastHeartbeat,
    required this.openPositions,
  });

  factory BotStatus.fromJson(Map<String, dynamic> json) => BotStatus(
        running: json['running'] as bool,
        mt5Connected: json['mt5_connected'] as bool,
        lastHeartbeat: json['last_heartbeat'] == null
            ? null
            : DateTime.parse(json['last_heartbeat'] as String),
        openPositions: json['open_positions'] as int,
      );
}

class Account {
  final double balance;
  final double equity;
  final int openPositions;
  final double dailyPnl;

  Account({
    required this.balance,
    required this.equity,
    required this.openPositions,
    required this.dailyPnl,
  });

  factory Account.fromJson(Map<String, dynamic> json) => Account(
        balance: (json['balance'] as num).toDouble(),
        equity: (json['equity'] as num).toDouble(),
        openPositions: json['open_positions'] as int,
        dailyPnl: (json['daily_pnl'] as num).toDouble(),
      );
}

class Strategy {
  final String name;
  final bool enabled;

  Strategy({required this.name, required this.enabled});

  factory Strategy.fromJson(Map<String, dynamic> json) => Strategy(
        name: json['name'] as String,
        enabled: json['enabled'] as bool,
      );
}

class Trade {
  final int id;
  final String symbol;
  final String side;
  final double entryPrice;
  final double? exitPrice;
  final double pnl;
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
        pnl: (json['pnl'] as num).toDouble(),
        openedAt: DateTime.parse(json['opened_at'] as String),
        closedAt: json['closed_at'] == null
            ? null
            : DateTime.parse(json['closed_at'] as String),
      );
}
