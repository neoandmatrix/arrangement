import 'dart:convert';

import 'package:bandobast/api.dart';
import 'package:bandobast/family/new_function_screen.dart';
import 'package:bandobast/family/vendor_update_card.dart';
import 'package:bandobast/family/quote_details.dart';
import 'package:bandobast/settings.dart';
import 'package:bandobast/setup_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await Settings.load();
    await Settings.setServer('http://demo.invalid');
    await Settings.setAppKey('synthetic-test-key');
    await Settings.setRole('family');
  });

  tearDown(() => Api.client.close());

  test('family ID is stable across launches and distinct from vendor identity', () async {
    final familyId = Settings.familyId;
    expect(RegExp(r'^F[0-9a-f]{8}$').hasMatch(familyId), isTrue);
    await Settings.load();
    expect(Settings.familyId, familyId);
    await Settings.setVendor('C01', 'Demo Caterer');
    await Settings.setRole('vendor');
    expect(Settings.participantId, 'C01');
    await Settings.setRole('family');
    expect(Settings.participantId, familyId);
  });

  testWidgets('selection screen keeps both testing roles', (tester) async {
    await tester.pumpWidget(const MaterialApp(home: SetupScreen()));
    expect(find.text('I’m planning a function'), findsNothing);
    expect(find.text("I'm planning a function"), findsOneWidget);
    expect(find.text("I'm a vendor"), findsOneWidget);
    expect(find.text('Test connection'), findsOneWidget);
  });

  testWidgets('planning form offers voice without replacing the venue map', (tester) async {
    Api.client = MockClient((request) async => http.Response(jsonEncode({
      'functions': [
        {'key': 'griha_pravesh', 'name': 'Griha pravesh', 'variants': [{'key': 'griha_pravesh', 'name': 'Griha pravesh'}],
          'default_needs': ['priest'], 'optional': []}
      ],
      'services': [{'key': 'priest', 'name': 'Priest', 'details_hint': null, 'negotiable': false}],
      'languages': [{'code': 'hi-IN', 'name': 'Hindi'}, {'code': 'kn-IN', 'name': 'Kannada'}],
      'tent_suggested_above_guests': 60,
    }), 200));
    await tester.pumpWidget(const MaterialApp(home: NewFunctionScreen()));
    await tester.pump();
    await tester.pump();
    expect(find.text('Fill with voice'), findsOneWidget);
    expect(find.text('Conversation language'), findsOneWidget);
    expect(find.text('Where is it?'), findsOneWidget);
    expect(find.text('Find and call vendors'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    await tester.pump();
  });

  testWidgets('price revision has separate approval and rejection actions', (tester) async {
    String? decisionPath;
    bool refreshed = false;
    Api.client = MockClient((request) async {
      decisionPath = request.url.path;
      expect(request.headers['X-Participant-ID'], Settings.familyId);
      return http.Response('{}', 200);
    });
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: VendorUpdateCard(
      update: {
        'id': 7, 'vendor_id': 'K01', 'kind': 'price', 'status': 'pending',
        'data': {'notes': 'New cake price', 'previous_price': 2400, 'quote': {'price_amount': 2800, 'price_unit': 'total'}},
      },
      services: const [],
      onRefresh: () async { refreshed = true; },
    ))));
    expect(find.text('Existing terms stay unchanged until you approve.'), findsOneWidget);
    expect(find.text('Reject revision'), findsOneWidget);
    await tester.tap(find.text('Approve revised price'));
    await tester.pumpAndSettle();
    expect(decisionPath, '/app/updates/7/approve');
    expect(refreshed, isTrue);
  });

  testWidgets('unsafe quote corrections disable approval and offer price rechecking', (tester) async {
    String? rechecked;
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: VendorUpdateCard(
      update: {'id': 8, 'vendor_id': 'K01', 'kind': 'quote', 'status': 'pending', 'data': {
        'notes': 'Corrected delivery charge', 'previous_price': 2400,
        'previous_quote': {'price_amount': 2400, 'transport_charge': 100},
        'quote': {'price_amount': 2400, 'transport_charge': 500000, 'price_unit': 'total'},
        'flags': [{'level': 'block', 'text': 'Transport needs a separate vendor repeat'}],
      }},
      services: const [], onRefresh: () async {}, onReconfirm: (id) async { rechecked = id; },
    ))));
    final approve = tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Approve corrected quote'));
    expect(approve.onPressed, isNull);
    await tester.tap(find.text('Reconfirm prices'));
    await tester.pumpAndSettle();
    expect(rechecked, 'K01');
  });

  testWidgets('invalid legacy amount types remain readable without being converted', (tester) async {
    await tester.pumpWidget(const MaterialApp(home: Scaffold(body: SingleChildScrollView(child: QuoteDetails(
      vendor: {'vendor_id': 'K01', 'quote': {'price_amount': 'unknown', 'transport_charge': 'not-a-number'}, 'flags': []},
    )))));
    await tester.tap(find.text('Price breakdown & details'));
    await tester.pumpAndSettle();
    expect(find.text('Invalid amount'), findsNWidgets(2));
    expect(tester.takeException(), isNull);
  });
}
