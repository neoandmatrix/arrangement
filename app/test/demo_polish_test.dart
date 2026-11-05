import 'dart:convert';

import 'package:bandobast/api.dart';
import 'package:bandobast/demo_details.dart';
import 'package:bandobast/demo_guide_screen.dart';
import 'package:bandobast/family/new_function_screen.dart';
import 'package:bandobast/family/progress_summary.dart';
import 'package:bandobast/settings.dart';
import 'package:bandobast/vendor/call_layout.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await Settings.load();
    await Settings.setServer('http://synthetic.invalid');
    await Settings.setRole('family');
  });
  tearDown(() => Api.client.close());

  test('sample defaults use the venue date and contain no pin, quote or booked state', () {
    final sample = demoDetails(now: DateTime.parse('2026-11-05T23:30:00Z'));
    expect(sample['date'], '2026-11-10');
    expect(sample['needs'], ['caterer']);
    expect(sample.containsKey('location'), isFalse);
    expect(sample.containsKey('quote'), isFalse);
    expect(sample.containsKey('booking'), isFalse);
    expect(sample.containsKey('area'), isFalse);
  });

  testWidgets('selected or legacy active vendor is not counted as confirmed without evidence', (tester) async {
    await tester.pumpWidget(const MaterialApp(home: Scaffold(body: ProgressSummary(services: [
      {'booking': {'primary': {'status': 'active'}, 'backup': {'status': 'standby', 'secured': false}}, 'vendors': []},
    ], pendingReviews: 0))));
    expect(find.text('Confirmed 0/1'), findsOneWidget);
    expect(find.text('Secured backups 0'), findsOneWidget);
    expect(find.text('Waiting for vendor booking confirmation'), findsOneWidget);
  });

  testWidgets('confirmed receipt and secured backup give truthful completed progress', (tester) async {
    await tester.pumpWidget(const MaterialApp(home: Scaffold(body: ProgressSummary(services: [
      {'booking': {'primary': {'status': 'active', 'confirmation': {'status': 'confirmed'}}, 'backup': {'secured': true}}, 'vendors': []},
    ], pendingReviews: 0))));
    expect(find.text('Confirmed 1/1'), findsOneWidget);
    expect(find.text('Secured backups 1'), findsOneWidget);
    expect(find.text('Bookings confirmed; follow-ups are scheduled'), findsOneWidget);
  });

  testWidgets('guide explains synthetic live calls and is not a one-click fake-success demo', (tester) async {
    await tester.pumpWidget(const MaterialApp(home: DemoGuideScreen()));
    expect(find.text('Prepare three devices'), findsOneWidget);
    expect(find.text('Open sample form'), findsNothing);
    expect(find.textContaining('actual role-play calls'), findsOneWidget);
  });

  testWidgets('sample form fills only answers and still requires manual venue selection', (tester) async {
    final requests = <String>[];
    Api.client = MockClient((request) async {
      requests.add(request.url.path);
      return http.Response(jsonEncode({
        'functions': [{'key': 'griha_pravesh', 'name': 'Griha pravesh', 'variants': [{'key': 'griha_pravesh', 'name': 'Griha pravesh'}], 'default_needs': ['caterer'], 'optional': []}],
        'services': [{'key': 'caterer', 'name': 'Caterer', 'details_hint': null, 'negotiable': true}],
        'languages': [{'code': 'hi-IN', 'name': 'Hindi'}], 'tent_suggested_above_guests': 60,
      }), 200);
    });
    await tester.pumpWidget(MaterialApp(home: NewFunctionScreen(initialDraft: demoDetails())));
    await tester.pump();
    await tester.pump();
    expect(requests, ['/app/catalog']);
    await tester.scrollUntilVisible(find.text('Tap the map to pin the venue.'), 160,
        scrollable: find.descendant(of: find.byType(ListView).first, matching: find.byType(Scrollable)).first);
    await tester.pumpAndSettle();
    expect(find.text('Tap the map to pin the venue.'), findsOneWidget);
    await tester.tap(find.text('Find and call vendors'));
    await tester.pump();
    expect(find.textContaining('pin the venue on the map'), findsOneWidget);
    expect(requests, ['/app/catalog']);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('call content scrolls instead of overflowing on short screens with long status text', (tester) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(home: MediaQuery(
      data: const MediaQueryData(size: Size(320, 480), textScaler: TextScaler.linear(1.4)),
      child: Scaffold(body: CallLayout(children: [
        const SizedBox(height: 150), const Text('Confirming a replacement booking for the family'),
        const Text('Long quoted details and an explanatory connection status remain readable without pushing the hang-up controls off an unscrollable page.'),
        const Spacer(), FilledButton(onPressed: () {}, child: const Text('Hang up')),
      ])),
    )));
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(find.text('Hang up'));
    await tester.pumpAndSettle();
    expect(find.text('Hang up').hitTestable(), findsOneWidget);
  });
}
