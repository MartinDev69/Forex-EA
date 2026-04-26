import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../api/client.dart';
import '../models/explanation.dart';
import '../models/status.dart';

class TradesScreen extends StatefulWidget {
  const TradesScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<TradesScreen> createState() => _TradesScreenState();
}

class _TradesScreenState extends State<TradesScreen> {
  List<Trade>? _trades;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final t = await widget.apiClient.trades();
      if (!mounted) return;
      setState(() {
        _trades = t;
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
    return Scaffold(
      appBar: AppBar(title: const Text('Trades')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : _error != null
                ? ListView(children: [Padding(padding: const EdgeInsets.all(24), child: Text('Error: $_error'))])
                : (_trades!.isEmpty
                    ? ListView(children: const [
                        Padding(
                          padding: EdgeInsets.all(40),
                          child: Center(child: Text('No trades yet.')),
                        )
                      ])
                    : ListView(
                        children: [
                          for (final t in _trades!)
                            Card(
                              child: ListTile(
                                onTap: () => _showExplanation(t),
                                leading: CircleAvatar(
                                  backgroundColor:
                                      t.side == 'BUY' ? Colors.green.shade700 : Colors.red.shade700,
                                  child: Text(
                                    t.side == 'BUY' ? 'B' : 'S',
                                    style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
                                  ),
                                ),
                                title: Text(t.symbol,
                                    style: const TextStyle(fontWeight: FontWeight.bold)),
                                subtitle: Text(
                                    '${dateFmt.format(t.openedAt.toLocal())} • entry ${t.entryPrice}'),
                                trailing: Text(
                                  fmt.format(t.pnl),
                                  style: TextStyle(
                                    fontWeight: FontWeight.bold,
                                    color: t.pnl >= 0 ? Colors.greenAccent : Colors.redAccent,
                                  ),
                                ),
                              ),
                            ),
                        ],
                      )),
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
                  child: Center(child: CircularProgressIndicator()),
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
