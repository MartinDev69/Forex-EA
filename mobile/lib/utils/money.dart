import 'package:intl/intl.dart';

/// Currency formatter that respects the broker's reported currency.
///
/// Falls back to USD when the API doesn't return a currency, and to a plain
/// "<code> <amount>" rendering when intl doesn't recognise the ISO code.
NumberFormat moneyFmt(String? currency) {
  final code = (currency ?? 'USD').toUpperCase();
  try {
    return NumberFormat.simpleCurrency(name: code, decimalDigits: 2);
  } catch (_) {
    return NumberFormat.currency(symbol: '$code ', decimalDigits: 2);
  }
}
