import 'package:bandobast/family/activity_timeline.dart';
import 'package:bandobast/family/family_dashboard.dart';
import 'package:bandobast/family/quote_details.dart';
import 'package:bandobast/family/booking_card.dart';
import 'package:bandobast/family/view_helpers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> fixture({bool pending = false}) => {
  'event': {
    'id': 1, 'label': 'Rudrabhishek',
    'brief': {'family_name': 'Demo', 'area': 'Whitefield', 'date': '2026-11-05', 'start_time': '09:00',
      'guests': 40, 'family_language': 'hi-IN', 'contact_name': 'Demo contact'},
  },
  'services': [
    {
      'category': 'priest', 'name': 'Priest', 'negotiable': false, 'budget': 80000,
      'booking': {'primary': {'vendor_id': 'P01', 'status': 'active', 'confirmation': {
        'id': 1, 'status': 'confirmed', 'updated_at': '2026-11-04T09:00:00',
        'terms': {'function': 'Rudrabhishek', 'family': 'Demo', 'date': '2026-11-05', 'start_time': '09:00',
          'arrive_by': '2026-11-05T08:30', 'area': 'Whitefield', 'city': 'Bengaluru', 'guests': 40, 'total': 572000,
          'quote': {'price_amount': 67000, 'price_unit': 'total', 'transport_charge': 500000, 'extra_charges_amount': 5000, 'advance_amount': 500000}},
      }}}, 'discovered': [],
      'vendors': [
        {'vendor_id': 'P01', 'name': 'Demo Pandit', 'area': 'Whitefield', 'language': 'te-IN', 'rating': 4.5,
          'review_count': 97, 'distance_km': 0.2, 'status': 'approved', 'landed_cost': 572000, 'savings': 49933000,
          'quote': {'available': true, 'price_amount': 67000, 'price_unit': 'total', 'transport_charge': 500000,
            'extra_charges_amount': 5000, 'advance_amount': 500000, 'includes_notes': 'Rudrabhishek and samagri'},
          'flags': [{'level': 'warn', 'text': 'Advance is more than half of the total'}, {'level': 'warn', 'text': 'Review warning'}]},
        {'vendor_id': 'P02', 'name': 'Other Pandit', 'status': 'no_answer', 'note': 'no answer', 'quote': null},
      ],
    },
    {
      'category': 'caterer', 'name': 'Caterer', 'negotiable': true, 'booking': {}, 'discovered': [],
      'vendors': [
        {'vendor_id': 'C01', 'name': 'Demo Caterer', 'area': 'HSR Layout', 'language': 'kn-IN', 'rating': 4.8,
          'review_count': 70, 'status': 'quoted', 'landed_cost': 18000, 'bookable': true, 'top': true,
          'quote': {'available': true, 'price_amount': 450, 'price_unit': 'per_plate', 'transport_charge': 0}, 'flags': []},
      ],
    },
  ],
  'updates': [
    {'id': 2, 'vendor_id': 'P01', 'kind': 'price', 'status': pending ? 'pending' : 'approved', 'created_at': '2026-11-04T09:10:00',
      'data': {'notes': 'Base price changed to 67000', 'previous_price': 50000, 'quote': {'price_amount': 67000, 'price_unit': 'total'}}},
  ],
  'alerts': [
    {'id': 3, 'created_at': '2026-11-04T09:15:00', 'level': 'info', 'message': 'Approved revised price from Demo Pandit.'},
    {'id': 2, 'created_at': '2026-11-04T09:10:00', 'level': 'warn', 'message': 'Demo Pandit: Base price changed to 67000 — revised price needs your approval.'},
    {'id': 1, 'created_at': '2026-11-04T09:00:00', 'level': 'info', 'message': 'Booked Demo Pandit as priest; no backup available yet.'},
  ],
  'checkins': [
    {'id': 1, 'vendor_id': 'P01', 'label': 'D-3 confirmation', 'due_at': '2026-11-02T18:00', 'status': 'pending'},
    {'id': 2, 'vendor_id': 'P01', 'label': 'D-1 confirmation', 'due_at': '2026-11-04T18:00', 'status': 'confirmed', 'done_at': '2026-11-04T09:20:00'},
  ],
  'calling_now': false,
};

Widget dashboard(Map<String, dynamic> state, {void Function(String, String, String)? act, void Function(int)? checkin, double textScale = 1}) => MaterialApp(
  theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFFB5481D)), useMaterial3: true),
  builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(textScale)), child: child!),
  home: Scaffold(body: FamilyDashboard(state: state, familyId: 'F12345678', busy: const {},
    onRefresh: () async {}, onAct: (id, action, done) async { act?.call(id, action, done); }, onCheckin: (id) async { checkin?.call(id); })),
);

