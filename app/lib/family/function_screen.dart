import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../format.dart';
import '../settings.dart';
import '../setup_screen.dart';
import 'new_function_screen.dart';

/// The family watches Bandobast work: vendors found, called, quotes in; then approves.
class FunctionScreen extends StatefulWidget {
  const FunctionScreen({super.key});

  @override
  State<FunctionScreen> createState() => _FunctionScreenState();
}

class _FunctionScreenState extends State<FunctionScreen> {
  Map<String, dynamic>? _state;
  String? _error;
  bool _loaded = false;
  Timer? _poll;
  final Set<String> _busy = {};

  @override
  void initState() {
    super.initState();
    _load();
    _poll = Timer.periodic(const Duration(seconds: 3), (_) => _load());
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final s = await Api.get('/events/current') as Map<String, dynamic>;
      if (!mounted) return;
      setState(() {
        _state = s;
        _error = null;
        _loaded = true;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loaded = true;
      });
    }
  }

  Future<void> _act(String vendorId, String action, String done) async {
    setState(() => _busy.add(vendorId));
    try {
      await Api.post('/events/current/vendors/$vendorId/$action');
      _snack(done);
      await _load();
    } catch (e) {
      _snack('$e');
    } finally {
      if (mounted) setState(() => _busy.remove(vendorId));
    }
  }

  void _snack(String text) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  void _newFunction() => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NewFunctionScreen()));

  Future<void> _switchRole() async {
    await Settings.setRole(null);
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const SetupScreen()), (_) => false);
  }

  @override
  Widget build(BuildContext context) {
    final event = _state?['event'] as Map<String, dynamic>?;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Bandobast'),
        actions: [
          if (event != null) IconButton(onPressed: _newFunction, tooltip: 'Plan another function', icon: const Icon(Icons.add)),
          IconButton(onPressed: _switchRole, tooltip: 'Switch role', icon: const Icon(Icons.swap_horiz)),
        ],
      ),
      body: !_loaded
          ? const Center(child: CircularProgressIndicator())
          : event == null
              ? _Empty(error: _error, onPlan: _newFunction)
              : RefreshIndicator(onRefresh: _load, child: _body(context, event)),
    );
  }

  Widget _body(BuildContext context, Map<String, dynamic> event) {
    final brief = event['brief'] as Map<String, dynamic>;
    final services = (_state!['services'] as List).cast<Map<String, dynamic>>();
    final alerts = (_state!['alerts'] as List).cast<Map<String, dynamic>>();
    final checkins = (_state!['checkins'] as List).cast<Map<String, dynamic>>();
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 40),
      children: [
        Card(
          color: theme.colorScheme.primaryContainer,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('${event['label']}', style: theme.textTheme.headlineSmall),
              Text('${brief['family_name']} family · ${brief['area']}'),
              Text('${prettyDate(brief['date'] as String?)} · muhurat ${brief['start_time']} · ${brief['guests']} guests'),
              if (_state!['calling_now'] == true)
                const Padding(
                  padding: EdgeInsets.only(top: 10),
                  child: Row(children: [
                    SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
                    SizedBox(width: 10),
                    Text('Bandobast is on a call…'),
                  ]),
                ),
            ]),
          ),
        ),
        if (_error != null) Card(child: ListTile(leading: const Icon(Icons.cloud_off), title: Text(_error!))),
        for (final a in alerts.take(5)) _AlertTile(alert: a),
        for (final s in services) _ServiceCard(service: s, busy: _busy, onAct: _act),
        if (checkins.isNotEmpty) _CheckinsCard(checkins: checkins, services: services, onCall: (id) async {
          try {
            await Api.post('/checkins/$id/call');
            _snack('Calling the vendor now');
          } catch (e) {
            _snack('$e');
          }
        }),
      ],
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.error, required this.onPlan});
  final String? error;
  final VoidCallback onPlan;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.celebration, size: 64),
            const SizedBox(height: 12),
            Text('Planning a function in Bengaluru?', style: Theme.of(context).textTheme.titleLarge, textAlign: TextAlign.center),
            const SizedBox(height: 6),
            const Text('Tell us what you need. We find vendors near the venue, call them in their language, '
                'bargain politely, and bring you the quotes.', textAlign: TextAlign.center),
            if (error != null) Padding(padding: const EdgeInsets.only(top: 12), child: Text(error!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
            const SizedBox(height: 20),
            FilledButton.icon(onPressed: onPlan, icon: const Icon(Icons.add), label: const Text('Plan a function')),
          ]),
        ),
      );
}

