import 'package:flutter/material.dart';

import '../api/client.dart';

/// Thrown by [runWithTwoFa] when the user dismissed the 2FA dialog.
/// Distinct from a void return so callers can tell "completed with no
/// value" from "didn't run at all" — necessary for actions like
/// clearBrokerConfig where the call type is void.
class TwoFaCancelled implements Exception {
  @override
  String toString() => '2FA prompt cancelled';
}

/// Run a 2FA-gated API call with prompt-and-retry UX.
///
/// Tries [call] with no code first — that succeeds outright when the
/// operator hasn't enrolled TOTP. If the server replies with
/// [TwoFaRequiredException], a 6-digit dialog is shown and [call] is
/// retried with the code. Wrong codes ([TwoFaInvalidException]) loop
/// back to the dialog with an inline "Code rejected" hint until the
/// user submits a valid code or taps Cancel.
///
/// Returns the call's result on success. Throws [TwoFaCancelled] if the
/// user dismissed the dialog; other exceptions are rethrown so callers
/// can show SnackBars / dialogs in their own house style. Cancellation
/// is silent by convention — wrap the call in a try/catch on
/// TwoFaCancelled and just return.
Future<T> runWithTwoFa<T>(
  BuildContext context,
  Future<T> Function(String? totpCode) call,
) async {
  try {
    return await call(null);
  } on TwoFaRequiredException {
    return _promptAndRetry<T>(context, call, null);
  }
}

Future<T> _promptAndRetry<T>(
  BuildContext context,
  Future<T> Function(String? totpCode) call,
  String? hint,
) async {
  String? hintText = hint;
  while (true) {
    final code = await _ask2faCode(context, hintText);
    if (code == null) throw TwoFaCancelled();
    try {
      return await call(code);
    } on TwoFaInvalidException {
      hintText = 'Code rejected — try again';
      continue;
    }
  }
}

Future<String?> _ask2faCode(BuildContext context, String? hint) {
  final controller = TextEditingController();
  return showDialog<String>(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => AlertDialog(
      title: const Text('2FA code'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Enter the 6-digit code from your authenticator.'),
          if (hint != null) ...[
            const SizedBox(height: 8),
            Text(hint, style: const TextStyle(color: Colors.redAccent, fontSize: 12)),
          ],
          const SizedBox(height: 12),
          TextField(
            controller: controller,
            autofocus: true,
            keyboardType: TextInputType.number,
            maxLength: 6,
            decoration: const InputDecoration(
              counterText: '',
              hintText: '••••••',
            ),
            onSubmitted: (v) => Navigator.of(ctx).pop(v.trim()),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(null),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
          child: const Text('Submit'),
        ),
      ],
    ),
  );
}
