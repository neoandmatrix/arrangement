import 'package:flutter/material.dart';

import '../format.dart';
import 'activity_timeline.dart';
import 'booking_card.dart';
import 'service_quotes.dart';
import 'vendor_update_card.dart';
import 'view_helpers.dart';

/// Three focused views of one occasion; changing tabs never changes bookings or rankings.
class FamilyDashboard extends StatefulWidget {
  const FamilyDashboard({super.key, required this.state, required this.familyId, required this.busy,
    required this.onRefresh, required this.onAct, required this.onCheckin, this.onCancel, this.onChooseBackup, this.error});
  final Map<String, dynamic> state;
  final String familyId;
  final Set<String> busy;
  final Future<void> Function() onRefresh;
  final Future<void> Function(String, String, String) onAct;
  final Future<void> Function(int) onCheckin;
  final String? error;
  final Future<void> Function(String)? onCancel;
  final Future<void> Function(String, String)? onChooseBackup;

  @override
  State<FamilyDashboard> createState() => _FamilyDashboardState();
}

class _FamilyDashboardState extends State<FamilyDashboard> with SingleTickerProviderStateMixin {
  late final TabController _tabs;
  final _bookingScroll = ScrollController();
  String? _category;

  List<Map<String, dynamic>> get _services => rows(widget.state['services']);
  List<Map<String, dynamic>> get _updates => rows(widget.state['updates']);
  List<Map<String, dynamic>> get _pending => _updates.where((u) => u['status'] == 'pending').toList();

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 3, vsync: this, initialIndex: _pending.isNotEmpty || _services.any(isSelected) ? 0 : 1);
    _category = _services.firstOrNull?['category'] as String?;
  }

  @override
  void didUpdateWidget(FamilyDashboard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if ((oldWidget.state['event'] as Map?)?['id'] != (widget.state['event'] as Map?)?['id']) {
      _category = _services.firstOrNull?['category'] as String?;
      _tabs.index = _pending.isNotEmpty || _services.any(isSelected) ? 0 : 1;
      if (_bookingScroll.hasClients) _bookingScroll.jumpTo(0);
    } else if (!_services.any((s) => s['category'] == _category)) {
      _category = _services.firstOrNull?['category'] as String?;
    }
  }

  @override
  void dispose() {
    _tabs.dispose();
    _bookingScroll.dispose();
    super.dispose();
  }

  void _quotes(String category) {
    setState(() => _category = category);
    _tabs.animateTo(1);
  }

  void _bookings() {
    _tabs.animateTo(0);
    if (_bookingScroll.hasClients) _bookingScroll.jumpTo(0);
  }

  @override
  Widget build(BuildContext context) {
    final checkins = rows(widget.state['checkins']);
    return Column(children: [
      TabBar(controller: _tabs, tabs: const [Tab(text: 'Bookings'), Tab(text: 'Quotes'), Tab(text: 'Timeline')]),
      if (_pending.isNotEmpty) Material(
        color: const Color(0xFFFFF4E0),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(children: [
            const Icon(Icons.pending_actions, size: 20),
            const SizedBox(width: 8),
            Expanded(child: Text('${_pending.length} ${_pending.any((u) => u['kind'] == 'backup_activation') ? 'replacement request' : _pending.any((u) => u['kind'] == 'confirmation_change') ? 'booking revision' : _pending.any((u) => u['kind'] == 'quote') ? 'quote revision' : 'price revision'}${_pending.length == 1 ? '' : 's'} to review', style: const TextStyle(fontSize: 13))),
            TextButton(onPressed: _bookings, child: const Text('Review')),
          ]),
        ),
      ),
      Expanded(child: TabBarView(controller: _tabs, children: [
        _page('bookings', [
          if (_pending.isNotEmpty) ...[
            _heading('Needs your approval', 'Existing prices stay unchanged until you decide.'),
            for (final update in _pending) VendorUpdateCard(key: ValueKey(update['id']), update: update, services: _services, onRefresh: widget.onRefresh,
              onReconfirm: (id) => widget.onAct(id, 'reconfirm', 'Bandobast will recheck the corrected quote before you can approve')),
            const SizedBox(height: 16),
          ],
          _heading('Your bookings', 'One card per service, with its vendor and backup.'),
          for (final service in _services) BookingCard(
            key: ValueKey(service['category']), service: service, checkins: checkins,
            onQuotes: () => _quotes(service['category'] as String), onCheckin: widget.onCheckin,
            rechecking: widget.busy.contains(booking(service, 'primary')?['vendor_id']),
            reviewPending: _pending.any((u) => u['vendor_id'] == booking(service, 'primary')?['vendor_id']),
            onReconfirm: () {
              final id = booking(service, 'primary')?['vendor_id'] as String?;
              if (id != null) widget.onAct(id, 'reconfirm', 'Bandobast will recheck the booked vendor’s charges; agreed terms will not change without your approval');
            },
            onConfirm: () {
              final id = booking(service, 'primary')?['vendor_id'] as String?;
              if (id != null) widget.onAct(id, 'confirm', 'Booking confirmation call requested');
            },
            onCancel: widget.onCancel == null ? null : () => widget.onCancel!(service['category'] as String),
            onBackupConfirm: () {
              final id = booking(service, 'backup')?['vendor_id'] as String?;
              if (id != null) widget.onAct(id, 'confirm-backup', 'Backup confirmation requested; it is not active yet');
            },
            onBackupChoose: widget.onChooseBackup == null ? null : (id) => widget.onChooseBackup!(service['category'] as String, id),
            onBackupReconfirm: () {
              final id = booking(service, 'backup')?['vendor_id'] as String?;
              if (id != null) widget.onAct(id, 'reconfirm', 'Bandobast will recheck the backup quote');
            },
          ),
        ]),
        _page('quotes', [
          _heading('Compare quotes', 'Choose a service to see its vendors and prices.'),
          Wrap(spacing: 8, runSpacing: 4, children: [
            for (final service in _services) ChoiceChip(
              label: Text('${service['name']}'), avatar: Icon(serviceIcon(service['category']), size: 18),
              selected: _category == service['category'], onSelected: (_) => setState(() => _category = service['category'] as String),
            ),
          ]),
          const SizedBox(height: 16),
          for (final service in _services.where((s) => s['category'] == _category)) ServiceQuotes(
            key: ValueKey(service['category']), service: service, busy: widget.busy,
            pendingRates: _pending.map((u) => '${u['vendor_id']}').toSet(), onAct: widget.onAct,
            onBookings: _bookings,
          ),
        ]),
        _page('timeline', [ActivityTimeline(services: _services, alerts: rows(widget.state['alerts']),
            updates: _updates, checkins: checkins, onCheckin: widget.onCheckin)]),
      ])),
    ]);
  }

  Widget _page(String section, List<Widget> children) => RefreshIndicator(
    onRefresh: widget.onRefresh,
    child: ListView(
      key: PageStorageKey('${(widget.state['event'] as Map)['id']}-$section'),
      controller: section == 'bookings' ? _bookingScroll : null,
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 32),
      children: [
        _occasion(),
        if (widget.error != null) Card(child: ListTile(leading: const Icon(Icons.cloud_off), title: Text(widget.error!))),
        const SizedBox(height: 16),
        ...children,
      ],
    ),
  );

  Widget _heading(String title, String subtitle) => Padding(
    padding: const EdgeInsets.only(bottom: 12),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text(title, style: Theme.of(context).textTheme.titleLarge),
      const SizedBox(height: 4),
      Text(subtitle, style: Theme.of(context).textTheme.bodySmall),
    ]),
  );

  Widget _occasion() {
    final event = widget.state['event'] as Map<String, dynamic>;
    final brief = event['brief'] as Map<String, dynamic>;
    final booked = _services.where(isBooked).length;
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      color: theme.colorScheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('YOUR OCCASION', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: theme.colorScheme.onPrimaryContainer)),
          const SizedBox(height: 4),
          Text('${event['label']}', style: theme.textTheme.headlineSmall),
          Text('${brief['family_name']} family · ${brief['area']}'),
          const SizedBox(height: 10),
          Wrap(spacing: 12, runSpacing: 4, children: [
            _fact(Icons.event_outlined, prettyDate(brief['date'] as String?)),
            _fact(Icons.schedule, 'Muhurat ${brief['start_time']}'),
            _fact(Icons.people_outline, '${brief['guests']} guests'),
          ]),
          const SizedBox(height: 12),
          Text('$booked of ${_services.length} services booked', style: theme.textTheme.bodySmall),
          const SizedBox(height: 6),
          LinearProgressIndicator(value: _services.isEmpty ? 0 : booked / _services.length, minHeight: 4, borderRadius: BorderRadius.circular(4)),
          if (widget.state['calling_now'] == true) const Padding(
            padding: EdgeInsets.only(top: 10),
            child: Row(children: [SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2)), SizedBox(width: 8), Expanded(child: Text('Bandobast is on a call…', style: TextStyle(fontSize: 13)))]),
          ),
          ExpansionTile(
            tilePadding: EdgeInsets.zero,
            title: const Text('Occasion details', style: TextStyle(fontSize: 13)),
            childrenPadding: const EdgeInsets.only(bottom: 12),
            children: [
              if ('${brief['contact_name'] ?? ''}'.isNotEmpty) _detail('Contact', '${brief['contact_name']}'),
              _detail('Language', languageNames[brief['family_language']] ?? '${brief['family_language'] ?? ''}'),
              if ('${brief['cuisine'] ?? ''}'.isNotEmpty) _detail('Food', '${brief['cuisine']}'),
              if ('${brief['rituals'] ?? ''}'.isNotEmpty) _detail('Rituals', '${brief['rituals']}'),
              _detail('Demo family ID', widget.familyId),
              const Align(alignment: Alignment.centerLeft, child: Text('Booked vendors can call this phone while it is connected.', style: TextStyle(fontSize: 12))),
            ],
          ),
        ]),
      ),
    );
  }

  Widget _fact(IconData icon, String label) => Row(mainAxisSize: MainAxisSize.min, children: [
    Icon(icon, size: 16), const SizedBox(width: 4), Flexible(child: Text(label, style: const TextStyle(fontSize: 12))),
  ]);

  Widget _detail(String label, String value) => Align(alignment: Alignment.centerLeft,
      child: Padding(padding: const EdgeInsets.only(bottom: 6), child: Text('$label: $value')));
}
