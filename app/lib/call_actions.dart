import 'package:flutter/material.dart';

import 'api.dart';
import 'vendor/call_screen.dart';

bool _starting = false;

/// Shared outgoing-call navigation keeps family and vendor actions consistent.
Future<bool> callAssistant(BuildContext context, {required String mode, String language = 'hi-IN',
    Map<String, dynamic>? draft, void Function(Map<String, dynamic>)? onForm}) async {
  if (_starting) return false;
  _starting = true;
  try {
    final ticket = await Api.post('/assistant-calls', {'mode': mode, 'language': language, 'draft': draft ?? {}}) as Map<String, dynamic>;
    if (!context.mounted) {
      Api.post('/sessions/${ticket['call_id']}/end').ignore();
      return false;
    }
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => CallScreen(
      ring: {'call_id': ticket['call_id'], 'purpose': mode}, ticket: ticket, onForm: onForm,
    )));
    return true;
  } catch (e) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
    return false;
  } finally {
    _starting = false;
  }
}

Future<void> callPerson(BuildContext context, String targetId, String name) async {
  if (_starting) return;
  _starting = true;
  try {
    final ticket = await Api.post('/direct-calls', {'target_id': targetId}) as Map<String, dynamic>;
    if (!context.mounted) {
      Api.post('/sessions/${ticket['call_id']}/end').ignore();
      return;
    }
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => CallScreen(
      ring: {'call_id': ticket['call_id'], 'kind': 'direct', 'peer_name': name, 'peer_id': targetId}, ticket: ticket,
    )));
  } catch (e) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
  } finally {
    _starting = false;
  }
}
