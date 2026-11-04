import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../call_actions.dart';
import '../family/confirmation_summary.dart';
import '../family/view_helpers.dart';
import '../format.dart';
import '../settings.dart';
import '../setup_screen.dart';
import 'ringer.dart';

/// The vendor's side: are calls reaching this phone, and what's happening with my jobs.
class VendorHome extends StatefulWidget {
  const VendorHome({super.key});

  @override
  State<VendorHome> createState() => _VendorHomeState();
}

class _VendorHomeState extends State<VendorHome> {
  Map<String, dynamic>? _jobs;
  String? _error;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    Ringer.instance.start(Settings.vendorId!);
    _load();
    _poll = Timer.periodic(const Duration(seconds: 5), (_) => _load());
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final jobs = await Api.get('/vendor/${Settings.vendorId}/jobs');
      if (!mounted) return;
      setState(() {
        _jobs = jobs as Map<String, dynamic>;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  Future<void> _switchRole() async {
    Ringer.instance.stop();
    await Settings.setRole(null);
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const SetupScreen()), (_) => false);
  }

  @override
  Widget build(BuildContext context) {
    final event = _jobs?['event'] as Map<String, dynamic>?;
    return Scaffold(
      appBar: AppBar(
        title: Text(Settings.vendorName ?? 'Vendor'),
        actions: [IconButton(onPressed: _switchRole, tooltip: 'Switch role', icon: const Icon(Icons.swap_horiz))],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _ReachabilityCard(),
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text('Demo vendor ID: ${Settings.vendorId}', style: Theme.of(context).textTheme.bodySmall),
            ),
            if (_error != null) Card(child: ListTile(leading: const Icon(Icons.error_outline), title: Text(_error!))),
            const SizedBox(height: 8),
            if (event == null)
              const Card(child: ListTile(title: Text('No function right now'), subtitle: Text('When a family needs you, Bandobast will call this phone.')))
            else
              _JobCard(event: event, jobs: _jobs!, onRefresh: _load),
          ],
        ),
      ),
    );
  }
}

class _ReachabilityCard extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<bool>(
      valueListenable: Ringer.instance.connected,
      builder: (context, online, _) => ValueListenableBuilder<bool>(
        valueListenable: Ringer.instance.pushEnabled,
        builder: (context, push, _) => Card(
          color: online ? const Color(0xFFE6F4EA) : const Color(0xFFFDECEA),
          child: ListTile(
            leading: Icon(online ? Icons.phone_in_talk : Icons.phone_disabled, color: online ? Colors.green[800] : Colors.red[800]),
            title: Text(online ? 'Ready for calls' : 'Not connected: reconnecting…'),
            subtitle: Text(push
                ? 'Calls also ring when the app is closed.'
                : 'Keep the app open to receive calls (push not set up).'),
          ),
        ),
      ),
    );
  }
}

const _enquiryText = {
  'shortlisted': 'You are on the shortlist; expect a call',
  'ringing': 'Calling you now',
  'in_call': 'On the call',
  'quoted': 'Quote sent; the family is deciding',
  'declined': 'You declined',
  'no_answer': 'Missed call',
  'callback': 'Call back later',
  'renegotiating': 'Calling about your quote',
  'approved': 'Booked!',
  'confirmed': 'Booking confirmed',
  'awaiting_confirmation': 'The family approved you; booking confirmation is pending',
  'confirming': 'Confirming the family-approved booking terms',
  'confirmation_failed': 'Booking confirmation is still needed',
  'awaiting_changes': 'Changed terms are awaiting family review',
  'standby': 'On standby as backup',
  'rejected': 'The family chose someone else',
};

class _JobCard extends StatelessWidget {
  const _JobCard({required this.event, required this.jobs, required this.onRefresh});

  final Map<String, dynamic> event;
  final Map<String, dynamic> jobs;
  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    final enquiry = jobs['enquiry'] as Map<String, dynamic>?;
    final quote = jobs['quote'] as Map<String, dynamic>?;
    final booking = jobs['booking'] as Map<String, dynamic>?;
    final checkins = (jobs['checkins'] as List?) ?? [];
    final updates = (jobs['updates'] as List?) ?? [];
    final confirmation = jobs['confirmation'] as Map<String, dynamic>?;
    final confirmed = confirmation?['status'] == 'confirmed' && booking?['status'] == 'active';
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${event['label']}', style: theme.textTheme.titleLarge),
            Text('${event['family']} family · ${event['area']}'),
            Text('${prettyDate(event['date'] as String?)} · muhurat ${event['start_time']} · ${event['guests']} guests',
                style: theme.textTheme.bodySmall),
            const Divider(height: 24),
            if (enquiry == null)
              const Text('You are not on this function\'s shortlist.')
            else
              Text(_enquiryText[enquiry['status']] ?? '${enquiry['status']}', style: theme.textTheme.titleMedium),
            if (quote != null) ...[
              const SizedBox(height: 8),
              Text('Your quote: ${rupees(quote['price_amount'] as num?)}${quote['price_unit'] == 'per_plate' ? ' per plate' : ''}'),
              if (quote['advance_amount'] != null) Text('Advance: ${rupees(quote['advance_amount'] as num?)}'),
            ],
            if (booking != null) ...[
              const SizedBox(height: 8),
              Chip(label: Text(booking['role'] == 'primary'
                  ? (confirmed ? 'Booking confirmed' : 'Selected · ${booking['status']}')
                  : 'Backup (${booking['status']})')),
              if (confirmation != null) ConfirmationSummary(confirmation: confirmation),
            ],
            if (enquiry != null) ...[
              const SizedBox(height: 12),
              FilledButton.tonalIcon(
                onPressed: () async {
                  await callAssistant(context, mode: 'vendor_update');
                  await onRefresh();
                },
                icon: const Icon(Icons.support_agent), label: const Text('Talk to Bandobast'),
              ),
              const Text('Report a revised rate, cancellation, delay or another update. New rates need the family’s approval.',
                  style: TextStyle(fontSize: 12)),
            ],
            if (booking?['role'] == 'primary' && selectedBookingStates.contains(booking?['status']) && event['family_id'] != null) ...[
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: () => callPerson(context, event['family_id'] as String, '${event['family']} family'),
                icon: const Icon(Icons.call), label: Text('Call family · ${event['family_id']}'),
              ),
            ],
            for (final u in updates.take(3))
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.message_outlined),
                title: Text('${(u['data'] as Map)['notes']}'),
                subtitle: Text(u['status'] == 'pending' ? 'Awaiting family approval' : 'Update ${u['status']}'),
              ),
            if (checkins.isNotEmpty) ...[
              const Divider(height: 24),
              Text('Follow-up calls', style: theme.textTheme.titleSmall),
              for (final c in checkins)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: Text('${c['label']}'),
                  subtitle: Text((c['due_at'] as String).replaceFirst('T', ' ')),
                  trailing: Text('${c['status']}'),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
