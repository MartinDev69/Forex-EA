import 'dart:async';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api/client.dart';
import '../models/allocator.dart';
import '../models/calendar.dart';
import '../models/correlation.dart';
import '../models/drift.dart';
import '../models/fill_stats.dart';
import '../models/regime.dart';
import '../models/status.dart';
import '../theme.dart';
import '../widgets/logo_spinner.dart';

const String _kBlackoutSymbolKey = 'antigreed:blackoutSymbol';
const String _kDefaultBlackoutSymbol = 'EURUSD';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({
    super.key,
    required this.apiClient,
    required this.onSignedOut,
    required this.onForgetDevice,
  });
  final ApiClient apiClient;
  final VoidCallback onSignedOut;
  final VoidCallback onForgetDevice;

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  BotStatus? _status;
  Account? _account;
  BlackoutStatus? _blackout;
  Regime? _regime;
  CorrelationResponse? _correlations;
  DriftResponse? _drift;
  FillStatsResponse? _fillStats;
  AllocatorResponse? _allocator;
  String _blackoutSymbol = _kDefaultBlackoutSymbol;
  bool _loading = true;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _loadSymbolThenRefresh();
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _loadSymbolThenRefresh() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString(_kBlackoutSymbolKey);
    if (saved != null && saved.isNotEmpty) _blackoutSymbol = saved;
    await _refresh();
  }

  Future<void> _saveSymbol(String symbol) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_kBlackoutSymbolKey, symbol);
  }

  Future<void> _refresh() async {
    try {
      final results = await Future.wait([
        widget.apiClient.status(),
        widget.apiClient.account(),
        widget.apiClient.blackoutStatus(_blackoutSymbol),
        widget.apiClient.regime(_blackoutSymbol),
        widget.apiClient.correlations(),
        widget.apiClient.drift(),
        widget.apiClient.fillStats(),
        widget.apiClient.allocator(),
      ]);
      if (!mounted) return;
      setState(() {
        _status = results[0] as BotStatus;
        _account = results[1] as Account;
        _blackout = results[2] as BlackoutStatus;
        _regime = results[3] as Regime;
        _correlations = results[4] as CorrelationResponse;
        _drift = results[5] as DriftResponse;
        _fillStats = results[6] as FillStatsResponse;
        _allocator = results[7] as AllocatorResponse;
        _loading = false;
        _error = null;
      });
    } on UnauthorizedException {
      // Token expired — bail back to login. ApiClient already cleared it.
      _timer?.cancel();
      if (mounted) widget.onSignedOut();
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
  }

  Future<void> _changeBlackoutSymbol() async {
    final controller = TextEditingController(text: _blackoutSymbol);
    final next = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Calendar symbol'),
        content: TextField(
          controller: controller,
          autofocus: true,
          textCapitalization: TextCapitalization.characters,
          decoration: const InputDecoration(
            hintText: 'EURUSD, XAUUSD, US30…',
          ),
          onSubmitted: (v) => Navigator.pop(ctx, v.trim().toUpperCase()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(
            onPressed: () =>
                Navigator.pop(ctx, controller.text.trim().toUpperCase()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (next == null || next.isEmpty || next == _blackoutSymbol) return;
    setState(() => _blackoutSymbol = next);
    await _saveSymbol(next);
    await _refresh();
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
        centerTitle: true,
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Image.asset('assets/antigreed-logo.png', height: 28, fit: BoxFit.contain),
            const SizedBox(width: 8),
            const Text('AntiGreed'),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 14),
            child: _RolePill(role: widget.apiClient.role ?? 'admin'),
          ),
          ValueListenableBuilder<ThemeMode>(
            valueListenable: themeMode,
            builder: (context, mode, _) => IconButton(
              tooltip: mode == ThemeMode.dark ? 'Switch to light' : 'Switch to dark',
              icon: Icon(mode == ThemeMode.dark
                  ? Icons.light_mode_outlined
                  : Icons.dark_mode_outlined),
              onPressed: toggleThemeMode,
            ),
          ),
          PopupMenuButton<String>(
            tooltip: 'Account',
            icon: const Icon(Icons.account_circle_outlined),
            onSelected: (value) async {
              if (value == 'sign_out') {
                widget.onSignedOut();
              } else if (value == 'forget_device') {
                final confirmed = await showDialog<bool>(
                  context: context,
                  builder: (ctx) => AlertDialog(
                    title: const Text('Forget this device?'),
                    content: const Text(
                      'Removes the saved PIN and biometric. You\'ll need to '
                      'sign in with your password and set up quick unlock again.',
                    ),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.of(ctx).pop(false),
                        child: const Text('Cancel'),
                      ),
                      FilledButton(
                        onPressed: () => Navigator.of(ctx).pop(true),
                        style: FilledButton.styleFrom(
                          backgroundColor: Colors.red.shade700,
                        ),
                        child: const Text('Forget'),
                      ),
                    ],
                  ),
                );
                if (confirmed == true) widget.onForgetDevice();
              }
            },
            itemBuilder: (_) => const [
              PopupMenuItem(
                value: 'sign_out',
                child: ListTile(
                  leading: Icon(Icons.logout),
                  title: Text('Sign out'),
                  subtitle: Text('Quick unlock stays set up',
                    style: TextStyle(fontSize: 11)),
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                ),
              ),
              PopupMenuItem(
                value: 'forget_device',
                child: ListTile(
                  leading: Icon(Icons.delete_outline),
                  title: Text('Forget this device'),
                  subtitle: Text('Wipes saved PIN + biometric',
                    style: TextStyle(fontSize: 11)),
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                ),
              ),
            ],
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.symmetric(vertical: 16),
          children: [
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 64),
                child: Center(child: LogoSpinner(size: 88, label: 'LOADING')),
              ),
            if (_error != null) _ErrorCard(message: _error!),
            if (_status != null)
              _StatusCard(
                status: _status!,
                onToggle: _toggleBot,
              ),
            if (_account != null) _AccountCard(account: _account!),
            if (_blackout != null)
              _BlackoutCard(
                status: _blackout!,
                onChangeSymbol: _changeBlackoutSymbol,
              ),
            if (_regime != null) _RegimeCard(regime: _regime!),
            if (_correlations != null && _correlations!.pairs.isNotEmpty)
              _CorrelationCard(data: _correlations!),
            if (_drift != null && _drift!.reports.isNotEmpty)
              _DriftCard(data: _drift!),
            if (_fillStats != null && _fillStats!.symbols.isNotEmpty)
              _ExecutionQualityCard(data: _fillStats!),
            if (_allocator != null && _allocator!.allocations.isNotEmpty)
              _AllocatorCard(data: _allocator!),
          ],
        ),
      ),
    );
  }
}

