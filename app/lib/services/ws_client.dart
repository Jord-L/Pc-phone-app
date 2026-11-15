// WebSocket client: receives JPEG frames, decodes them, acks them.
//
// Mirrors the browser test page in server/testpage.py. The wire format is
// documented at the top of server/net.py; the header layout here must match
// FRAME_HEADER there ("<BIHH": type u8, frame id u32, width u16, height u16).
//
// Also sends input (mouse move/click/scroll). Positions are FRACTIONS of the
// PC screen so they stay correct whatever the stream or PC resolution is.

import 'dart:async';
import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:flutter/painting.dart' show Offset;
import 'package:web_socket_channel/web_socket_channel.dart';

const int _headerSize = 9;
const int _msgFrame = 1;

// Must match CLOSE_AUTH_FAILED in server/net.py.
const int _closeAuthFailed = 4401;

class WsClient {
  WsClient(this.host, this.port, {this.pin = ''});

  final String host;
  final int port;
  final String pin;

  // True once the server rejected the PIN. Retrying with the same wrong PIN
  // would only hit the server's lockout, so the client stops instead.
  final ValueNotifier<bool> authFailed = ValueNotifier<bool>(false);

  // PC mouse pointer as a fraction of the screen (0..1 = on screen). Screen
  // capture does not contain the pointer, so the UI draws it from this.
  final ValueNotifier<Offset?> cursor = ValueNotifier<Offset?>(null);

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
      _connected = true;
      // The PIN has to be the first thing the server sees. (A server on this
      // same PC ignores it, so sending it unconditionally is harmless.)
      if (pin.isNotEmpty) {
        channel.sink.add(jsonEncode({'type': 'auth', 'pin': pin}));
      }
      _sub = channel.stream.listen(
        _onMessage,
        onError: (Object e) => _onClosed('error: $e'),
        onDone: () => _onClosed('disconnected', closeCode: channel.closeCode),
        cancelOnError: true,
      );
    } catch (e) {
      _onClosed('cannot connect: $e');
    }
  }

  void _onClosed(String reason, {int? closeCode}) {
    if (_disposed) return;
    _sub?.cancel();
    _sub = null;
    _channel = null;
    _connected = false;
    _releaseHeldButtons();
    if (authFailed.value || closeCode == _closeAuthFailed) {
      authFailed.value = true;
      status.value = 'wrong PIN - check the PIN shown on the PC';
      return;
    }
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
          if (msg['type'] == 'auth_failed') authFailed.value = true;
          if (msg['type'] == 'cursor') {
            final x = msg['x'], y = msg['y'];
            if (x is num && y is num) cursor.value = Offset(x.toDouble(), y.toDouble());
          }
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

  // ----------------------------------------------------------------- input

  bool _connected = false;
  final Set<String> _held = <String>{};

  /// False in the server's "view only" mode (the hello message says so).
  bool get inputAllowed => hello.value == null || hello.value!['input'] != false;

  void _send(Map<String, Object?> msg) {
    if (!_connected) return;
    _channel?.sink.add(jsonEncode(msg));
  }

  /// Move the PC cursor by a fraction of the screen size (touchpad style).
  void sendMove(double dx, double dy) => _send({'type': 'move', 'dx': dx, 'dy': dy});

  /// Put the PC cursor at a position given as 0..1 of the screen (direct touch).
  void sendAbs(double x, double y) => _send({'type': 'abs', 'x': x, 'y': y});

  /// btn: 'left' | 'right' | 'middle'.
  void sendButton(String btn, bool down) {
    if (down) {
      _held.add(btn);
    } else {
      _held.remove(btn);
    }
    _send({'type': 'button', 'btn': btn, 'down': down});
  }

  /// Wheel notches; dy > 0 scrolls the PC content down.
  void sendScroll(double dx, double dy) => _send({'type': 'scroll', 'dx': dx, 'dy': dy});

  /// A named key press half: 'enter', 'backspace', 'ctrl', 'a', 'f5', ...
  /// (the full list is in InputSink.key in server/platform/base.py).
  void sendKey(String name, bool down) => _send({'type': 'key', 'name': name, 'down': down});

  /// Literal text, typed on the PC whatever its keyboard layout.
  void sendText(String text) => _send({'type': 'text', 'text': text});

  /// A button the UI is holding when the connection drops must not stay
  /// "held" in our bookkeeping (the server releases its side by itself).
  void _releaseHeldButtons() => _held.clear();

  void dispose() {
    // Let go of anything still held so the PC is never left mid-drag.
    for (final b in _held.toList()) {
      sendButton(b, false);
    }
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
    authFailed.dispose();
    cursor.dispose();
  }
}
