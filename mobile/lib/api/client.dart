import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/status.dart';

class ApiClient {
  ApiClient({required this.baseUrl});

  final String baseUrl;

  Uri _u(String path) => Uri.parse('$baseUrl$path');

  Future<BotStatus> status() async {
    final r = await http.get(_u('/status'));
    _check(r);
    return BotStatus.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<Account> account() async {
    final r = await http.get(_u('/account'));
    _check(r);
    return Account.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<List<Strategy>> strategies() async {
    final r = await http.get(_u('/strategies'));
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list
        .map((e) => Strategy.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<Strategy> toggleStrategy(String name) async {
    final r = await http.post(_u('/strategies/$name/toggle'));
    _check(r);
    return Strategy.fromJson(json.decode(r.body) as Map<String, dynamic>);
  }

  Future<List<Trade>> trades({int limit = 20}) async {
    final r = await http.get(_u('/trades?limit=$limit'));
    _check(r);
    final list = json.decode(r.body) as List<dynamic>;
    return list.map((e) => Trade.fromJson(e as Map<String, dynamic>)).toList();
  }

  Future<void> startBot() async {
    final r = await http.post(_u('/bot/start'));
    _check(r);
  }

  Future<void> stopBot() async {
    final r = await http.post(_u('/bot/stop'));
    _check(r);
  }

  void _check(http.Response r) {
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw ApiException(r.statusCode, r.body);
    }
  }
}

class ApiException implements Exception {
  ApiException(this.statusCode, this.body);
  final int statusCode;
  final String body;
  @override
  String toString() => 'ApiException($statusCode): $body';
}
