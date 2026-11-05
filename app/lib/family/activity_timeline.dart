import 'package:flutter/material.dart';

import '../format.dart';
import 'booking_card.dart';
import 'view_helpers.dart';

/// Presentation-only history: do not turn scheduled calls into completed events or stale approvals.
List<Map<String, dynamic>> timelineEntries({required List<Map<String, dynamic>> services,
    required List<Map<String, dynamic>> alerts, required List<Map<String, dynamic>> updates,
    required List<Map<String, dynamic>> checkins}) {
  final entries = <Map<String, dynamic>>[];
  final reportedMessages = <String>{};
  for (final update in updates) {
    final data = update['data'] as Map<String, dynamic>;
    final name = vendorName(services, '${update['vendor_id']}');
    final kind = update['kind'];
    reportedMessages.add(kind == 'quote' ? '$name: corrected charges need your approval; existing terms are unchanged.'
        : '$name: ${data['notes']}${kind == 'price' ? ' — revised price needs your approval.' : ''}');
    final quote = data['quote'] as Map?;
    entries.add({
      'key': 'update-${update['id']}', 'time': update['created_at'], 'reported': true,
      'title': switch (kind) { 'price' => 'Price revision', 'quote' => 'Quote correction', 'backup_activation' => 'Replacement needs family approval', 'cancelled' => 'Vendor cancelled', 'delayed' => 'Arrival delayed', _ => 'Vendor update' },
      'detail': '$name\n${kind == 'backup_activation' ? 'Replacement total: ${quotedMoney(data['landed_cost'])}' : kind == 'quote' ? 'Corrected total: ${rupees(data['landed_cost'] as num?)}' : quote != null ? '${quotedMoney(data['previous_price'])} → ${quotedMoney(quote['price_amount'])}${quote['price_unit'] == 'per_plate' ? ' per plate' : ' base price'}' : data['notes']}'
          '${data['eta'] != null ? '\nArrival: ${data['eta']}' : ''}',
      'status': kind == 'price' || kind == 'quote' || kind == 'backup_activation' ? '${update['status']}' : null,
      'level': update['status'] == 'pending' || kind == 'cancelled' || kind == 'delayed' ? 'warn' : 'info',
      'icon': kind == 'price' ? Icons.receipt_long_outlined : Icons.message_outlined,
    });
  }
  final seen = <String>{};
  for (final alert in alerts) {
    final message = '${alert['message']}';
    // An update and its auto-generated alert describe the same report. Show it only once,
    // with the revision's current status; keep separate approval/booking/switch events.
    if (reportedMessages.contains(message) || !seen.add('${alert['created_at']}|$message')) continue;
    final approved = message.startsWith('Approved revised price from ');
    final rejected = message.startsWith('Rejected revised price from ');
    entries.add({
      'key': 'alert-${alert['id']}', 'time': alert['created_at'],
      'title': approved ? 'Price revision approved' : rejected ? 'Price revision rejected'
          : message.contains('confirmed standby availability') ? 'Backup standby confirmed · not booked'
          : message.contains('confirmed the') && message.contains('replacement booking') ? 'Replacement booking confirmed'
          : message.contains('confirmed the') && message.contains('booking') ? 'Booking confirmed'
          : message.contains('vendor booking confirmation is pending') ? 'Vendor approved · confirmation pending'
          : message.contains('Switched to backup') ? 'Backup activated'
          : alert['level'] == 'critical' ? 'Booking needs attention' : message.startsWith('Booked ') ? 'Vendor booked' : 'Booking update',
      'detail': approved || rejected ? message.replaceFirst(RegExp(r'^(Approved|Rejected) revised price from '), '').replaceFirst(RegExp(r'\.$'), '') : message,
      'level': alert['level'], 'icon': approved ? Icons.check_circle_outline : rejected ? Icons.block
          : alert['level'] == 'critical' ? Icons.warning_amber_outlined : Icons.event_available_outlined,
    });
  }
  for (final checkin in checkins.where((c) => c['status'] != 'pending')) {
    entries.add({
      'key': 'checkin-${checkin['id']}', 'time': checkin['done_at'],
      'title': '${checkin['label']}',
      'detail': '${vendorName(services, '${checkin['vendor_id']}')} · ${checkin['status']}'
          '${checkin['eta'] != null ? '\nArrival: ${checkin['eta']}' : ''}'
          '${checkin['notes'] != null ? '\n${checkin['notes']}' : ''}',
      'level': checkin['status'] == 'confirmed' ? 'info' : 'warn', 'icon': Icons.phone_in_talk_outlined,
    });
  }
  entries.sort((a, b) => '${b['time'] ?? ''}'.compareTo('${a['time'] ?? ''}'));
  return entries;
}

