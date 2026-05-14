import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api/client.dart';
import '../api/config.dart';
import '../models/allocator.dart';
import '../models/broker.dart';
import '../models/calendar.dart';
import '../models/correlation.dart';
import '../models/drift.dart';
import '../models/ea_config.dart';
import '../models/fill_stats.dart';
import '../models/propfirm.dart';
import '../models/regime.dart';
import '../models/status.dart';
import '../theme.dart';
import '../utils/money.dart';
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
  PropFirmStatus? _propfirm;
  List<Trade> _trades = const [];
  // Whether the current user has their own broker_config saved. Admins
  // always pass; non-admins see the welcome panel until this is true,
  // then the full dashboard.
  bool _hasBroker = false;
  EaConfig? _eaConfig;
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
        widget.apiClient.trades(limit: 50),
        widget.apiClient.brokerConfig(),
        widget.apiClient.propfirm().catchError(
          (_) => PropFirmStatus(enabled: false, initialized: false),
        ),
      ]);
      if (!mounted) return;
      final cfg = results[9] as BrokerConfig?;
      setState(() {
        _status = results[0] as BotStatus;
        _account = results[1] as Account;
        _blackout = results[2] as BlackoutStatus;
        _regime = results[3] as Regime;
        _correlations = results[4] as CorrelationResponse;
        _drift = results[5] as DriftResponse;
        _fillStats = results[6] as FillStatsResponse;
        _allocator = results[7] as AllocatorResponse;
        _trades = results[8] as List<Trade>;
        _hasBroker = cfg != null && cfg.login > 0;
        _propfirm = results[10] as PropFirmStatus;
        _loading = false;
        _error = null;
      });
      // Pull EA config out-of-band for non-admins so we can render the
      // copy-trading panel. Done after the main payload so a failure
      // here doesn't blow up the dashboard.
      if (!_isAdmin && _eaConfig == null) {
        try {
          final c = await widget.apiClient.eaConfig();
          if (mounted) setState(() => _eaConfig = c);
        } catch (_) {
          // Silent — the panel just stays unfilled and the user sees a
          // refresh button. Common case is a transient network blip.
        }
      }
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

  Future<void> _rotateEaKey() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Rotate EA API key?'),
        content: const Text(
          'Your installed AntiGreedCopier EA will stop working until you '
          'paste the new key into its Inputs tab.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Rotate'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      final c = await widget.apiClient.rotateEaKey();
      if (!mounted) return;
      setState(() => _eaConfig = c);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('New key generated — paste it into your EA.')),
      );
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Rotate failed: $e')),
      );
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
            // Robot hero strip — same trading-floor energy the web dashboard
            // got. Keeps the visual hierarchy: hero → KPI → account → trades.
            const _HeroStrip(),
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 64),
                child: Center(child: LogoSpinner(size: 88, label: 'LOADING')),
              ),
            if (_error != null) _ErrorCard(message: _error!),

            // Non-admin operators without a broker config yet see only
            // the welcome panel. Once they save broker creds (via the
            // Broker tab), the full dashboard unlocks.
            if (!_canSeeDashboard)
              _WelcomeCard(adId: widget.apiClient.username ?? 'operator'),

            // Copy-trading EA setup — collapsible, hidden by default
            // for non-admins so it doesn't dominate the dashboard.
            if (!_isAdmin) _EaSetupCard(config: _eaConfig, onRotate: _rotateEaKey),

            if (_canSeeDashboard && _account != null) _AccountCard(account: _account!),
            if (_canSeeDashboard && _propfirm != null && _propfirm!.enabled)
              _PropFirmCard(data: _propfirm!, currency: _account?.currency),
            if (_canSeeDashboard && _account != null && _status != null)
              _KpiGrid(account: _account!, status: _status!, trades: _trades),
            if (_canSeeDashboard && _status != null)
              _StatusCard(
                status: _status!,
                onToggle: _toggleBot,
              ),
            if (_canSeeDashboard && _blackout != null)
              _BlackoutCard(
                status: _blackout!,
                onChangeSymbol: _changeBlackoutSymbol,
              ),
            if (_canSeeDashboard && _regime != null) _RegimeCard(regime: _regime!),
            if (_canSeeDashboard && _correlations != null && _correlations!.pairs.isNotEmpty)
              _CorrelationCard(data: _correlations!),
            if (_canSeeDashboard && _drift != null && _drift!.reports.isNotEmpty)
              _DriftCard(data: _drift!),
            if (_canSeeDashboard && _fillStats != null && _fillStats!.symbols.isNotEmpty)
              _ExecutionQualityCard(data: _fillStats!),
            if (_canSeeDashboard && _allocator != null && _allocator!.allocations.isNotEmpty)
              _AllocatorCard(data: _allocator!),
            // Version footer — semantic version on the left, internal
            // build sub-tag on the right so we can still verify which
            // APK is actually on the phone.
            Padding(
              padding: const EdgeInsets.fromLTRB(0, 24, 0, 32),
              child: Center(
                child: Text(
                  'AntiGreed v$appVersion · $appBuildTag',
                  style: TextStyle(
                    color: mutedColor(context),
                    fontSize: 10, letterSpacing: 1.4,
                    fontFamily: 'monospace',
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  bool get _isAdmin => (widget.apiClient.role ?? 'admin') == 'admin';
  bool get _canSeeDashboard => _isAdmin || _hasBroker;
}

class _WelcomeCard extends StatelessWidget {
  const _WelcomeCard({required this.adId});
  final String adId;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      padding: const EdgeInsets.fromLTRB(20, 24, 20, 24),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        border: Border.all(color: accent.withValues(alpha: 0.20)),
        borderRadius: BorderRadius.circular(14),
        boxShadow: isDark
            ? [BoxShadow(color: accent.withValues(alpha: 0.10), blurRadius: 18, spreadRadius: -8)]
            : null,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'ANTIGREED · WELCOME',
            style: TextStyle(
              fontSize: 10, letterSpacing: 4,
              color: accent, fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Hello, $adId!',
            style: TextStyle(
              fontSize: 20, fontWeight: FontWeight.w700,
              color: isDark ? kText : kLightText,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Your subscription is active. Per-account broker sessions are coming soon — '
            'for now your access is registered. Contact the admin to discuss your setup.',
            style: TextStyle(color: mutedColor(context), fontSize: 12, height: 1.5),
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              Icon(Icons.badge_outlined, size: 14, color: mutedColor(context)),
              const SizedBox(width: 6),
              Text(
                'AD-ID  ',
                style: TextStyle(color: mutedColor(context), fontSize: 11, letterSpacing: 2),
              ),
              Text(
                adId,
                style: TextStyle(
                  color: accent, fontSize: 12, fontFamily: 'monospace',
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _EaSetupCard extends StatefulWidget {
  const _EaSetupCard({required this.config, required this.onRotate});
  final EaConfig? config;
  final Future<void> Function() onRotate;

  @override
  State<_EaSetupCard> createState() => _EaSetupCardState();
}

class _EaSetupCardState extends State<_EaSetupCard> {
  bool _showKey = false;
  bool _rotating = false;

  Future<void> _copy(String value, String label) async {
    if (value.isEmpty) return;
    await Clipboard.setData(ClipboardData(text: value));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('$label copied to clipboard.')),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;
    final cfg = widget.config;
    final keyDisplay = cfg == null
        ? '—'
        : (_showKey ? cfg.apiKey : _mask(cfg.apiKey));

    return Container(
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 12),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        border: Border.all(color: isDark ? kEdge : kLightEdge),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Theme(
        // Strip ExpansionTile's default top/bottom divider — the card
        // already has its own border.
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          tilePadding: const EdgeInsets.symmetric(horizontal: 16),
          childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          iconColor: accent,
          collapsedIconColor: mutedColor(context),
          title: Row(
            children: [
              Icon(Icons.precision_manufacturing_outlined, size: 18, color: accent),
              const SizedBox(width: 10),
              Text(
                'Your copy-trading EA',
                style: TextStyle(
                  fontSize: 14, fontWeight: FontWeight.w700,
                  color: isDark ? kText : kLightText,
                ),
              ),
            ],
          ),
          subtitle: Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Text(
              cfg == null
                  ? 'Loading…'
                  : 'API key + install steps for AntiGreedCopier',
              style: TextStyle(color: mutedColor(context), fontSize: 11),
            ),
          ),
          children: [
            Text(
              'The AntiGreed bot runs on the admin\'s account. To mirror its '
              'trades onto your MT5, install the AntiGreedCopier EA and paste '
              'these three values into its Inputs tab.',
              style: TextStyle(color: mutedColor(context), fontSize: 12, height: 1.5),
            ),
            const SizedBox(height: 12),
            _EaField(
              label: 'API URL',
              value: cfg?.apiBaseUrl ?? '—',
              onCopy: () => _copy(cfg?.apiBaseUrl ?? '', 'API URL'),
            ),
            _EaField(
              label: 'API key',
              value: keyDisplay,
              monospace: true,
              onCopy: () => _copy(cfg?.apiKey ?? '', 'API key'),
              trailing: IconButton(
                tooltip: _showKey ? 'Hide' : 'Show',
                visualDensity: VisualDensity.compact,
                icon: Icon(
                  _showKey ? Icons.visibility_off_outlined : Icons.visibility_outlined,
                  size: 18,
                ),
                onPressed: () => setState(() => _showKey = !_showKey),
              ),
            ),
            _EaField(
              label: 'AD-ID',
              value: cfg?.adId ?? '—',
              monospace: true,
              onCopy: () => _copy(cfg?.adId ?? '', 'AD-ID'),
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                OutlinedButton.icon(
                  onPressed: _rotating ? null : () async {
                    setState(() => _rotating = true);
                    await widget.onRotate();
                    if (mounted) setState(() => _rotating = false);
                  },
                  icon: const Icon(Icons.refresh, size: 16),
                  label: Text(_rotating ? 'Rotating…' : 'Rotate key'),
                ),
              ],
            ),
            const SizedBox(height: 14),
            const Divider(),
            const SizedBox(height: 6),
            Text(
              'INSTALL · 5 STEPS',
              style: TextStyle(
                color: accent, fontSize: 10, letterSpacing: 2.6,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 10),
            ..._installSteps(context).asMap().entries.map((e) {
              return _EaStep(n: e.key + 1, body: e.value);
            }),
          ],
        ),
      ),
    );
  }

  static String _mask(String key) {
    if (key.length <= 12) return '••••••••';
    return '${key.substring(0, 4)}••••••••${key.substring(key.length - 4)}';
  }

  List<Widget> _installSteps(BuildContext context) {
    final muted = mutedColor(context);
    final code = TextStyle(
      fontFamily: 'monospace', fontSize: 11.5,
      color: Theme.of(context).brightness == Brightness.dark ? kText : kLightText,
    );
    return [
      Text.rich(TextSpan(style: TextStyle(color: muted, fontSize: 12, height: 1.5), children: [
        const TextSpan(text: 'Download '),
        TextSpan(text: 'AntiGreedCopier.mq5', style: code),
        const TextSpan(text: ' from the web dashboard and drop it into MT5\'s data folder under '),
        TextSpan(text: 'MQL5/Experts/', style: code),
        const TextSpan(text: '. (File → Open Data Folder inside MT5 opens the right place.)'),
      ])),
      Text.rich(TextSpan(style: TextStyle(color: muted, fontSize: 12, height: 1.5), children: [
        const TextSpan(text: 'In MT5 press '),
        TextSpan(text: 'F4', style: code),
        const TextSpan(text: ' to open MetaEditor, open the file, press '),
        TextSpan(text: 'F7', style: code),
        const TextSpan(text: ' to compile. Should report 0 errors.'),
      ])),
      Text.rich(TextSpan(style: TextStyle(color: muted, fontSize: 12, height: 1.5), children: [
        const TextSpan(text: 'Tools → Options → Expert Advisors → tick '),
        const TextSpan(text: '"Allow WebRequest for listed URL"', style: TextStyle(fontStyle: FontStyle.italic)),
        const TextSpan(text: ' and add the API URL above.'),
      ])),
      Text.rich(TextSpan(style: TextStyle(color: muted, fontSize: 12, height: 1.5), children: [
        const TextSpan(text: 'Drag AntiGreedCopier from the Navigator onto any chart. In the Inputs tab, paste the three values above.'),
      ])),
      Text.rich(TextSpan(style: TextStyle(color: muted, fontSize: 12, height: 1.5), children: [
        const TextSpan(text: 'Click the green '),
        const TextSpan(text: 'Algo Trading', style: TextStyle(fontWeight: FontWeight.w700)),
        const TextSpan(text: ' button. The 😊 in the chart\'s top-right means the EA is live.'),
      ])),
    ];
  }
}

class _EaField extends StatelessWidget {
  const _EaField({
    required this.label,
    required this.value,
    required this.onCopy,
    this.trailing,
    this.monospace = false,
  });
  final String label;
  final String value;
  final VoidCallback onCopy;
  final Widget? trailing;
  final bool monospace;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Container(
        padding: const EdgeInsets.fromLTRB(10, 8, 6, 8),
        decoration: BoxDecoration(
          color: isDark ? kSurface2 : kLightSurface2,
          border: Border.all(color: isDark ? kEdge : kLightEdge),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    label,
                    style: TextStyle(
                      color: mutedColor(context),
                      fontSize: 9, letterSpacing: 1.6,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    value,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 12.5,
                      fontFamily: monospace ? 'monospace' : null,
                      color: isDark ? kText : kLightText,
                    ),
                  ),
                ],
              ),
            ),
            ?trailing,
            IconButton(
              tooltip: 'Copy',
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.copy_outlined, size: 18),
              onPressed: onCopy,
            ),
          ],
        ),
      ),
    );
  }
}

