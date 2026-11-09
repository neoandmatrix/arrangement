import 'package:flutter/material.dart';

import '../format.dart';
import 'confirmation_summary.dart';
import 'view_helpers.dart';

/// Candidate, confirmed standby and active replacement are deliberately different states.
class BackupPanel extends StatelessWidget {
  const BackupPanel({super.key, required this.service, this.onRetry, this.onChoose, this.onReconfirm, this.busy = false});
  final Map<String, dynamic> service;
  final VoidCallback? onRetry;
  final Future<void> Function(String)? onChoose;
  final VoidCallback? onReconfirm;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final primary = booking(service, 'primary');
    final backup = booking(service, 'backup');
    final vendor = vendorIn(service, backup?['vendor_id'] as String?);
    final secured = backup?['secured'] == true;
    final status = backup?['status'];
    final confirmation = backup?['confirmation'] as Map<String, dynamic>?;
    final eligible = backup != null && !{'cancelled', 'declined', 'replaced'}.contains(status);
    final replacement = {'replacement_pending', 'needs_replacement'}.contains(primary?['status']);
    final canManage = primary?['status'] == 'active' || replacement;
    final theme = Theme.of(context);
    final label = switch (status) {
      'backup_on_file' => 'Kept on file · called only if your vendor cancels',
      'awaiting_activation' || 'activating_backup' => 'Confirming replacement',
      'activation_awaiting_approval' => 'Activation needs your approval',
      'confirming_backup' || 'awaiting_backup_confirmation' => 'Checking standby availability',
      'backup_confirmation_failed' => 'Backup did not confirm',
      'backup_awaiting_changes' => 'Backup changes need review',
      'declined' || 'cancelled' => 'Backup unavailable',
      _ => secured ? 'Standby confirmed' : 'Standby not confirmed',
    };
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Divider(),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(secured || status == 'backup_on_file' ? Icons.shield_outlined : Icons.info_outline, size: 20,
              color: secured || status == 'backup_on_file' ? Colors.green.shade800 : theme.colorScheme.secondary),
          const SizedBox(width: 8),
          Expanded(child: Text('Backup · ${eligible ? label : 'No secured backup'}', style: theme.textTheme.titleSmall)),
        ]),
        if (eligible) ...[
          const SizedBox(height: 6),
          Text('${vendor?['name'] ?? backup['vendor_id']}'),
          if (vendor?['landed_cost'] != null) Text('Quoted total ${quotedMoney(vendor!['landed_cost'])}', style: theme.textTheme.bodySmall),
          Text(secured ? 'Available as standby, not booked. No advance is due unless activated.'
              : replacement ? 'The replacement is not active until its booking confirmation is complete.'
              : 'Selecting a backup is not proof of availability. No booking or advance is due.', style: const TextStyle(fontSize: 12)),
          if (confirmation?['reason'] != null) Text('${confirmation!['reason']}', style: theme.textTheme.bodySmall),
          if (vendor?['needs_price_reconfirmation'] == true && onReconfirm != null)
            OutlinedButton.icon(onPressed: busy ? null : onReconfirm, icon: const Icon(Icons.fact_check_outlined), label: const Text('Recheck backup prices')),
          if (canManage && onRetry != null && !{'confirming_backup', 'activating_backup', 'activation_awaiting_approval', 'backup_awaiting_changes'}.contains(status))
            TextButton.icon(onPressed: busy ? null : onRetry, icon: const Icon(Icons.phone_callback),
                label: Text(replacement ? 'Retry backup activation' : secured ? 'Recheck standby' : 'Confirm standby')),
          if (confirmation != null) ConfirmationSummary(confirmation: confirmation),
        ],
        if (canManage && onChoose != null) TextButton.icon(
          onPressed: busy ? null : () => _choose(context), icon: const Icon(Icons.swap_horiz), label: const Text('Choose another backup'),
        ),
      ]),
    );
  }

  Future<void> _choose(BuildContext context) async {
    final candidates = rows(service['vendors']).where((v) => v['bookable'] == true && (v['quote'] as Map?)?['available'] == true
        && v['vendor_id'] != booking(service, 'primary')?['vendor_id'] && v['vendor_id'] != booking(service, 'backup')?['vendor_id']).toList();
    final chosen = await showModalBottomSheet<String>(context: context, builder: (context) => SafeArea(
      child: ListView(shrinkWrap: true, children: [
        const ListTile(title: Text('Choose a backup'), subtitle: Text('Bandobast will confirm standby availability before marking it secured.')),
        if (candidates.isEmpty) const ListTile(title: Text('No other bookable quotes yet'), subtitle: Text('Recheck prices or collect another quote first.')),
        for (final vendor in candidates) ListTile(
          title: Text('${vendor['name']}'), subtitle: Text('Quoted total ${quotedMoney(vendor['landed_cost'])}'),
          onTap: () => Navigator.pop(context, vendor['vendor_id'] as String),
        ),
      ]),
    ));
    if (chosen != null) await onChoose!(chosen);
  }
}
