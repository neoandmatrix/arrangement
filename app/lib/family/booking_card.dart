import 'package:flutter/material.dart';

import '../call_actions.dart';
import 'quote_details.dart';
import 'view_helpers.dart';
import 'confirmation_summary.dart';
import 'backup_panel.dart';

class BookingCard extends StatelessWidget {
  const BookingCard({super.key, required this.service, required this.checkins, required this.onQuotes, required this.onCheckin, this.onReconfirm, this.onConfirm, this.onCancel, this.onBackupConfirm, this.onBackupChoose, this.onBackupReconfirm, this.rechecking = false, this.reviewPending = false});
  final Map<String, dynamic> service;
  final List<Map<String, dynamic>> checkins;
  final VoidCallback onQuotes;
  final Future<void> Function(int) onCheckin;
  final VoidCallback? onReconfirm;
  final bool rechecking;
  final VoidCallback? onConfirm;
  final VoidCallback? onCancel;
  final bool reviewPending;
  final VoidCallback? onBackupConfirm;
  final Future<void> Function(String)? onBackupChoose;
  final VoidCallback? onBackupReconfirm;

  @override
  Widget build(BuildContext context) {
    final primary = booking(service, 'primary');
    final active = isBooked(service);
    final selected = isSelected(service);
    final confirmation = primary?['confirmation'] as Map<String, dynamic>?;
    final confirmed = active && confirmation?['status'] == 'confirmed';
    final state = primary?['status'];
    final vendor = vendorIn(service, primary?['vendor_id'] as String?);
    final replacing = {'replacement_pending', 'needs_replacement'}.contains(state);
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
            Flexible(child: Text(replacing ? 'Replacement needed' : confirmed ? 'Confirmed' : active ? 'Booked' : selected ? 'Awaiting confirmation' : state == 'cancelled' ? 'Cancelled' : state == 'declined' ? 'Declined' : 'Not booked',
                textAlign: TextAlign.end, style: TextStyle(fontSize: 12, color: confirmed ? Colors.green.shade800 : Theme.of(context).colorScheme.secondary))),
          ]),
          const SizedBox(height: 12),
          if (replacing) ...[
            Text('Original vendor: ${vendor?['name'] ?? primary?['vendor_id']}'),
            const Text('The primary could not fulfil the booking. No replacement is active until its vendor confirms.', style: TextStyle(fontSize: 13)),
            if (state == 'replacement_pending' && onCancel != null) TextButton(onPressed: onCancel, child: const Text('Cancel replacement request')),
            if (state == 'needs_replacement') TextButton(onPressed: onQuotes, child: const Text('View other quotes')),
          ] else if (selected) ...[
            Text('${vendor?['name'] ?? primary?['vendor_id']}', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 8),
            if (vendor != null) QuoteAmount(vendor: vendor),
            if (!confirmed) Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(state == 'confirming' ? 'Bandobast is confirming the approved terms with the vendor.'
                  : state == 'awaiting_changes' ? 'The vendor changed terms. Review the request before booking can be confirmed.'
                  : 'The family approved this vendor, but spoken booking confirmation is still needed.', style: const TextStyle(fontSize: 13)),
            ),
            if (confirmation?['reason'] != null) Text('${confirmation!['reason']}', style: Theme.of(context).textTheme.bodySmall),
            if (!confirmed && state != 'confirming' && !reviewPending && vendor?['needs_price_reconfirmation'] != true && onConfirm != null)
              FilledButton.tonalIcon(onPressed: rechecking ? null : onConfirm, icon: const Icon(Icons.phone_callback), label: Text(confirmation == null || confirmation['status'] == 'pending' ? 'Confirm booking' : 'Retry confirmation')),
            if (!active && onCancel != null) TextButton(onPressed: rechecking ? null : onCancel, child: const Text('Cancel this selection')),
            const SizedBox(height: 8),
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
            if (confirmation != null) ConfirmationSummary(confirmation: confirmation),
            if (confirmed && pending.isNotEmpty) ExpansionTile(
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
          if (primary != null && (selected || replacing)) BackupPanel(service: service, onRetry: onBackupConfirm,
            onChoose: onBackupChoose, onReconfirm: onBackupReconfirm, busy: rechecking),
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