class _EaStep extends StatelessWidget {
  const _EaStep({required this.n, required this.body});
  final int n;
  final Widget body;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 22, height: 22,
            margin: const EdgeInsets.only(right: 10, top: 1),
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.18),
              border: Border.all(color: accent.withValues(alpha: 0.6)),
              borderRadius: BorderRadius.circular(11),
            ),
            alignment: Alignment.center,
            child: Text(
              '$n',
              style: TextStyle(
                color: accent, fontFamily: 'monospace',
                fontWeight: FontWeight.w700, fontSize: 11,
              ),
            ),
          ),
          Expanded(child: body),
        ],
      ),
    );
  }
}

class _HeroStrip extends StatelessWidget {
  const _HeroStrip();

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final edge = isDark ? kEdge : kLightEdge;
    final overlayDark = isDark ? Colors.black : Colors.white;
    final eyebrowColor = isDark ? kNeonGreen : kLightWin;
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 12),
      height: 132,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: edge),
      ),
      clipBehavior: Clip.antiAlias,
      child: Row(
        children: [
          // Main: trading floor, with copy.
          Expanded(
            flex: 7,
            child: Stack(
              fit: StackFit.expand,
              children: [
                ColorFiltered(
                  colorFilter: ColorFilter.mode(
                    overlayDark.withValues(alpha: isDark ? 0.20 : 0.10),
                    BlendMode.darken,
                  ),
                  child: Image.asset(
                    'assets/img/robot-trading-floor.jpg',
                    fit: BoxFit.cover,
                  ),
                ),
                Container(
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.centerLeft,
                      end: Alignment.centerRight,
                      colors: [
                        overlayDark.withValues(alpha: isDark ? 0.85 : 0.78),
                        overlayDark.withValues(alpha: isDark ? 0.45 : 0.35),
                        overlayDark.withValues(alpha: 0),
                      ],
                    ),
                  ),
                ),
                Container(
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.bottomCenter,
                      end: Alignment.topCenter,
                      colors: [
                        overlayDark.withValues(alpha: isDark ? 0.7 : 0.55),
                        overlayDark.withValues(alpha: 0),
                      ],
                    ),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      Text(
                        'ANTIGREED',
                        style: TextStyle(
                          color: eyebrowColor.withValues(alpha: 0.85),
                          fontSize: 9,
                          letterSpacing: 4,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 3),
                      const TickerText(
                        'Trading on autopilot.',
                        tone: TickerTone.win,
                        size: 16,
                      ),
                      const SizedBox(height: 3),
                      Text(
                        'M15 · regime-gated',
                        style: TextStyle(
                          color: (isDark ? Colors.white : Colors.black87)
                              .withValues(alpha: 0.55),
                          fontSize: 10,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          // Accent: iridescent robot.
          Expanded(
            flex: 3,
            child: Container(
              decoration: BoxDecoration(border: Border(left: BorderSide(color: edge))),
              child: Stack(
                fit: StackFit.expand,
                children: [
                  Image.asset(
                    'assets/img/robot-iridescent.jpg',
                    fit: BoxFit.cover,
                    alignment: Alignment.center,
                  ),
                  Container(
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.topCenter,
                        end: Alignment.bottomCenter,
                        colors: [
                          overlayDark.withValues(alpha: 0),
                          overlayDark.withValues(alpha: isDark ? 0.7 : 0.5),
                        ],
                      ),
                    ),
                  ),
                  Positioned(
                    bottom: 8,
                    left: 0,
                    right: 0,
                    child: Text(
                      'AI BOT',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: eyebrowColor.withValues(alpha: 0.9),
                        fontSize: 8,
                        letterSpacing: 3,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}


class _KpiGrid extends StatelessWidget {
  const _KpiGrid({required this.account, required this.status, required this.trades});
  final Account account;
  final BotStatus status;
  final List<Trade> trades;

  @override
  Widget build(BuildContext context) {
    final closed = trades.where((t) => t.closedAt != null).toList();
    final wins = closed.where((t) => t.pnl > 0).length;
    final wr = closed.isEmpty ? 0 : ((wins / closed.length) * 100).round();
    final sessionPnl = trades.fold<double>(0, (s, t) => s + t.pnl);
    final sessionPnlSign = sessionPnl >= 0 ? '+' : '';
    final pnlTone = sessionPnl >= 0 ? TickerTone.win : TickerTone.loss;
    final hb = status.lastHeartbeat == null
        ? '—'
        : DateFormat('HH:mm:ss').format(status.lastHeartbeat!.toLocal());

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: GridView.count(
        shrinkWrap: true,
        crossAxisCount: 2,
        crossAxisSpacing: 8,
        mainAxisSpacing: 8,
        physics: const NeverScrollableScrollPhysics(),
        childAspectRatio: 1.85,
        children: [
          _KpiTile(
            label: 'WIN RATE',
            value: '$wr%',
            sub: '$wins / ${closed.length} closed',
            tone: wr >= 50 ? TickerTone.win : TickerTone.neutral,
          ),
          _KpiTile(
            label: 'SESSION P&L',
            value: '$sessionPnlSign\$${sessionPnl.abs().toStringAsFixed(0)}',
            sub: '${trades.length} trades',
            tone: pnlTone,
          ),
          _KpiTile(
            label: 'OPEN POS.',
            value: '${status.openPositions}',
            sub: 'live trades',
            tone: TickerTone.neutral,
          ),
          _KpiTile(
            label: 'HEARTBEAT',
            value: hb,
            valueSize: 14,
            sub: 'last tick',
            tone: TickerTone.neutral,
          ),
        ],
      ),
    );
  }
}

class _KpiTile extends StatelessWidget {
  const _KpiTile({
    required this.label,
    required this.value,
    required this.sub,
    this.tone = TickerTone.neutral,
    this.valueSize = 20,
  });
  final String label;
  final String value;
  final String sub;
  final TickerTone tone;
  final double valueSize;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final glow = tone == TickerTone.loss
        ? (isDark ? kNeonRed : kLightLoss)
        : (isDark ? kNeonGreen : kLightWin);
    final hasGlow = tone != TickerTone.neutral;
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: hasGlow
              ? glow.withValues(alpha: isDark ? 0.22 : 0.30)
              : (isDark ? kEdge : kLightEdge),
        ),
        boxShadow: hasGlow && isDark
            ? [
                BoxShadow(
                  color: glow.withValues(alpha: 0.18),
                  blurRadius: 18,
                  spreadRadius: -8,
                ),
              ]
            : null,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: TextStyle(
              color: mutedColor(context),
              fontSize: 9,
              letterSpacing: 2.4,
              fontWeight: FontWeight.w600,
            ),
          ),
          TickerText(value, tone: tone, size: valueSize),
          Text(
            sub,
            style: TextStyle(color: mutedColor(context), fontSize: 10),
          ),
        ],
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
    final fmt = moneyFmt(account.currency);
    final pnlPositive = account.dailyPnl >= 0;
    final pnlTone = pnlPositive ? TickerTone.win : TickerTone.loss;
    final muted = mutedColor(context);
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 6, horizontal: 12),
      padding: const EdgeInsets.all(18),
      decoration: glowPanel(context, tone: pnlTone),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'ACCOUNT',
            style: TextStyle(
              color: muted,
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
            style: TextStyle(color: muted, fontSize: 9, letterSpacing: 3),
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
          style: TextStyle(color: mutedColor(context), fontSize: 9, letterSpacing: 2),
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

enum _CorrTier { strong, moderate, low }

({_CorrTier tier, Color color, String label}) _classifyCorrelation(
  BuildContext context, double value,
) {
  final isDark = Theme.of(context).brightness == Brightness.dark;
  final mag = value.abs();
  final dir = value >= 0 ? 'same direction' : 'inverse';
  if (mag >= 0.60) {
    return (
      tier: _CorrTier.strong,
      color: isDark ? kNeonRed : kLightLoss,
      label: 'Strong · $dir — same trade twice',
    );
  }
  if (mag >= 0.30) {
    return (
      tier: _CorrTier.moderate,
      color: kAmber,
      label: 'Moderate · $dir — throttled near heat cap',
    );
  }
  return (
    tier: _CorrTier.low,
    color: isDark ? kMuted : kLightMuted,
    label: 'Low · $dir — independent enough to stack',
  );
}

/// Wraps a long dashboard card in an ExpansionTile and remembers
/// expanded/collapsed state in SharedPreferences keyed by [storageKey].
class _Collapsible extends StatefulWidget {
  const _Collapsible({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.storageKey,
    required this.child,
    this.trailing,
  });
  final IconData icon;
  final String title;
  final String subtitle;
  final String storageKey;
  final Widget? trailing;
  final Widget child;

  @override
  State<_Collapsible> createState() => _CollapsibleState();
}

class _CollapsibleState extends State<_Collapsible> {
  bool _expanded = true;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    _restore();
  }

  Future<void> _restore() async {
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _expanded = prefs.getBool('antigreed:${widget.storageKey}') ?? true;
      _ready = true;
    });
  }

  Future<void> _save(bool v) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('antigreed:${widget.storageKey}', v);
  }

  void _toggle() {
    setState(() => _expanded = !_expanded);
    _save(_expanded);
  }

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;
    final muted = mutedColor(context);
    final textColor = isDark ? kText : kLightText;

    // Wait until SharedPreferences resolves so the body doesn't flash
    // open then snap shut. Until then the header is rendered collapsed.
    final showBody = _ready && _expanded;

    // Hand-rolled card + collapsible body — no ExpansionTile, no
    // Material widget magic, no Theme override. Avoids Material 3
    // surface-tint bleeding through as a pale-grey block, and the
    // child widgets inherit the parent app theme unchanged.
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        border: Border.all(color: isDark ? kEdge : kLightEdge),
        borderRadius: BorderRadius.circular(12),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Clickable header row. InkWell gives a ripple but doesn't
          // change the surface colour underneath.
          InkWell(
            onTap: _toggle,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 14, 14),
              child: Row(
                children: [
                  Icon(widget.icon, color: accent),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          widget.title,
                          style: TextStyle(
                            color: textColor, fontSize: 15,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          widget.subtitle,
                          style: TextStyle(color: muted, fontSize: 11, height: 1.4),
                        ),
                      ],
                    ),
                  ),
                  if (widget.trailing != null) ...[
                    widget.trailing!,
                    const SizedBox(width: 10),
                  ],
                  // Chevron rotates 90° between collapsed and expanded.
                  AnimatedRotation(
                    duration: const Duration(milliseconds: 180),
                    turns: showBody ? 0.25 : 0,
                    child: Icon(
                      Icons.chevron_right,
                      color: showBody ? accent : muted,
                      size: 22,
                    ),
                  ),
                ],
              ),
            ),
          ),
          // Animated body. AnimatedCrossFade renders SizedBox.shrink()
          // when collapsed, so there's nothing taking vertical space.
          AnimatedSize(
            duration: const Duration(milliseconds: 180),
            alignment: Alignment.topCenter,
            curve: Curves.easeOutCubic,
            child: showBody
                ? Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                    child: widget.child,
                  )
                : const SizedBox(width: double.infinity, height: 0),
          ),
        ],
      ),
    );
  }
}

