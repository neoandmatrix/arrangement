import 'package:flutter/material.dart';

import '../call_actions.dart';
import 'quote_details.dart';
import 'view_helpers.dart';

class BookingCard extends StatelessWidget {
  const BookingCard({super.key, required this.service, required this.checkins, required this.onQuotes, required this.onCheckin, this.onReconfirm, this.rechecking = false});
  final Map<String, dynamic> service;
  final List<Map<String, dynamic>> checkins;
  final VoidCallback onQuotes;
  final Future<void> Function(int) onCheckin;
  final VoidCallback? onReconfirm;
  final bool rechecking;

  @override
  Widget build(BuildContext context) {
    final primary = booking(service, 'primary');
    final active = isBooked(service);
    final vendor = vendorIn(service, primary?['vendor_id'] as String?);
    final backup = booking(service, 'backup');
    final standby = backup?['status'] == 'standby';
    final standbyVendor = vendorIn(service, backup?['vendor_id'] as String?);
    final pending = checkins.where((c) => c['vendor_id'] == primary?['vendor_id'] && c['status'] == 'pending').toList()
      ..sort((a, b) => '${a['due_at']}'.compareTo('${b['due_at']}'));
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(serviceIcon(service['category']), color: Theme.of(context).colorScheme.primary),
            const SizedBox(width: 8),
            Expanded(child: Text('${service['name']}', style: Theme.of(context).textTheme.titleMedium)),
            Text(active ? 'Booked' : primary?['status'] == 'cancelled' ? 'Cancelled' : 'Not booked',
                style: TextStyle(fontSize: 12, color: active ? Colors.green.shade800 : Theme.of(context).colorScheme.secondary)),
          ]),
          const SizedBox(height: 12),
          if (active) ...[
            Text('${vendor?['name'] ?? primary?['vendor_id']}', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 8),
            if (vendor != null) QuoteAmount(vendor: vendor),
            const SizedBox(height: 8),
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(standby ? Icons.shield_outlined : Icons.info_outline, size: 18, color: Theme.of(context).colorScheme.secondary),
              const SizedBox(width: 6),
              Expanded(child: Text(standby ? 'Backup: ${standbyVendor?['name'] ?? backup?['vendor_id']}' : 'No backup on standby', style: const TextStyle(fontSize: 13))),
            ]),
            if (vendor != null) QuoteWarning(vendor: vendor),
            if (vendor?['needs_price_reconfirmation'] == true) OutlinedButton.icon(
              onPressed: rechecking ? null : onReconfirm, icon: const Icon(Icons.fact_check_outlined), label: const Text('Reconfirm prices'),
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: () => callPerson(context, primary!['vendor_id'] as String, '${vendor?['name'] ?? primary['vendor_id']}'),
              icon: const Icon(Icons.call), label: const Text('Call vendor'),
            ),
            if (vendor != null) QuoteDetails(vendor: vendor),
            if (pending.isNotEmpty) ExpansionTile(
              tilePadding: EdgeInsets.zero,
              title: Text('Follow-up calls (${pending.length})'),
              subtitle: Text('Next pending: ${shortDateTime(pending.first['due_at'])}'),
              children: [for (final checkin in pending) FollowupTile(key: ValueKey(checkin['id']), checkin: checkin, onCall: onCheckin)],
            ),
          ] else ...[
            Text(primary?['status'] == 'cancelled' ? 'Choose a replacement vendor for this service.'
                : '${rows(service['vendors']).where((v) => v['status'] == 'quoted').length} quotes received · compare vendors before approving.'),
            const SizedBox(height: 8),
            TextButton.icon(onPressed: onQuotes, icon: const Icon(Icons.receipt_long_outlined), label: const Text('View quotes')),
          ],
        ]),
      ),
    );
  }
}

class FollowupTile extends StatefulWidget {
  const FollowupTile({super.key, required this.checkin, required this.onCall, this.vendorName});
  final Map<String, dynamic> checkin;
  final Future<void> Function(int) onCall;
  final String? vendorName;

  @override
  State<FollowupTile> createState() => _FollowupTileState();
}

class _FollowupTileState extends State<FollowupTile> {
  bool _busy = false;

  Future<void> _call() async {
    setState(() => _busy = true);
    try {
      await widget.onCall(widget.checkin['id'] as int);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final due = DateTime.tryParse('${widget.checkin['due_at']}');
    final overdue = due != null && due.isBefore(DateTime.now());
    return ListTile(
      contentPadding: EdgeInsets.zero,
      title: Text('${widget.checkin['label']}'),
      subtitle: Text([if (widget.vendorName != null) widget.vendorName!, shortDateTime(widget.checkin['due_at']), if (overdue) 'Overdue · not called yet'].join('\n')),
      trailing: TextButton(onPressed: _busy ? null : _call, child: Text(_busy ? 'Calling…' : 'Call now')),
    );
  }
}
