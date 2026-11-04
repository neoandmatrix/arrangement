import 'package:flutter/material.dart';

import '../format.dart';
import 'quote_details.dart';
import 'view_helpers.dart';

/// Compare one service at a time; unsuccessful calls stay available without dominating the page.
class ServiceQuotes extends StatelessWidget {
  const ServiceQuotes({super.key, required this.service, required this.busy, required this.pendingRates,
    required this.onAct, required this.onBookings});

  final Map<String, dynamic> service;
  final Set<String> busy;
  final Set<String> pendingRates;
  final Future<void> Function(String, String, String) onAct;
  final VoidCallback onBookings;

  @override
  Widget build(BuildContext context) {
    final vendors = rows(service['vendors']);
    final quoted = vendors.where((v) => (v['quote'] as Map?)?['available'] == true).toList();
    final others = vendors.where((v) => (v['quote'] as Map?)?['available'] != true).toList();
    final found = rows(service['discovered']);
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [
        Icon(serviceIcon(service['category']), color: Theme.of(context).colorScheme.primary),
        const SizedBox(width: 8),
        Expanded(child: Text('${service['name']}', style: Theme.of(context).textTheme.titleLarge)),
      ]),
      if (service['budget'] != null) Padding(padding: const EdgeInsets.only(top: 4),
          child: Text('Budget ${rupees(service['budget'] as num)}')),
      if (isBooked(service)) TextButton.icon(onPressed: onBookings, icon: const Icon(Icons.verified_outlined), label: const Text('Already booked · view booking')),
      if (vendors.isEmpty) const Card(child: ListTile(leading: Icon(Icons.search), title: Text('Finding vendors near your venue…'))),
      for (final vendor in quoted) _quote(context, vendor),
      if (quoted.isEmpty && vendors.isNotEmpty) const Padding(
        padding: EdgeInsets.symmetric(vertical: 12),
        child: Text('No available quotes yet. You can check the call results below.'),
      ),
      if (others.isNotEmpty) Card(child: ExpansionTile(
        title: Text('Other vendors (${others.length})'),
        subtitle: const Text('Call results and unavailable vendors'),
        children: [for (final vendor in others) ListTile(
          title: Text('${vendor['name']}'),
          subtitle: Text(_result(vendor)),
          leading: Icon((vendorStatuses[vendor['status']] ?? ('', Icons.help_outline)).$2),
        )],
      )),
      if (found.isNotEmpty) Card(child: ExpansionTile(
        title: Text('Found nearby (${found.length})'),
        subtitle: const Text('Display only · not contacted in this demo'),
        children: [for (final place in found) ListTile(
          leading: const Icon(Icons.place_outlined),
          title: Text('${place['name']}'),
          subtitle: Text([if (place['distance_km'] != null) '${place['distance_km']} km', if (place['address'] != null) '${place['address']}'].join(' · ')),
        )],
      )),
    ]);
  }

  Widget _quote(BuildContext context, Map<String, dynamic> vendor) {
    final id = vendor['vendor_id'] as String;
    final canDecide = vendor['status'] == 'quoted' && !isBooked(service);
    final pending = pendingRates.contains(id);
    final top = vendor['top'] == true && canDecide && !pending;
    final status = vendorStatuses[vendor['status']] ?? ('${vendor['status']}', Icons.help_outline);
    return Card(
      key: ValueKey('quote-$id'),
      margin: const EdgeInsets.symmetric(vertical: 8),
      shape: top ? RoundedRectangleBorder(borderRadius: BorderRadius.circular(12), side: BorderSide(color: Theme.of(context).colorScheme.primary)) : null,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (top) Text('BEST PICK', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: Theme.of(context).colorScheme.primary)),
          Text('${vendor['name']}', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          Text('★ ${vendor['rating']} · ${vendor['area']} · ${languageNames[vendor['language']] ?? vendor['language']}',
              style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(height: 8),
          QuoteAmount(vendor: vendor),
          if (!canDecide) Padding(padding: const EdgeInsets.only(top: 6), child: Text(status.$1)),
          QuoteWarning(vendor: vendor),
          if (pending) TextButton(onPressed: onBookings, child: const Text('Review the pending price revision first')),
          if (canDecide) Padding(
            padding: const EdgeInsets.only(top: 12),
            child: Wrap(spacing: 8, runSpacing: 4, children: [
              FilledButton.icon(
                onPressed: busy.contains(id) || pending || vendor['bookable'] != true ? null
                    : () => onAct(id, 'approve', 'Booked ${vendor['name']}; the next best is on standby'),
                icon: const Icon(Icons.check), label: const Text('Approve'),
              ),
              if (service['negotiable'] == true) OutlinedButton(
                onPressed: busy.contains(id) ? null : () => onAct(id, 'renegotiate', 'Bandobast will call ${vendor['name']} back for a better price'),
                child: const Text('Ask for better'),
              ),
              TextButton(onPressed: busy.contains(id) ? null : () => onAct(id, 'reject', 'Rejected ${vendor['name']}'), child: const Text('Reject')),
            ]),
          ),
          QuoteDetails(vendor: vendor),
        ]),
      ),
    );
  }

  String _result(Map<String, dynamic> vendor) {
    final label = (vendorStatuses[vendor['status']] ?? ('${vendor['status']}', Icons.help_outline)).$1;
    final note = '${vendor['note'] ?? ''}'.trim();
    return [label, if (note.isNotEmpty && note.toLowerCase() != label.toLowerCase()) note].join(' · ');
  }
}
