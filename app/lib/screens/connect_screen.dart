// Manual "type the PC address" screen. Saved hosts and mDNS discovery replace
// most of this in milestones 6/7.

import 'package:flutter/material.dart';

import 'remote_screen.dart';

class ConnectScreen extends StatefulWidget {
  const ConnectScreen({super.key});

  @override
  State<ConnectScreen> createState() => _ConnectScreenState();
}

class _ConnectScreenState extends State<ConnectScreen> {
  // Controllers own native text-editing resources, so they are created once
  // here (not in build, which runs many times) and released in dispose.
  final _host = TextEditingController();
  final _port = TextEditingController(text: '8765'); // DEFAULT_PORT in server/config.py
  String? _error;

  @override
  void dispose() {
    _host.dispose();
    _port.dispose();
    super.dispose();
  }

  void _connect() {
    final host = _host.text.trim();
    final port = int.tryParse(_port.text.trim());
    if (host.isEmpty || port == null || port < 1 || port > 65535) {
      setState(() => _error = 'Enter the PC address (e.g. 192.168.1.20) and a valid port.');
      return;
    }
    setState(() => _error = null);
    Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => RemoteScreen(host: host, port: port)),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('PhoneDesk')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              controller: _host,
              decoration: const InputDecoration(
                labelText: 'PC address',
                hintText: '192.168.1.20',
              ),
              // Plain URL keyboard: gives digits and dots without autocorrect
              // "fixing" an IP address.
              keyboardType: TextInputType.url,
              autocorrect: false,
              onSubmitted: (_) => _connect(),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _port,
              decoration: const InputDecoration(labelText: 'Port'),
              keyboardType: TextInputType.number,
              onSubmitted: (_) => _connect(),
            ),
            const SizedBox(height: 20),
            FilledButton(onPressed: _connect, child: const Text('Connect')),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ),
          ],
        ),
      ),
    );
  }
}
