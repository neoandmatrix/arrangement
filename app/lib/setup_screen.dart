import 'package:flutter/material.dart';

import 'api.dart';
import 'family/function_screen.dart';
import 'format.dart';
import 'settings.dart';
import 'vendor/vendor_home.dart';

/// First launch: where the server is, the access key, and who is using this phone.
class SetupScreen extends StatefulWidget {
  const SetupScreen({super.key});

  @override
  State<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends State<SetupScreen> {
  final _server = TextEditingController(text: Settings.server);
  final _key = TextEditingController(text: Settings.appKey);
  String? _status;
  bool _ok = false;
  bool _busy = false;

  Future<void> _test() async {
    setState(() {
      _busy = true;
      _status = null;
    });
    await Settings.setServer(_server.text);
    await Settings.setAppKey(_key.text);
    try {
      await Api.get('/catalog');
      setState(() {
        _ok = true;
        _status = 'Connected';
      });
    } catch (e) {
      setState(() {
        _ok = false;
        _status = '$e';
      });
    } finally {
      setState(() => _busy = false);
    }
  }

  Future<void> _family() async {
    await Settings.setRole('family');
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const FunctionScreen()), (_) => false);
  }

  Future<void> _vendor() async {
    final List vendors;
    try {
      vendors = await Api.get('/vendors') as List;
    } catch (e) {
      setState(() => _status = '$e');
      return;
    }
    if (!mounted) return;
    final chosen = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      builder: (context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.8,
        builder: (context, scroll) => ListView(
          controller: scroll,
          children: [
            const ListTile(
              title: Text('Which vendor are you?'),
              subtitle: Text('Demo vendors are fictional (the challenge allows synthetic data only).'),
            ),
            for (final v in vendors.cast<Map<String, dynamic>>())
              ListTile(
                leading: CircleAvatar(child: Text(v['id'] as String)),
                title: Text(v['name'] as String),
                subtitle: Text('${serviceNames[v['category']]} · ${v['area']} · ${languageNames[v['language']]}'),
                trailing: Text('★ ${v['rating']}'),
                onTap: () => Navigator.pop(context, v),
              ),
          ],
        ),
      ),
    );
    if (chosen == null) return;
    await Settings.setVendor(chosen['id'] as String, chosen['name'] as String);
    await Settings.setRole('vendor');
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const VendorHome()), (_) => false);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            const SizedBox(height: 12),
            Text('Bandobast', style: theme.textTheme.displaySmall?.copyWith(color: theme.colorScheme.primary, fontWeight: FontWeight.w700)),
            const Text('Your family function, arranged: we call the pandit, caterer and decorator for you.'),
            const SizedBox(height: 24),
            Text('Server', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            TextField(
              controller: _server,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(
                labelText: 'Server address',
                hintText: 'http://192.168.1.20:8765 or https://….trycloudflare.com',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _key,
              obscureText: true,
              decoration: const InputDecoration(labelText: 'App key (APP_KEY in the server\'s .env)', border: OutlineInputBorder()),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                FilledButton.tonal(onPressed: _busy ? null : _test, child: const Text('Test connection')),
                const SizedBox(width: 12),
                if (_busy) const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2)),
                if (_status != null)
                  Expanded(child: Text(_status!, style: TextStyle(color: _ok ? Colors.green[800] : theme.colorScheme.error))),
              ],
            ),
            const SizedBox(height: 28),
            Text('Who is using this phone?', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            _RoleCard(
              icon: Icons.family_restroom,
              title: 'I\'m planning a function',
              subtitle: 'Griha pravesh, puja or a baby ceremony in Bengaluru',
              onTap: _ok ? _family : null,
            ),
            _RoleCard(
              icon: Icons.storefront,
              title: 'I\'m a vendor',
              subtitle: 'Priest, caterer, decorator, cake, photographer, tent and sound',
              onTap: _ok ? _vendor : null,
            ),
            if (!_ok) const Padding(padding: EdgeInsets.only(top: 8), child: Text('Test the connection first.')),
          ],
        ),
      ),
    );
  }
}

class _RoleCard extends StatelessWidget {
  const _RoleCard({required this.icon, required this.title, required this.subtitle, this.onTap});

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        enabled: onTap != null,
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        leading: Icon(icon, size: 36),
        title: Text(title),
        subtitle: Text(subtitle),
        trailing: const Icon(Icons.chevron_right),
        onTap: onTap,
      ),
    );
  }
}
