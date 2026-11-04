import 'dart:async';
import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:web_socket_channel/io.dart';

import 'api.dart';

/// Android PCM16 audio, with bounded server transport and native communication-mic echo control.
class CallAudio {
  static const _native = MethodChannel('bandobast/audio');
  static const _input = EventChannel('bandobast/audio_input');
  IOWebSocketChannel? _socket;
  StreamSubscription<dynamic>? _mic;
  StreamSubscription<dynamic>? _messages;
  bool muted = false;
  bool _closed = false;
  bool _nativeStarted = false;
  bool _ready = false;
  final void Function(Map<String, dynamic>) onMessage;
  final void Function(String) onEnded;

  CallAudio({required this.onMessage, required this.onEnded});

  Future<void> start(Map<String, dynamic> ticket) async {
    if (!await Permission.microphone.request().isGranted) throw ApiError('Microphone permission is needed');
    if (_closed) return;
    final socket = IOWebSocketChannel.connect(Api.audioSocket(ticket['audio_path'] as String),
        headers: {'Authorization': 'Bearer ${ticket['audio_token']}'});
    _socket = socket;
    await socket.ready.timeout(const Duration(seconds: 15));
    if (_closed) return;
    _messages = socket.stream.listen((message) {
      if (_closed) return;
      if (message is List<int>) {
        if (_nativeStarted) _native.invokeMethod<void>('play', Uint8List.fromList(message)).catchError((Object e) => onEnded('$e'));
      } else {
        final data = jsonDecode(message as String) as Map<String, dynamic>;
        if (data['type'] == 'clear') {
          _native.invokeMethod<void>('clear').ignore();
        } else if (data['type'] == 'ended' || data['type'] == 'error') {
          onEnded('${data['reason'] ?? 'Call ended'}');
        } else {
          if (data['type'] == 'connected') _ready = true;
          onMessage(data);
        }
      }
    }, onDone: () => onEnded('Call ended'), onError: (Object e) => onEnded('Connection lost: $e'));
    _mic = _input.receiveBroadcastStream().listen((chunk) {
      if (!_closed && _ready) {
        final audio = chunk as Uint8List;
        socket.sink.add(muted ? Uint8List(audio.length) : audio);
      }
    }, onError: (Object e) => onEnded('Microphone error: $e'));
    final info = await _native.invokeMapMethod<String, dynamic>('start', {'outputRate': ticket['sample_rate_out']});
    _nativeStarted = true;
    if (_closed) {
      await _native.invokeMethod<void>('stop');
      _nativeStarted = false;
      return;
    }
    onMessage({'type': 'audio_ready', 'echoCancellation': info?['echoCancellation'] == true});
  }

  Future<void> speaker(bool on) => _native.invokeMethod<void>('speaker', on);

  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _ready = false;
    await _mic?.cancel();
    await _messages?.cancel();
    try {
      _socket?.sink.add(jsonEncode({'type': 'hangup'}));
      await _socket?.sink.close().timeout(const Duration(seconds: 2));
    } catch (_) {}
    if (_nativeStarted) {
      try {
        await _native.invokeMethod<void>('stop');
      } catch (_) {
        // Cleanup must still let the call screen close after a native audio failure.
      }
      _nativeStarted = false;
    }
  }
}
