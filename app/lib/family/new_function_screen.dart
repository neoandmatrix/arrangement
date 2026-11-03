import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:latlong2/latlong.dart';

import '../api.dart';
import 'function_screen.dart';

const _bengaluru = LatLng(12.9716, 77.5946);
const _userAgent = 'Bandobast app (student project, Great Indian AI Internship Challenge)';

/// The family describes the function: where, what, which services, when, and a budget.
class NewFunctionScreen extends StatefulWidget {
  const NewFunctionScreen({super.key});

  @override
  State<NewFunctionScreen> createState() => _NewFunctionScreenState();
}

class _NewFunctionScreenState extends State<NewFunctionScreen> {
  Map<String, dynamic>? _catalog;
  String? _loadError;

  final _map = MapController();
  LatLng? _pin;
  final _area = TextEditingController();
  final _search = TextEditingController();
  List<Map<String, dynamic>> _places = [];

  String? _function;
  String? _variant;
  final Set<String> _needs = {};
  DateTime? _date;
  TimeOfDay? _muhurat;
  final _guests = TextEditingController(text: '40');
  final _family = TextEditingController();
  final _contact = TextEditingController();
  String _language = 'hi-IN';
  final _cuisine = TextEditingController();
  final _rituals = TextEditingController();
  final Map<String, TextEditingController> _budget = {};
  final Map<String, TextEditingController> _wishes = {};
  bool _submitting = false;

  @override
  void initState() {
    super.initState();
    Api.get('/catalog').then((c) => setState(() => _catalog = c as Map<String, dynamic>)).catchError((e) {
      setState(() => _loadError = '$e');
    });
  }

  List<Map<String, dynamic>> get _functions => (_catalog!['functions'] as List).cast<Map<String, dynamic>>();
  List<Map<String, dynamic>> get _services => (_catalog!['services'] as List).cast<Map<String, dynamic>>();
  Map<String, dynamic>? get _fn {
    for (final f in _functions) {
      if (f['key'] == _function) return f;
    }
    return null;
  }

  void _pickFunction(Map<String, dynamic> f) {
    setState(() {
      _function = f['key'] as String;
      _variant = ((f['variants'] as List).first as Map)['key'] as String;
      _needs
        ..clear()
        ..addAll((f['default_needs'] as List).cast<String>());
    });
  }

  // Map search and reverse lookup go to OpenStreetMap's Nominatim (one request at a time, as its policy asks).

  Future<void> _searchPlaces() async {
    final q = _search.text.trim();
    if (q.isEmpty) return;
    final uri = Uri.https('nominatim.openstreetmap.org', '/search', {
      'q': q,
      'format': 'jsonv2',
      'limit': '6',
      'viewbox': '77.35,13.20,77.85,12.75', // Bengaluru
      'bounded': '1',
    });
    try {
      final r = await http.get(uri, headers: {'User-Agent': _userAgent});
      setState(() => _places = (jsonDecode(utf8.decode(r.bodyBytes)) as List).cast<Map<String, dynamic>>());
      if (_places.isEmpty) _snack('Nothing found in Bengaluru for "$q"');
    } catch (e) {
      _snack('Search failed: $e');
    }
  }

  Future<void> _setPin(LatLng point, {String? label}) async {
    setState(() {
      _pin = point;
      _places = [];
    });
    _map.move(point, 15);
    if (label != null) {
      _area.text = label;
      return;
    }
    try {
      final uri = Uri.https('nominatim.openstreetmap.org', '/reverse', {
        'lat': '${point.latitude}',
        'lon': '${point.longitude}',
        'format': 'jsonv2',
        'zoom': '16',
      });
      final r = await http.get(uri, headers: {'User-Agent': _userAgent});
      final address = (jsonDecode(utf8.decode(r.bodyBytes))['address'] ?? {}) as Map<String, dynamic>;
      final area = address['suburb'] ?? address['neighbourhood'] ?? address['quarter'] ?? address['city_district'];
      if (area != null && mounted) setState(() => _area.text = '$area');
    } catch (_) {
      // The family can type the area themselves.
    }
  }

