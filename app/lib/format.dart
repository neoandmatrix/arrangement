import 'package:intl/intl.dart';

final _rupees = NumberFormat.currency(locale: 'en_IN', symbol: '₹', decimalDigits: 0);

String rupees(num? amount) => amount == null ? '—' : _rupees.format(amount);

/// Old invalid quotes must remain reviewable without coercing a string into an agreed price.
String quotedMoney(Object? amount) => amount == null ? '—' : amount is num ? rupees(amount) : 'Invalid amount';

String prettyDate(String? iso) {
  if (iso == null || iso.isEmpty) return '';
  final d = DateTime.tryParse(iso);
  return d == null ? iso : DateFormat('EEE, d MMM y').format(d);
}

const languageNames = {
  'hi-IN': 'Hindi',
  'kn-IN': 'Kannada',
  'ta-IN': 'Tamil',
  'te-IN': 'Telugu',
  'mr-IN': 'Marathi',
  'bn-IN': 'Bengali',
  'gu-IN': 'Gujarati',
  'ml-IN': 'Malayalam',
  'pa-IN': 'Punjabi',
  'en-IN': 'English',
};

const serviceNames = {
  'priest': 'Priest',
  'caterer': 'Caterer',
  'decorator': 'Decorator',
  'cake': 'Cake',
  'photographer': 'Photographer',
  'tent_sound': 'Tent and sound',
};
