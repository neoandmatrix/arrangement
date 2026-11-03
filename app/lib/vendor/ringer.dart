import 'dart:async';
import 'dart:convert';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:uuid/uuid.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../api.dart';

/// How a vendor's phone gets Bandobast's calls.
///
/// - While the app runs: a live WebSocket to the server; a "ring" message shows the
///   full-screen incoming-call UI.
/// - When the app is closed: a Firebase push wakes it and shows the same UI (needs
///   google-services.json in android/app; without it, only the live connection rings).
class Ringer {
  Ringer._();
  static final instance = Ringer._();

  final connected = ValueNotifier<bool>(false);
  final pushEnabled = ValueNotifier<bool>(false);

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _ping;
  Timer? _retry;
  String? _vendorId;

  Future<void> start(String vendorId) async {
    _vendorId = vendorId;
    await FlutterCallkitIncoming.requestNotificationPermission({
      'title': 'Notifications',
      'rationaleMessagePermission': 'Bandobast needs notifications to show incoming calls.',
      'postNotificationMessageRequired': 'Allow notifications in Settings to receive calls.',
    });
    if (!await FlutterCallkitIncoming.canUseFullScreenIntent()) {
      await FlutterCallkitIncoming.requestFullIntentPermission();
    }
    await _registerPush(vendorId);
    _connect();
  }

  void stop() {
    _vendorId = null;
    _retry?.cancel();
    _ping?.cancel();
    _sub?.cancel();
    _channel?.sink.close();
    connected.value = false;
  }

  void _connect() {
    final vendorId = _vendorId;
    if (vendorId == null) return;
    _sub?.cancel();
    final channel = WebSocketChannel.connect(Api.vendorSocket(vendorId));
    _channel = channel;
    channel.ready.then((_) {
      connected.value = true;
      _ping?.cancel();
      _ping = Timer.periodic(const Duration(seconds: 20), (_) => channel.sink.add(jsonEncode({'type': 'ping'})));
    }).catchError((_) => _scheduleRetry());
    _sub = channel.stream.listen(
      (raw) {
        final message = jsonDecode(raw as String) as Map<String, dynamic>;
        if (message['type'] == 'ring') showIncoming(message);
        if (message['type'] == 'cancel') hideIncoming(message['call_id'] as String);
      },
      onDone: _scheduleRetry,
      onError: (_) => _scheduleRetry(),
    );
  }

  void _scheduleRetry() {
    connected.value = false;
    _ping?.cancel();
    _retry?.cancel();
    if (_vendorId != null) _retry = Timer(const Duration(seconds: 3), _connect);
  }

  Future<void> _registerPush(String vendorId) async {
    String? token;
    try {
      await Firebase.initializeApp();
      await FirebaseMessaging.instance.requestPermission();
      // The classic registration token, which the server's FCM v1 send expects. (register()/onRegistered
      // return a Firebase Installation ID instead.)
      // ignore: deprecated_member_use
      token = await FirebaseMessaging.instance.getToken();
      FirebaseMessaging.onMessage.listen((m) => handlePush(m.data));
      // ignore: deprecated_member_use
      FirebaseMessaging.instance.onTokenRefresh.listen((t) => Api.post('/vendor/register', {'vendor_id': vendorId, 'push_token': t}));
    } catch (e) {
      debugPrint('Push not available (no google-services.json?): $e');
    }
    pushEnabled.value = token != null;
    try {
      await Api.post('/vendor/register', {'vendor_id': vendorId, 'push_token': token});
    } catch (e) {
      debugPrint('Register failed: $e');
    }
  }
}

/// Firebase push while the app is closed or in the background.
@pragma('vm:entry-point')
Future<void> firebaseBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
  await handlePush(message.data);
}

Future<void> handlePush(Map<String, dynamic> data) async {
  if (data['type'] == 'ring') await showIncoming(data);
  if (data['type'] == 'cancel') await hideIncoming(data['call_id'] as String);
}

const _purpose = {
  'enquiry': 'Enquiry for a booking',
  'renegotiation': 'About your quote',
  'checkin': 'Booking follow-up',
};

/// Show the full-screen incoming call, once per call (it may arrive by socket and push).
Future<void> showIncoming(Map<String, dynamic> ring) async {
  final callId = ring['call_id'] as String;
  final active = await FlutterCallkitIncoming.activeCalls();
  if (active.any((c) => c.extra?['call_id'] == callId)) return;
  final seconds = int.tryParse('${ring['ring_seconds'] ?? 40}') ?? 40;
  await FlutterCallkitIncoming.showCallkitIncoming(CallKitParams(
    id: const Uuid().v4(),
    nameCaller: 'Bandobast · ${ring['family'] ?? ''}',
    appName: 'Bandobast',
    handle: '${_purpose[ring['purpose']] ?? 'Call'} · ${ring['function'] ?? ''}',
    type: 0,
    duration: seconds * 1000,
    extra: ring.map((k, v) => MapEntry(k, v is String ? v : jsonEncode(v))),
    missedCallNotification: const NotificationParams(
      showNotification: true,
      subtitle: 'Missed call from Bandobast',
      isShowCallback: false,
    ),
    android: const AndroidParams(
      isCustomNotification: true,
      isShowLogo: false,
      backgroundColor: '#5B2A12',
      actionColor: '#2E7D32',
      textColor: '#FFFFFF',
      isShowFullLockedScreen: true,
      textAccept: 'Answer',
      textDecline: 'Decline',
      incomingCallNotificationChannelName: 'Bandobast calls',
      missedCallNotificationChannelName: 'Missed Bandobast calls',
    ),
  ));
}

Future<void> hideIncoming(String callId) async {
  for (final c in await FlutterCallkitIncoming.activeCalls()) {
    if (c.extra?['call_id'] == callId && !c.isAccepted) await FlutterCallkitIncoming.endCall(c.id);
  }
}