class _CorrelationCard extends StatefulWidget {
  const _CorrelationCard({required this.data});
  final CorrelationResponse data;

  @override
  State<_CorrelationCard> createState() => _CorrelationCardState();
}

class _CorrelationCardState extends State<_CorrelationCard> {
  // Tap a summary pill to filter the rows. 'strong' is the default
  // because that's what most operators care about — same-side stacks
  // double the same bet.
  _CorrTier _filter = _CorrTier.strong;

  @override
  Widget build(BuildContext context) {
    final data = widget.data;
    final pairs = [...data.pairs]
      ..sort((a, b) => b.value.abs().compareTo(a.value.abs()));
    final isDark = Theme.of(context).brightness == Brightness.dark;

    int strong = 0, moderate = 0, low = 0;
    for (final p in pairs) {
      final m = p.value.abs();
      if (m >= 0.60) {
        strong++;
      } else if (m >= 0.30) {
        moderate++;
      } else {
        low++;
      }
    }

    bool inTier(double v) {
      final m = v.abs();
      switch (_filter) {
        case _CorrTier.strong:
          return m >= 0.60;
        case _CorrTier.moderate:
          return m >= 0.30 && m < 0.60;
        case _CorrTier.low:
          return m < 0.30;
      }
    }

    final filteredRows = pairs.where((p) => inTier(p.value)).take(20).toList();

    return _Collapsible(
      icon: Icons.hub_outlined,
      title: 'Correlations',
      subtitle:
          'Pairs that move together — taking both same-side stacks the same bet.',
      storageKey: 'card.correlations',
      trailing: Text(
        '${data.count} pairs',
        style: TextStyle(color: mutedColor(context), fontSize: 11),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Tap-to-filter pill row. Selected pill is highlighted; the
          // others fade and act as buttons.
          Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              _SummaryPill(
                count: strong, label: 'strong',
                color: isDark ? kNeonRed : kLightLoss,
                selected: _filter == _CorrTier.strong,
                onTap: () => setState(() => _filter = _CorrTier.strong),
              ),
              _SummaryPill(
                count: moderate, label: 'moderate', color: kAmber,
                selected: _filter == _CorrTier.moderate,
                onTap: () => setState(() => _filter = _CorrTier.moderate),
              ),
              _SummaryPill(
                count: low, label: 'low',
                color: isDark ? kNeonGreen : kLightWin,
                selected: _filter == _CorrTier.low,
                onTap: () => setState(() => _filter = _CorrTier.low),
              ),
              const SizedBox(width: 4),
              Text(
                '⇈ same · ⇅ inverse',
                style: TextStyle(
                  fontSize: 10, color: mutedColor(context),
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          if (filteredRows.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 18),
              child: Center(
                child: Text(
                  'No ${_filter.name} correlations.',
                  style: TextStyle(color: mutedColor(context), fontSize: 12),
                ),
              ),
            )
          else
            ...filteredRows.map((p) => _CorrelationRow(pair: p)),
        ],
      ),
    );
  }
}

