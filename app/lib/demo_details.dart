import 'package:intl/intl.dart';

/// Only form defaults. No venue pin, quote, call result or booking is created here.
Map<String, dynamic> demoDetails({DateTime? now}) {
  final venueNow = (now ?? DateTime.now()).toUtc().add(const Duration(hours: 5, minutes: 30));
  final day = DateTime.utc(venueNow.year, venueNow.month, venueNow.day).add(const Duration(days: 4));
  return {
    'function_type': 'griha_pravesh', 'function_variant': 'griha_pravesh',
    'family_name': 'Demo Sharma', 'contact_name': 'Demo family', 'family_language': 'hi-IN',
    'date': DateFormat('yyyy-MM-dd').format(day), 'start_time': '09:00', 'guests': 40,
    'cuisine': 'North Indian vegetarian meal, no onion or garlic', 'rituals': 'Griha pravesh in Hindi',
    'needs': ['caterer'], 'budget_caterer': 25000,
  };
}