class ActivityTimeline extends StatelessWidget {
  const ActivityTimeline({super.key, required this.services, required this.alerts, required this.updates,
    required this.checkins, required this.onCheckin});
  final List<Map<String, dynamic>> services;
  final List<Map<String, dynamic>> alerts;
  final List<Map<String, dynamic>> updates;
  final List<Map<String, dynamic>> checkins;
  final Future<void> Function(int) onCheckin;

  @override
  Widget build(BuildContext context) {
    final entries = timelineEntries(services: services, alerts: alerts, updates: updates, checkins: checkins);
    final pending = checkins.where((c) => c['status'] == 'pending').toList()
      ..sort((a, b) => '${a['due_at']}'.compareTo('${b['due_at']}'));
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      if (pending.isNotEmpty) Card(child: ExpansionTile(
        leading: const Icon(Icons.schedule),
        title: Text('Scheduled follow-ups (${pending.length})'),
        subtitle: const Text('Automatic follow-ups · venue times are IST'),
        childrenPadding: const EdgeInsets.symmetric(horizontal: 16),
        children: [for (final checkin in pending) FollowupTile(
          key: ValueKey(checkin['id']), checkin: checkin, onCall: onCheckin,
          vendorName: vendorName(services, '${checkin['vendor_id']}'),
        )],
      )),
      const SizedBox(height: 12),
      Text('Activity timeline', style: Theme.of(context).textTheme.titleLarge),
      const SizedBox(height: 4),
      const Text('Newest first · reports, decisions and completed calls'),
      const SizedBox(height: 16),
      if (entries.isEmpty) const Card(child: ListTile(leading: Icon(Icons.timeline), title: Text('Your activity will appear here as Bandobast works.'))),
      for (var i = 0; i < entries.length; i++) _TimelineRow(entry: entries[i], last: i == entries.length - 1),
    ]);
  }
}

class _TimelineRow extends StatelessWidget {
  const _TimelineRow({required this.entry, required this.last});
  final Map<String, dynamic> entry;
  final bool last;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = entry['level'] == 'critical' ? theme.colorScheme.error
        : entry['level'] == 'warn' ? const Color(0xFF9A6200) : theme.colorScheme.primary;
    return IntrinsicHeight(child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SizedBox(width: 32, child: Column(children: [
        Icon(entry['icon'] as IconData, size: 24, color: color),
        const SizedBox(height: 4),
        if (!last) Expanded(child: VerticalDivider(width: 1, thickness: 1, color: theme.colorScheme.outlineVariant)),
      ])),
      const SizedBox(width: 12),
      Expanded(child: Padding(
        padding: const EdgeInsets.only(bottom: 24),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${entry['title']}', style: theme.textTheme.titleSmall),
          if ('${entry['time'] ?? ''}'.isNotEmpty) Text('${entry['reported'] == true ? 'Reported ' : ''}${shortDateTime(entry['time'])}', style: theme.textTheme.bodySmall),
          const SizedBox(height: 4),
          Text('${entry['detail']}'),
          if (entry['status'] != null) Padding(padding: const EdgeInsets.only(top: 4),
              child: Text('Revision ${entry['status']}', style: TextStyle(fontSize: 12, color: color))),
        ]),
      )),
    ]));
  }
}
