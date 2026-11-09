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
          Text('One function. Two caterers. A backup ready if needed.', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
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
      _step(context, '3', 'Answer enquiries naturally', 'C02: quote ₹800 per plate; Bandobast counters (about ₹700), settle near ₹650. No transport or extras, GST included, ₹4,000 advance. C03: quote ₹700; settle near ₹620, same terms, ₹5,000 advance. Quotes well above the family\'s budget get a counter-offer; quotes within it are accepted. Bandobast—not this guide—chooses the counter-offers.'),
      _step(context, '4', 'Approve C03 and confirm', 'The family can choose any bookable quote, not just the best pick. Approve C03. Answer each approved-term read-back clearly. It stays unconfirmed until every required answer is recorded.'),
      _step(context, '5', 'C02 is kept as the backup', 'No call is made to C02 now. The family sees C02 kept on file as the backup: not booked, and called only if C03 cancels.'),
      _step(context, '6', 'Show the failure and recovery', 'From C03’s vendor page, use Talk to Bandobast and cancel: the caterer cannot fulfil the booking. Only a cancellation switches to the backup; a delay just alerts the family. C02 then receives a booking call. Answer it fully. Only after confirmation should C02 become active. A higher replacement cost or advance needs family approval first.'),
      _step(context, '7', 'Show follow-ups and the timeline', 'Confirmed vendors get automatic follow-ups in IST while the server is running. On the function day Bandobast also asks the vendor to share their live location, saying the family requested it. Call now can demonstrate the same dispatch path without waiting days. Expired windows are skipped, not treated as vendor no-answers. The Timeline shows the actual decisions and results.'),
      const Card(child: Padding(padding: EdgeInsets.all(16), child: Text(
        'For a short video, edit out waiting—not outcomes. Show the real calls, bargaining, approval, the cancellation and the confirmed replacement. WhatsApp, payments and post-event ratings are not part of this app demo. Only the current function is shown.',
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
