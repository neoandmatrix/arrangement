import 'package:flutter/material.dart';

import '../format.dart';
import 'view_helpers.dart';

/// Amounts wrap independently: large Indian-currency values must not squeeze a savings chip.
class QuoteAmount extends StatelessWidget {
  const QuoteAmount({super.key, required this.vendor});
  final Map<String, dynamic> vendor;

  @override
  Widget build(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
    Wrap(spacing: 8, crossAxisAlignment: WrapCrossAlignment.center, children: [
      Text(rupees(vendor['landed_cost'] as num?), style: Theme.of(context).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700)),
      const Text('total cost'),
    ]),
    if (vendor['savings'] != null) Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Text('Saved ${rupees(vendor['savings'] as num)}', style: TextStyle(color: Colors.green.shade800, fontWeight: FontWeight.w600)),
    ),
  ]);
}

class QuoteDetails extends StatelessWidget {
  const QuoteDetails({super.key, required this.vendor});
  final Map<String, dynamic> vendor;

  @override
  Widget build(BuildContext context) {
    final quote = vendor['quote'] as Map<String, dynamic>?;
    final flags = rows(vendor['flags']);
    return ExpansionTile(
      tilePadding: EdgeInsets.zero,
      childrenPadding: const EdgeInsets.only(bottom: 8),
      title: const Text('Price breakdown & details'),
      children: [
        if (quote != null) ...[
          _line('Base price${quote['price_unit'] == 'per_plate' ? ' per plate' : ''}', rupees(quote['price_amount'] as num?)),
          if (quote['transport_charge'] != null) _line('Transport', rupees(quote['transport_charge'] as num)),
          if (quote['extra_charges_amount'] != null) _line('Extras', rupees(quote['extra_charges_amount'] as num)),
          if (quote['advance_amount'] != null) _line('Advance', rupees(quote['advance_amount'] as num)),
          if (quote['gst_included'] != null) _line('GST', quote['gst_included'] == true ? 'Included' : 'Additional'),
          if ('${quote['includes_notes'] ?? ''}'.isNotEmpty) _line('Covers', '${quote['includes_notes']}'),
        ],
        _line('Vendor', '${vendor['area'] ?? ''} · ${languageNames[vendor['language']] ?? vendor['language'] ?? ''}'),
        _line('Demo vendor ID', '${vendor['vendor_id']}'),
        if (vendor['rating'] != null) _line('Rating', '★ ${vendor['rating']} (${vendor['review_count']} reviews)'),
        if (vendor['distance_km'] != null) _line('Distance', '${vendor['distance_km']} km'),
        for (final flag in flags) Align(
          alignment: Alignment.centerLeft,
          child: Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text('• ${flag['text']}', style: TextStyle(fontSize: 13,
                color: flag['level'] == 'block' ? Theme.of(context).colorScheme.error : flag['level'] == 'warn' ? const Color(0xFF9A6200) : null)),
          ),
        ),
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

class QuoteWarning extends StatelessWidget {
  const QuoteWarning({super.key, required this.vendor});
  final Map<String, dynamic> vendor;

  @override
  Widget build(BuildContext context) {
    final flags = rows(vendor['flags']).where((f) => f['level'] == 'warn' || f['level'] == 'block').toList();
    if (flags.isEmpty) return const SizedBox.shrink();
    final blocked = flags.any((f) => f['level'] == 'block');
    final first = flags.firstWhere((f) => f['level'] == 'block', orElse: () => flags.first);
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(Icons.info_outline, size: 18, color: blocked ? Theme.of(context).colorScheme.error : const Color(0xFF9A6200)),
        const SizedBox(width: 6),
        Expanded(child: Text('${first['text']}${flags.length > 1 ? ' · ${flags.length - 1} more in details' : ''}',
            style: TextStyle(fontSize: 13, color: blocked ? Theme.of(context).colorScheme.error : const Color(0xFF9A6200)))),
      ]),
    );
  }
}