class _RolePill extends StatelessWidget {
  const _RolePill({required this.role});
  final String role;

  @override
  Widget build(BuildContext context) {
    final isAdmin = role == 'admin';
    final fg = isAdmin ? Colors.cyanAccent : Colors.grey.shade300;
    return Container(
      margin: const EdgeInsets.only(right: 4),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: (isAdmin ? Colors.cyanAccent : Colors.grey).withValues(alpha: 0.12),
        border: Border.all(color: fg.withValues(alpha: 0.5)),
        borderRadius: BorderRadius.circular(6),
      ),
      alignment: Alignment.center,
      child: Text(
        role.toUpperCase(),
        style: TextStyle(fontSize: 10, letterSpacing: 1.5, color: fg, fontWeight: FontWeight.w600),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status, required this.onToggle});
  final BotStatus status;
  final VoidCallback? onToggle;

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
            if (onToggle != null)
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
              )
            else
              Text(
                'Read-only account · ask an admin to start or stop the bot.',
                style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
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
    final pnlPositive = account.dailyPnl >= 0;
    final pnlTone = pnlPositive ? TickerTone.win : TickerTone.loss;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 6, horizontal: 12),
      padding: const EdgeInsets.all(18),
      decoration: glowPanel(glow: pnlPositive ? kNeonGreen : kNeonRed),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'ACCOUNT',
            style: TextStyle(
              color: kMuted,
              fontSize: 10,
              letterSpacing: 3,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 10),
          // Hero number — equity, the one a trader actually watches.
          TickerText(fmt.format(account.equity), size: 34),
          const SizedBox(height: 4),
          Text(
            'EQUITY',
            style: TextStyle(color: kMuted, fontSize: 9, letterSpacing: 3),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: _AccountStat(
                  label: 'BALANCE',
                  value: fmt.format(account.balance),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _AccountStat(
                  label: 'TODAY P&L',
                  value: fmt.format(account.dailyPnl),
                  tone: pnlTone,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _AccountStat(
                  label: 'OPEN',
                  value: '${account.openPositions}',
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _AccountStat extends StatelessWidget {
  const _AccountStat({
    required this.label,
    required this.value,
    this.tone = TickerTone.neutral,
  });
  final String label;
  final String value;
  final TickerTone tone;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: const TextStyle(color: kMuted, fontSize: 9, letterSpacing: 2),
        ),
        const SizedBox(height: 4),
        TickerText(value, tone: tone, size: 14),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});
  final String label;
  final String value;
  final Color? valueColor = null;

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

class _BlackoutCard extends StatelessWidget {
  const _BlackoutCard({required this.status, required this.onChangeSymbol});
  final BlackoutStatus status;
  final VoidCallback onChangeSymbol;

  @override
  Widget build(BuildContext context) {
    final tone = _tone(status);
    final color = _colorFor(tone);
    final (icon, headline, subline) = _summary(status);

    return Card(
      color: color.withValues(alpha: 0.12),
      shape: RoundedRectangleBorder(
        side: BorderSide(color: color.withValues(alpha: 0.6)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: color),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Calendar · ${status.symbol}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                TextButton.icon(
                  onPressed: onChangeSymbol,
                  icon: const Icon(Icons.edit, size: 14),
                  label: const Text('Change'),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(headline, style: TextStyle(color: color, fontWeight: FontWeight.w600)),
            if (subline != null) ...[
              const SizedBox(height: 4),
              Text(subline, style: TextStyle(color: Colors.grey.shade400, fontSize: 12)),
            ],
            if (!status.enabled) ...[
              const SizedBox(height: 8),
              Text(
                'Blackout disabled. Trades will not be blocked around events.',
                style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
              ),
            ],
          ],
        ),
      ),
    );
  }

  static String _tone(BlackoutStatus s) {
    if (!s.enabled) return 'muted';
    if (s.blackout) return 'danger';
    final m = s.minutesUntilNext;
    if (m != null && m <= (s.beforeMin + 30)) return 'warn';
    return 'ok';
  }

  static Color _colorFor(String tone) {
    switch (tone) {
      case 'danger':
        return Colors.redAccent;
      case 'warn':
        return Colors.amber;
      case 'ok':
        return Colors.greenAccent;
      default:
        return Colors.grey;
    }
  }

  static (IconData, String, String?) _summary(BlackoutStatus s) {
    if (!s.enabled) {
      return (Icons.event_busy, 'Blackout off', null);
    }
    if (s.blackout && s.currentEvent != null) {
      final e = s.currentEvent!;
      final ts = DateFormat('HH:mm').format(e.eventTime.toLocal());
      return (
        Icons.block,
        'BLACKOUT · ${e.currency} ${e.impact.toUpperCase()}',
        '${e.title} at $ts — new trades on ${s.symbol} are blocked.',
      );
    }
    if (s.nextEvent != null && s.minutesUntilNext != null) {
      final e = s.nextEvent!;
      final ts = DateFormat('EEE HH:mm').format(e.eventTime.toLocal());
      final countdown = _fmtDuration(s.minutesUntilNext!);
      return (
        Icons.schedule,
        'Next · ${e.currency} ${e.impact.toUpperCase()} in $countdown',
        '${e.title} · $ts',
      );
    }
    return (Icons.check_circle_outline, 'Clear', 'No high-impact events in the window.');
  }

  static String _fmtDuration(double totalMinutes) {
    if (totalMinutes < 1) return '<1m';
    final m = totalMinutes.floor();
    if (m < 60) return '${m}m';
    final h = m ~/ 60;
    final rem = m % 60;
    if (h < 24) return rem == 0 ? '${h}h' : '${h}h ${rem}m';
    final d = h ~/ 24;
    final hRem = h % 24;
    return hRem == 0 ? '${d}d' : '${d}d ${hRem}h';
  }
}

class _RegimeCard extends StatelessWidget {
  const _RegimeCard({required this.regime});
  final Regime regime;

  @override
  Widget build(BuildContext context) {
    final (icon, color, headline, subline) = _render(regime);
    return Card(
      color: color.withValues(alpha: 0.10),
      shape: RoundedRectangleBorder(
        side: BorderSide(color: color.withValues(alpha: 0.5)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: color),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Regime · ${regime.symbol}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                if (regime.adx != null)
                  Text(
                    'ADX ${regime.adx!.toStringAsFixed(0)}',
                    style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                  ),
              ],
            ),
            const SizedBox(height: 6),
            Text(headline, style: TextStyle(color: color, fontWeight: FontWeight.w600)),
            if (subline != null) ...[
              const SizedBox(height: 4),
              Text(subline, style: TextStyle(color: Colors.grey.shade400, fontSize: 12)),
            ],
          ],
        ),
      ),
    );
  }

  static (IconData, Color, String, String?) _render(Regime r) {
    if (!r.isKnown) {
      return (
        Icons.help_outline,
        Colors.grey,
        'Unknown',
        'Waiting for the bot to classify this symbol.',
      );
    }
    final vol = r.volatility != 'unknown' ? ' · vol ${r.volatility}' : '';
    switch (r.trend) {
      case 'trend_up':
        return (
          Icons.trending_up,
          Colors.greenAccent,
          'Trend up$vol',
          'Trend strategies are favored; mean-reversion will be gated.',
        );
      case 'trend_down':
        return (
          Icons.trending_down,
          Colors.redAccent,
          'Trend down$vol',
          'Trend strategies are favored; mean-reversion will be gated.',
        );
      case 'range':
        return (
          Icons.swap_horiz,
          Colors.amber,
          'Range$vol',
          'Mean-reversion is favored; trend entries will be gated.',
        );
      default:
        return (Icons.help_outline, Colors.grey, r.label, null);
    }
  }
}

