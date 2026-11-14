// Shows the live PC screen plus a stats overlay. No input yet (milestone 4).

import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../services/ws_client.dart';

class RemoteScreen extends StatefulWidget {
  const RemoteScreen({super.key, required this.host, required this.port});

  final String host;
  final int port;

  @override
  State<RemoteScreen> createState() => _RemoteScreenState();
}

class _RemoteScreenState extends State<RemoteScreen> {
  // Created in initState and destroyed in dispose so the connection lives
  // exactly as long as this screen: pressing Back closes the socket, which
  // lets the server stop capturing.
  late final WsClient _client;
  bool _showStats = true;

  @override
  void initState() {
    super.initState();
    _client = WsClient(widget.host, widget.port)..start();
  }

  @override
  void dispose() {
    _client.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: GestureDetector(
          // Temporary: tap toggles the overlay. The touch layer replaces
          // this in milestone 4.
          onTap: () => setState(() => _showStats = !_showStats),
          behavior: HitTestBehavior.opaque,
          child: Stack(
            fit: StackFit.expand,
            children: [
              // Only this subtree rebuilds per frame.
              ValueListenableBuilder<ui.Image?>(
                valueListenable: _client.frame,
                builder: (context, image, _) {
                  if (image == null) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  // RawImage paints an already-decoded ui.Image directly.
                  // BoxFit.contain keeps the PC's aspect ratio (letterboxed).
                  return RawImage(
                    image: image,
                    fit: BoxFit.contain,
                    // Frames arrive downscaled; smooth scaling up to the
                    // phone's resolution looks better than blocky pixels.
                    filterQuality: FilterQuality.low,
                  );
                },
              ),
              if (_showStats) Positioned(left: 4, top: 4, child: _StatsOverlay(client: _client)),
            ],
          ),
        ),
      ),
    );
  }
}

class _StatsOverlay extends StatelessWidget {
  const _StatsOverlay({required this.client});

  final WsClient client;

  @override
  Widget build(BuildContext context) {
    // Rebuild when ANY of these change; all are low-frequency (<= 1/s).
    return AnimatedBuilder(
      animation: Listenable.merge([client.status, client.serverStats, client.shownFps, client.hello]),
      builder: (context, _) {
        final s = client.serverStats.value;
        final h = client.hello.value;
        final lines = <String>[client.status.value];
        if (h != null) {
          final screen = h['screen'] as Map<String, dynamic>?;
          final stream = h['stream'] as Map<String, dynamic>?;
          lines.add('screen ${screen?['width']}x${screen?['height']}  '
              'max ${stream?['max_width']}w q${stream?['quality']} target ${stream?['fps']} fps');
        }
        if (s != null) {
          lines.add('capture ${s['capture_fps']}  sent ${s['sent_fps']}  shown ${client.shownFps.value} fps  '
              'dropped ${s['dropped']}');
          lines.add('grab ${s['grab_ms']} ms  encode ${s['encode_ms']} ms  '
              '${s['frame_kb']} KB  ${s['kbps']} kbit/s');
          lines.add('latency ${s['latency_ms'] ?? '-'} ms  max ${s['latency_max_ms'] ?? '-'} ms  '
              'in flight ${s['in_flight']}');
        }
        return Container(
          padding: const EdgeInsets.all(4),
          color: Colors.black54,
          child: Text(
            lines.join('\n'),
            style: const TextStyle(color: Colors.greenAccent, fontSize: 11, fontFamily: 'monospace'),
          ),
        );
      },
    );
  }
}
