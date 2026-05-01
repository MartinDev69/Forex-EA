import 'package:flutter/material.dart';
import '../api/client.dart';
import '../models/status.dart';
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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Strategies')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: LogoSpinner(size: 80, label: 'LOADING'))
            : _error != null
                ? ListView(children: [Padding(padding: const EdgeInsets.all(24), child: Text('Error: $_error'))])
                : ListView(
                    children: [
                      for (final s in _strategies!)
                        Card(
                          child: SwitchListTile(
                            title: Text(
                              _prettyName(s.name),
                              style: const TextStyle(fontWeight: FontWeight.w600),
                            ),
                            subtitle: Text(s.name, style: TextStyle(color: Colors.grey.shade500)),
                            value: s.enabled,
                            onChanged: (_) => _toggle(s),
                          ),
                        ),
                    ],
                  ),
      ),
    );
  }

  String _prettyName(String raw) => raw
      .split('_')
      .map((w) => w.isEmpty ? w : w[0].toUpperCase() + w.substring(1))
      .join(' ');
}