class _CorrelationCard extends StatelessWidget {
  const _CorrelationCard({required this.data});
  final CorrelationResponse data;

  @override
  Widget build(BuildContext context) {
    // Show top 8 by absolute value — that's where the concentration risk lives.
    final pairs = [...data.pairs]
      ..sort((a, b) => b.value.abs().compareTo(a.value.abs()));
    final top = pairs.take(8).toList();

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.hub_outlined, color: Colors.cyanAccent),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Correlation',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                Text(
                  '${data.count} pairs',
                  style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Top correlated pairs · throttle blocks pile-ons above the heat cap.',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
            ),
            const SizedBox(height: 8),
            ...top.map((p) => _CorrelationRow(pair: p)),
          ],
        ),
      ),
    );
  }
}

class _CorrelationRow extends StatelessWidget {
  const _CorrelationRow({required this.pair});
  final CorrelationPair pair;

  @override
  Widget build(BuildContext context) {
    final v = pair.value;
    final mag = v.abs();
    final color = mag >= 0.60
        ? (v >= 0 ? Colors.redAccent : Colors.greenAccent)
        : mag >= 0.30
            ? Colors.amber
            : Colors.grey;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          Expanded(
            child: Text(
              '${pair.symbolA} ↔ ${pair.symbolB}',
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            decoration: BoxDecoration(
              color: color.withValues(alpha: 0.15),
              border: Border.all(color: color.withValues(alpha: 0.6)),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              v.toStringAsFixed(2),
              style: TextStyle(
                color: color,
                fontWeight: FontWeight.w700,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _DriftCard extends StatelessWidget {
  const _DriftCard({required this.data});
  final DriftResponse data;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.insights_outlined, color: Colors.cyanAccent),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Strategy drift',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                Text(
                  '${data.count} tracked',
                  style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Live performance vs backtest baseline.',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
            ),
            const SizedBox(height: 8),
            ...data.reports.map((r) => _DriftRow(report: r)),
          ],
        ),
      ),
    );
  }
}

