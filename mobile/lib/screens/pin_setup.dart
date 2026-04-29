import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Two-stage PIN entry: enter once, then re-enter to confirm. Returns the
/// chosen PIN on success, null if the user aborts.
Future<String?> showPinSetup(BuildContext context, {String title = 'Set quick-unlock PIN'}) async {
  return Navigator.of(context).push<String>(
    MaterialPageRoute(
      fullscreenDialog: true,
      builder: (_) => _PinSetupScreen(title: title),
    ),
  );
}

class _PinSetupScreen extends StatefulWidget {
  const _PinSetupScreen({required this.title});
  final String title;

  @override
  State<_PinSetupScreen> createState() => _PinSetupScreenState();
}

class _PinSetupScreenState extends State<_PinSetupScreen> {
  final _entry = TextEditingController();
  final _focus = FocusNode();
  String? _firstPin;     // null until step 1 confirmed
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _focus.requestFocus());
  }

  @override
  void dispose() {
    _entry.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _handleSubmit(String value) {
    if (value.length < 4 || value.length > 6) {
      setState(() => _error = 'PIN must be 4–6 digits.');
      return;
    }
    if (_firstPin == null) {
      // Step 1 → ask for confirmation.
      setState(() {
        _firstPin = value;
        _error = null;
        _entry.clear();
      });
      _focus.requestFocus();
      return;
    }
    // Step 2 → must match step 1.
    if (value != _firstPin) {
      setState(() {
        _error = 'PINs don\'t match. Start over.';
        _firstPin = null;
        _entry.clear();
      });
      _focus.requestFocus();
      return;
    }
    Navigator.of(context).pop(value);
  }

  @override
  Widget build(BuildContext context) {
    final step = _firstPin == null ? 1 : 2;
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.title),
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => Navigator.of(context).pop(null),
        ),
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                step == 1 ? 'Choose a 4–6 digit PIN' : 'Re-enter PIN to confirm',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              Text(
                step == 1
                    ? 'You\'ll use this to unlock the app on every launch.'
                    : 'Just to make sure you\'ve got it.',
                style: TextStyle(color: Colors.grey.shade500, fontSize: 12),
              ),
              const SizedBox(height: 32),
              TextField(
                controller: _entry,
                focusNode: _focus,
                obscureText: true,
                keyboardType: TextInputType.number,
                maxLength: 6,
                inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                style: const TextStyle(
                  fontSize: 28,
                  letterSpacing: 16,
                  fontFeatures: [FontFeature.tabularFigures()],
                ),
                textAlign: TextAlign.center,
                decoration: const InputDecoration(
                  border: OutlineInputBorder(),
                  counterText: '',
                ),
                onSubmitted: _handleSubmit,
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(
                  _error!,
                  style: const TextStyle(color: Colors.redAccent, fontSize: 12),
                ),
              ],
              const SizedBox(height: 24),
              FilledButton(
                onPressed: () => _handleSubmit(_entry.text),
                child: Text(step == 1 ? 'Continue' : 'Confirm'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
