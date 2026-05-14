import 'package:flutter/material.dart';
import '../api/client.dart';
import '../models/status.dart';
import '../theme.dart';
import '../widgets/logo_spinner.dart';

class StrategiesScreen extends StatefulWidget {
  const StrategiesScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<StrategiesScreen> createState() => _StrategiesScreenState();
}

class _StrategiesScreenState extends State<StrategiesScreen> {
  List<Strategy>? _strategies;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final s = await widget.apiClient.strategies();
      if (!mounted) return;
      setState(() {
        _strategies = s;
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

  Future<void> _toggle(Strategy s) async {
    try {
      final updated = await widget.apiClient.toggleStrategy(s.name);
      if (!mounted) return;
      setState(() {
        _strategies = _strategies!
            .map((x) => x.name == updated.name ? updated : x)
            .toList();
      });
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error: $e')),
        );
      }
    }
  }

  Future<void> _setMode(Strategy s, String mode) async {
    try {
      final updated = await widget.apiClient.setStrategyMode(s.name, mode);
      if (!mounted) return;
      setState(() {
        _strategies = _strategies!
            .map((x) => x.name == updated.name ? updated : x)
            .toList();
      });
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error: $e')),
        );
      }
    }
  }

  Future<void> _toggleCopyable(Strategy s) async {
    try {
      final updated = await widget.apiClient
          .setStrategyUserCopyable(s.name, !s.userCopyable);
      if (!mounted) return;
      setState(() {
        _strategies = _strategies!
            .map((x) => x.name == updated.name ? updated : x)
            .toList();
      });
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
    final all = _strategies ?? const <Strategy>[];
    final isAdmin = widget.apiClient.isAdmin;
    final exec = all.where((s) => !s.isSignalOnly).toList();
    final signal = all.where((s) => s.isSignalOnly).toList();
    final copyable = all.where((s) => s.userCopyable).toList();

    return Scaffold(
      appBar: AppBar(title: const Text('Strategies')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: LogoSpinner(size: 80, label: 'LOADING'))
            : _error != null
                ? ListView(children: [Padding(padding: const EdgeInsets.all(24), child: Text('Error: $_error'))])
                : isAdmin
                    ? _adminView(exec: exec, signal: signal)
                    : _userView(copyable),
      ),
    );
  }

  Widget _adminView({required List<Strategy> exec, required List<Strategy> signal}) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
      children: [
        _SectionHeader(
          icon: Icons.precision_manufacturing_outlined,
          title: 'Auto-execute',
          subtitle: 'Bot places orders',
        ),
        if (exec.isEmpty)
          const _EmptyHint(text: 'No strategies in execute mode.'),
        for (final s in exec)
          _StrategyTile(
            strategy: s,
            onToggle: () => _toggle(s),
            onMoveToSignal: () => _setMode(s, 'signal'),
            onMoveToExecute: () => _setMode(s, 'execute'),
            onToggleCopyable: () => _toggleCopyable(s),
          ),
        const SizedBox(height: 14),
        _SectionHeader(
          icon: Icons.podcasts,
          title: 'Signals only',
          subtitle: 'Telegram alerts, no orders',
        ),
        if (signal.isEmpty)
          const _EmptyHint(
            text: 'Tap the gear icon on a strategy above to move it here.',
          ),
        for (final s in signal)
          _StrategyTile(
            strategy: s,
            onToggle: () => _toggle(s),
            onMoveToSignal: () => _setMode(s, 'signal'),
            onMoveToExecute: () => _setMode(s, 'execute'),
            onToggleCopyable: () => _toggleCopyable(s),
          ),
      ],
    );
  }

  Widget _userView(List<Strategy> copyable) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
      children: [
        _SectionHeader(
          icon: Icons.copy_outlined,
          title: 'Your EA copies these',
          subtitle: 'Strategies admin enabled for operators',
        ),
        const SizedBox(height: 6),
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
          child: Text(
            'These are the strategies your EA-copier will use and signal '
            'on. The admin controls which are shared — you can\'t toggle '
            'them from here.',
            style: TextStyle(
              fontSize: 12, color: mutedColor(context), height: 1.45,
            ),
          ),
        ),
        if (copyable.isEmpty)
          const _EmptyHint(
            text: 'No strategies enabled for operators yet — ask admin.',
          ),
        for (final s in copyable)
          _ReadOnlyStrategyTile(strategy: s),
      ],
    );
  }
}

String _prettyName(String raw) => raw
    .split('_')
    .map((w) => w.isEmpty ? w : w[0].toUpperCase() + w.substring(1))
    .join(' ');

class _SectionHeader extends StatelessWidget {
  const _SectionHeader({
    required this.icon,
    required this.title,
    required this.subtitle,
  });
  final IconData icon;
  final String title;
  final String subtitle;