class _DriftRow extends StatelessWidget {
  const _DriftRow({required this.report});
  final DriftReport report;

  @override
  Widget build(BuildContext context) {
    final color = _statusColor(report.status);
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        border: Border.all(color: color.withValues(alpha: 0.5)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${report.strategy} · ${report.symbol}',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  report.status.toUpperCase(),
                  style: TextStyle(
                    color: color,
                    fontWeight: FontWeight.w700,
                    fontSize: 10,
                    letterSpacing: 1.2,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            report.note,
            style: TextStyle(color: Colors.grey.shade400, fontSize: 11),
          ),
          if (report.metrics.isNotEmpty) ...[
            const SizedBox(height: 6),
            Row(
              children: report.metrics
                  .map((m) => Expanded(child: _DriftMetricChip(metric: m)))
                  .toList(),
            ),
          ],
        ],
      ),
    );
  }

  static Color _statusColor(String status) {
    switch (status) {
      case 'ok':
        return Colors.greenAccent;
      case 'warn':
        return Colors.amber;
      case 'danger':
        return Colors.redAccent;
      default:
        return Colors.grey;
    }
  }
}

class _DriftMetricChip extends StatelessWidget {
  const _DriftMetricChip({required this.metric});
  final DriftMetric metric;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 6),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.20),
          borderRadius: BorderRadius.circular(4),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              metric.name.replaceAll('_', ' '),
              style: TextStyle(color: Colors.grey.shade500, fontSize: 9),
            ),
            Text(
              '${metric.live.toStringAsFixed(2)}/${metric.baseline.toStringAsFixed(2)}',
              style: const TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w600,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ExecutionQualityCard extends StatelessWidget {
  const _ExecutionQualityCard({required this.data});
  final FillStatsResponse data;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.speed_outlined, color: Colors.cyanAccent),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Execution quality',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                Text(
                  'last ${data.windowHours}h',
                  style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Slippage in pips · positive = adverse to you.',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
            ),
            const SizedBox(height: 8),
            ...data.symbols.map((s) => _ExecutionRow(stats: s)),
          ],
        ),
      ),
    );
  }
}

