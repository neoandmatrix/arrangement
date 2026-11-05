import 'package:flutter/material.dart';

import 'view_helpers.dart';

/// Derived from actual server state, never from a scripted demo stage or a selected candidate.
class ProgressSummary extends StatelessWidget {
  const ProgressSummary({super.key, required this.services, required this.pendingReviews});
  final List<Map<String, dynamic>> services;
  final int pendingReviews;

  @override
  Widget build(BuildContext context) {
    final confirmed = services.where((s) => isBooked(s) && (booking(s, 'primary')?['confirmation'] as Map?)?['status'] == 'confirmed').length;
    final quoted = services.where((s) => rows(s['vendors']).any((v) => v['bookable'] == true && (v['quote'] as Map?)?['available'] == true)).length;
    final secured = services.where((s) => booking(s, 'backup')?['secured'] == true).length;
    final replacing = services.any((s) => {'replacement_pending', 'needs_replacement'}.contains(booking(s, 'primary')?['status']));
    final waiting = services.any((s) => isSelected(s) && !(isBooked(s) && (booking(s, 'primary')?['confirmation'] as Map?)?['status'] == 'confirmed'));
    final availableToApprove = services.any((s) => !isSelected(s) && rows(s['vendors']).any((v) => v['bookable'] == true && (v['quote'] as Map?)?['available'] == true));
    final next = pendingReviews > 0 ? 'Next: review the pending requests'
        : replacing ? 'A replacement is needed; check the backup below'
        : waiting ? 'Waiting for vendor booking confirmation'
        : availableToApprove ? 'Next: compare quotes and approve a vendor'
        : confirmed > 0 && secured < confirmed ? 'Primary confirmed; backup availability is still being checked'
        : confirmed == services.length && confirmed > 0 ? 'Bookings confirmed; follow-ups are scheduled'
        : confirmed > 0 ? 'Still collecting quotes for the remaining services'
        : quoted > 0 ? 'Next: compare quotes and approve a vendor' : 'Finding and calling vendors near your venue';
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(next, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
      const SizedBox(height: 6),
      Wrap(spacing: 12, runSpacing: 4, children: [
        Text('Quotes $quoted/${services.length}', style: const TextStyle(fontSize: 12)),
        Text('Confirmed $confirmed/${services.length}', style: const TextStyle(fontSize: 12)),
        Text('Secured backups $secured', style: const TextStyle(fontSize: 12)),
      ]),
      const SizedBox(height: 6),
      LinearProgressIndicator(value: services.isEmpty ? 0 : confirmed / services.length, minHeight: 4, borderRadius: BorderRadius.circular(4)),
    ]);
  }
}
