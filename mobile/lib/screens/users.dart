import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../api/client.dart';
import '../models/user.dart';
import '../widgets/logo_spinner.dart';

class UsersScreen extends StatefulWidget {
  const UsersScreen({super.key, required this.apiClient});
  final ApiClient apiClient;

  @override
  State<UsersScreen> createState() => _UsersScreenState();
}

class _UsersScreenState extends State<UsersScreen> {
  List<AppUser>? _users;
  AdIdPool? _pool;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final results = await Future.wait([
        widget.apiClient.listUsers(),
        widget.apiClient.unclaimedPool(),
      ]);
      if (!mounted) return;
      setState(() {
        _users = results[0] as List<AppUser>;
        _pool = results[1] as AdIdPool;
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e is ApiException ? e.detail : e.toString();
      });
    }
  }

  void _snack(String msg, {bool ok = true}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg),
        backgroundColor: ok ? null : Colors.red.shade800,
      ),
    );
  }

  String _humanErr(Object e) => e is ApiException ? e.detail : e.toString();

  Future<void> _assign() async {
    if (_pool == null || _pool!.unclaimed.isEmpty) {
      _snack('Pool is empty — refill first.', ok: false);
      return;
    }
    final result = await showDialog<AssignResult>(
      context: context,
      builder: (_) => _AssignDialog(apiClient: widget.apiClient, pool: _pool!),
    );
    if (result == null) return;
    if (result.setupUrl != null) {
      _showSetupUrl(result);
    } else {
      _snack('Setup link emailed to ${result.email}.');
    }
    await _load();
  }

  Future<void> _resend(AppUser u) async {
    try {
      final result = await widget.apiClient.resendSetupLink(u.username);
      if (result.setupUrl != null) {
        _showSetupUrl(result);
      } else {
        _snack('Fresh setup link emailed to ${result.email}.');
      }
    } catch (e) {
      _snack('Resend failed: ${_humanErr(e)}', ok: false);
    }
  }

  void _showSetupUrl(AssignResult r) {
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Setup link · ${r.adId}'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'SMTP is not configured on the server — copy this link and send '
              'it to the recipient manually.',
              style: TextStyle(fontSize: 12),
            ),
            const SizedBox(height: 12),
            SelectableText(
              r.setupUrl!,
              style: const TextStyle(fontFamily: 'monospace', fontSize: 11),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: r.setupUrl!));
              Navigator.pop(ctx);
              _snack('Link copied to clipboard.');
            },
            child: const Text('Copy'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Done'),
          ),
        ],
      ),
    );
  }

  Future<void> _refillPool() async {
    try {
      final pool = await widget.apiClient.refillPool();
      if (!mounted) return;
      setState(() => _pool = pool);
      _snack('Pool refilled to 100.');
    } catch (e) {
      _snack('Refill failed: ${_humanErr(e)}', ok: false);
    }
  }

  Future<void> _resetPassword(AppUser u) async {
    final updated = await showDialog<bool>(
      context: context,
      builder: (_) => _ResetPasswordDialog(apiClient: widget.apiClient, username: u.username),
    );
    if (updated == true) _snack('Password updated.');
  }

  Future<void> _delete(AppUser u) async {
    if (u.username == widget.apiClient.username || u.isAdmin) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete operator "${u.username}"?'),
        content: const Text('This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await widget.apiClient.deleteUser(u.username);
      _snack('Operator deleted.');
      await _load();
    } catch (e) {
      _snack('Delete failed: ${_humanErr(e)}', ok: false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Operators'),
        actions: [
          IconButton(
            tooltip: 'Refill pool',
            onPressed: _refillPool,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _assign,
        icon: const Icon(Icons.mail_outline),
        label: const Text('Assign AD-ID'),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading
            ? const Center(child: LogoSpinner(size: 80, label: 'LOADING'))
            : _error != null
                ? ListView(children: [
                    Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text('Error: $_error',
                          style: const TextStyle(color: Colors.redAccent)),
                    ),
                  ])
                : ListView(
                    padding: const EdgeInsets.fromLTRB(0, 8, 0, 96),
                    children: [
                      if (_pool != null)
                        Padding(
                          padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
                          child: Text(
                            'Unclaimed pool: ${_pool!.size} of 100',
                            style: TextStyle(color: Colors.grey.shade400, fontSize: 12),
                          ),
                        ),
                      for (final u in _users ?? <AppUser>[])
                        _UserTile(
                          user: u,
                          isSelf: u.username == widget.apiClient.username,
                          onResend: () => _resend(u),
                          onReset: () => _resetPassword(u),
                          onDelete: () => _delete(u),
                        ),
                      if ((_users ?? []).isEmpty)
                        const Padding(
                          padding: EdgeInsets.all(24),
                          child: Center(child: Text('No operators yet.')),
                        ),
                    ],
                  ),
      ),
    );
  }
}

