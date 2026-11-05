import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

List<Map<String, dynamic>> rows(Object? value) => value is List ? value.cast<Map<String, dynamic>>() : [];

Map<String, dynamic>? booking(Map<String, dynamic> service, String role) =>
    (service['booking'] as Map?)?[role] as Map<String, dynamic>?;

bool isBooked(Map<String, dynamic> service) => booking(service, 'primary')?['status'] == 'active';

const selectedBookingStates = {'active', 'awaiting_confirmation', 'confirming', 'confirmation_failed', 'awaiting_changes', 'replacement_pending'};
bool isSelected(Map<String, dynamic> service) => selectedBookingStates.contains(booking(service, 'primary')?['status']);

Map<String, dynamic>? vendorIn(Map<String, dynamic> service, String? id) =>
    rows(service['vendors']).where((v) => v['vendor_id'] == id).firstOrNull;

String vendorName(List<Map<String, dynamic>> services, String id) {
  for (final service in services) {
    final vendor = vendorIn(service, id);
    if (vendor != null) return '${vendor['name']}';
  }
  return id;
}

String shortDateTime(Object? value) {
  final text = '${value ?? ''}';
  final date = DateTime.tryParse(text);
  return date == null ? text : DateFormat('d MMM · h:mm a').format(date);
}

IconData serviceIcon(Object? category) => switch (category) {
  'priest' => Icons.temple_hindu_outlined,
  'caterer' => Icons.restaurant_outlined,
  'decorator' => Icons.local_florist_outlined,
  'cake' => Icons.cake_outlined,
  'photographer' => Icons.photo_camera_outlined,
  'tent_sound' => Icons.speaker_group_outlined,
  _ => Icons.celebration_outlined,
};

const vendorStatuses = {
  'shortlisted': ('Shortlisted', Icons.schedule),
  'ringing': ('Ringing…', Icons.ring_volume),
  'in_call': ('On the call', Icons.phone_in_talk),
  'quoted': ('Quote received', Icons.receipt_long),
  'declined': ('Not available', Icons.event_busy),
  'no_answer': ('No answer', Icons.phone_missed),
  'callback': ('Asked to call back', Icons.phone_callback),
  'failed': ('Call failed', Icons.error_outline),
  'renegotiating': ('Renegotiating…', Icons.handshake),
  'reconfirming': ('Rechecking prices…', Icons.fact_check_outlined),
  'approved': ('Booked', Icons.verified),
  'confirmed': ('Confirmed', Icons.verified),
  'awaiting_confirmation': ('Approved · awaiting confirmation', Icons.pending_actions),
  'confirming': ('Confirming booking…', Icons.phone_in_talk),
  'confirmation_failed': ('Confirmation needed', Icons.phone_missed),
  'awaiting_changes': ('Changed terms · review needed', Icons.pending_actions),
  'standby': ('Backup on standby', Icons.shield_outlined),
  'standby_confirmed': ('Standby confirmed · not booked', Icons.shield_outlined),
  'awaiting_backup_confirmation': ('Standby not confirmed', Icons.pending_actions),
  'confirming_backup': ('Confirming standby', Icons.phone_in_talk),
  'backup_confirmation_failed': ('Backup did not confirm', Icons.phone_missed),
  'backup_awaiting_changes': ('Backup changes need review', Icons.pending_actions),
  'awaiting_activation': ('Replacement confirmation queued', Icons.pending_actions),
  'activating_backup': ('Confirming replacement', Icons.phone_in_talk),
  'activation_awaiting_approval': ('Replacement needs approval', Icons.pending_actions),
  'replacement_pending': ('Replacement pending', Icons.swap_horiz),
  'needs_replacement': ('No confirmed replacement', Icons.warning_amber_outlined),
  'rejected': ('Rejected', Icons.block),
  'replaced': ('Replaced by backup', Icons.swap_horiz),
  'cancelled': ('Cancelled', Icons.event_busy),
};
