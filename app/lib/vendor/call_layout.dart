import 'package:flutter/material.dart';

/// Keep call controls reachable on short screens and with larger text, without changing the palette.
class CallLayout extends StatelessWidget {
  const CallLayout({super.key, required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => LayoutBuilder(builder: (context, constraints) => SingleChildScrollView(
    child: ConstrainedBox(
      constraints: BoxConstraints(minHeight: constraints.maxHeight),
      child: IntrinsicHeight(child: Padding(padding: const EdgeInsets.all(24), child: Column(children: children))),
    ),
  ));
}
