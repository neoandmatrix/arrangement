import 'package:flutter/material.dart';

import '../api.dart';
import '../format.dart';

class VendorUpdateCard extends StatefulWidget {
  const VendorUpdateCard({super.key, required this.update, required this.services, required this.onRefresh});
  final Map<String, dynamic> update;
  final List<Map<String, dynamic>> services;
  final Future<void> Function() onRefresh;

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
    final pending = u['kind'] == 'price' && u['status'] == 'pending';
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
          Text('Update from $name', style: Theme.of(context).textTheme.titleMedium),
          Text('${data['notes']}'),
          if (quote != null) ...[
            const SizedBox(height: 6),
            Text('${rupees(data['previous_price'] as num?)} → ${rupees(quote['price_amount'] as num?)}'
                '${quote['price_unit'] == 'per_plate' ? ' per plate' : ' base price'}'),
            Text(pending ? 'Existing terms stay unchanged until you approve.' : 'Revision ${u['status']}.', style: Theme.of(context).textTheme.bodySmall),
          ],
          if (data['eta'] != null) Text('New arrival time: ${data['eta']}'),
          if (pending) Wrap(spacing: 8, children: [
            FilledButton(onPressed: _busy ? null : () => _decide('approve'), child: const Text('Approve revised price')),
            TextButton(onPressed: _busy ? null : () => _decide('reject'), child: const Text('Reject revision')),
          ]),
        ]),
      ),
    );
  }
}
