import 'package:flutter/material.dart';

import '../api.dart';
import '../format.dart';

class VendorUpdateCard extends StatefulWidget {
  const VendorUpdateCard({super.key, required this.update, required this.services, required this.onRefresh, this.onReconfirm});
  final Map<String, dynamic> update;
  final List<Map<String, dynamic>> services;
  final Future<void> Function() onRefresh;
  final Future<void> Function(String)? onReconfirm;

  @override
  State<VendorUpdateCard> createState() => _VendorUpdateCardState();
}

class _VendorUpdateCardState extends State<VendorUpdateCard> {
  bool _busy = false;

  Future<void> _decide(String decision) async {
    setState(() => _busy = true);
    try {
      await Api.post('/updates/${widget.update['id']}/$decision');
      await widget.onRefresh();
    } catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$e')));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final u = widget.update;
    final data = u['data'] as Map<String, dynamic>;
    final quote = data['quote'] as Map<String, dynamic>?;
    final pending = (u['kind'] == 'price' || u['kind'] == 'quote' || u['kind'] == 'backup_activation') && u['status'] == 'pending';
    final flags = ((data['flags'] as List?) ?? []).cast<Map<String, dynamic>>();
    final blocked = flags.any((f) => f['level'] == 'block');
    final activation = u['kind'] == 'backup_activation';
    final correction = u['kind'] == 'quote' || activation;
    final eventChange = u['kind'] == 'confirmation_change';
    String name = '${u['vendor_id']}';
    for (final s in widget.services) {
      for (final v in (s['vendors'] as List)) {
        if (v['vendor_id'] == u['vendor_id']) name = '${v['name']}';
      }
    }
    return Card(
      color: pending ? const Color(0xFFFFF4E0) : null,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${activation ? 'Replacement approval for' : correction ? 'Corrected quote from' : 'Update from'} $name', style: Theme.of(context).textTheme.titleMedium),
          Text('${data['notes']}'),
          if (eventChange) ...[
            Text('${data['review_note'] ?? 'Review this booking change before retrying confirmation.'}', style: Theme.of(context).textTheme.bodySmall),
            TextButton(onPressed: _busy ? null : () => _decide('reject'), child: const Text('Reject change · retry original terms')),
          ],
          if (quote != null) ...[
            const SizedBox(height: 6),
            if (!correction) Text('${quotedMoney(data['previous_price'])} → ${quotedMoney(quote['price_amount'])}'
                '${quote['price_unit'] == 'per_plate' ? ' per plate' : ' base price'}'),
            if (correction) ...[
              if (activation) Text('Previous approved total: ${quotedMoney(data['previous_total'])}'),
              if (data['landed_cost'] != null) Text('${activation ? 'Replacement' : 'Corrected'} total: ${rupees(data['landed_cost'] as num)}'),
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: Text(activation ? 'Review replacement terms' : 'Review all corrected charges'),
                children: [
                  for (final entry in {'price_amount': 'Base price', 'price_unit': 'Pricing unit', 'transport_charge': 'Transport',
                    'extra_charges_amount': 'Extras', 'advance_amount': 'Advance', 'gst_included': 'GST included'}.entries)
                    Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      SizedBox(width: 100, child: Text(entry.value)),
                      Expanded(child: Text('${_value((data['previous_quote'] as Map?)?[entry.key])} → ${_value(quote[entry.key])}')),
                    ])),
                  if (quote['includes_notes'] != null) Align(alignment: Alignment.centerLeft, child: Text('Covers: ${quote['includes_notes']}')),
                ],
              ),
            ],
            Text(pending ? (activation ? 'Approval queues the replacement call. It is not active until the vendor confirms.' : 'Existing terms stay unchanged until you approve.') : 'Revision ${u['status']}.', style: Theme.of(context).textTheme.bodySmall),
          ],
          if (data['eta'] != null) Text('New arrival time: ${data['eta']}'),
          for (final flag in flags.where((f) => f['level'] == 'block')) Padding(
            padding: const EdgeInsets.only(top: 4), child: Text('• ${flag['text']}', style: TextStyle(fontSize: 12, color: Theme.of(context).colorScheme.error)),
          ),
          if (pending && blocked && widget.onReconfirm != null) OutlinedButton.icon(
            onPressed: _busy ? null : () => widget.onReconfirm!('${u['vendor_id']}'),
            icon: const Icon(Icons.fact_check_outlined), label: const Text('Reconfirm prices'),
          ),
          if (pending) Wrap(spacing: 8, children: [
            FilledButton(onPressed: _busy || blocked ? null : () => _decide('approve'), child: Text(activation ? 'Approve replacement terms' : correction ? 'Approve corrected quote' : 'Approve revised price')),
            TextButton(onPressed: _busy ? null : () => _decide('reject'), child: Text(activation ? 'Decline replacement' : 'Reject revision')),
          ]),
        ]),
      ),
    );
  }

  String _value(Object? value) => value is num ? rupees(value) : value == true ? 'Yes' : value == false ? 'No' : '${value ?? '—'}';
}