class _AlertTile extends StatelessWidget {
  const _AlertTile({required this.alert});
  final Map<String, dynamic> alert;

  @override
  Widget build(BuildContext context) {
    final level = alert['level'];
    final (icon, color) = switch (level) {
      'critical' => (Icons.warning_amber, const Color(0xFFFDECEA)),
      'warn' => (Icons.info_outline, const Color(0xFFFFF4E0)),
      _ => (Icons.check_circle_outline, const Color(0xFFE6F4EA)),
    };
    return Card(color: color, child: ListTile(leading: Icon(icon), title: Text('${alert['message']}'), dense: true));
  }
}

const _status = {
  'shortlisted': ('Shortlisted', Icons.schedule),
  'ringing': ('Ringing…', Icons.ring_volume),
  'in_call': ('On the call', Icons.phone_in_talk),
  'quoted': ('Quote received', Icons.receipt_long),
  'declined': ('Not available', Icons.event_busy),
  'no_answer': ('No answer', Icons.phone_missed),
  'callback': ('Asked to call back', Icons.phone_callback),
  'failed': ('Call failed', Icons.error_outline),
  'renegotiating': ('Renegotiating…', Icons.handshake),
  'approved': ('Booked', Icons.verified),
  'standby': ('Backup on standby', Icons.shield_outlined),
  'rejected': ('Rejected', Icons.block),
};

class _ServiceCard extends StatelessWidget {
  const _ServiceCard({required this.service, required this.busy, required this.onAct});

  final Map<String, dynamic> service;
  final Set<String> busy;
  final Future<void> Function(String vendorId, String action, String done) onAct;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final vendors = (service['vendors'] as List).cast<Map<String, dynamic>>();
    final found = (service['discovered'] as List).cast<Map<String, dynamic>>();
    final booked = (service['booking'] as Map).isNotEmpty;
    return Card(
      margin: const EdgeInsets.only(top: 12),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text('${service['name']}', style: theme.textTheme.titleLarge)),
            if (service['budget'] != null) Text('Budget ${rupees(service['budget'] as num)}', style: theme.textTheme.bodySmall),
          ]),
          if (vendors.isEmpty) const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: Text('Finding vendors near the venue…')),
          for (final v in vendors)
            _VendorTile(
              vendor: v,
              negotiable: service['negotiable'] == true,
              serviceBooked: booked,
              busy: busy.contains(v['vendor_id']),
              onAct: onAct,
            ),
          if (found.isNotEmpty)
            ExpansionTile(
              tilePadding: EdgeInsets.zero,
              title: Text('Also found nearby (${found.length})', style: theme.textTheme.bodyMedium),
              subtitle: const Text('From OpenStreetMap and the web. Not contacted in this demo.', style: TextStyle(fontSize: 12)),
              children: [
                for (final p in found)
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(p['source'] == 'web' ? Icons.public : Icons.map_outlined, size: 20),
                    title: Text('${p['name']}'),
                    subtitle: Text([if (p['distance_km'] != null) '${p['distance_km']} km', if (p['address'] != null) p['address']].join(' · ')),
                  ),
              ],
            ),
        ]),
      ),
    );
  }
}

class _VendorTile extends StatelessWidget {
  const _VendorTile({required this.vendor, required this.negotiable, required this.serviceBooked, required this.busy, required this.onAct});