class _SummaryPill extends StatelessWidget {
  const _SummaryPill({
    required this.count,
    required this.label,
    required this.color,
    this.selected = false,
    this.onTap,
  });
  final int count;
  final String label;
  final Color color;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    // When this pill is the active filter, give it a brighter fill;
    // unselected pills sit at a low alpha so the user can read them
    // as "tap to filter to this tier".
    final fillAlpha = selected ? 0.26 : 0.08;
    final borderAlpha = selected ? 0.85 : 0.30;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(20),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
          decoration: BoxDecoration(
            color: color.withValues(alpha: fillAlpha),
            border: Border.all(
              color: color.withValues(alpha: borderAlpha),
              width: selected ? 1.4 : 1,
            ),
            borderRadius: BorderRadius.circular(20),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '$count',
                style: TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                  color: color,
                ),
              ),
              const SizedBox(width: 5),
              Text(
                label,
                style: TextStyle(
                  color: color,
                  fontSize: 9,
                  letterSpacing: 1.2,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
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
    final c = _classifyCorrelation(context, pair.value);
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final mag = (pair.value.abs() * 100).round();
    final arrow = pair.value >= 0 ? '⇈' : '⇅';
    final isStrong = c.tier != _CorrTier.low;

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 3),
      padding: const EdgeInsets.fromLTRB(11, 9, 11, 9),
      decoration: BoxDecoration(
        color: isStrong
            ? c.color.withValues(alpha: isDark ? 0.10 : 0.07)
            : (isDark ? kSurface2 : kLightSurface2),
        border: Border.all(
          color: isStrong
              ? c.color.withValues(alpha: isDark ? 0.32 : 0.30)
              : (isDark ? kEdge : kLightEdge),
        ),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(
                arrow,
                style: TextStyle(
                  color: c.color,
                  fontSize: 14,
                  fontFamily: 'monospace',
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text.rich(
                  TextSpan(
                    children: [
                      TextSpan(
                        text: pair.symbolA,
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                      TextSpan(
                        text: pair.value >= 0 ? '  ↔  ' : '  ↮  ',
                        style: TextStyle(color: mutedColor(context)),
                      ),
                      TextSpan(
                        text: pair.symbolB,
                        style: const TextStyle(fontWeight: FontWeight.w700),
                      ),
                    ],
                  ),
                  style: TextStyle(
                    fontSize: 12,
                    fontFamily: 'monospace',
                    color: isDark ? kText : kLightText,
                  ),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: c.color.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '$mag%',
                  style: TextStyle(
                    color: c.color,
                    fontWeight: FontWeight.w700,
                    fontFamily: 'monospace',
                    fontSize: 12,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 4),
          Padding(
            padding: const EdgeInsets.only(left: 22),
            child: Text(
              c.label,
              style: TextStyle(
                color: isStrong ? c.color.withValues(alpha: 0.9) : mutedColor(context),
                fontSize: 10,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _DriftCard extends StatefulWidget {
  const _DriftCard({required this.data});
  final DriftResponse data;

  @override
  State<_DriftCard> createState() => _DriftCardState();
}

class _DriftCardState extends State<_DriftCard> {
  String? _selectedSymbol;

  @override
  Widget build(BuildContext context) {
    // Preserve first-seen order so the tab list is stable across polls —
    // alphabetical would shuffle when symbols drop in/out mid-session.
    final symbols = <String>[];
    for (final r in widget.data.reports) {
      if (!symbols.contains(r.symbol)) symbols.add(r.symbol);
    }
    final active = (_selectedSymbol != null && symbols.contains(_selectedSymbol))
        ? _selectedSymbol!
        : (symbols.isNotEmpty ? symbols.first : '');
    final filtered = active.isEmpty
        ? widget.data.reports
        : widget.data.reports.where((r) => r.symbol == active).toList();
    final muted = Colors.grey.shade400;

    return _Collapsible(
      icon: Icons.insights_outlined,
      title: 'Strategy drift',
      subtitle: 'Live performance vs backtest baseline.',
      storageKey: 'card.drift',
      trailing: Text(
        '${widget.data.count} tracked',
        style: TextStyle(color: muted, fontSize: 12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (symbols.length > 1) ...[
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: symbols.map((s) {
                  final selected = s == active;
                  return Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: ChoiceChip(
                      label: Text(s),
                      selected: selected,
                      onSelected: (_) => setState(() => _selectedSymbol = s),
                    ),
                  );
                }).toList(),
              ),
            ),
            const SizedBox(height: 8),
          ],
          ...filtered.map((r) => _DriftRow(report: r)),
        ],
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
    return _Collapsible(
      icon: Icons.speed_outlined,
      title: 'Execution quality',
      subtitle: 'Slippage in pips · positive = adverse to you.',
      storageKey: 'card.fillStats',
      trailing: Text(
        'last ${data.windowHours}h',
        style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: data.symbols.map((s) => _ExecutionRow(stats: s)).toList(),
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
    return _Collapsible(
      icon: Icons.tune,
      title: 'Auto-allocator',
      subtitle:
          'Champion → full risk · challenger → half · probe → sliver · cold → paused.',
      storageKey: 'card.allocator',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: data.allocations.map((a) => _AllocatorRow(alloc: a)).toList(),
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

class _PropFirmCard extends StatelessWidget {
  const _PropFirmCard({required this.data, this.currency});
  final PropFirmStatus data;
  final String? currency;

  @override
  Widget build(BuildContext context) {
    final fmt = moneyFmt(currency);
    final muted = mutedColor(context);
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;

    final dailyPct = (data.dailyLossPct ?? 0) * 100;
    final dailyLimitPct = (data.maxDailyLossPct ?? 0) * 100;
    final ddPct = (data.totalDrawdownPct ?? 0) * 100;
    final ddLimitPct = (data.maxTotalDrawdownPct ?? 0) * 100;
    final profitPct = (data.profitTargetPct != null && data.profitAmount != null
            && data.initialBalance != null && data.initialBalance! > 0)
        ? (data.profitAmount! / data.initialBalance!) * 100
        : 0.0;
    final targetPct = (data.profitTargetPct ?? 0) * 100;

    Color statusColor;
    String statusLabel;
    if (data.killedPermanently == true || data.killedToday == true) {
      statusColor = Colors.redAccent;
      statusLabel = data.killedPermanently == true ? 'KILLED' : 'KILLED TODAY';
    } else if (dailyPct >= dailyLimitPct * 0.8 || ddPct >= ddLimitPct * 0.8) {
      statusColor = Colors.amber;
      statusLabel = 'AT RISK';
    } else {
      statusColor = accent;
      statusLabel = 'HEALTHY';
    }

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        border: Border.all(color: statusColor.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.shield_outlined, color: statusColor, size: 18),
              const SizedBox(width: 8),
              Text(
                'PROP FIRM · ${(data.preset ?? "challenge").toUpperCase()}',
                style: TextStyle(
                  color: muted, fontSize: 10, letterSpacing: 2.4,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const Spacer(),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: statusColor.withValues(alpha: 0.16),
                  border: Border.all(color: statusColor.withValues(alpha: 0.5)),
                  borderRadius: BorderRadius.circular(5),
                ),
                child: Text(
                  statusLabel,
                  style: TextStyle(
                    color: statusColor, fontSize: 10,
                    fontWeight: FontWeight.w800, letterSpacing: 1.4,
                  ),
                ),
              ),
            ],
          ),
          if (data.killedReason != null) ...[
            const SizedBox(height: 8),
            Text(data.killedReason!,
                style: TextStyle(color: Colors.redAccent, fontSize: 12)),
          ],
          const SizedBox(height: 14),
          _PropfirmRow(
            label: 'PROFIT',
            value: '${fmt.format(data.profitAmount ?? 0)} '
                '/ ${fmt.format(data.profitTargetAmount ?? 0)}',
            pct: profitPct,
            limit: targetPct,
            color: accent,
            inverted: false,
          ),
          const SizedBox(height: 10),
          _PropfirmRow(
            label: 'DAILY LOSS',
            value: '${fmt.format(data.dailyLossAmount ?? 0)} '
                '/ ${fmt.format(data.dailyLossLimitAmount ?? 0)}',
            pct: dailyPct,
            limit: dailyLimitPct,
            color: Colors.amber,
            inverted: true,
          ),
          const SizedBox(height: 10),
          _PropfirmRow(
            label: 'TOTAL DD',
            value: '${fmt.format(data.totalDrawdownAmount ?? 0)} '
                '/ ${fmt.format(data.totalDrawdownLimitAmount ?? 0)}',
            pct: ddPct,
            limit: ddLimitPct,
            color: Colors.redAccent,
            inverted: true,
          ),
          if (data.minTradingDays != null) ...[
            const SizedBox(height: 12),
            Row(
              children: [
                Text(
                  'TRADING DAYS',
                  style: TextStyle(
                    color: muted, fontSize: 10, letterSpacing: 1.6,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  '${data.tradingDaysCount ?? 0} / ${data.minTradingDays}',
                  style: const TextStyle(
                    fontSize: 13, fontWeight: FontWeight.w700,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _PropfirmRow extends StatelessWidget {
  const _PropfirmRow({
    required this.label,
    required this.value,
    required this.pct,
    required this.limit,
    required this.color,
    required this.inverted,
  });
  final String label;
  final String value;
  final double pct;
  final double limit;
  final Color color;
  final bool inverted; // true: closer to limit = worse (loss/dd); false: profit

  @override
  Widget build(BuildContext context) {
    final muted = mutedColor(context);
    final fill = (limit > 0 ? (pct / limit).clamp(0.0, 1.0) : 0.0);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text(
              label,
              style: TextStyle(
                color: muted, fontSize: 10, letterSpacing: 1.6,
                fontWeight: FontWeight.w600,
              ),
            ),
            const Spacer(),
            Text(
              value,
              style: const TextStyle(
                fontSize: 12, fontWeight: FontWeight.w700,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ],
        ),
        const SizedBox(height: 4),
        Stack(
          children: [
            Container(
              height: 6,
              decoration: BoxDecoration(
                color: muted.withValues(alpha: 0.15),
                borderRadius: BorderRadius.circular(3),
              ),
            ),
            FractionallySizedBox(
              widthFactor: fill,
              child: Container(
                height: 6,
                decoration: BoxDecoration(
                  color: color,
                  borderRadius: BorderRadius.circular(3),
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}
