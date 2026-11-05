import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../settings.dart';
import '../setup_screen.dart';
import '../vendor/ringer.dart';
import '../demo_guide_screen.dart';
import 'family_dashboard.dart';
import 'new_function_screen.dart';

/// Polling and actions stay here; the dashboard only organizes the existing function data.
class FunctionScreen extends StatefulWidget {
  const FunctionScreen({super.key});

  @override
  State<FunctionScreen> createState() => _FunctionScreenState();
}

class _FunctionScreenState extends State<FunctionScreen> {
  Map<String, dynamic>? _state;
  String? _error;
  bool _loaded = false;
  Timer? _poll;
  final Set<String> _busy = {};

  @override
  void initState() {
    super.initState();
    Ringer.instance.start(Settings.familyId);
    _load();
    _poll = Timer.periodic(const Duration(seconds: 3), (_) => _load());
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final state = await Api.get('/events/current') as Map<String, dynamic>;
      if (!mounted) return;
      setState(() {
        _state = state;
        _error = null;
        _loaded = true;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loaded = true;
      });
    }
  }

  Future<void> _act(String vendorId, String action, String done) async {
    setState(() => _busy.add(vendorId));
    try {
      await Api.post('/events/current/vendors/$vendorId/$action');
      _snack(done);
      await _load();
    } catch (e) {
      _snack('$e');
    } finally {
      if (mounted) setState(() => _busy.remove(vendorId));
    }
  }

  Future<void> _checkin(int id) async {
    try {
      await Api.post('/checkins/$id/call');
      _snack('Follow-up queued; Bandobast will call when the line is free');
      await _load();
    } catch (e) {
      _snack('$e');
    }
  }

  Future<void> _cancelSelection(String category) async {
    final approved = await showDialog<bool>(context: context, builder: (context) => AlertDialog(
      title: const Text('Cancel this selection?'),
      content: const Text('This stops the unconfirmed selection or replacement request. You can choose another quote afterwards.'),
      actions: [TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Keep selection')),
        FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Cancel selection'))],
    ));
    if (approved != true) return;
    try {
      await Api.post('/events/current/bookings/$category/cancel');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  void _snack(String text) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _chooseBackup(String category, String vendorId) async {
    try {
      await Api.post('/events/current/services/$category/backup/$vendorId');
      _snack('Backup selected; Bandobast will confirm availability or request activation approval');
      await _load();
    } catch (error) {
      _snack('$error');
    }
  }

  void _newFunction() => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NewFunctionScreen()));

  Future<void> _switchRole() async {
    Ringer.instance.stop();
    await Settings.setRole(null);
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const SetupScreen()), (_) => false);
  }

  @override
  Widget build(BuildContext context) {
    final event = _state?['event'] as Map<String, dynamic>?;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Bandobast'),
        actions: [
          if (event != null) IconButton(onPressed: _newFunction, tooltip: 'Plan another function', icon: const Icon(Icons.add)),
          IconButton(onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const DemoGuideScreen(allowPlanning: true))),
              tooltip: 'Demo walkthrough', icon: const Icon(Icons.help_outline)),
          IconButton(onPressed: _switchRole, tooltip: 'Switch role', icon: const Icon(Icons.swap_horiz)),
        ],
      ),
      body: !_loaded
          ? const Center(child: CircularProgressIndicator())
          : event == null
              ? _empty(context)
              : FamilyDashboard(state: _state!, familyId: Settings.familyId, error: _error,
                  busy: _busy, onRefresh: _load, onAct: _act, onCheckin: _checkin, onCancel: _cancelSelection, onChooseBackup: _chooseBackup),
    );
  }

  Widget _empty(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(24),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.celebration, size: 64),
        const SizedBox(height: 12),
        Text('Planning a function in Bengaluru?', style: Theme.of(context).textTheme.titleLarge, textAlign: TextAlign.center),
        const SizedBox(height: 6),
        Text('Your demo family ID: ${Settings.familyId}', style: Theme.of(context).textTheme.bodySmall),
        const Text('Tell us what you need. We find vendors near the venue, call them in their language, '
            'bargain politely, and bring you the quotes.', textAlign: TextAlign.center),
        if (_error != null) Padding(padding: const EdgeInsets.only(top: 12), child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
        const SizedBox(height: 20),
        FilledButton.icon(onPressed: _newFunction, icon: const Icon(Icons.add), label: const Text('Plan a function')),
      ]),
    ),
  );
}
