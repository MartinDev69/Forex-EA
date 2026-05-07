import 'dart:async';
import 'package:flutter/material.dart';
import '../api/client.dart';
import '../models/broker.dart';
import '../widgets/logo_spinner.dart';

class BrokerScreen extends StatefulWidget {
  const BrokerScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<BrokerScreen> createState() => _BrokerScreenState();
}

class _BrokerScreenState extends State<BrokerScreen> {
  List<BrokerPreset> _presets = [];
  BrokerConfig? _saved;
  BrokerStatus? _status;
  BrokerTestResult? _testResult;

  String _broker = 'exness';
  final _login = TextEditingController();
  final _password = TextEditingController();
  final _server = TextEditingController();
  final _path = TextEditingController();

  bool _loading = true;
  bool _testing = false;
  bool _saving = false;
  String? _loadError;
  Timer? _statusTimer;

  @override
  void initState() {
    super.initState();
    _load();
    _statusTimer = Timer.periodic(const Duration(seconds: 6), (_) => _refreshStatus());
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _login.dispose();
    _password.dispose();
    _server.dispose();
    _path.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() { _loading = true; _loadError = null; });
    try {
      final results = await Future.wait([
        widget.apiClient.brokerPresets(),
        widget.apiClient.brokerConfig(),
        widget.apiClient.brokerStatus(),
      ]);
      if (!mounted) return;
      final presets = results[0] as List<BrokerPreset>;
      final saved = results[1] as BrokerConfig?;
      final status = results[2] as BrokerStatus;
      setState(() {
        _presets = presets;
        _saved = saved;
        _status = status;
        if (saved != null) {
          _broker = saved.broker;
          _login.text = saved.login.toString();
          _server.text = saved.server;
          _path.text = saved.mt5Path;
        } else if (presets.isNotEmpty) {
          _broker = presets.first.id;
        }
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() { _loading = false; _loadError = e.toString(); });
    }
  }

  Future<void> _refreshStatus() async {
    try {
      final s = await widget.apiClient.brokerStatus();
      if (mounted) setState(() => _status = s);
    } catch (_) { /* offline: next tick */ }
  }

  BrokerPreset? get _activePreset {
    for (final p in _presets) {
      if (p.id == _broker) return p;
    }
    return null;
  }

  bool _canSubmit() {
    if (_broker.isEmpty) return false;
    final loginInt = int.tryParse(_login.text.trim());
    if (loginInt == null || loginInt <= 0) return false;
    if (_server.text.trim().isEmpty) return false;
    if ((_saved?.passwordSet ?? false) == false && _password.text.isEmpty) return false;
    return true;
  }

  Map<String, dynamic> _payload() => {
        'broker': _broker,
        'login': int.parse(_login.text.trim()),
        'password': _password.text,
        'server': _server.text.trim(),
        'mt5Path': _path.text.trim(),
      };