class _UserTile extends StatelessWidget {
  const _UserTile({
    required this.user,
    required this.isSelf,
    required this.onResend,
    required this.onReset,
    required this.onDelete,
  });
  final AppUser user;
  final bool isSelf;
  final VoidCallback onResend;
  final VoidCallback onReset;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final statusLabel = user.isAdmin
        ? 'admin'
        : user.passwordSet
            ? 'active'
            : 'pending';
    final statusColor = user.isAdmin
        ? Colors.cyanAccent
        : user.passwordSet
            ? Colors.greenAccent
            : Colors.amberAccent;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
        child: Row(
          children: [
            CircleAvatar(
              backgroundColor: statusColor.withValues(alpha: 0.15),
              child: Icon(
                user.isAdmin
                    ? Icons.admin_panel_settings
                    : user.passwordSet
                        ? Icons.verified_user_outlined
                        : Icons.mark_email_unread_outlined,
                color: statusColor,
                size: 18,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(children: [
                    Text(
                      user.username,
                      style: const TextStyle(
                        fontWeight: FontWeight.w600,
                        letterSpacing: 0.5,
                      ),
                    ),
                    if (isSelf) ...[
                      const SizedBox(width: 6),
                      Text('(you)', style: TextStyle(color: Colors.grey.shade500, fontSize: 11)),
                    ],
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(
                        color: statusColor.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: statusColor.withValues(alpha: 0.4)),
                      ),
                      child: Text(
                        statusLabel,
                        style: TextStyle(
                          color: statusColor,
                          fontSize: 10,
                          fontWeight: FontWeight.w600,
                          letterSpacing: 1,
                        ),
                      ),
                    ),
                  ]),
                  if (user.email != null && user.email!.isNotEmpty)
                    Text(user.email!, style: TextStyle(color: Colors.grey.shade400, fontSize: 11)),
                  if (user.createdAt.isNotEmpty)
                    Text(user.createdAt, style: TextStyle(color: Colors.grey.shade600, fontSize: 10)),
                ],
              ),
            ),
            PopupMenuButton<String>(
              onSelected: (v) {
                if (v == 'resend') onResend();
                if (v == 'reset') onReset();
                if (v == 'delete') onDelete();
              },
              itemBuilder: (_) => [
                if (user.isPending)
                  const PopupMenuItem(value: 'resend', child: Text('Resend setup link')),
                if (user.passwordSet)
                  const PopupMenuItem(value: 'reset', child: Text('Reset password')),
                PopupMenuItem(
                  value: 'delete',
                  enabled: !isSelf && !user.isAdmin,
                  child: Text(
                    'Delete',
                    style: TextStyle(
                      color: (isSelf || user.isAdmin) ? Colors.grey : Colors.redAccent,
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _AssignDialog extends StatefulWidget {
  const _AssignDialog({required this.apiClient, required this.pool});
  final ApiClient apiClient;
  final AdIdPool pool;

  @override
  State<_AssignDialog> createState() => _AssignDialogState();
}

class _AssignDialogState extends State<_AssignDialog> {
  final _email = TextEditingController();
  late String _adId = widget.pool.unclaimed.first;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _email.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final email = _email.text.trim();
    if (email.isEmpty || !email.contains('@')) {
      setState(() => _error = 'Enter a valid email address.');
      return;
    }
    setState(() { _busy = true; _error = null; });
    try {
      final result = await widget.apiClient.assignUser(adId: _adId, email: email);
      if (mounted) Navigator.pop(context, result);
    } catch (e) {
      setState(() {
        _busy = false;
        _error = e is ApiException ? e.detail : e.toString();
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Assign AD-ID'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            DropdownButtonFormField<String>(
              initialValue: _adId,
              decoration: const InputDecoration(
                labelText: 'AD-ID',
                border: OutlineInputBorder(),
              ),
              items: [
                for (final id in widget.pool.unclaimed)
                  DropdownMenuItem(value: id, child: Text(id)),
              ],
              onChanged: (v) { if (v != null) setState(() => _adId = v); },
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _email,
              keyboardType: TextInputType.emailAddress,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: 'Recipient email',
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.mail_outline),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'They will receive an email link to pick their own password. '
              'The link expires in 24 hours.',
              style: TextStyle(color: Colors.grey.shade500, fontSize: 11),
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(_error!, style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: _busy ? null : () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _busy ? null : _submit,
          child: _busy
              ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Assign & email'),
        ),
      ],
    );
  }
}

class _ResetPasswordDialog extends StatefulWidget {
  const _ResetPasswordDialog({required this.apiClient, required this.username});
  final ApiClient apiClient;
  final String username;

  @override
  State<_ResetPasswordDialog> createState() => _ResetPasswordDialogState();
}

class _ResetPasswordDialogState extends State<_ResetPasswordDialog> {
  final _password = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final p = _password.text;
    if (p.length < 12) {
      setState(() => _error = 'Password must be 12+ chars.');
      return;
    }
    setState(() { _busy = true; _error = null; });
    try {
      await widget.apiClient.resetUserPassword(widget.username, p);
      if (mounted) Navigator.pop(context, true);
    } catch (e) {
      setState(() {
        _busy = false;
        _error = e is ApiException ? e.detail : e.toString();
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text('Reset password · ${widget.username}'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _password,
            obscureText: true,
            decoration: const InputDecoration(
              labelText: 'New password (12+ chars)',
              border: OutlineInputBorder(),
            ),
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Text(_error!, style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
          ],
        ],
      ),
      actions: [
        TextButton(
          onPressed: _busy ? null : () => Navigator.pop(context, false),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _busy ? null : _submit,
          child: _busy
              ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Save'),
        ),
      ],
    );
  }
}
