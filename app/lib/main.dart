import 'dart:convert';

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';

import 'api.dart';
import 'family/function_screen.dart';
import 'settings.dart';
import 'setup_screen.dart';
import 'vendor/call_screen.dart';
import 'vendor/ringer.dart';
import 'vendor/vendor_home.dart';

final navigatorKey = GlobalKey<NavigatorState>();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Settings.load();
  try {
    FirebaseMessaging.onBackgroundMessage(firebaseBackgroundHandler);
  } catch (_) {
    // No Firebase config in this build: calls ring over the live connection only.
  }
  FlutterCallkitIncoming.onEvent.listen(_onCallEvent);
  runApp(const BandobastApp());
  // Answered from the lock screen while the app was closed: open the call now.
  WidgetsBinding.instance.addPostFrameCallback((_) => _openAcceptedCall());
}

Map<String, dynamic> _ringOf(CallKitParams params) {
  final extra = Map<String, dynamic>.from(params.extra ?? {});
  final ice = extra['ice_servers'];
  if (ice is String) {
    try {
      extra['ice_servers'] = jsonDecode(ice);
    } catch (_) {}
  }
  return extra;
}

final _opened = <String>{};

void _openCall(CallKitParams params) {
  if (!_opened.add(params.id)) return;
  navigatorKey.currentState?.push(MaterialPageRoute(builder: (_) => CallScreen(ring: _ringOf(params), callkitId: params.id)));
}

Future<void> _openAcceptedCall() async {
  for (final call in await FlutterCallkitIncoming.activeCalls()) {
    if (call.isAccepted) _openCall(call);
  }
}

void _onCallEvent(CallEvent? event) {
  switch (event) {
    case CallEventActionCallAccept(:final callKitParams):
      _openCall(callKitParams);
    case CallEventActionCallDecline(:final callKitParams):
      final callId = callKitParams.extra?['call_id'];
      if (callId != null) Api.post('/calls/$callId/decline').ignore();
    default:
      break;
  }
}

class BandobastApp extends StatelessWidget {
  const BandobastApp({super.key});

  @override
  Widget build(BuildContext context) {
    final scheme = ColorScheme.fromSeed(seedColor: const Color(0xFFB5481D));
    return MaterialApp(
      title: 'Bandobast',
      navigatorKey: navigatorKey,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorScheme: scheme, useMaterial3: true, scaffoldBackgroundColor: const Color(0xFFFAF7F2)),
      home: !Settings.configured
          ? const SetupScreen()
          : Settings.role == 'vendor'
              ? const VendorHome()
              : const FunctionScreen(),
    );
  }
}