  Future<void> _test() async {
    if (!_canSubmit()) return;
    setState(() { _testing = true; _testResult = null; });
    try {
      final p = _payload();
      final r = await widget.apiClient.testBroker(
        broker: p['broker'] as String,
        login: p['login'] as int,
        password: p['password'] as String,
        server: p['server'] as String,
        mt5Path: p['mt5Path'] as String,
      );
      if (mounted) setState(() => _testResult = r);
    } catch (e) {
      if (mounted) {
        setState(() => _testResult = BrokerTestResult(ok: false, error: e.toString()));
      }
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  Future<void> _save() async {
    if (!_canSubmit()) return;
    setState(() => _saving = true);
    try {
      final p = _payload();
      final saved = await widget.apiClient.saveBrokerConfig(
        broker: p['broker'] as String,
        login: p['login'] as int,
        password: p['password'] as String,
        server: p['server'] as String,
        mt5Path: p['mt5Path'] as String,
      );
      if (mounted) {
        setState(() { _saved = saved; _password.clear(); });
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Saved. Restart the bot to pick up new creds.')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save failed: $e')));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _clear() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Remove broker credentials?'),
        content: const Text('The encrypted MT5 password will be deleted from the server.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Remove'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.apiClient.clearBrokerConfig();
      if (mounted) {
        setState(() { _saved = null; _password.clear(); });
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Remove failed: $e')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('MT5 broker')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: LogoSpinner(size: 80, label: 'LOADING'))
            : ListView(
                padding: const EdgeInsets.all(12),
                children: [
                  if (_loadError != null) _ErrorCard(message: _loadError!),
                  // The live status reflects the singleton bot's MT5
                  // connection — admin's broker. Hide it for non-admins
                  // so they don't think it's their own.
                  if (widget.apiClient.isAdmin) _StatusCard(status: _status),
                  _FormCard(
                    presets: _presets,
                    active: _broker,
                    onBrokerChanged: (id) => setState(() {
                      _broker = id;
                      final p = _activePreset;
                      if (p != null) {
                        if (_server.text.isEmpty && p.servers.isNotEmpty) {
                          _server.text = p.servers.first;
                        }
                        if (_path.text.isEmpty && p.mt5PathHint.isNotEmpty) {
                          _path.text = p.mt5PathHint;
                        }
                      }
                    }),
                    loginCtl: _login,
                    passwordCtl: _password,
                    serverCtl: _server,
                    pathCtl: _path,
                    saved: _saved,
                    activePreset: _activePreset,
                    onChanged: () => setState(() {}),
                  ),
                  const SizedBox(height: 8),
                  _ActionRow(
                    testing: _testing,
                    saving: _saving,
                    canSubmit: _canSubmit(),
                    hasSaved: _saved != null,
                    onTest: _test,
                    onSave: _save,
                    onClear: _clear,
                  ),
                  if (_testResult != null) _TestResultCard(result: _testResult!),
                ],
              ),
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status});
  final BrokerStatus? status;

  @override
  Widget build(BuildContext context) {
    final connected = status?.connected ?? false;
    final label = status == null
        ? 'unknown'
        : status!.connected
            ? 'connected'
            : (status!.lastError != null ? 'disconnected' : 'idle');
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  width: 10, height: 10,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: connected ? Colors.greenAccent : Colors.redAccent,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  label.toUpperCase(),
                  style: TextStyle(
                    letterSpacing: 2,
                    fontWeight: FontWeight.w600,
                    color: Colors.grey.shade300,
                    fontSize: 12,
                  ),
                ),
                const Spacer(),
                if (status?.staleS != null)
                  Text(
                    _staleLabel(status!.staleS!),
                    style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
                  ),
              ],
            ),
            if (status?.lastError != null) ...[
              const SizedBox(height: 8),
              Text(
                status!.lastError!,
                style: const TextStyle(color: Colors.redAccent, fontSize: 12),
              ),
            ],
            if (status?.connected == true && status?.accountInfo != null) ...[
              const SizedBox(height: 12),
              _AccountGrid(status: status!),
            ],
          ],
        ),
      ),
    );
  }

  String _staleLabel(double s) {
    if (s < 60) return '${s.round()}s ago';
    if (s < 3600) return '${(s / 60).round()}m ago';
    return '${(s / 3600).round()}h ago';
  }
}

class _AccountGrid extends StatelessWidget {
  const _AccountGrid({required this.status});
  final BrokerStatus status;

  @override
  Widget build(BuildContext context) {
    final info = status.accountInfo ?? const {};
    final balance = info['balance'];
    final currency = info['currency'];
    final leverage = info['leverage'];
    return Wrap(
      spacing: 16, runSpacing: 8,
      children: [
        _Pair(label: 'Server', value: status.server ?? '—'),
        _Pair(label: 'Login',  value: status.login?.toString() ?? '—'),
        _Pair(
          label: 'Balance',
          value: balance == null
              ? '—'
              : '${(balance as num).toStringAsFixed(2)} ${currency ?? ''}',
        ),
        _Pair(label: 'Leverage', value: leverage == null ? '—' : '1:$leverage'),
      ],
    );
  }
}

class _Pair extends StatelessWidget {
  const _Pair({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: TextStyle(color: Colors.grey.shade500, fontSize: 10, letterSpacing: 1.5)),
        const SizedBox(height: 2),
        Text(value, style: const TextStyle(fontWeight: FontWeight.w600)),
      ],
    );
  }
}

