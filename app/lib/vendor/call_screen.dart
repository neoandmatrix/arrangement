import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:permission_handler/permission_handler.dart';

import '../api.dart';
import '../format.dart';

/// A call with Bandobast, answered from the incoming-call screen.
///
/// The phone sends a WebRTC offer to /app/calls/{id}/answer; the server answers and runs the
/// same voice pipeline (Gnani Prisma STT -> LLM -> Gnani Timbre TTS) as every other channel.
/// Bandobast hangs up when the conversation is done, which closes the connection here.
class CallScreen extends StatefulWidget {
  const CallScreen({super.key, required this.ring, required this.callkitId});

  final Map<String, dynamic> ring;
  final String callkitId;

  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  RTCPeerConnection? _pc;
  MediaStream? _mic;
  String _status = 'Connecting…';
  bool _muted = false;
  bool _speaker = false;
  bool _ended = false;
  final _clock = Stopwatch();
  Timer? _tick;

  String get _callId => widget.ring['call_id'] as String;

  @override
  void initState() {
    super.initState();
    _connect();
  }

  List<dynamic> _iceServers() {
    final raw = widget.ring['ice_servers'];
    final parsed = raw is String ? jsonDecode(raw) : raw;
    return parsed is List && parsed.isNotEmpty
        ? parsed
        : [
            {'urls': 'stun:stun.l.google.com:19302'}
          ];
  }

  Future<void> _connect() async {
    try {
      if (!await Permission.microphone.request().isGranted) {
        throw 'Microphone permission is needed to take the call';
      }
      final pc = await createPeerConnection({'iceServers': _iceServers(), 'sdpSemantics': 'unified-plan'});
      _pc = pc;
      final gathered = Completer<void>();
      pc.onIceGatheringState = (state) {
        if (state == RTCIceGatheringState.RTCIceGatheringStateComplete && !gathered.isCompleted) gathered.complete();
      };
      pc.onConnectionState = (state) {
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
          _clock.start();
          _tick = Timer.periodic(const Duration(seconds: 1), (_) => mounted ? setState(() {}) : null);
          setState(() => _status = 'On call');
          FlutterCallkitIncoming.setCallConnected(widget.callkitId);
        }
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateFailed ||
            state == RTCPeerConnectionState.RTCPeerConnectionStateClosed) {
          _end(state == RTCPeerConnectionState.RTCPeerConnectionStateFailed ? 'Connection lost' : 'Call ended');
        }
      };
      _mic = await navigator.mediaDevices.getUserMedia({
        'audio': {'echoCancellation': true, 'noiseSuppression': true, 'autoGainControl': true},
        'video': false,
      });
      for (final track in _mic!.getAudioTracks()) {
        await pc.addTrack(track, _mic!);
      }
      final offer = await pc.createOffer({'offerToReceiveAudio': true, 'offerToReceiveVideo': false});
      await pc.setLocalDescription(offer);
      // Send the offer once our network candidates are in it (no trickle needed).
      await gathered.future.timeout(const Duration(seconds: 4), onTimeout: () {});
      final local = await pc.getLocalDescription();
      final answer = await Api.post('/calls/$_callId/answer', {'sdp': local!.sdp, 'type': local.type});
      await pc.setRemoteDescription(RTCSessionDescription(answer['sdp'] as String, answer['type'] as String));
      await Helper.setSpeakerphoneOn(_speaker);
      if (mounted) setState(() => _status = 'Connecting audio…');
    } catch (e) {
      _end('Could not connect: $e');
    }
  }

  Future<void> _end(String why) async {
    if (_ended) return;
    _ended = true;
    _clock.stop();
    _tick?.cancel();
    await FlutterCallkitIncoming.endCall(widget.callkitId);
    for (final t in _mic?.getTracks() ?? <MediaStreamTrack>[]) {
      await t.stop();
    }
    await _mic?.dispose();
    await _pc?.close();
    if (!mounted) return;
    setState(() => _status = why);
    await Future.delayed(const Duration(seconds: 2));
    if (mounted && Navigator.canPop(context)) Navigator.pop(context);
  }

  @override
  void dispose() {
    _end('Call ended');
    super.dispose();
  }

  String get _duration {
    final s = _clock.elapsed.inSeconds;
    return '${(s ~/ 60).toString().padLeft(2, '0')}:${(s % 60).toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final ring = widget.ring;
    final service = serviceNames[ring['category']] ?? '';
    return PopScope(
      canPop: _ended,
      child: Scaffold(
        backgroundColor: const Color(0xFF5B2A12),
        body: SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              children: [
                const SizedBox(height: 40),
                const CircleAvatar(
                  radius: 44,
                  backgroundColor: Color(0xFFF2B33D),
                  child: Icon(Icons.celebration, size: 44, color: Color(0xFF5B2A12)),
                ),
                const SizedBox(height: 20),
                Text('Bandobast', style: Theme.of(context).textTheme.headlineMedium?.copyWith(color: Colors.white)),
                const SizedBox(height: 6),
                Text(
                  'For the ${ring['family'] ?? 'family'} · ${ring['function'] ?? ''}',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Colors.white70, fontSize: 16),
                ),
                if (service.isNotEmpty)
                  Text('$service · ${prettyDate(ring['date'] as String?)} · ${ring['area'] ?? ''}',
                      textAlign: TextAlign.center, style: const TextStyle(color: Colors.white54)),
                const SizedBox(height: 28),
                Text(_clock.isRunning ? _duration : _status,
                    style: const TextStyle(color: Colors.white, fontSize: 20, fontFeatures: [FontFeature.tabularFigures()])),
                if (_clock.isRunning) Text(_status, style: const TextStyle(color: Colors.white54)),
                const Spacer(),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    _RoundButton(
                      icon: _muted ? Icons.mic_off : Icons.mic,
                      label: _muted ? 'Unmute' : 'Mute',
                      onPressed: () {
                        setState(() => _muted = !_muted);
                        for (final t in _mic?.getAudioTracks() ?? <MediaStreamTrack>[]) {
                          t.enabled = !_muted;
                        }
                      },
                    ),
                    _RoundButton(
                      icon: Icons.call_end,
                      label: 'Hang up',
                      color: const Color(0xFFC62828),
                      onPressed: () => _end('Call ended'),
                    ),
                    _RoundButton(
                      icon: _speaker ? Icons.volume_up : Icons.hearing,
                      label: _speaker ? 'Speaker' : 'Earpiece',
                      onPressed: () {
                        setState(() => _speaker = !_speaker);
                        Helper.setSpeakerphoneOn(_speaker);
                      },
                    ),
                  ],
                ),
                const SizedBox(height: 32),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _RoundButton extends StatelessWidget {
  const _RoundButton({required this.icon, required this.label, required this.onPressed, this.color});

  final IconData icon;
  final String label;
  final VoidCallback onPressed;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Material(
          color: color ?? Colors.white24,
          shape: const CircleBorder(),
          child: IconButton(
            iconSize: 32,
            padding: const EdgeInsets.all(18),
            onPressed: onPressed,
            icon: Icon(icon, color: Colors.white),
          ),
        ),
        const SizedBox(height: 8),
        Text(label, style: const TextStyle(color: Colors.white70)),
      ],
    );
  }
}
