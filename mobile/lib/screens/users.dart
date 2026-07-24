import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import '../api/client.dart';
import '../models/subscription_request.dart';
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
  List<SubscriptionRequest> _requests = const [];
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
        widget.apiClient.listSubscriptionRequests().catchError(
          (_) => <SubscriptionRequest>[],
        ),
      ]);
      if (!mounted) return;
      setState(() {
        _users = results[0] as List<AppUser>;
        _pool = results[1] as AdIdPool;
        _requests = results[2] as List<SubscriptionRequest>;
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

  Future<void> _approveRequest(SubscriptionRequest req) async {
    try {
      await widget.apiClient.approveSubscriptionRequest(req.id);
      _snack('${req.displayName} approved — setup link sent via Telegram.');
      await _load();
    } catch (e) {
      _snack('Approval failed: ${_humanErr(e)}', ok: false);
    }
  }

  Future<void> _rejectRequest(SubscriptionRequest req) async {
    final controller = TextEditingController();
    final reason = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Reject ${req.displayName}?'),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLength: 200,
          decoration: const InputDecoration(
            hintText: 'Reason (sent to the user)',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            style: FilledButton.styleFrom(backgroundColor: Colors.red.shade700),
            child: const Text('Reject'),
          ),
        ],
      ),
    );
    if (reason == null || reason.isEmpty) return;
    try {
      await widget.apiClient.rejectSubscriptionRequest(req.id, reason);
      _snack('Request rejected.');
      await _load();
    } catch (e) {
      _snack('Reject failed: ${_humanErr(e)}', ok: false);
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

  Future<void> _extend(AppUser u) async {
    if (u.isAdmin) return;
    final code = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(
        title: Text('Extend ${u.username}'),
        children: [
          for (final entry in const [
            ('5h', '5 hours'),
            ('1w', '1 week'),
            ('2w', '2 weeks'),
            ('1m', '1 month'),
            ('2m', '2 months'),
            ('3m', '3 months'),
          ])
            SimpleDialogOption(
              onPressed: () => Navigator.pop(ctx, entry.$1),
              child: Text('+ ${entry.$2}'),
            ),
        ],
      ),
    );
    if (code == null) return;
    try {
      await widget.apiClient.extendUser(u.username, code);
      _snack('${u.username} extended by $code.');
      await _load();
    } catch (e) {
      _snack('Extend failed: ${_humanErr(e)}', ok: false);
    }
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
                      if (_requests.isNotEmpty) ...[
                        const Padding(
                          padding: EdgeInsets.fromLTRB(16, 6, 16, 8),
                          child: Text(
                            'PENDING SIGNUP REQUESTS',
                            style: TextStyle(
                              fontSize: 10, letterSpacing: 2,
                              color: Colors.amberAccent, fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        for (final r in _requests)
                          _RequestTile(
                            request: r,
                            onApprove: () => _approveRequest(r),
                            onReject: () => _rejectRequest(r),
                          ),
                        const Divider(height: 32),
                      ],
                      for (final u in _users ?? <AppUser>[])
                        _UserTile(
                          user: u,
                          isSelf: u.username == widget.apiClient.username,
                          onResend: () => _resend(u),
                          onReset: () => _resetPassword(u),
                          onDelete: () => _delete(u),
                          onExtend: () => _extend(u),
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
    required this.onExtend,
  });
  final AppUser user;
  final bool isSelf;
  final VoidCallback onResend;
  final VoidCallback onReset;
  final VoidCallback onDelete;
  final VoidCallback onExtend;

  String _formatSubscription() {
    if (user.expiresAt == null) return '';
    final exp = DateTime.tryParse(user.expiresAt!);
    if (exp == null) return user.expiresAt!;
    final now = DateTime.now().toUtc();
    final diff = exp.difference(now);
    if (diff.isNegative) {
      final daysAgo = -diff.inDays;
      return daysAgo > 0
          ? 'expired ${daysAgo}d ago'
          : 'expired ${-diff.inHours}h ago';
    }
    if (diff.inHours < 24) return '${diff.inHours}h left';
    return '${diff.inDays}d left';
  }

  @override
  Widget build(BuildContext context) {
    final statusLabel = user.isAdmin
        ? 'admin'
        : user.expired
            ? 'expired'
            : user.passwordSet
                ? 'active'
                : 'pending';
    final statusColor = user.isAdmin
        ? Colors.cyanAccent
        : user.expired
            ? Colors.redAccent
            : user.passwordSet
                ? Colors.greenAccent
                : Colors.amberAccent;
    final subscription = _formatSubscription();
    final subColor = user.expired
        ? Colors.redAccent
        : (user.expiresAt != null && (DateTime.tryParse(user.expiresAt!)
                ?.difference(DateTime.now().toUtc())
                .inHours ?? 1000) < 48)
            ? Colors.amberAccent
            : Colors.grey.shade400;
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
                  if (subscription.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: Text(
                        subscription,
                        style: TextStyle(
                          color: subColor,
                          fontSize: 11,
                          fontFamily: 'monospace',
                          fontWeight: user.expired ? FontWeight.w700 : FontWeight.w500,
                        ),
                      ),
                    ),
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
                if (v == 'extend') onExtend();
              },
              itemBuilder: (_) => [
                if (!user.isAdmin)
                  const PopupMenuItem(value: 'extend', child: Text('Extend subscription…')),
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
  String _duration = '1m';
  bool _busy = false;
  String? _error;

  static const _durations = [
    ('5h', '5 hours'),
    ('1w', '1 week'),
    ('2w', '2 weeks'),
    ('1m', '1 month'),
    ('2m', '2 months'),
    ('3m', '3 months'),
  ];

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
      final result = await widget.apiClient.assignUser(
        adId: _adId, email: email, duration: _duration,
      );
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
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _duration,
              decoration: const InputDecoration(
                labelText: 'Subscription duration',
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.timer_outlined),
              ),
              items: [
                for (final entry in _durations)
                  DropdownMenuItem(value: entry.$1, child: Text(entry.$2)),
              ],
              onChanged: (v) { if (v != null) setState(() => _duration = v); },
            ),
            const SizedBox(height: 8),
            Text(
              'They will receive an email link to pick their own password. '
              'The setup link expires in 24 hours; the subscription itself '
              'expires after the duration above.',
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

class _RequestTile extends StatelessWidget {
  const _RequestTile({
    required this.request,
    required this.onApprove,
    required this.onReject,
  });
  final SubscriptionRequest request;
  final VoidCallback onApprove;
  final VoidCallback onReject;

  @override
  Widget build(BuildContext context) {
    final created = DateFormat('MMM d · HH:mm').format(request.createdAt.toLocal());
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      padding: const EdgeInsets.fromLTRB(14, 12, 12, 12),
      decoration: BoxDecoration(
        color: Colors.amber.withValues(alpha: 0.08),
        border: Border.all(color: Colors.amber.withValues(alpha: 0.4)),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.person_add_alt, size: 18, color: Colors.amber.shade300),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  request.displayName,
                  style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w700),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: Colors.amber.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(5),
                ),
                child: Text(
                  request.duration.toUpperCase(),
                  style: TextStyle(
                    fontSize: 10, fontWeight: FontWeight.w700,
                    color: Colors.amber.shade300, letterSpacing: 1.2,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (request.phoneNumber != null && request.phoneNumber!.isNotEmpty)
            _kv('Phone', request.phoneNumber!),
          if (request.email.isNotEmpty &&
              !request.email.endsWith('@no-email.local'))
            _kv('Email', request.email),
          _kv('Requested', created),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: onApprove,
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('Approve'),
                  style: FilledButton.styleFrom(
                    backgroundColor: Colors.greenAccent.shade700,
                    foregroundColor: Colors.black,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              OutlinedButton.icon(
                onPressed: onReject,
                icon: const Icon(Icons.close, size: 16, color: Colors.redAccent),
                label: const Text('Reject', style: TextStyle(color: Colors.redAccent)),
                style: OutlinedButton.styleFrom(
                  side: BorderSide(color: Colors.red.shade400),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _kv(String label, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 72,
            child: Text(
              label,
              style: TextStyle(
                color: Colors.grey.shade400, fontSize: 11,
                letterSpacing: 1.2, fontWeight: FontWeight.w600,
              ),
            ),
          ),
          Expanded(
            child: Text(value, style: const TextStyle(fontSize: 13)),
          ),
        ],
      ),
    );
  }
}
