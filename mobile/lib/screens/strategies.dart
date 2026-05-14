import 'package:flutter/material.dart';
import '../api/client.dart';
import '../models/status.dart';
import '../theme.dart';
import '../utils/twofa.dart';
import '../widgets/logo_spinner.dart';

class StrategiesScreen extends StatefulWidget {
  const StrategiesScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<StrategiesScreen> createState() => _StrategiesScreenState();
}

class _StrategiesScreenState extends State<StrategiesScreen> {
  List<Strategy>? _strategies;
  Set<String> _signalPicks = const {};
  Set<String> _executePicks = const {};
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final results = await Future.wait([
        widget.apiClient.strategies(),
        // Picks only matter for non-admin; admin gets empty sets.
        widget.apiClient.myPicks().catchError(
          (_) => (signal: <String>{}, execute: <String>{}),
        ),
      ]);
      if (!mounted) return;
      final s = results[0] as List<Strategy>;
      final picks = results[1] as ({Set<String> signal, Set<String> execute});
      setState(() {
        _strategies = s;
        _signalPicks = picks.signal;
        _executePicks = picks.execute;
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

  void _applyUpdated(Strategy updated) {
    if (!mounted) return;
    setState(() {
      _strategies = _strategies!
          .map((x) => x.name == updated.name ? updated : x)
          .toList();
    });
  }

  Future<void> _toggle(Strategy s) async {
    try {
      _applyUpdated(await runWithTwoFa<Strategy>(
        context, (code) => widget.apiClient.toggleStrategy(s.name, totpCode: code),
      ));
    } on TwoFaCancelled {
      // No-op — keep the previous toggle state visible.
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
      _applyUpdated(await runWithTwoFa<Strategy>(
        context, (code) => widget.apiClient.setStrategyMode(s.name, mode, totpCode: code),
      ));
    } on TwoFaCancelled {
      // No-op — keep the previous mode visible.
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
      _applyUpdated(await runWithTwoFa<Strategy>(
        context,
        (code) => widget.apiClient
            .setStrategyUserCopyable(s.name, !s.userCopyable, totpCode: code),
      ));
    } on TwoFaCancelled {
      // No-op — keep the previous copyable flag visible.
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
    // Non-admin sees only the strategies they personally picked at
    // signup. Admin's user_copyable veto still applies (if admin flips
    // a picked strategy admin-only, the user just stops seeing it).
    final picked = _signalPicks.union(_executePicks);
    final mine = all
        .where((s) => picked.contains(s.name) && s.userCopyable)
        .toList();

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
                    : _userView(mine),
      ),
    );
  }

  /// Which kind a user picked this strategy as: 'signal', 'execute', or
  /// 'both'. Drives the live-status sub-label on the read-only view.
  String _pickKindFor(String name) {
    final inSignal = _signalPicks.contains(name);
    final inExecute = _executePicks.contains(name);
    if (inSignal && inExecute) return 'both';
    if (inExecute) return 'execute';
    if (inSignal) return 'signal';
    return 'execute';
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
            text:
                'No strategies for you yet — your picks from signup will appear here once admin approves your subscription.',
          ),
        for (final s in copyable)
          _ReadOnlyStrategyTile(strategy: s, pickKind: _pickKindFor(s.name)),
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
  const _ReadOnlyStrategyTile({
    required this.strategy,
    required this.pickKind,
  });
  final Strategy strategy;
  /// 'signal', 'execute', or 'both' — chosen by the user at signup.
  final String pickKind;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final muted = mutedColor(context);
    final accent = isDark ? kNeonGreen : kLightWin;
    final borderColor = strategy.enabled
        ? accent.withValues(alpha: isDark ? 0.28 : 0.30)
        : (isDark ? kEdge : kLightEdge);
    final String modeText;
    if (pickKind == 'both') {
      modeText = '● live · auto-copy + signal alerts';
    } else if (pickKind == 'signal') {
      modeText = '● live · signal alerts only';
    } else {
      modeText = '● live · auto-copy on';
    }

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
            pickKind == 'signal'
                ? Icons.podcasts
                : Icons.precision_manufacturing_outlined,
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
