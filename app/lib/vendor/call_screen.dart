import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';

import '../api.dart';
import '../audio.dart';
import '../format.dart';

/// A call with Bandobast, answered from the incoming-call screen.
///
/// PCM audio travels through an authenticated WebSocket, without ICE or TURN.
/// AI calls use the shared Gnani pipeline; direct calls relay audio to the booked party.
class CallScreen extends StatefulWidget {
  const CallScreen({super.key, required this.ring, this.callkitId, this.ticket, this.onForm});

  final Map<String, dynamic> ring;
  final String? callkitId;
  final Map<String, dynamic>? ticket;
  final void Function(Map<String, dynamic>)? onForm;

  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  CallAudio? _audio;
  String _status = 'Connecting…';
  bool _muted = false;
  bool _speaker = false;
  bool _ended = false;
  final _clock = Stopwatch();
  Timer? _tick;
  StreamSubscription<CallEvent?>? _callkitEvents;
  String? _notice;
  Map<String, dynamic>? _draft;

  String get _callId => widget.ring['call_id'] as String;

  @override
  void initState() {
    super.initState();
    if (widget.callkitId != null) {
      _callkitEvents = FlutterCallkitIncoming.onEvent.listen((event) {
        if (event is CallEventActionCallEnded && event.callKitParams.id == widget.callkitId) _end('Call ended');
      });
    }
    _connect();
  }

  Future<void> _connect() async {
    try {
      final ticket = widget.ticket ?? await Api.post('/calls/$_callId/connect') as Map<String, dynamic>;
      if (_ended) return;
      final audio = CallAudio(onMessage: _message, onEnded: (why) => _end(why));
      _audio = audio;
      await audio.start(ticket);
    } catch (e) {
      _end('Could not connect: $e');
    }
  }

  void _message(Map<String, dynamic> message) {
    if (!mounted || _ended) return;
    if (message['type'] == 'connected') {
      if (!_clock.isRunning) {
        _clock.start();
        _tick = Timer.periodic(const Duration(seconds: 1), (_) => mounted ? setState(() {}) : null);
      }
      if (widget.callkitId != null) FlutterCallkitIncoming.setCallConnected(widget.callkitId!);
      setState(() => _status = 'On call');
    } else if (message['type'] == 'ringing') {
      setState(() => _status = 'Ringing…');
    } else if (message['type'] == 'form') {
      _draft = Map<String, dynamic>.from(message['draft'] as Map);
      widget.onForm?.call(_draft!);
      setState(() => _notice = message['complete'] == true
          ? 'Details filled. Review the form and pin the venue before submitting.'
          : 'Your answers are filling the form. You can review and edit afterwards.');
    } else if (message['type'] == 'update_saved') {
      setState(() => _notice = 'Update sent to the family. Price changes still need their approval.');
    } else if (message['type'] == 'booking_confirmed') {
      setState(() => _notice = 'Booking confirmed. The agreed terms are saved for both sides.');
    } else if (message['type'] == 'standby_confirmed') {
      setState(() => _notice = 'Standby availability confirmed. You are not booked and no advance is due now.');
    } else if (message['type'] == 'booking_pending_review') {
      setState(() => _notice = 'Changed terms need family review. The booking is not confirmed.');
    } else if (message['type'] == 'booking_declined') {
      setState(() => _notice = 'Booking declined or withdrawn. The family has been notified.');
    } else if (message['type'] == 'audio_ready' && message['echoCancellation'] != true) {
      setState(() => _notice ??= 'Use the earpiece or headphones to reduce echo on this phone.');
    }
  }

  Future<void> _end(String why) async {
    if (_ended) return;
    _ended = true;
    _clock.stop();
    _tick?.cancel();
    await _callkitEvents?.cancel();
    if (widget.callkitId != null) {
      try {
        await FlutterCallkitIncoming.endCall(widget.callkitId!);
      } catch (_) {}
    }
    await _audio?.close();
    Api.post('/sessions/$_callId/end').ignore();
    if (!mounted) return;
    setState(() => _status = why);
    await Future.delayed(const Duration(seconds: 2));
    if (mounted && Navigator.canPop(context)) Navigator.pop(context, _draft);
  }

  @override
  void dispose() {
    _tick?.cancel();
    _callkitEvents?.cancel();
    _audio?.close();
    if (!_ended) Api.post('/sessions/$_callId/end').ignore();
    super.dispose();
  }

  String get _duration {
    final s = _clock.elapsed.inSeconds;
    return '${(s ~/ 60).toString().padLeft(2, '0')}:${(s % 60).toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final ring = widget.ring;
    final direct = ring['kind'] == 'direct';
    final form = ring['purpose'] == 'form';
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
                Text(direct ? '${ring['peer_name'] ?? ring['caller_name'] ?? ring['caller_id'] ?? 'Direct call'}' : 'Bandobast',
                    style: Theme.of(context).textTheme.headlineMedium?.copyWith(color: Colors.white), textAlign: TextAlign.center),
                const SizedBox(height: 6),
                Text(
                  direct ? 'Direct family–vendor call · ${ring['peer_id'] ?? ring['caller_id'] ?? ''}'
                      : form ? 'Let’s fill your planning form together'
                      : ring['purpose'] == 'vendor_update' ? 'Tell us what has changed'
                      : 'For the ${ring['family'] ?? 'family'} · ${ring['function'] ?? ''}',
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
                const SizedBox(height: 16),
                Text(_notice ?? (direct ? 'Speak directly. This call is not transcribed or recorded.'
                    : form ? 'One question at a time. Your venue stays selected on the map.'
                    : 'Speak naturally, in your language.'),
                    textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70)),
                const Spacer(),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    _RoundButton(
                      icon: _muted ? Icons.mic_off : Icons.mic,
                      label: _muted ? 'Unmute' : 'Mute',
                      onPressed: () {
                        setState(() => _muted = !_muted);
                        _audio?.muted = _muted;
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
                        _audio?.speaker(_speaker);
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