void main() {
  test('follow-up times remain IST when stored with a timezone offset', () {
    expect(shortDateTime('2026-11-19T12:30:00Z'), '19 Nov · 6:00 PM IST');
    expect(shortDateTime('2026-11-19T18:00:00+05:30'), '19 Nov · 6:00 PM IST');
  });
  test('timeline removes duplicate reports, retains decisions, and excludes pending calls', () {
    final state = fixture();
    final entries = timelineEntries(services: (state['services'] as List).cast<Map<String, dynamic>>(),
      alerts: (state['alerts'] as List).cast<Map<String, dynamic>>(), updates: (state['updates'] as List).cast<Map<String, dynamic>>(),
      checkins: (state['checkins'] as List).cast<Map<String, dynamic>>());
    expect(entries.length, 4);
    expect(entries.first['key'], 'checkin-2');
    expect(entries.map((e) => e['key']), isNot(contains('alert-2')));
    expect(entries.map((e) => e['key']), isNot(contains('checkin-1')));
    expect(entries.firstWhere((e) => e['key'] == 'update-2')['status'], 'approved');
  });

  testWidgets('bookings are separate from quotes and old update cards', (tester) async {
    await tester.pumpWidget(dashboard(fixture()));
    expect(find.text('Your bookings'), findsOneWidget);
    expect(find.text('Demo Pandit'), findsOneWidget);
    expect(find.text('Other Pandit'), findsNothing);
    expect(find.text('Update from Demo Pandit'), findsNothing);
    expect(find.text('Demo family ID: F12345678'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('service filters show only the selected category and keep approval working', (tester) async {
    final actions = <String>[];
    await tester.pumpWidget(dashboard(fixture(), act: (id, action, _) => actions.add('$id:$action')));
    await tester.tap(find.text('Quotes'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ChoiceChip, 'Caterer'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Approve'), 180, scrollable: find.byType(Scrollable).last);
    expect(find.text('Demo Caterer'), findsOneWidget);
    expect(find.text('Demo Pandit'), findsNothing);
    await tester.tap(find.text('Approve'));
    await tester.pumpAndSettle();
    expect(actions, ['C01:approve']);
    expect(tester.takeException(), isNull);
  });

  testWidgets('timeline has dated activity and collapsed scheduled calls', (tester) async {
    await tester.pumpWidget(dashboard(fixture()));
    await tester.tap(find.text('Timeline'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Activity timeline'), 150, scrollable: find.byType(Scrollable).last);
    expect(find.text('Activity timeline'), findsOneWidget);
    expect(find.text('Scheduled follow-ups (1)'), findsOneWidget);
    expect(find.text('Call now'), findsNothing);
    expect(find.text('Price revision approved'), findsOneWidget);
    expect(find.textContaining('revised price needs your approval'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending revisions remain reachable from the timeline', (tester) async {
    await tester.pumpWidget(dashboard(fixture(pending: true)));
    await tester.tap(find.text('Timeline'));
    await tester.pumpAndSettle();
    expect(find.text('1 price revision to review'), findsOneWidget);
    await tester.tap(find.text('Review'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Approve revised price'), 120, scrollable: find.byType(Scrollable).last);
    expect(find.text('Approve revised price'), findsOneWidget);
  });

  testWidgets('large amounts and savings fit narrow screens at larger text sizes', (tester) async {
    tester.view.physicalSize = const Size(320, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final vendor = ((fixture()['services'] as List).first['vendors'] as List).first as Map<String, dynamic>;
    await tester.pumpWidget(MaterialApp(home: MediaQuery(
      data: const MediaQueryData(size: Size(320, 900), textScaler: TextScaler.linear(1.4)),
      child: Scaffold(body: SingleChildScrollView(child: Padding(padding: const EdgeInsets.all(16),
        child: Column(children: [QuoteAmount(vendor: vendor), QuoteDetails(vendor: vendor)])))),
    )));
    expect(tester.takeException(), isNull);
    await tester.tap(find.text('Price breakdown & details'));
    await tester.pumpAndSettle();
    expect(find.text('Transport'), findsOneWidget);
    expect(find.text('Review warning'), findsNothing);
    expect(find.text('• Review warning'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('all dashboard tabs fit a narrow phone with enlarged text', (tester) async {
    tester.view.physicalSize = const Size(320, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(dashboard(fixture(pending: true), textScale: 1.4));
    expect(tester.takeException(), isNull);
    for (final tab in ['Quotes', 'Timeline', 'Bookings']) {
      await tester.tap(find.text(tab));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    }
  });

  testWidgets('polling does not reset the selected quote category', (tester) async {
    await tester.pumpWidget(dashboard(fixture()));
    await tester.tap(find.text('Quotes'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ChoiceChip, 'Caterer'));
    await tester.pumpAndSettle();
    await tester.pumpWidget(dashboard(fixture()));
    await tester.pumpAndSettle();
    expect(tester.widget<ChoiceChip>(find.widgetWithText(ChoiceChip, 'Caterer')).selected, isTrue);
  });

  testWidgets('follow-up actions remain available inside their booking', (tester) async {
    final called = <int>[];
    await tester.pumpWidget(dashboard(fixture(), checkin: called.add));
    await tester.ensureVisible(find.text('Follow-up calls (1)'));
    await tester.pumpAndSettle();
    expect(find.text('Call vendor'), findsOneWidget);
    await tester.tap(find.text('Follow-up calls (1)'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Call now'));
    await tester.tap(find.text('Call now'));
    await tester.pumpAndSettle();
    expect(called, [1]);
    expect(tester.takeException(), isNull);
  });

  testWidgets('approved vendor stays selected but unconfirmed and has no follow-up actions', (tester) async {
    final state = fixture();
    final service = (state['services'] as List).first as Map<String, dynamic>;
    final primary = (service['booking'] as Map)['primary'] as Map<String, dynamic>;
    primary['status'] = 'awaiting_confirmation';
    (primary['confirmation'] as Map)['status'] = 'pending';
    await tester.pumpWidget(dashboard(state));
    expect(find.text('Awaiting confirmation'), findsOneWidget);
    expect(find.text('Confirm booking'), findsOneWidget);
    expect(find.text('Follow-up calls (1)'), findsNothing);
    await tester.tap(find.text('Quotes'));
    await tester.pumpAndSettle();
    expect(find.text('Vendor selected · view confirmation'), findsOneWidget);
    expect(find.text('Approve'), findsNothing); // another priest cannot be approved during confirmation
    expect(tester.takeException(), isNull);
  });

  testWidgets('missed confirmation offers a retry action for the same approved vendor', (tester) async {
    final state = fixture();
    final service = (state['services'] as List).first as Map<String, dynamic>;
    final primary = (service['booking'] as Map)['primary'] as Map<String, dynamic>;
    primary['status'] = 'confirmation_failed';
    (primary['confirmation'] as Map)['status'] = 'no_answer';
    final actions = <String>[];
    await tester.pumpWidget(dashboard(state, act: (id, action, _) => actions.add('$id:$action')));
    await tester.ensureVisible(find.text('Retry confirmation'));
    await tester.tap(find.text('Retry confirmation'));
    await tester.pumpAndSettle();
    expect(actions, ['P01:confirm']);
  });

  testWidgets('confirmed summary exposes the approved date, advance and arrival deadline', (tester) async {
    await tester.pumpWidget(dashboard(fixture()));
    await tester.ensureVisible(find.text('Confirmed booking summary'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Confirmed booking summary'));
    await tester.pumpAndSettle();
    expect(find.text('Arrival / setup'), findsOneWidget);
    expect(find.text('Advance'), findsOneWidget);
    expect(find.text('Confirmed'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('only secured standby is labelled confirmed and never as booked', (tester) async {
    final state = fixture();
    final service = (state['services'] as List).first as Map<String, dynamic>;
    final terms = Map<String, dynamic>.from(((service['booking'] as Map)['primary']['confirmation'] as Map)['terms'] as Map);
    terms['purpose'] = 'standby';
    (service['booking'] as Map)['backup'] = {'vendor_id': 'P02', 'status': 'standby', 'secured': true,
      'confirmation': {'id': 2, 'status': 'confirmed', 'purpose': 'standby', 'terms': terms}};
    await tester.pumpWidget(dashboard(state));
    await tester.ensureVisible(find.text('Backup · Standby confirmed'));
    await tester.pumpAndSettle();
    expect(find.text('Backup · Standby confirmed'), findsOneWidget);
    expect(find.text('Available as standby, not booked. No advance is due unless activated.'), findsOneWidget);
    expect(find.text('Confirmed standby terms · not booked'), findsOneWidget);
  });

  testWidgets('legacy standby without confirmation is not shown as secured', (tester) async {
    final state = fixture();
    final service = (state['services'] as List).first as Map<String, dynamic>;
    (service['booking'] as Map)['backup'] = {'vendor_id': 'P02', 'status': 'standby', 'secured': false};
    await tester.pumpWidget(dashboard(state));
    expect(find.text('Backup · Standby not confirmed'), findsOneWidget);
    expect(find.text('Backup · Standby confirmed'), findsNothing);
  });

  testWidgets('replacement pending does not look like an active confirmed vendor', (tester) async {
    final state = fixture();
    final service = (state['services'] as List).first as Map<String, dynamic>;
    final primary = (service['booking'] as Map)['primary'] as Map;
    primary['status'] = 'replacement_pending';
    primary.remove('confirmation');
    (service['booking'] as Map)['backup'] = {'vendor_id': 'P02', 'status': 'awaiting_activation', 'secured': false};
    await tester.pumpWidget(dashboard(state));
    expect(find.text('Replacement needed'), findsOneWidget);
    expect(find.text('Backup · Confirming replacement'), findsOneWidget);
    expect(find.text('The replacement is not active until its booking confirmation is complete.'), findsOneWidget);
    expect(find.text('Confirmed'), findsNothing);
  });

  testWidgets('queued follow-ups disable duplicate Call now actions', (tester) async {
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: FollowupTile(
      checkin: const {'id': 1, 'label': 'D-1 confirmation', 'due_at': '2026-11-19T18:00', 'dispatch_state': 'queued'}, onCall: (_) async {},
    ))));
    expect(find.text('Queued'), findsOneWidget);
    expect(tester.widget<TextButton>(find.widgetWithText(TextButton, 'Queued')).onPressed, isNull);
  });
}