  @override
  Widget build(BuildContext context) {
    final muted = mutedColor(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 8, 4, 8),
      child: Row(
        children: [
          Icon(icon, size: 16, color: muted),
          const SizedBox(width: 8),
          Text(
            title.toUpperCase(),
            style: TextStyle(
              fontSize: 11,
              letterSpacing: 2.5,
              fontWeight: FontWeight.w700,
              color: muted,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              subtitle,
              style: TextStyle(fontSize: 10, color: muted, fontStyle: FontStyle.italic),
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyHint extends StatelessWidget {
  const _EmptyHint({required this.text});
  final String text;
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
      child: Text(
        text,
        style: TextStyle(
          fontSize: 11,
          color: mutedColor(context),
          fontStyle: FontStyle.italic,
        ),
      ),
    );
  }
}

class _StrategyTile extends StatelessWidget {
  const _StrategyTile({
    required this.strategy,
    required this.onToggle,
    required this.onMoveToSignal,
    required this.onMoveToExecute,
    required this.onToggleCopyable,
  });
  final Strategy strategy;
  final VoidCallback onToggle;
  final VoidCallback onMoveToSignal;
  final VoidCallback onMoveToExecute;
  final VoidCallback onToggleCopyable;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final isSignal = strategy.isSignalOnly;
    final muted = mutedColor(context);
    final accent = isSignal
        ? (isDark ? kAmber : kAmber)
        : (isDark ? kNeonGreen : kLightWin);
    final accentBg = accent.withValues(alpha: isDark ? 0.10 : 0.08);
    final borderColor = strategy.enabled
        ? accent.withValues(alpha: isDark ? 0.28 : 0.30)
        : (isDark ? kEdge : kLightEdge);

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
      decoration: BoxDecoration(
        color: strategy.enabled ? accentBg : (isDark ? kSurface : kLightSurface),
        border: Border.all(color: borderColor),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _prettyName(strategy.name),
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                    color: isDark ? kText : kLightText,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  strategy.name,
                  style: TextStyle(
                    fontSize: 10,
                    fontFamily: 'monospace',
                    color: muted,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  strategy.enabled
                      ? (isSignal ? '● armed · alerts only' : '● armed · executing')
                      : '○ disarmed',
                  style: TextStyle(
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    color: strategy.enabled ? accent : muted,
                    letterSpacing: 0.6,
                  ),
                ),
              ],
            ),
          ),
          IconButton(
            tooltip: strategy.userCopyable
                ? 'Copyable by operators — tap to keep admin-only'
                : 'Admin-only — tap to share with operators',
            icon: Icon(
              strategy.userCopyable ? Icons.people_outline : Icons.lock_outline,
              size: 18,
              color: strategy.userCopyable ? accent : muted,
            ),
            onPressed: onToggleCopyable,
            visualDensity: VisualDensity.compact,
          ),
          IconButton(
            tooltip: isSignal ? 'Move to auto-execute' : 'Move to signal-only',
            icon: Icon(
              isSignal ? Icons.precision_manufacturing_outlined : Icons.podcasts,
              size: 18,
              color: muted,
            ),
            onPressed: isSignal ? onMoveToExecute : onMoveToSignal,
            visualDensity: VisualDensity.compact,
          ),
          Switch(
            value: strategy.enabled,
            activeThumbColor: accent,
            onChanged: (_) => onToggle(),
          ),
        ],
      ),
    );
  }
}

class _ReadOnlyStrategyTile extends StatelessWidget {
  const _ReadOnlyStrategyTile({required this.strategy});
  final Strategy strategy;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final muted = mutedColor(context);
    final accent = isDark ? kNeonGreen : kLightWin;
    final borderColor = strategy.enabled
        ? accent.withValues(alpha: isDark ? 0.28 : 0.30)
        : (isDark ? kEdge : kLightEdge);
    final modeText = strategy.isSignalOnly
        ? '● live · signal alerts only'
        : '● live · auto-copy on';

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      decoration: BoxDecoration(
        color: strategy.enabled
            ? accent.withValues(alpha: isDark ? 0.10 : 0.08)
            : (isDark ? kSurface : kLightSurface),
        border: Border.all(color: borderColor),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Icon(
            strategy.isSignalOnly ? Icons.podcasts : Icons.precision_manufacturing_outlined,
            color: strategy.enabled ? accent : muted,
            size: 20,
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _prettyName(strategy.name),
                  style: TextStyle(
                    fontSize: 14, fontWeight: FontWeight.w700,
                    color: isDark ? kText : kLightText,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  strategy.enabled ? modeText : '○ paused by admin',
                  style: TextStyle(
                    fontSize: 11, fontWeight: FontWeight.w600,
                    color: strategy.enabled ? accent : muted,
                    letterSpacing: 0.6,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