class _ExecutionRow extends StatelessWidget {
  const _ExecutionRow({required this.stats});
  final FillSymbolStats stats;

  @override
  Widget build(BuildContext context) {
    final color = _toneFor(stats);
    final slipSign = stats.avgSlippagePips >= 0 ? '+' : '';
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        border: Border.all(color: color.withValues(alpha: 0.5)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  stats.symbol,
                  style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                ),
              ),
              Text(
                '${stats.fillCount} fills'
                '${stats.rejectedCount > 0 ? " · ${stats.rejectedCount} rejected" : ""}',
                style: TextStyle(color: Colors.grey.shade400, fontSize: 11),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              Expanded(child: _StatChip(
                label: 'avg slip',
                value: '$slipSign${stats.avgSlippagePips.toStringAsFixed(2)} p',
              )),
              Expanded(child: _StatChip(
                label: 'max slip',
                value: '${stats.maxSlippagePips >= 0 ? '+' : ''}'
                    '${stats.maxSlippagePips.toStringAsFixed(2)} p',
              )),
              Expanded(child: _StatChip(
                label: 'latency',
                value: '${stats.avgLatencyMs.toStringAsFixed(0)} ms',
              )),
              Expanded(child: _StatChip(
                label: 'p95',
                value: '${stats.p95LatencyMs.toStringAsFixed(0)} ms',
              )),
            ],
          ),
        ],
      ),
    );
  }

  static Color _toneFor(FillSymbolStats s) {
    if (s.avgSlippagePips >= 1.5 || s.rejectedCount > 2) return Colors.redAccent;
    if (s.avgSlippagePips >= 0.5 || s.rejectedCount > 0) return Colors.amber;
    return Colors.greenAccent;
  }
}

class _StatChip extends StatelessWidget {
  const _StatChip({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 6),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.20),
          borderRadius: BorderRadius.circular(4),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label, style: TextStyle(color: Colors.grey.shade500, fontSize: 9)),
            Text(
              value,
              style: const TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w600,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _AllocatorCard extends StatelessWidget {
  const _AllocatorCard({required this.data});
  final AllocatorResponse data;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.tune, color: Colors.cyanAccent),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'Auto-allocator',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Champion → full risk · challenger → half · probe → sliver · cold → paused.',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
            ),
            const SizedBox(height: 8),
            ...data.allocations.map((a) => _AllocatorRow(alloc: a)),
          ],
        ),
      ),
    );
  }
}

class _AllocatorRow extends StatelessWidget {
  const _AllocatorRow({required this.alloc});
  final Allocation alloc;

  @override
  Widget build(BuildContext context) {
    final tone = _toneFor(alloc.role);
    final rSign = alloc.avgR >= 0 ? '+' : '';
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: 0.10),
        border: Border.all(color: tone.withValues(alpha: 0.5)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${alloc.strategy} · ${alloc.symbol}',
                  style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: tone.withValues(alpha: 0.25),
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Text(
                  alloc.role.toUpperCase(),
                  style: TextStyle(
                    color: tone,
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.6,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '${(alloc.weight * 100).toStringAsFixed(0)}%',
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  fontFeatures: [FontFeature.tabularFigures()],
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              Expanded(child: _StatChip(
                label: 'avg R',
                value: '$rSign${alloc.avgR.toStringAsFixed(2)}',
              )),
              Expanded(child: _StatChip(
                label: 'win',
                value: '${(alloc.winRate * 100).toStringAsFixed(0)}%',
              )),
              Expanded(child: _StatChip(
                label: 'trades',
                value: '${alloc.sampleSize}',
              )),
            ],
          ),
          if (alloc.note.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              alloc.note,
              style: TextStyle(color: Colors.grey.shade400, fontSize: 11),
            ),
          ],
        ],
      ),
    );
  }

  static Color _toneFor(String role) {
    switch (role) {
      case 'champion':
        return Colors.greenAccent;
      case 'challenger':
        return Colors.lightBlueAccent;
      case 'probe':
        return Colors.amber;
      case 'cold':
      default:
        return Colors.grey;
    }
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
