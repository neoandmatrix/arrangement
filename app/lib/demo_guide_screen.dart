import 'package:flutter/material.dart';

import 'demo_details.dart';
import 'family/new_function_screen.dart';

/// An honest, repeatable live role-play—not a shortcut around the voice or decision guards.
class DemoGuideScreen extends StatelessWidget {
  const DemoGuideScreen({super.key, this.allowPlanning = false});
  final bool allowPlanning;

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Demo walkthrough')),
    body: ListView(padding: const EdgeInsets.all(16), children: [
      Card(color: Theme.of(context).colorScheme.primaryContainer, child: const Padding(
        padding: EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('One function. Two caterers. A confirmed backup.', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          SizedBox(height: 8),
          Text('Use fictional family and vendor details. Quotes and confirmations must come from the actual role-play calls, not this guide.'),
        ]),
      )),
      _step(context, '1', 'Prepare three devices', 'Use one family phone and two vendor phones on the same server. Sign in as C02 (Hindi) and C03 (Kannada) before submitting. Keep both vendor apps connected. Use earpieces or headphones.'),
      _step(context, '2', 'Plan a small housewarming', 'Use the demo form or fill by voice: Demo Sharma family, 40 guests, caterer only, ₹25,000 budget. Select a venue in HSR Layout on the map. A different pin can change the shortlist. Review before submitting.'),
      if (allowPlanning) Padding(padding: const EdgeInsets.only(bottom: 12), child: FilledButton.icon(
        onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => NewFunctionScreen(initialDraft: demoDetails()))),
        icon: const Icon(Icons.edit_note), label: const Text('Open sample form'),
      )),
      _step(context, '3', 'Answer enquiries naturally', 'C02: opening rate ₹500 per plate; final rate ₹480, no transport or extras, GST included, ₹4,000 advance. C03: opening rate ₹520; final rate ₹500, no transport or extras, GST included, ₹5,000 advance. Say firm amounts, explicit zero for absent charges, and repeat final rates. Bandobast—not this guide—chooses counter-offers.'),
      _step(context, '4', 'Approve C03 and confirm', 'The family can choose any bookable quote, not just the best pick. Approve C03. Answer each approved-term read-back clearly. It stays unconfirmed until every required answer is recorded.'),
      _step(context, '5', 'Secure C02 as standby', 'Answer C02’s standby call. Standby is not a booking and no advance is due now. The family should see Standby confirmed—not merely a selected backup.'),
      _step(context, '6', 'Show the failure and recovery', 'From C03’s vendor page, use Talk to Bandobast and report that the caterer cannot fulfil the booking. C02 then receives a separate activation call. Answer it fully. Only after confirmation should C02 become active. A higher replacement cost or advance needs family approval first.'),
      _step(context, '7', 'Show follow-ups and the timeline', 'Confirmed vendors get automatic follow-ups in IST while the server is running. Call now can demonstrate the same dispatch path without waiting days. Expired windows are skipped, not treated as vendor no-answers. The Timeline shows the actual decisions and results.'),
      const Card(child: Padding(padding: EdgeInsets.all(16), child: Text(
        'For a short video, edit out waiting—not outcomes. Show the real calls, approval, standby and confirmed replacement. WhatsApp, payments and post-event ratings are not part of this app demo. Only the current function is shown.',
      ))),
      const Padding(padding: EdgeInsets.symmetric(vertical: 16), child: Text('Voice: Gnani Prisma STT + Timbre TTS. Quotes, negotiation, approvals and backup decisions are enforced in code.', textAlign: TextAlign.center)),
    ]),
  );

  Widget _step(BuildContext context, String number, String title, String description) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 10),
    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      CircleAvatar(radius: 16, backgroundColor: Theme.of(context).colorScheme.secondaryContainer, child: Text(number)),
      const SizedBox(width: 12),
      Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(title, style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 4), Text(description),
      ])),
    ]),
  );
}
