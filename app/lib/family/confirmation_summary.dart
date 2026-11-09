import 'package:flutter/material.dart';

import '../format.dart';
import 'view_helpers.dart';

/// Both parties see the same immutable family-approved terms, not the model's recollection.
class ConfirmationSummary extends StatelessWidget {
  const ConfirmationSummary({super.key, required this.confirmation});
  final Map<String, dynamic> confirmation;

  @override
  Widget build(BuildContext context) {
    final terms = confirmation['terms'] as Map<String, dynamic>;
    final quote = terms['quote'] as Map<String, dynamic>;
    final confirmed = confirmation['status'] == 'confirmed';
    final standby = confirmation['purpose'] == 'standby';
    return ExpansionTile(
      tilePadding: EdgeInsets.zero,
      title: Text(standby ? confirmed ? 'Confirmed standby terms · not booked' : 'Proposed standby terms' : confirmed ? 'Confirmed booking summary' : 'Family-approved terms'),
      childrenPadding: const EdgeInsets.only(bottom: 12),
      children: [
        _line('Occasion', '${terms['function']}'),
        _line('Family', '${terms['family']}'),
        _line('Date', prettyDate(terms['date'] as String?)),
        _line('Muhurat', '${terms['start_time']}'),
        _line('Arrival / setup', shortDateTime(terms['arrive_by'])),
        _line('Venue', '${terms['area']}, ${terms['city']}'),
        if (terms['location'] is Map) _line('Venue pin', '${(terms['location'] as Map)['lat']}, ${(terms['location'] as Map)['lon']}'),
        _line('Guests', '${terms['guests']}'),
        _line('Base price', '${quotedMoney(quote['price_amount'])}${quote['price_unit'] == 'per_plate' ? ' per plate' : ' total'}'),
        _line('Transport', quotedMoney(quote['transport_charge'])),
        _line('Extras', quotedMoney(quote['extra_charges_amount'])),
        if (quote['extras_at_actuals'] == true)
          _line('Billed later', '${quote['extra_charges_notes'] ?? 'Some costs'}, at actual cost'),
        _line('Total', quotedMoney(terms['total'])),
        _line(standby ? 'Advance if activated' : 'Advance', quotedMoney(quote['advance_amount'])),
        if (standby) const Align(alignment: Alignment.centerLeft, child: Text('Standby is not a booking. No advance is due now.')),
        if (quote['gst_included'] != null) _line('GST', quote['gst_included'] == true ? 'Included'
            : quote['gst_percent'] != null ? 'Extra ${quote['gst_percent']}% (in the total)' : 'Not included'),
        for (final key in ['cuisine', 'rituals', 'wishes'])
          if ('${terms[key] ?? ''}'.isNotEmpty) _line(key == 'cuisine' ? 'Food' : key == 'rituals' ? 'Rituals' : 'Wishes', '${terms[key]}'),
        if ('${quote['includes_notes'] ?? ''}'.isNotEmpty) _line('Includes', '${quote['includes_notes']}'),
        if (confirmed) _line('Confirmed', shortDateTime(confirmation['updated_at'])),
      ],
    );
  }

  Widget _line(String label, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 4),
    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      SizedBox(width: 100, child: Text(label, style: const TextStyle(fontSize: 13))),
      Expanded(child: Text(value)),
    ]),
  );
}
