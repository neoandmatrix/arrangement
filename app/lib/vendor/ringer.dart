import 'dart:async';
import 'dart:convert';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:uuid/uuid.dart';
import 'package:web_socket_channel/io.dart';

import '../api.dart';
import '../settings.dart';

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

  IOWebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _ping;
  Timer? _retry;
  String? _vendorId;
  final List<StreamSubscription<dynamic>> _pushSubscriptions = [];

  Future<void> start(String vendorId) async {
    if (_vendorId == vendorId) return;
    stop();
    _vendorId = vendorId;
    try {
      await _register(vendorId, null);
    } catch (e) {
      debugPrint('Demo registration failed: $e');
    }
    if (_vendorId != vendorId) return;
    await FlutterCallkitIncoming.requestNotificationPermission({
      'title': 'Notifications',
      'rationaleMessagePermission': 'Bandobast needs notifications to show incoming calls.',
      'postNotificationMessageRequired': 'Allow notifications in Settings to receive calls.',
    });
    if (!await FlutterCallkitIncoming.canUseFullScreenIntent()) {
      await FlutterCallkitIncoming.requestFullIntentPermission();
    }
    await _registerPush(vendorId);
    if (_vendorId == vendorId) _connect();
  }

  void stop() {
    _vendorId = null;
    _retry?.cancel();
    _ping?.cancel();
    _sub?.cancel();
    _channel?.sink.close();
    for (final sub in _pushSubscriptions) {
      sub.cancel();
    }
    _pushSubscriptions.clear();
    connected.value = false;
    pushEnabled.value = false;
  }

  void _connect() {
    final vendorId = _vendorId;
    if (vendorId == null) return;
    _sub?.cancel();
    _channel?.sink.close();
    final channel = IOWebSocketChannel.connect(Api.presenceSocket(vendorId), headers: {'X-App-Key': Settings.appKey});
    _channel = channel;
    channel.ready.then((_) {
      if (_channel != channel || _vendorId != vendorId) return;
      connected.value = true;
      _ping?.cancel();
      _ping = Timer.periodic(const Duration(seconds: 20), (_) => channel.sink.add(jsonEncode({'type': 'ping'})));
    }).catchError((_) {
      if (_channel == channel) _scheduleRetry();
    });
    _sub = channel.stream.listen(
      (raw) {
        final message = jsonDecode(raw as String) as Map<String, dynamic>;
        if (message['type'] == 'ring') showIncoming(message);
        if (message['type'] == 'cancel') hideIncoming(message['call_id'] as String);
      },
      onDone: () { if (_channel == channel) _scheduleRetry(); },
      onError: (_) { if (_channel == channel) _scheduleRetry(); },
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
      if (_vendorId != vendorId) return;
      _pushSubscriptions.add(FirebaseMessaging.onMessage.listen((m) => handlePush(m.data)));
      // ignore: deprecated_member_use
      _pushSubscriptions.add(FirebaseMessaging.instance.onTokenRefresh.listen((t) => _register(vendorId, t)));
    } catch (e) {
      debugPrint('Push not available (no google-services.json?): $e');
    }
    if (_vendorId != vendorId) return;
    pushEnabled.value = token != null;
    try {
      await _register(vendorId, token);
    } catch (e) {
      debugPrint('Register failed: $e');
    }
  }

  Future<void> _register(String id, String? token) => id.startsWith('F') && id.length == 9
      ? Api.post('/participants/register', {'participant_id': id, 'push_token': token}).then((_) {})
      : Api.post('/vendor/register', {'vendor_id': id, 'push_token': token}).then((_) {});
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
  'direct': 'Direct family–vendor call',
  'enquiry': 'Enquiry for a booking',
  'renegotiation': 'About your quote',
  'checkin': 'Booking follow-up',
};

/// Show the full-screen incoming call, once per call (it may arrive by socket and push).
final _showing = <String>{};

Future<void> showIncoming(Map<String, dynamic> ring) async {
  final callId = ring['call_id'] as String;
  if (!_showing.add(callId)) return;
  try {
  final active = await FlutterCallkitIncoming.activeCalls();
  if (active.any((c) => c.extra?['call_id'] == callId)) return;
  final seconds = int.tryParse('${ring['ring_seconds'] ?? 40}') ?? 40;
  await FlutterCallkitIncoming.showCallkitIncoming(CallKitParams(
    id: const Uuid().v4(),
    nameCaller: ring['kind'] == 'direct' ? '${ring['caller_name'] ?? ring['caller_id']}' : 'Bandobast · ${ring['family'] ?? ''}',
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
  } finally {
    _showing.remove(callId);
  }
}

Future<void> hideIncoming(String callId) async {
  for (final c in await FlutterCallkitIncoming.activeCalls()) {
    if (c.extra?['call_id'] == callId && !c.isAccepted) await FlutterCallkitIncoming.endCall(c.id);
  }
}
