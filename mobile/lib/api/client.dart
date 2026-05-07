import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/allocator.dart';
import '../models/broker.dart';
import '../models/calendar.dart';
import '../models/correlation.dart';
import '../models/drift.dart';
import '../models/explanation.dart';
import '../models/fill_stats.dart';
import '../models/regime.dart';
import '../models/status.dart';
import '../models/user.dart';

class ApiClient {
  ApiClient({required this.baseUrl, this.token});

  final String baseUrl;
  String? token;
  String? username;
  String? role;

  bool get isAdmin => role == 'admin';

  Uri _u(String path) => Uri.parse('$baseUrl$path');

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (token != null) 'Authorization': 'Bearer $token',
      };

  /// Sign in. On success, stashes the JWT on this client.
  Future<void> login(String user, String password) async {
    final r = await http.post(
      _u('/auth/login'),
      headers: {'Content-Type': 'application/json'},
      body: json.encode({'username': user, 'password': password}),
    );
    if (r.statusCode == 401) {
      throw ApiException(401, 'Invalid username or password.');
    }
    if (r.statusCode == 429) {
      throw ApiException(429, 'Too many attempts. Try again later.');
    }
    _check(r);
    final body = json.decode(r.body) as Map<String, dynamic>;
    token = body['access_token'] as String;
    username = body['username'] as String? ?? user;
    // Legacy tokens issued before roles existed will return admin from the server.
    role = body['role'] as String? ?? 'admin';
  }

  void logout() {
    token = null;
    username = null;
    role = null;
  }

  Future<BotStatus> status() async {
    final r = await http.get(_u('/status'), headers: _headers);
    _check(r);
    return BotStatus.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<Account> account() async {
    final r = await http.get(_u('/account'), headers: _headers);
    _check(r);
    return Account.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<List<Strategy>> strategies() async {
    final r = await http.get(_u('/strategies'), headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list
        .map((e) => Strategy.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<Strategy> setStrategyMode(String name, String mode) async {
    final r = await http.post(
      _u('/strategies/${Uri.encodeComponent(name)}/mode'),
      headers: _headers,
      body: json.encode({'mode': mode}),
    );
    _check(r);
    return Strategy.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<Strategy> toggleStrategy(String name) async {
    final r = await http.post(_u('/strategies/$name/toggle'), headers: _headers);
    _check(r);
    return Strategy.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<List<Trade>> trades({int limit = 20}) async {
    final r = await http.get(_u('/trades?limit=$limit'), headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list.map((e) => Trade.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<List<PendingOrder>> pendingOrders() async {
    final r = await http.get(_u('/orders/pending'), headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list.map((e) => PendingOrder.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> startBot() async {
    final r = await http.post(_u('/bot/start'), headers: _headers);
    _check(r);
  }

  Future<void> stopBot() async {
    final r = await http.post(_u('/bot/stop'), headers: _headers);
    _check(r);
  }

  // ---------- Broker management ----------

  Future<List<BrokerPreset>> brokerPresets() async {
    final r = await http.get(_u('/brokers'), headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list
        .map((e) => BrokerPreset.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<BrokerConfig?> brokerConfig() async {
    final r = await http.get(_u('/broker/config'), headers: _headers);
    _check(r);
    if (r.body.trim().isEmpty || r.body.trim() == 'null') return null;
    final parsed = json.decode(r.body);
    if (parsed == null) return null;
    return BrokerConfig.fromJson(parsed as Map<String, dynamic>);
  }

  Future<BrokerConfig> saveBrokerConfig({
    required String broker,
    required int login,
    required String password,
    required String server,
    required String mt5Path,
  }) async {
    final r = await http.put(
      _u('/broker/config'),
      headers: _headers,
      body: json.encode({
        'broker': broker,
        'login': login,
        'password': password,
        'server': server,
        'mt5_path': mt5Path,
      }),
    );
    _check(r);
    return BrokerConfig.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<void> clearBrokerConfig() async {
    final r = await http.delete(_u('/broker/config'), headers: _headers);
    _check(r);
  }

  Future<BrokerTestResult> testBroker({
    required String broker,
    required int login,
    required String password,
    required String server,
    required String mt5Path,
  }) async {
    final r = await http.post(
      _u('/broker/test'),
      headers: _headers,
      body: json.encode({
        'broker': broker,
        'login': login,
        'password': password,
        'server': server,
        'mt5_path': mt5Path,
      }),
    );
    _check(r);
    return BrokerTestResult.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<BrokerStatus> brokerStatus() async {
    final r = await http.get(_u('/broker/status'), headers: _headers);
    _check(r);
    return BrokerStatus.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  // ---------- Economic calendar ----------

  Future<BlackoutStatus> blackoutStatus(String symbol) async {
    final r = await http.get(
      _u('/calendar/blackout/${Uri.encodeComponent(symbol)}'),
      headers: _headers,
    );
    _check(r);
    return BlackoutStatus.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<List<CalendarEvent>> calendarEvents({
    int hoursAhead = 24,
    String? symbol,
  }) async {
    final q = <String, String>{'hours_ahead': '$hoursAhead'};
    if (symbol != null && symbol.isNotEmpty) q['symbol'] = symbol;
    final uri = _u('/calendar/events').replace(queryParameters: q);
    final r = await http.get(uri, headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list
        .map((e) => CalendarEvent.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  // ---------- Market regime ----------

  Future<Regime> regime(String symbol) async {
    final r = await http.get(
      _u('/regime/${Uri.encodeComponent(symbol)}'),
      headers: _headers,
    );
    _check(r);
    return Regime.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<CorrelationResponse> correlations() async {
    final r = await http.get(_u('/correlation'), headers: _headers);
    _check(r);
    return CorrelationResponse.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<DriftResponse> drift() async {
    final r = await http.get(_u('/drift'), headers: _headers);
    _check(r);
    return DriftResponse.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<FillStatsResponse> fillStats({int windowHours = 24}) async {
    final r = await http.get(
      _u('/fills/stats?window_hours=$windowHours'),
      headers: _headers,
    );
    _check(r);
    return FillStatsResponse.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<AllocatorResponse> allocator() async {
    final r = await http.get(_u('/allocator'), headers: _headers);
    _check(r);
    return AllocatorResponse.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  /// Returns null when the server has no explanation for this trade
  /// (404 — trade pre-dates the feature or explanations were disabled).
  Future<TradeExplanation?> tradeExplanation(int tradeId) async {
    final r = await http.get(_u('/trades/$tradeId/explain'), headers: _headers);
    if (r.statusCode == 404) return null;
    _check(r);
    return TradeExplanation.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  // ---------- User management (admin only) ----------

  Future<List<AppUser>> listUsers() async {
    final r = await http.get(_u('/users'), headers: _headers);
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list
        .map((e) => AppUser.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<AdIdPool> unclaimedPool() async {
    final r = await http.get(_u('/users/pool'), headers: _headers);
    _check(r);
    return AdIdPool.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<AdIdPool> refillPool({int target = 100}) async {
    final r = await http.post(
      _u('/users/pool/refill?target=$target'),
      headers: _headers,
    );
    _check(r);
    return AdIdPool.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<AssignResult> assignUser({
    required String adId,
    required String email,
    String duration = '1m',
  }) async {
    final r = await http.post(
      _u('/users/assign'),
      headers: _headers,
      body: json.encode({'ad_id': adId, 'email': email, 'duration': duration}),
    );
    _check(r);
    return AssignResult.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<AppUser> extendUser(String username, String duration) async {
    final r = await http.post(
      _u('/users/$username/extend'),
      headers: _headers,
      body: json.encode({'duration': duration}),
    );
    _check(r);
    return AppUser.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<AssignResult> resendSetupLink(String username) async {
    final r = await http.post(
      _u('/users/$username/resend'),
      headers: _headers,
    );
    _check(r);
    return AssignResult.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<void> deleteUser(String username) async {
    final r = await http.delete(_u('/users/$username'), headers: _headers);
    _check(r);
  }

  Future<void> resetUserPassword(String username, String newPassword) async {
    final r = await http.post(
      _u('/users/$username/reset-password'),
      headers: _headers,
      body: json.encode({'password': newPassword}),
    );
    _check(r);
  }

  void _check(http.Response r) {
    if (r.statusCode == 401) {
      // Token expired or was rotated server-side — bounce to login.
      logout();
      throw UnauthorizedException();
    }
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw ApiException(r.statusCode, r.body);
    }
  }
}

class ApiException implements Exception {
  ApiException(this.statusCode, this.body);
  final int statusCode;
  final String body;

  /// FastAPI renders HTTPException as {"detail": "..."}. Pull that out so the
  /// UI can surface a clean message instead of the raw JSON envelope.
  String get detail {
    try {
      final parsed = json.decode(body);
      if (parsed is Map && parsed['detail'] is String) {
        return parsed['detail'] as String;
      }
    } catch (_) { /* body wasn't JSON */ }
    return body;
  }

  @override
  String toString() => 'ApiException($statusCode): $detail';
}

class UnauthorizedException implements Exception {
  @override
  String toString() => 'Unauthorized';
}
