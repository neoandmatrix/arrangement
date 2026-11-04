import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

List<Map<String, dynamic>> rows(Object? value) => value is List ? value.cast<Map<String, dynamic>>() : [];

Map<String, dynamic>? booking(Map<String, dynamic> service, String role) =>
    (service['booking'] as Map?)?[role] as Map<String, dynamic>?;

bool isBooked(Map<String, dynamic> service) => booking(service, 'primary')?['status'] == 'active';

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
  'standby': ('Backup on standby', Icons.shield_outlined),
  'rejected': ('Rejected', Icons.block),
  'replaced': ('Replaced by backup', Icons.swap_horiz),
  'cancelled': ('Cancelled', Icons.event_busy),
};