  void _snack(String text) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> _submit() async {
    final problems = <String>[
      if (_pin == null) 'pin the venue on the map',
      if (_area.text.trim().isEmpty) 'the area',
      if (_function == null) 'the occasion',
      if (_needs.isEmpty) 'at least one service',
      if (_date == null) 'the date',
      if (_muhurat == null) 'the muhurat time',
      if ((int.tryParse(_guests.text) ?? 0) <= 0) 'the number of guests',
      if (_family.text.trim().isEmpty) 'the family name',
    ];
    if (problems.isNotEmpty) {
      _snack('Still needed: ${problems.join(', ')}');
      return;
    }
    final m = _muhurat!;
    final body = {
      'function_type': _function,
      'function_variant': _variant,
      'family_name': _family.text.trim(),
      'contact_name': _contact.text.trim(),
      'family_language': _language,
      'location': {'lat': _pin!.latitude, 'lon': _pin!.longitude, 'label': _area.text.trim()},
      'area': _area.text.trim(),
      'date': DateFormat('yyyy-MM-dd').format(_date!),
      'start_time': '${m.hour.toString().padLeft(2, '0')}:${m.minute.toString().padLeft(2, '0')}',
      'guests': int.parse(_guests.text),
      'cuisine': _cuisine.text.trim(),
      'rituals': _rituals.text.trim(),
      'needs': _needs.toList(),
      'budget': {
        for (final n in _needs) n: ?int.tryParse(_budget[n]?.text ?? ''),
      },
      'details': {
        for (final n in _needs)
          if ((_wishes[n]?.text.trim() ?? '').isNotEmpty) n: _wishes[n]!.text.trim(),
      },
    };
    setState(() => _submitting = true);
    try {
      await Api.post('/events', body);
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const FunctionScreen()), (_) => false);
    } catch (e) {
      _snack('$e');
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_catalog == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Plan a function')),
        body: Center(child: _loadError == null ? const CircularProgressIndicator() : Text(_loadError!)),
      );
    }
    final theme = Theme.of(context);
    final guests = int.tryParse(_guests.text) ?? 0;
    final tentHint = guests > (_catalog!['tent_suggested_above_guests'] as int) && !_needs.contains('tent_sound');
    return Scaffold(
      appBar: AppBar(title: const Text('Plan a function')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 100),
        children: [
          _Section('Where is it?'),
          Row(children: [
            Expanded(
              child: TextField(
                controller: _search,
                textInputAction: TextInputAction.search,
                onSubmitted: (_) => _searchPlaces(),
                decoration: const InputDecoration(hintText: 'Search a place in Bengaluru', prefixIcon: Icon(Icons.search), border: OutlineInputBorder(), isDense: true),
              ),
            ),
            IconButton.filledTonal(onPressed: _searchPlaces, icon: const Icon(Icons.arrow_forward)),
          ]),
          for (final p in _places)
            ListTile(
              dense: true,
              leading: const Icon(Icons.place_outlined),
              title: Text((p['name'] as String?)?.isNotEmpty == true ? p['name'] as String : (p['display_name'] as String).split(',').first),
              subtitle: Text((p['display_name'] as String).split(', ').skip(1).take(3).join(', ')),
              onTap: () => _setPin(LatLng(double.parse(p['lat'] as String), double.parse(p['lon'] as String))),
            ),
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: SizedBox(
              height: 240,
              child: FlutterMap(
                mapController: _map,
                options: MapOptions(initialCenter: _bengaluru, initialZoom: 11.5, onTap: (_, point) => _setPin(point)),
                children: [
                  TileLayer(urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', userAgentPackageName: 'ai.bandobast.app'),
                  if (_pin != null)
                    MarkerLayer(markers: [
                      Marker(point: _pin!, width: 40, height: 40, alignment: Alignment.topCenter, child: Icon(Icons.location_on, size: 40, color: theme.colorScheme.primary)),
                    ]),
                  const SimpleAttributionWidget(source: Text('OpenStreetMap contributors')),
                ],
              ),
            ),
          ),
          const SizedBox(height: 4),
          Text(_pin == null ? 'Tap the map to pin the venue.' : 'Pinned. Tap elsewhere to move it.', style: theme.textTheme.bodySmall),
          const SizedBox(height: 8),
          TextField(controller: _area, decoration: const InputDecoration(labelText: 'Area', hintText: 'e.g. HSR Layout', border: OutlineInputBorder())),

          _Section('What\'s the occasion?'),
          Wrap(spacing: 8, children: [
            for (final f in _functions) ChoiceChip(label: Text(f['name'] as String), selected: _function == f['key'], onSelected: (_) => _pickFunction(f)),
          ]),
          if (_fn != null) ...[
            const SizedBox(height: 8),
            Wrap(spacing: 8, runSpacing: 4, children: [
              for (final v in (_fn!['variants'] as List).cast<Map<String, dynamic>>())
                ChoiceChip(
                  label: Text(v['name'] as String),
                  selected: _variant == v['key'],
                  onSelected: (_) => setState(() => _variant = v['key'] as String),
                ),
            ]),
          ],

          _Section('What do you need?'),
          if (_function == null) const Text('Pick the occasion first; we\'ll suggest the usual services.'),
          Wrap(spacing: 8, runSpacing: 4, children: [
            for (final s in _services)
              FilterChip(
                label: Text(s['name'] as String),
                selected: _needs.contains(s['key']),
                onSelected: (on) => setState(() => on ? _needs.add(s['key'] as String) : _needs.remove(s['key'])),
              ),
          ]),
          if (tentHint)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text('For more than ${_catalog!['tent_suggested_above_guests']} guests, most homes need a tent and sound.', style: TextStyle(color: theme.colorScheme.tertiary)),
            ),

          _Section('When, and how many guests?'),
          Row(children: [
            Expanded(
              child: OutlinedButton.icon(
                icon: const Icon(Icons.event),
                label: Text(_date == null ? 'Date' : DateFormat('d MMM y').format(_date!)),
                onPressed: () async {
                  final now = DateTime.now();
                  final d = await showDatePicker(context: context, firstDate: now, lastDate: now.add(const Duration(days: 365)), initialDate: _date ?? now.add(const Duration(days: 10)));
                  if (d != null) setState(() => _date = d);
                },
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: OutlinedButton.icon(
                icon: const Icon(Icons.schedule),
                label: Text(_muhurat == null ? 'Muhurat' : _muhurat!.format(context)),
                onPressed: () async {
                  final t = await showTimePicker(context: context, initialTime: _muhurat ?? const TimeOfDay(hour: 9, minute: 0), helpText: 'Muhurat start time');
                  if (t != null) setState(() => _muhurat = t);
                },
              ),
            ),
          ]),
          const SizedBox(height: 12),
          TextField(
            controller: _guests,
            keyboardType: TextInputType.number,
            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(labelText: 'Guests', border: OutlineInputBorder()),
          ),

          _Section('About the family'),
          TextField(controller: _family, decoration: const InputDecoration(labelText: 'Family surname', hintText: 'e.g. Sharma', border: OutlineInputBorder())),
          const SizedBox(height: 12),
          TextField(controller: _contact, decoration: const InputDecoration(labelText: 'Contact person', border: OutlineInputBorder())),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _language,
            decoration: const InputDecoration(labelText: 'Your language', border: OutlineInputBorder()),
            items: [for (final l in (_catalog!['languages'] as List)) DropdownMenuItem(value: l['code'] as String, child: Text(l['name'] as String))],
            onChanged: (v) => setState(() => _language = v ?? 'hi-IN'),
          ),
          const SizedBox(height: 12),
          TextField(controller: _cuisine, decoration: const InputDecoration(labelText: 'Food', hintText: 'e.g. North Indian veg, no onion or garlic', border: OutlineInputBorder())),
          const SizedBox(height: 12),
          TextField(controller: _rituals, decoration: const InputDecoration(labelText: 'Rituals and their language', hintText: 'e.g. North Indian vidhi, in Hindi', border: OutlineInputBorder())),

          if (_needs.isNotEmpty) _Section('Budget and wishes'),
          for (final s in _services.where((s) => _needs.contains(s['key'])))
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(s['name'] as String, style: theme.textTheme.titleSmall),
                const SizedBox(height: 6),
                TextField(
                  controller: _budget.putIfAbsent(s['key'] as String, TextEditingController.new),
                  keyboardType: TextInputType.number,
                  inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                  decoration: const InputDecoration(labelText: 'Budget (₹, optional)', prefixText: '₹ ', border: OutlineInputBorder(), isDense: true),
                ),
                if (s['details_hint'] != null) ...[
                  const SizedBox(height: 8),
                  TextField(
                    controller: _wishes.putIfAbsent(s['key'] as String, TextEditingController.new),
                    decoration: InputDecoration(
                      labelText: s['key'] == 'cake' ? 'Cake (needed)' : 'Wishes (optional)',
                      hintText: s['details_hint'] as String,
                      border: const OutlineInputBorder(),
                      isDense: true,
                    ),
                  ),
                ],
              ]),
            ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _submitting ? null : _submit,
        icon: _submitting ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.call),
        label: Text(_submitting ? 'Sending…' : 'Find and call vendors'),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section(this.title);
  final String title;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 24, bottom: 10),
        child: Text(title, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
      );
}