class _FormCard extends StatelessWidget {
  const _FormCard({
    required this.presets,
    required this.active,
    required this.onBrokerChanged,
    required this.loginCtl,
    required this.passwordCtl,
    required this.serverCtl,
    required this.pathCtl,
    required this.saved,
    required this.activePreset,
    required this.onChanged,
  });
  final List<BrokerPreset> presets;
  final String active;
  final ValueChanged<String> onBrokerChanged;
  final TextEditingController loginCtl;
  final TextEditingController passwordCtl;
  final TextEditingController serverCtl;
  final TextEditingController pathCtl;
  final BrokerConfig? saved;
  final BrokerPreset? activePreset;
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context) {
    final passwordSet = saved?.passwordSet ?? false;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Broker',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 10, letterSpacing: 1.5),
            ),
            const SizedBox(height: 6),
            DropdownButtonFormField<String>(
              initialValue: active,
              isExpanded: true,
              decoration: const InputDecoration(border: OutlineInputBorder()),
              items: [
                for (final p in presets)
                  DropdownMenuItem(value: p.id, child: Text(p.displayName)),
              ],
              onChanged: (v) { if (v != null) onBrokerChanged(v); },
            ),
            if ((activePreset?.notes ?? '').isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(
                activePreset!.notes,
                style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
              ),
            ],
            const SizedBox(height: 14),
            TextField(
              controller: loginCtl,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(
                labelText: 'Account login',
                hintText: '12345678',
                border: OutlineInputBorder(),
              ),
              onChanged: (_) => onChanged(),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: serverCtl,
              decoration: InputDecoration(
                labelText: 'Server',
                hintText: activePreset?.servers.isNotEmpty == true
                    ? activePreset!.servers.first
                    : 'e.g. Exness-MT5Real5',
                border: const OutlineInputBorder(),
                helperText: activePreset == null || activePreset!.servers.isEmpty
                    ? null
                    : 'Suggestions: ${activePreset!.servers.take(3).join(", ")}',
                helperMaxLines: 2,
              ),
              onChanged: (_) => onChanged(),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: passwordCtl,
              obscureText: true,
              decoration: InputDecoration(
                labelText: 'Password',
                hintText: passwordSet ? '•••••••• (leave blank to keep)' : 'MT5 password',
                border: const OutlineInputBorder(),
                helperText: passwordSet
                    ? 'Saved · ${saved!.passwordFingerprint}'
                    : null,
              ),
              onChanged: (_) => onChanged(),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: pathCtl,
              decoration: InputDecoration(
                labelText: 'MT5 terminal path (Windows, optional)',
                hintText: activePreset?.mt5PathHint.isNotEmpty == true
                    ? activePreset!.mt5PathHint
                    : r'C:\Program Files\MetaTrader 5\terminal64.exe',
                border: const OutlineInputBorder(),
              ),
              onChanged: (_) => onChanged(),
            ),
          ],
        ),
      ),
    );
  }
}

class _ActionRow extends StatelessWidget {
  const _ActionRow({
    required this.testing,
    required this.saving,
    required this.canSubmit,
    required this.hasSaved,
    required this.onTest,
    required this.onSave,
    required this.onClear,
  });
  final bool testing;
  final bool saving;
  final bool canSubmit;
  final bool hasSaved;
  final VoidCallback onTest;
  final VoidCallback onSave;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    final busy = testing || saving;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: Wrap(
        spacing: 8, runSpacing: 8,
        children: [
          OutlinedButton.icon(
            onPressed: (busy || !canSubmit) ? null : onTest,
            icon: testing
                ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.science_outlined),
            label: Text(testing ? 'Testing…' : 'Test connection'),
          ),
          FilledButton.icon(
            onPressed: (busy || !canSubmit) ? null : onSave,
            icon: saving
                ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.save_outlined),
            label: Text(saving ? 'Saving…' : 'Connect & save'),
          ),
          if (hasSaved)
            TextButton.icon(
              onPressed: busy ? null : onClear,
              icon: const Icon(Icons.delete_outline, color: Colors.redAccent),
              label: const Text('Remove', style: TextStyle(color: Colors.redAccent)),
            ),
        ],
      ),
    );
  }
}

class _TestResultCard extends StatelessWidget {
  const _TestResultCard({required this.result});
  final BrokerTestResult result;

  @override
  Widget build(BuildContext context) {
    final color = result.ok ? Colors.greenAccent : Colors.redAccent;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(result.ok ? Icons.check_circle : Icons.error_outline, color: color),
                const SizedBox(width: 8),
                Text(
                  result.ok ? 'Connection succeeded' : 'Connection failed',
                  style: TextStyle(color: color, fontWeight: FontWeight.w600),
                ),
              ],
            ),
            if (result.error != null) ...[
              const SizedBox(height: 8),
              Text(result.error!, style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
            ],
            if (result.ok && result.account != null) ...[
              const SizedBox(height: 12),
              Wrap(
                spacing: 16, runSpacing: 8,
                children: [
                  _Pair(label: 'Login',   value: '${result.account!['login']}'),
                  _Pair(label: 'Server',  value: '${result.account!['server']}'),
                  _Pair(
                    label: 'Balance',
                    value: '${result.account!['balance']} ${result.account!['currency'] ?? ''}',
                  ),
                  _Pair(label: 'Leverage', value: '1:${result.account!['leverage']}'),
                ],
              ),
            ],
          ],
        ),
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
              child: Text(message, style: const TextStyle(color: Colors.white)),
            ),
          ],
        ),
      ),
    );
  }
}
