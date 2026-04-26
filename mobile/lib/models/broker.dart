class BrokerPreset {
  final String id;
  final String displayName;
  final List<String> servers;
  final String mt5PathHint;
  final String notes;

  BrokerPreset({
    required this.id,
    required this.displayName,
    required this.servers,
    required this.mt5PathHint,
    required this.notes,
  });

  factory BrokerPreset.fromJson(Map<String, dynamic> json) => BrokerPreset(
        id: json['id'] as String,
        displayName: json['display_name'] as String,
        servers: (json['servers'] as List<dynamic>).map((e) => e as String).toList(),
        mt5PathHint: (json['mt5_path_hint'] as String?) ?? '',
        notes: (json['notes'] as String?) ?? '',
      );
}

class BrokerConfig {
  final String broker;
  final int login;
  final String server;
  final String mt5Path;
  final bool passwordSet;
  final String passwordFingerprint;
  final String updatedAt;

  BrokerConfig({
    required this.broker,
    required this.login,
    required this.server,
    required this.mt5Path,
    required this.passwordSet,
    required this.passwordFingerprint,
    required this.updatedAt,
  });

  factory BrokerConfig.fromJson(Map<String, dynamic> json) => BrokerConfig(
        broker: json['broker'] as String,
        login: json['login'] as int,
        server: json['server'] as String,
        mt5Path: (json['mt5_path'] as String?) ?? '',
        passwordSet: json['password_set'] as bool,
        passwordFingerprint: (json['password_fingerprint'] as String?) ?? '',
        updatedAt: (json['updated_at'] as String?) ?? '',
      );
}

class BrokerStatus {
  final bool connected;
  final String? broker;
  final String? server;
  final int? login;
  final Map<String, dynamic>? accountInfo;
  final String? lastError;
  final DateTime? updatedAt;
  final double? staleS;

  BrokerStatus({
    required this.connected,
    this.broker,
    this.server,
    this.login,
    this.accountInfo,
    this.lastError,
    this.updatedAt,
    this.staleS,
  });

  factory BrokerStatus.fromJson(Map<String, dynamic> json) => BrokerStatus(
        connected: json['connected'] as bool,
        broker: json['broker'] as String?,
        server: json['server'] as String?,
        login: json['login'] as int?,
        accountInfo: json['account_info'] as Map<String, dynamic>?,
        lastError: json['last_error'] as String?,
        updatedAt: json['updated_at'] == null
            ? null
            : DateTime.parse(json['updated_at'] as String),
        staleS: json['stale_s'] == null ? null : (json['stale_s'] as num).toDouble(),
      );
}

class BrokerTestResult {
  final bool ok;
  final String? error;
  final Map<String, dynamic>? account;

  BrokerTestResult({required this.ok, this.error, this.account});

  factory BrokerTestResult.fromJson(Map<String, dynamic> json) => BrokerTestResult(
        ok: json['ok'] as bool,
        error: json['error'] as String?,
        account: json['account'] as Map<String, dynamic>?,
      );
}
