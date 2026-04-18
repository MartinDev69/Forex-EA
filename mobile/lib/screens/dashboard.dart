import 'dart:async';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../api/client.dart';
import '../models/status.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  BotStatus? _status;
  Account? _account;
  bool _loading = true;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final results = await Future.wait([
        widget.apiClient.status(),
        widget.apiClient.account(),
      ]);
      if (!mounted) return;
      setState(() {
        _status = results[0] as BotStatus;
        _account = results[1] as Account;
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
  }

  Future<void> _toggleBot() async {
    try {
      if (_status?.running ?? false) {
        await widget.apiClient.stopBot();
      } else {
        await widget.apiClient.startBot();
      }
      await _refresh();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error: $e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('AntiGreed'),
        centerTitle: true,
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.symmetric(vertical: 16),
          children: [
            if (_loading) const Center(child: CircularProgressIndicator()),
            if (_error != null) _ErrorCard(message: _error!),
            if (_status != null) _StatusCard(status: _status!, onToggle: _toggleBot),
            if (_account != null) _AccountCard(account: _account!),
          ],
        ),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status, required this.onToggle});
  final BotStatus status;
  final VoidCallback onToggle;

  @override
  Widget build(BuildContext context) {
    final running = status.running;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  running ? Icons.play_circle : Icons.stop_circle,
                  color: running ? Colors.greenAccent : Colors.redAccent,
                  size: 32,
                ),
                const SizedBox(width: 12),
                Text(
                  running ? 'Bot running' : 'Bot stopped',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ],
            ),
            const SizedBox(height: 12),
            _Row(label: 'MT5 connected', value: status.mt5Connected ? 'yes' : 'no'),
            _Row(label: 'Open positions', value: '${status.openPositions}'),
            _Row(
              label: 'Last heartbeat',
              value: status.lastHeartbeat == null
                  ? '—'
                  : DateFormat('HH:mm:ss').format(status.lastHeartbeat!.toLocal()),
            ),
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                onPressed: onToggle,
                icon: Icon(running ? Icons.stop : Icons.play_arrow),
                label: Text(running ? 'Stop bot' : 'Start bot'),
                style: FilledButton.styleFrom(
                  backgroundColor: running ? Colors.redAccent : Colors.greenAccent.shade700,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _AccountCard extends StatelessWidget {
  const _AccountCard({required this.account});
  final Account account;

  @override
  Widget build(BuildContext context) {
    final fmt = NumberFormat.currency(symbol: '\$', decimalDigits: 2);
    final pnlColor = account.dailyPnl >= 0 ? Colors.greenAccent : Colors.redAccent;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Account', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            _Row(label: 'Balance', value: fmt.format(account.balance)),
            _Row(label: 'Equity', value: fmt.format(account.equity)),
            _Row(
              label: 'Today P&L',
              value: fmt.format(account.dailyPnl),
              valueColor: pnlColor,
            ),
            _Row(label: 'Open positions', value: '${account.openPositions}'),
          ],
        ),
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value, this.valueColor});
  final String label;
  final String value;
  final Color? valueColor;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label, style: TextStyle(color: Colors.grey.shade400)),
          Text(
            value,
            style: TextStyle(
              fontWeight: FontWeight.w600,
              color: valueColor,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}

class _ErrorCard extends StatelessWidget {
  const _ErrorCard({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Card(
      color: Colors.red.shade900,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            const Icon(Icons.error_outline, color: Colors.white),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                'Can\'t reach the bot API.\n$message',
                style: const TextStyle(color: Colors.white),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
