import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../api/client.dart';
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