  final Map<String, dynamic> vendor;
  final bool negotiable;
  final bool serviceBooked;
  final bool busy;
  final Future<void> Function(String vendorId, String action, String done) onAct;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final id = vendor['vendor_id'] as String;
    final status = vendor['status'] as String;
    final (label, icon) = _status[status] ?? (status, Icons.help_outline);
    final quote = vendor['quote'] as Map<String, dynamic>?;
    final flags = (vendor['flags'] as List).cast<Map<String, dynamic>>();
    final canDecide = status == 'quoted' && !serviceBooked && quote != null;
    final top = vendor['top'] == true && canDecide;
    return Container(
      margin: const EdgeInsets.only(top: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: top ? theme.colorScheme.primary : theme.dividerColor, width: top ? 2 : 1),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(child: Text('${vendor['name']}', style: theme.textTheme.titleMedium)),
          if (top) Chip(label: const Text('Best pick'), visualDensity: VisualDensity.compact, backgroundColor: theme.colorScheme.primaryContainer),
        ]),
        Text(
          '★ ${vendor['rating']} (${vendor['review_count']}) · ${vendor['area']}'
          '${vendor['distance_km'] != null ? ' · ${vendor['distance_km']} km' : ''} · speaks ${languageNames[vendor['language']] ?? vendor['language']}',
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 6),
        Row(children: [
          Icon(icon, size: 18, color: theme.colorScheme.secondary),
          const SizedBox(width: 6),
          Text(label, style: TextStyle(color: theme.colorScheme.secondary, fontWeight: FontWeight.w600)),
          if (vendor['note'] != null && status != 'quoted') Flexible(child: Text('  ${vendor['note']}', style: theme.textTheme.bodySmall, overflow: TextOverflow.ellipsis)),
        ]),
        if (quote != null && quote['available'] == true) ...[
          const SizedBox(height: 8),
          Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
            Text(rupees(vendor['landed_cost'] as num?), style: theme.textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(width: 6),
            const Padding(padding: EdgeInsets.only(bottom: 4), child: Text('all in')),
            const Spacer(),
            if (vendor['savings'] != null)
              Chip(
                avatar: const Icon(Icons.savings, size: 18),
                label: Text('Saved ${rupees(vendor['savings'] as num)}'),
                backgroundColor: const Color(0xFFE6F4EA),
                visualDensity: VisualDensity.compact,
              ),
          ]),
          Text(_breakdown(quote), style: theme.textTheme.bodySmall),
          if ((quote['includes_notes'] ?? '').toString().isNotEmpty) Text('Covers: ${quote['includes_notes']}', style: theme.textTheme.bodySmall),
          for (final f in flags.where((f) => f['level'] != 'info' || '${f['text']}'.startsWith('Extra')))
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text('• ${f['text']}', style: TextStyle(fontSize: 12, color: f['level'] == 'block' ? theme.colorScheme.error : const Color(0xFF9A6200))),
            ),
        ],
        if (canDecide) ...[
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 4, children: [
            FilledButton.icon(
              onPressed: busy || vendor['bookable'] != true ? null : () => onAct(id, 'approve', 'Booked ${vendor['name']}; the next best is on standby'),
              icon: const Icon(Icons.check),
              label: const Text('Approve'),
            ),
            if (negotiable)
              OutlinedButton.icon(
                onPressed: busy ? null : () => onAct(id, 'renegotiate', 'Bandobast will call ${vendor['name']} back for a better price'),
                icon: const Icon(Icons.handshake_outlined),
                label: const Text('Ask for better'),
              ),
            TextButton(
              onPressed: busy ? null : () => onAct(id, 'reject', 'Rejected ${vendor['name']}'),
              child: const Text('Reject'),
            ),
          ]),
        ],
      ]),
    );
  }

  static String _breakdown(Map<String, dynamic> q) {
    final unit = q['price_unit'] == 'per_plate' ? ' per plate' : '';
    return [
      '${rupees(q['price_amount'] as num?)}$unit',
      if (q['transport_charge'] != null) 'transport ${rupees(q['transport_charge'] as num)}',
      if ((q['extra_charges_amount'] ?? 0) != 0) 'extras ${rupees(q['extra_charges_amount'] as num)}',
      if (q['gst_included'] != null) q['gst_included'] == true ? 'GST included' : 'plus GST',
      if (q['advance_amount'] != null) 'advance ${rupees(q['advance_amount'] as num)}',
    ].join(' · ');
  }
}

class _CheckinsCard extends StatelessWidget {
  const _CheckinsCard({required this.checkins, required this.services, required this.onCall});

  final List<Map<String, dynamic>> checkins;
  final List<Map<String, dynamic>> services;
  final Future<void> Function(int id) onCall;

  String _vendorName(String id) {
    for (final s in services) {
      for (final v in (s['vendors'] as List)) {
        if (v['vendor_id'] == id) return v['name'] as String;
      }
    }
    return id;
  }

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.only(top: 12),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Follow-up calls', style: Theme.of(context).textTheme.titleLarge),
          const Text('Bandobast checks in with booked vendors before the day, and switches to the backup if one falls through.',
              style: TextStyle(fontSize: 12)),
          for (final c in checkins)
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text('${_vendorName(c['vendor_id'] as String)} · ${c['label']}'),
              subtitle: Text('${(c['due_at'] as String).replaceFirst('T', ' ')} · ${c['status']}${c['eta'] != null ? ' · ETA ${c['eta']}' : ''}'),
              trailing: c['status'] == 'pending'
                  ? TextButton(onPressed: () => onCall(c['id'] as int), child: const Text('Call now'))
                  : null,
            ),
        ]),
      ),
    );
  }
}
