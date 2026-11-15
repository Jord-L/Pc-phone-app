// WebSocket client: receives JPEG frames, decodes them, acks them.
//
// Mirrors the browser test page in server/testpage.py. The wire format is
// documented at the top of server/net.py; the header layout here must match
// FRAME_HEADER there ("<BIHH": type u8, frame id u32, width u16, height u16).

import 'dart:async';
import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

const int _headerSize = 9;
const int _msgFrame = 1;

class WsClient {
  WsClient(this.host, this.port);

  final String host;
  final int port;

  // ValueNotifiers instead of setState in a widget: the frame changes up to
  // 30 times a second but the stats only once a second. Separate notifiers
  // let each part of the UI rebuild only when ITS data changes.
  final ValueNotifier<ui.Image?> frame = ValueNotifier<ui.Image?>(null);
  final ValueNotifier<String> status = ValueNotifier<String>('connecting...');
  final ValueNotifier<Map<String, dynamic>?> hello = ValueNotifier(null);
  final ValueNotifier<Map<String, dynamic>?> serverStats = ValueNotifier(null);
  final ValueNotifier<int> shownFps = ValueNotifier<int>(0);

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _fpsTimer;
  Timer? _retryTimer;
  bool _disposed = false;

  // Client-side stale-frame dropping (same idea as the server's single slot):
  // while one JPEG is decoding, keep only the NEWEST frame that arrives and
  // throw away any older one still waiting. Queueing them would make the
  // picture fall further and further behind the real screen.
  bool _decoding = false;
  Uint8List? _pending;
  int _shownThisSecond = 0;

  void start() {
    _fpsTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      shownFps.value = _shownThisSecond;
      _shownThisSecond = 0;
    });
    _connect();
  }

  Future<void> _connect() async {
    if (_disposed) return;
    status.value = 'connecting to $host:$port...';
    try {
      final channel = WebSocketChannel.connect(Uri.parse('ws://$host:$port/ws'));
      _channel = channel;
      // `ready` completes when the handshake succeeds and throws if the
      // server is unreachable. Without awaiting it, a failed connection only
      // shows up later as an unhandled stream error.
      await channel.ready;
      if (_disposed) {
        channel.sink.close();
        return;
      }
      status.value = 'connected';
      _sub = channel.stream.listen(
        _onMessage,
        onError: (Object e) => _onClosed('error: $e'),
        onDone: () => _onClosed('disconnected'),
        cancelOnError: true,
      );
    } catch (e) {
      _onClosed('cannot connect: $e');
    }
  }

  void _onClosed(String reason) {
    if (_disposed) return;
    _sub?.cancel();
    _sub = null;
    _channel = null;
    status.value = '$reason - retrying';
    // Phones drop Wi-Fi briefly all the time (screen off, roaming between
    // access points). Retrying automatically beats making the user reconnect.
    _retryTimer?.cancel();
    _retryTimer = Timer(const Duration(seconds: 1), _connect);
  }

  void _onMessage(dynamic data) {
    if (data is String) {
      // JSON control message.
      try {
        final msg = jsonDecode(data);
        if (msg is Map<String, dynamic>) {
          if (msg['type'] == 'hello') hello.value = msg;
          if (msg['type'] == 'stats') serverStats.value = msg;
        }
      } on FormatException {
        // Ignore malformed JSON rather than tearing down the stream.
      }
      return;
    }
    if (data is List<int>) {
      final bytes = data is Uint8List ? data : Uint8List.fromList(data);
      if (_decoding) {
        _pending = bytes; // overwrites (drops) any older waiting frame
      } else {
        _decode(bytes);
      }
    }
  }

  Future<void> _decode(Uint8List packet) async {
    _decoding = true;
    try {
      if (packet.length <= _headerSize) return;
      // sublistView = a window onto the same memory, no copy. The packet may
      // itself be a view with an offset, which sublistView handles correctly
      // (using packet.buffer directly would not).
      final header = ByteData.sublistView(packet, 0, _headerSize);
      if (header.getUint8(0) != _msgFrame) return;
      final frameId = header.getUint32(1, Endian.little);
      final jpeg = Uint8List.sublistView(packet, _headerSize);

      // Decode manually instead of using Image.memory: this way we know the
      // exact moment decoding finishes (needed for an honest ack), and we
      // control stale-frame dropping ourselves. Decoding runs on the engine's
      // background threads, so it does not block the UI.
      final codec = await ui.instantiateImageCodec(jpeg);
      final ui.Image image;
      try {
        image = (await codec.getNextFrame()).image;
      } finally {
        codec.dispose();
      }
      if (_disposed) {
        image.dispose();
        return;
      }

      final old = frame.value;
      frame.value = image;
      // Decoded images hold native (non-garbage-collected) memory, so the
      // previous one must be released explicitly or memory grows by one
      // frame 15-30 times a second. Safe to do right away: the RawImage
      // widget keeps its own clone of whatever it is currently painting.
      old?.dispose();
      _shownThisSecond++;

      // Ack AFTER decode so the server's latency number covers
      // network + decode, i.e. roughly "when the user could see it".
      _channel?.sink.add(jsonEncode({'type': 'ack', 'id': frameId}));
    } catch (e) {
      debugPrint('frame decode failed: $e');
    } finally {
      _decoding = false;
      final next = _pending;
      _pending = null;
      if (next != null && !_disposed) {
        // Not awaited on purpose: this starts the next decode and returns.
        _decode(next);
      }
    }
  }

  void dispose() {
    _disposed = true;
    _fpsTimer?.cancel();
    _retryTimer?.cancel();
    _sub?.cancel();
    _channel?.sink.close();
    frame.value?.dispose();
    frame.dispose();
    status.dispose();
    hello.dispose();
    serverStats.dispose();
    shownFps.dispose();
  }
}
