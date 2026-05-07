import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../api/client.dart';
import '../models/explanation.dart';
import '../models/status.dart';
import '../theme.dart';
import '../widgets/logo_spinner.dart';
import '../widgets/strategy_chart.dart';

/// Format a price using the broker's typical decimal precision per symbol
/// class — keeps the trade row from leaking float artifacts like
/// "1.1723000000000001" instead of "1.17230".
String _fmtPrice(String symbol, double price) {
  final s = symbol.toUpperCase();
  if (s.contains('XAU') || s.contains('GOLD') || s.contains('OIL')) {
    return price.toStringAsFixed(2);
  }
  if (s.endsWith('JPY') || s.endsWith('JPYM')) {
    return price.toStringAsFixed(3);
  }
  return price.toStringAsFixed(5);
}

class TradesScreen extends StatefulWidget {
  const TradesScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<TradesScreen> createState() => _TradesScreenState();
}

enum _TradeTab { open, pending, closed }

class _TradesScreenState extends State<TradesScreen> {
  List<Trade>? _trades;
  List<PendingOrder> _pending = const [];
  String? _error;
  bool _loading = true;
  _TradeTab _tab = _TradeTab.open;
  DateTime? _date; // filter by opened_at / placed_at date (local)

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final results = await Future.wait([
        widget.apiClient.trades(limit: 50),
        widget.apiClient.pendingOrders().catchError((_) => <PendingOrder>[]),
      ]);
      if (!mounted) return;
      setState(() {
        _trades = results[0] as List<Trade>;
        _pending = results[1] as List<PendingOrder>;
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

  bool _matchesDate(DateTime ts) {
    if (_date == null) return true;
    final local = ts.toLocal();
    return local.year == _date!.year &&
        local.month == _date!.month &&
        local.day == _date!.day;
  }

  Future<void> _pickDate() async {
    final now = DateTime.now();
    final picked = await showDatePicker(
      context: context,
      initialDate: _date ?? now,
      firstDate: DateTime(now.year - 1),
      lastDate: now,
    );
    if (picked != null) setState(() => _date = picked);
  }

  void _showExplanation(Trade t) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Theme.of(context).colorScheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (sheetCtx) => _ExplanationSheet(
        apiClient: widget.apiClient,
        trade: t,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final fmt = NumberFormat.currency(symbol: '\$', decimalDigits: 2);
    final dateFmt = DateFormat('MM-dd HH:mm');
    final allTrades = _trades ?? const <Trade>[];
    final openTrades = allTrades
        .where((t) => t.closedAt == null && _matchesDate(t.openedAt))
        .toList()
      ..sort((a, b) => b.openedAt.compareTo(a.openedAt));
    final closedTrades = allTrades
        .where((t) => t.closedAt != null && _matchesDate(t.openedAt))
        .toList()
      ..sort((a, b) => b.closedAt!.compareTo(a.closedAt!));
    final pendingFiltered = _pending
        .where((p) => _matchesDate(p.placedAt))
        .toList()
      ..sort((a, b) => b.placedAt.compareTo(a.placedAt));

    final openCount = allTrades.where((t) => t.closedAt == null).length;
    final closedCount = allTrades.where((t) => t.closedAt != null).length;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Trades'),
        actions: [
          IconButton(
            tooltip: _date == null ? 'Filter by date' : 'Clear date filter',
            icon: Icon(_date == null ? Icons.calendar_today_outlined : Icons.event_busy),
            onPressed: _date == null ? _pickDate : () => setState(() => _date = null),
          ),
        ],
      ),
      body: Column(
        children: [
          _TradeTabBar(
            current: _tab,
            counts: (open: openCount, pending: _pending.length, closed: closedCount),
            onChange: (t) => setState(() => _tab = t),
          ),
          if (_date != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(
                  color: kAmber.withValues(alpha: 0.10),
                  border: Border.all(color: kAmber.withValues(alpha: 0.3)),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Row(
                  children: [
                    const Icon(Icons.event, size: 14, color: kAmber),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        DateFormat('EEE, MMM d, y').format(_date!),
                        style: const TextStyle(
                          color: kAmber, fontSize: 12, fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    InkWell(
                      onTap: () => setState(() => _date = null),
                      child: const Icon(Icons.close, size: 14, color: kAmber),
                    ),
                  ],
                ),
              ),
            ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _load,
              child: _loading
                  ? const Center(child: LogoSpinner(size: 80, label: 'LOADING'))
                  : _error != null
                      ? ListView(children: [Padding(padding: const EdgeInsets.all(24), child: Text('Error: $_error'))])
                      : _buildBody(
                          openTrades: openTrades,
                          closedTrades: closedTrades,
                          pendingFiltered: pendingFiltered,
                          fmt: fmt,
                          dateFmt: dateFmt,
                        ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildBody({
    required List<Trade> openTrades,
    required List<Trade> closedTrades,
    required List<PendingOrder> pendingFiltered,
    required NumberFormat fmt,
    required DateFormat dateFmt,
  }) {
    final List<Widget> children;
    String emptyText;
    final filterSuffix = _date == null ? '' : ' on that day';

    switch (_tab) {
      case _TradeTab.open:
        emptyText = 'No open positions$filterSuffix.';
        children = openTrades
            .map((t) => _TradeTile(
                  trade: t,
                  fmt: fmt,
                  dateFmt: dateFmt,
                  onTap: () => _showExplanation(t),
                ))
            .toList();
        break;
      case _TradeTab.pending:
        emptyText = 'No pending orders$filterSuffix.';
        children = pendingFiltered.map((p) => _PendingTile(order: p, dateFmt: dateFmt)).toList();
        break;
      case _TradeTab.closed:
        emptyText = 'No closed trades$filterSuffix.';
        children = closedTrades
            .map((t) => _TradeTile(
                  trade: t,
                  fmt: fmt,
                  dateFmt: dateFmt,
                  onTap: () => _showExplanation(t),
                ))
            .toList();
        break;
    }

    if (children.isEmpty) {
      return ListView(
        children: [
          Padding(
            padding: const EdgeInsets.all(40),
            child: Center(
              child: Text(emptyText, style: TextStyle(color: mutedColor(context))),
            ),
          ),
        ],
      );
    }

    return ListView.separated(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
      itemCount: children.length,
      separatorBuilder: (_, _) => const SizedBox(height: 7),
      itemBuilder: (_, i) => children[i],
    );
  }
}

typedef _TabCounts = ({int open, int pending, int closed});

class _TradeTabBar extends StatelessWidget {
  const _TradeTabBar({
    required this.current,
    required this.counts,
    required this.onChange,
  });
  final _TradeTab current;
  final _TabCounts counts;
  final ValueChanged<_TradeTab> onChange;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
      child: Container(
        padding: const EdgeInsets.all(3),
        decoration: BoxDecoration(
          color: isDark ? kSurface : kLightSurface,
          border: Border.all(color: isDark ? kEdge : kLightEdge),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(
          children: [
            _tab(context, _TradeTab.open, 'Open', counts.open),
            _tab(context, _TradeTab.pending, 'Pending', counts.pending),
            _tab(context, _TradeTab.closed, 'Closed', counts.closed),
          ],
        ),
      ),
    );
  }

  Widget _tab(BuildContext context, _TradeTab tab, String label, int count) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final active = current == tab;
    final activeBg = isDark ? const Color(0xFF12151F) : const Color(0xFFE8EDF4);
    return Expanded(
      child: GestureDetector(
        onTap: () => onChange(tab),
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 8),
          decoration: BoxDecoration(
            color: active ? activeBg : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Text(
                label,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: active ? FontWeight.w700 : FontWeight.w500,
                  color: active
                      ? (isDark ? kText : kLightText)
                      : mutedColor(context),
                  letterSpacing: 0.4,
                ),
              ),
              const SizedBox(width: 6),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                decoration: BoxDecoration(
                  color: active
                      ? (isDark ? kNeonGreen : kLightWin).withValues(alpha: 0.12)
                      : (isDark ? const Color(0xFF181C2A) : const Color(0xFFE8EDF4)),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  '$count',
                  style: TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                    color: active
                        ? (isDark ? kNeonGreen : kLightWin)
                        : mutedColor(context),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _PendingTile extends StatelessWidget {
  const _PendingTile({required this.order, required this.dateFmt});
  final PendingOrder order;
  final DateFormat dateFmt;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final isBuy = order.isBuy;
    final color = isBuy
        ? (isDark ? kNeonGreen : kLightWin)
        : (isDark ? kNeonRed : kLightLoss);
    final muted = mutedColor(context);
    final label = order.orderType.replaceAll('_', ' ').toUpperCase();
    return Container(
      padding: const EdgeInsets.fromLTRB(13, 12, 13, 12),
      decoration: BoxDecoration(
        color: isDark ? kSurface : kLightSurface,
        border: Border.all(color: isDark ? kEdge : kLightEdge),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Container(
            width: 36,
            height: 36,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: color.withValues(alpha: isDark ? 0.10 : 0.08),
              border: Border.all(color: color.withValues(alpha: 0.30)),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Icon(
              isBuy ? Icons.arrow_upward : Icons.arrow_downward,
              size: 16,
              color: color,
            ),
          ),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  order.symbol,
                  style: TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 13,
                    color: isDark ? kText : kLightText,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  '$label · ${dateFmt.format(order.placedAt.toLocal())}',
                  style: TextStyle(color: muted, fontSize: 10),
                ),
              ],
            ),
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(
                _fmtPrice(order.symbol, order.price),
                style: TextStyle(
                  fontWeight: FontWeight.w700,
                  fontSize: 13,
                  fontFamily: 'monospace',
                  color: isDark ? kText : kLightText,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                'lot ${order.volume.toStringAsFixed(2)}',
                style: TextStyle(color: muted, fontSize: 10),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _TradeTile extends StatelessWidget {
  const _TradeTile({
    required this.trade,
    required this.fmt,
    required this.dateFmt,
    required this.onTap,
  });
  final Trade trade;
  final NumberFormat fmt;
  final DateFormat dateFmt;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final isBuy = trade.side == 'BUY';
    final sideColor = isBuy
        ? (isDark ? kNeonGreen : kLightWin)
        : (isDark ? kNeonRed : kLightLoss);
    final muted = mutedColor(context);
    final pnlPositive = trade.pnl >= 0;
    final pnlColor = pnlPositive
        ? (isDark ? kNeonGreen : kLightWin)
        : (isDark ? kNeonRed : kLightLoss);
    final pnlShadow = isDark
        ? <Shadow>[Shadow(color: pnlColor.withValues(alpha: 0.5), blurRadius: 8)]
        : const <Shadow>[];

    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(14),
      child: Container(
        padding: const EdgeInsets.fromLTRB(13, 12, 13, 12),
        decoration: BoxDecoration(
          color: isDark ? kSurface : kLightSurface,
          border: Border.all(color: isDark ? kEdge : kLightEdge),
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          children: [
            // Side badge — rounded square, neon-tinted.
            Container(
              width: 36,
              height: 36,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: sideColor.withValues(alpha: isDark ? 0.10 : 0.08),
                border: Border.all(color: sideColor.withValues(alpha: 0.30)),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                isBuy ? 'B' : 'S',
                style: TextStyle(
                  color: sideColor,
                  fontWeight: FontWeight.w700,
                  fontSize: 13,
                  letterSpacing: 0.5,
                ),
              ),
            ),
            const SizedBox(width: 11),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    trade.symbol,
                    style: TextStyle(
                      fontWeight: FontWeight.w700,
                      fontSize: 13,
                      color: isDark ? kText : kLightText,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${dateFmt.format(trade.openedAt.toLocal())} · ${_fmtPrice(trade.symbol, trade.entryPrice)}',
                    style: TextStyle(color: muted, fontSize: 10),
                  ),
                ],
              ),
            ),
            // Right cluster — pnl OR open pill, plus status pill.
            Column(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                if (trade.closedAt == null)
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
                    decoration: BoxDecoration(
                      color: kAmber.withValues(alpha: isDark ? 0.10 : 0.10),
                      border: Border.all(color: kAmber.withValues(alpha: 0.4)),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: const Text(
                      'OPEN',
                      style: TextStyle(
                        color: kAmber,
                        fontWeight: FontWeight.w700,
                        fontSize: 9,
                        letterSpacing: 1.2,
                      ),
                    ),
                  )
                else ...[
                  Text(
                    '${pnlPositive ? '+' : ''}${trade.pnl.toStringAsFixed(2)}',
                    style: TextStyle(
                      color: pnlColor,
                      fontWeight: FontWeight.w700,
                      fontSize: 14,
                      fontFeatures: const [FontFeature.tabularFigures()],
                      shadows: pnlShadow,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                    decoration: BoxDecoration(
                      color: (isDark ? kSurface2 : kLightSurface2),
                      border: Border.all(color: isDark ? kEdge : kLightEdge),
                      borderRadius: BorderRadius.circular(20),
                    ),
                    child: Text(
                      'CLOSED',
                      style: TextStyle(
                        color: muted,
                        fontWeight: FontWeight.w600,
                        fontSize: 8,
                        letterSpacing: 1.2,
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ExplanationSheet extends StatefulWidget {
  const _ExplanationSheet({required this.apiClient, required this.trade});
  final ApiClient apiClient;
  final Trade trade;

  @override
  State<_ExplanationSheet> createState() => _ExplanationSheetState();
}

class _ExplanationSheetState extends State<_ExplanationSheet> {
  TradeExplanation? _exp;
  // Tri-state: null while loading, 'missing' when 404, 'error' otherwise.
  String? _status = 'loading';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final exp = await widget.apiClient.tradeExplanation(widget.trade.id);
      if (!mounted) return;
      setState(() {
        _exp = exp;
        _status = exp == null ? 'missing' : null;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _status = 'error');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
      ),
      child: Container(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.of(context).size.height * 0.8,
        ),
        padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Center(
                child: Container(
                  width: 36,
                  height: 4,
                  margin: const EdgeInsets.only(bottom: 16),
                  decoration: BoxDecoration(
                    color: Colors.grey.shade600,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              Text(
                'Why this trade?',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 4),
              Text(
                '${widget.trade.symbol} · ${widget.trade.side}',
                style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
              ),
              const SizedBox(height: 16),
              if (_status == 'loading')
                const Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(child: LogoSpinner(size: 56)),
                )
              else if (_status == 'missing')
                Text(
                  'No explanation logged for this trade — it pre-dates the explain feature, or was opened with explanations disabled.',
                  style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                )
              else if (_status == 'error')
                Text(
                  "Couldn't load explanation. Try again later.",
                  style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                )
              else if (_exp != null)
                _ExplanationBody(exp: _exp!),
            ],
          ),
        ),
      ),
    );
  }
}

class _ExplanationBody extends StatelessWidget {
  const _ExplanationBody({required this.exp});
  final TradeExplanation exp;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            _Tag(text: exp.strategy),
            if (exp.allocatorRole != null)
              _Tag(
                text: '${exp.allocatorRole} · '
                    '${((exp.allocatorWeight ?? 0) * 100).toStringAsFixed(0)}%',
                tone: _allocatorTone(exp.allocatorRole!),
              ),
            if (exp.regimeLabel != null)
              _Tag(text: 'regime: ${exp.regimeLabel}'),
          ],
        ),
        const SizedBox(height: 12),
        _StatGrid(rows: [
          ('signal price', exp.signalPrice.toStringAsFixed(5)),
          ('stop / target',
              '${exp.signalStop.toStringAsFixed(5)} / ${exp.signalTarget.toStringAsFixed(5)}'),
          ('R:R · stop dist',
              '${exp.riskReward.toStringAsFixed(2)} · ${exp.stopDistancePips.toStringAsFixed(1)}p'),
          ('lot · balance',
              '${exp.lotSize.toStringAsFixed(2)} · ${exp.accountBalance.toStringAsFixed(0)}'),
        ]),
        if (exp.regimeAdx != null || exp.regimeAtrPct != null) ...[
          const SizedBox(height: 12),
          Text(
            [
              if (exp.regimeAdx != null) 'ADX ${exp.regimeAdx!.toStringAsFixed(1)}',
              if (exp.regimeAtrPct != null)
                'ATR pct ${(exp.regimeAtrPct! * 100).toStringAsFixed(0)}%',
            ].join(' · '),
            style: TextStyle(color: Colors.grey.shade400, fontSize: 11),
          ),
        ],
        if (exp.notes.isNotEmpty) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: Colors.black.withValues(alpha: 0.20),
              borderRadius: BorderRadius.circular(6),
              border: Border.all(color: Colors.grey.shade800),
            ),
            child: Text(
              '"${exp.notes}"',
              style: TextStyle(
                color: Colors.grey.shade300,
                fontSize: 12,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        ],
        if (exp.bars.length >= 2) ...[
          const SizedBox(height: 14),
          Row(
            children: [
              Icon(Icons.show_chart,
                  size: 14, color: Theme.of(context).brightness == Brightness.dark
                      ? kNeonGreen : kLightWin),
              const SizedBox(width: 6),
              Text(
                'CHART AT SIGNAL TIME',
                style: TextStyle(
                  fontSize: 10,
                  letterSpacing: 2.0,
                  fontWeight: FontWeight.w700,
                  color: Theme.of(context).brightness == Brightness.dark
                      ? kNeonGreen : kLightWin,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          StrategyChart(
            bars: exp.bars,
            overlays: exp.overlays,
            subplots: exp.subplots,
            entry: exp.signalPrice,
            stop: exp.signalStop,
            target: exp.signalTarget,
            side: exp.side,
            symbol: exp.symbol,
          ),
        ],
        if (exp.indicators.isNotEmpty) ...[
          const SizedBox(height: 14),
          _IndicatorPanel(indicators: exp.indicators),
        ],
      ],
    );
  }

  static Color _allocatorTone(String role) {
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

class _IndicatorPanel extends StatelessWidget {
  const _IndicatorPanel({required this.indicators});
  final Map<String, dynamic> indicators;

  String _fmt(Object? v) {
    if (v == null) return '—';
    if (v is bool) return v ? 'yes' : 'no';
    if (v is int) return v.toString();
    if (v is num) {
      final d = v.toDouble();
      final a = d.abs();
      if (a < 10) return d.toStringAsFixed(2);
      if (a < 100) return d.toStringAsFixed(1);
      return d.toStringAsFixed(4);
    }
    return v.toString();
  }

  @override
  Widget build(BuildContext context) {
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final accent = isDark ? kNeonGreen : kLightWin;
    final entries = indicators.entries.toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(Icons.insights_outlined, size: 14, color: accent),
            const SizedBox(width: 6),
            Text(
              'WHAT THE STRATEGY SAW',
              style: TextStyle(
                fontSize: 10,
                letterSpacing: 2.0,
                fontWeight: FontWeight.w700,
                color: accent,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final entry in entries)
              Container(
                padding: const EdgeInsets.fromLTRB(10, 6, 10, 7),
                decoration: BoxDecoration(
                  color: accent.withValues(alpha: isDark ? 0.08 : 0.06),
                  border: Border.all(color: accent.withValues(alpha: 0.32)),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      entry.key.replaceAll('_', ' '),
                      style: TextStyle(
                        color: accent.withValues(alpha: 0.85),
                        fontSize: 9,
                        letterSpacing: 1.4,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      _fmt(entry.value),
                      style: TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                        color: isDark ? kText : kLightText,
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class _Tag extends StatelessWidget {
  const _Tag({required this.text, this.tone});
  final String text;
  final Color? tone;

  @override
  Widget build(BuildContext context) {
    final c = tone ?? Colors.grey.shade400;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.10),
        border: Border.all(color: c.withValues(alpha: 0.4)),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        text,
        style: TextStyle(color: c, fontSize: 10, fontWeight: FontWeight.w600),
      ),
    );
  }
}

class _StatGrid extends StatelessWidget {
  const _StatGrid({required this.rows});
  final List<(String, String)> rows;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        for (var i = 0; i < rows.length; i += 2)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Row(
              children: [
                Expanded(child: _Cell(label: rows[i].$1, value: rows[i].$2)),
                const SizedBox(width: 8),
                if (i + 1 < rows.length)
                  Expanded(child: _Cell(label: rows[i + 1].$1, value: rows[i + 1].$2))
                else
                  const Expanded(child: SizedBox.shrink()),
              ],
            ),
          ),
      ],
    );
  }
}

class _Cell extends StatelessWidget {
  const _Cell({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.20),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: Colors.grey.shade800),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: TextStyle(color: Colors.grey.shade500, fontSize: 10)),
          const SizedBox(height: 2),
          Text(
            value,
            style: const TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w600,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}
