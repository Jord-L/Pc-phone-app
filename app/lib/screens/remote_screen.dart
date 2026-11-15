// Live PC screen + touch control + mouse buttons + stats overlay.
//
// TOUCH MODES (toggle in the bottom bar):
//   Touchpad (default): like a laptop trackpad. Dragging one finger moves the
//     PC cursor RELATIVE to where it was; tap = left click; two-finger tap =
//     right click; three-finger tap = middle click; two fingers dragging =
//     scroll. Precise even though the phone screen is small.
//   Direct: the PC cursor jumps to wherever your finger is; tap = click there.
//
// The Left / Right buttons send press and release separately, so holding Left
// with one thumb while dragging with a finger does a click-and-drag. "Hold"
// latches the left button down for single-handed dragging.

import 'dart:async';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../services/ws_client.dart';
import '../widgets/keyboard_panel.dart';

enum TouchMode { touchpad, direct }

class RemoteScreen extends StatefulWidget {
  const RemoteScreen({super.key, required this.host, required this.port, this.pin = ''});

  final String host;
  final int port;
  final String pin;

  @override
  State<RemoteScreen> createState() => _RemoteScreenState();
}

class _RemoteScreenState extends State<RemoteScreen> {
  // Created in initState and destroyed in dispose so the connection lives
  // exactly as long as this screen: pressing Back closes the socket, which
  // lets the server stop capturing.
  late final WsClient _client;
  bool _showStats = false;
  bool _showKeyboard = false;
  TouchMode _mode = TouchMode.touchpad;
  bool _holdLeft = false;

  // ---- tuning ----
  // Phone pixels -> PC pixels. 1.0 = the cursor covers the PC screen when the
  // finger covers the picture; acceleration below adds speed on fast swipes.
  static const double _gain = 1.0;
  // Logical pixels of finger travel per mouse-wheel notch.
  static const double _pxPerNotch = 35;
  // A touch counts as a tap only if short and nearly still.
  static const Duration _tapMaxTime = Duration(milliseconds: 300);
  static const double _tapSlop = 12;
  // Touchpad mode ignores the first few pixels so a tap with a slightly
  // wobbly finger doesn't nudge the cursor off the target before it clicks.
  static const double _moveSlop = 4;
  static const Duration _flushEvery = Duration(milliseconds: 16);

  // ---- gesture state (one gesture = first finger down .. last finger up) ----
  final Map<int, Offset> _pointers = {};
  DateTime _downAt = DateTime.now();
  double _travel = 0;
  int _maxPointers = 0;

  // ---- output queued between flushes (touch events can arrive faster than
  // is useful to send; coalescing keeps the Wi-Fi link and PC unflooded) ----
  double _pendDx = 0, _pendDy = 0; // fractions of the PC screen
  double _pendScrollX = 0, _pendScrollY = 0; // wheel notches
  Offset? _pendAbs;
  Timer? _flushTimer;

  Size _area = Size.zero;

  @override
  void initState() {
    super.initState();
    _client = WsClient(widget.host, widget.port, pin: widget.pin)..start();
    _client.authFailed.addListener(_onAuthFailed);
  }

  @override
  void dispose() {
    _flushTimer?.cancel();
    _client.authFailed.removeListener(_onAuthFailed);
    _client.dispose();
    super.dispose();
  }

  void _onAuthFailed() {
    if (!_client.authFailed.value || !mounted) return;
    // Back to the connect screen with the reason, instead of a dead black view.
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Wrong PIN. Check the PIN shown in the PC window.')),
    );
    Navigator.of(context).pop();
  }

  // ------------------------------------------------------------- geometry

  /// Where the PC picture sits inside the touch area (it is letterboxed).
  Rect _imageRect() {
    final screen = _client.hello.value?['screen'] as Map<String, dynamic>?;
    double w = (screen?['width'] as num?)?.toDouble() ?? 0;
    double h = (screen?['height'] as num?)?.toDouble() ?? 0;
    final img = _client.frame.value;
    if ((w <= 0 || h <= 0) && img != null) {
      w = img.width.toDouble();
      h = img.height.toDouble();
    }
    if (w <= 0 || h <= 0 || _area.isEmpty) return Offset.zero & _area;
    final fitted = applyBoxFit(BoxFit.contain, Size(w, h), _area).destination;
    return Alignment.center.inscribe(fitted, Offset.zero & _area);
  }

  Offset _toFraction(Offset local) {
    final r = _imageRect();
    if (r.isEmpty) return Offset.zero;
    return Offset(
      ((local.dx - r.left) / r.width).clamp(0.0, 1.0),
      ((local.dy - r.top) / r.height).clamp(0.0, 1.0),
    );
  }

  // --------------------------------------------------------------- output

  void _scheduleFlush() => _flushTimer ??= Timer(_flushEvery, _flush);

  void _flush() {
    _flushTimer = null;
    if (_pendAbs != null) {
      _client.sendAbs(_pendAbs!.dx, _pendAbs!.dy);
      _pendAbs = null;
    }
    if (_pendDx != 0 || _pendDy != 0) {
      _client.sendMove(_pendDx, _pendDy);
      _pendDx = _pendDy = 0;
    }
    if (_pendScrollX != 0 || _pendScrollY != 0) {
      _client.sendScroll(_pendScrollX, _pendScrollY);
      _pendScrollX = _pendScrollY = 0;
    }
  }

  void _click(String button) {
    // Anything queued (e.g. the move that put the cursor on the target) must
    // reach the PC BEFORE the click, or it would click at the old position.
    _flushTimer?.cancel();
    _flush();
    _client.sendButton(button, true);
    _client.sendButton(button, false);
  }

  // --------------------------------------------------------------- touch

  void _onDown(PointerDownEvent e) {
    if (!_client.inputAllowed) return;
    if (_pointers.isEmpty) {
      _downAt = DateTime.now();
      _travel = 0;
      _maxPointers = 0;
    }
    _pointers[e.pointer] = e.localPosition;
    if (_pointers.length > _maxPointers) _maxPointers = _pointers.length;

    if (_mode == TouchMode.direct && _pointers.length == 1) {
      _pendAbs = _toFraction(e.localPosition);
      _scheduleFlush();
    }
  }

  void _onMove(PointerMoveEvent e) {
    if (!_pointers.containsKey(e.pointer)) return;
    _pointers[e.pointer] = e.localPosition;
    final d = e.delta;
    _travel += d.distance;

    final n = _pointers.length;
    if (n >= 2) {
      // Two (or more) fingers: scroll. Each finger reports its own movement,
      // so dividing by n gives the movement of their midpoint. The sign makes
      // it "natural": finger up = page moves up = view goes down.
      _pendScrollY += -d.dy / n / _pxPerNotch;
      _pendScrollX += -d.dx / n / _pxPerNotch;
      _scheduleFlush();
      return;
    }
    // A finger left a multi-finger gesture: ignore the remaining one until
    // everything is lifted, or the cursor would jump as the scroll ends.
    if (_maxPointers > 1) return;

    if (_mode == TouchMode.direct) {
      _pendAbs = _toFraction(e.localPosition);
      _scheduleFlush();
      return;
    }

    if (_travel < _moveSlop) return;
    final r = _imageRect();
    if (r.isEmpty) return;
    // Pointer acceleration: slow movement stays precise, fast swipes cover
    // the screen in a short flick. Same idea as a laptop trackpad.
    final accel = _gain * (1 + (d.distance / 10).clamp(0.0, 2.0));
    _pendDx += d.dx * accel / r.width;
    _pendDy += d.dy * accel / r.height;
    _scheduleFlush();
  }

  void _onUp(PointerEvent e) {
    if (_pointers.remove(e.pointer) == null) return;
    if (_pointers.isNotEmpty) return;

    final wasTap = e is PointerUpEvent &&
        DateTime.now().difference(_downAt) < _tapMaxTime &&
        _travel < _tapSlop * (_maxPointers > 1 ? 1.5 : 1);
    if (wasTap) {
      switch (_maxPointers) {
        case 1:
          _click('left');
        case 2:
          _click('right');
        case 3:
          _click('middle');
      }
    } else {
      _flushTimer?.cancel();
      _flush(); // don't leave the last bit of movement in the queue
    }
    _maxPointers = 0;
  }

  void _toggleHold() {
    setState(() => _holdLeft = !_holdLeft);
    _client.sendButton('left', _holdLeft);
  }

  // ---------------------------------------------------------------- build

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: LayoutBuilder(
                builder: (context, constraints) {
                  _area = constraints.biggest;
                  return Listener(
                    behavior: HitTestBehavior.opaque,
                    onPointerDown: _onDown,
                    onPointerMove: _onMove,
                    onPointerUp: _onUp,
                    onPointerCancel: _onUp,
                    child: Stack(
                      fit: StackFit.expand,
                      children: [
                        // Only this subtree rebuilds per frame.
                        ValueListenableBuilder<ui.Image?>(
                          valueListenable: _client.frame,
                          builder: (context, image, _) {
                            if (image == null) {
                              // Nothing to show yet: say why (connecting,
                              // retrying, ...) instead of a bare spinner.
                              return Center(
                                child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    const CircularProgressIndicator(),
                                    const SizedBox(height: 12),
                                    ValueListenableBuilder<String>(
                                      valueListenable: _client.status,
                                      builder: (context, s, _) =>
                                          Text(s, style: const TextStyle(color: Colors.white70)),
                                    ),
                                  ],
                                ),
                              );
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
                        // The PC pointer (not part of the captured picture).
                        IgnorePointer(
                          child: ValueListenableBuilder<Offset?>(
                            valueListenable: _client.cursor,
                            builder: (context, pos, _) => CustomPaint(
                              painter: _CursorPainter(pos, _imageRect()),
                            ),
                          ),
                        ),
                        if (_showStats)
                          Positioned(left: 4, top: 4, child: IgnorePointer(child: _StatsOverlay(client: _client))),
                      ],
                    ),
                  );
                },
              ),
            ),
            // The panel sits between the picture and the buttons. The phone
            // keyboard that opens with it shrinks the picture area; touch
            // mapping uses the live LayoutBuilder size, so it stays correct.
            if (_showKeyboard) KeyboardPanel(client: _client),
            _buildBar(),
          ],
        ),
      ),
    );
  }

  Widget _buildBar() {
    return AnimatedBuilder(
      // Rebuild when "view only" becomes known (hello arrives).
      animation: _client.hello,
      builder: (context, _) {
        final enabled = _client.inputAllowed;
        return Container(
          color: const Color(0xFF1B1B1F),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
          child: Row(
            children: [
              Expanded(
                flex: 3,
                child: _HoldButton(
                  label: 'Left click',
                  enabled: enabled,
                  onDown: () => _client.sendButton('left', true),
                  onUp: () => _client.sendButton('left', false),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                flex: 3,
                child: _HoldButton(
                  label: 'Right click',
                  enabled: enabled,
                  onDown: () => _client.sendButton('right', true),
                  onUp: () => _client.sendButton('right', false),
                ),
              ),
              const SizedBox(width: 6),
              _BarToggle(
                label: 'Keyboard',
                icon: Icons.keyboard,
                active: _showKeyboard,
                enabled: enabled,
                onTap: () => setState(() => _showKeyboard = !_showKeyboard),
              ),
              const SizedBox(width: 6),
              _BarToggle(
                label: 'Hold',
                icon: Icons.push_pin,
                active: _holdLeft,
                enabled: enabled,
                onTap: _toggleHold,
              ),
              const SizedBox(width: 6),
              _BarToggle(
                label: _mode == TouchMode.touchpad ? 'Touchpad' : 'Direct',
                icon: _mode == TouchMode.touchpad ? Icons.touch_app : Icons.ads_click,
                active: false,
                enabled: enabled,
                onTap: () => setState(
                  () => _mode = _mode == TouchMode.touchpad ? TouchMode.direct : TouchMode.touchpad,
                ),
              ),
              const SizedBox(width: 6),
              _BarToggle(
                label: enabled ? 'Stats' : 'View only',
                icon: Icons.insights,
                active: _showStats,
                enabled: true,
                onTap: () => setState(() => _showStats = !_showStats),
              ),
            ],
          ),
        );
      },
    );
  }
}

/// A button that reports press and release separately (a normal button only
/// reports a finished tap, which cannot hold the mouse button for a drag).
class _HoldButton extends StatefulWidget {
  const _HoldButton({required this.label, required this.onDown, required this.onUp, required this.enabled});

  final String label;
  final VoidCallback onDown;
  final VoidCallback onUp;
  final bool enabled;

  @override
  State<_HoldButton> createState() => _HoldButtonState();
}

class _HoldButtonState extends State<_HoldButton> {
  bool _down = false;

  void _set(bool down) {
    if (!widget.enabled || _down == down) return;
    setState(() => _down = down);
    down ? widget.onDown() : widget.onUp();
  }

  @override
  void dispose() {
    // Never leave the PC's button pressed if this widget disappears mid-press.
    if (_down) widget.onUp();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Listener(
      onPointerDown: (_) => _set(true),
      onPointerUp: (_) => _set(false),
      onPointerCancel: (_) => _set(false),
      child: Container(
        height: 48,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: !widget.enabled
              ? Colors.white10
              : _down
                  ? scheme.primary
                  : scheme.primaryContainer,
          borderRadius: BorderRadius.circular(10),
        ),
        child: Text(
          widget.label,
          style: TextStyle(
            fontWeight: FontWeight.w600,
            color: !widget.enabled
                ? Colors.white38
                : _down
                    ? scheme.onPrimary
                    : scheme.onPrimaryContainer,
          ),
        ),
      ),
    );
  }
}

class _BarToggle extends StatelessWidget {
  const _BarToggle({
    required this.label,
    required this.icon,
    required this.active,
    required this.enabled,
    required this.onTap,
  });

  final String label;
  final IconData icon;
  final bool active;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = !enabled ? Colors.white38 : (active ? scheme.primary : Colors.white70);
    return InkWell(
      onTap: enabled ? onTap : null,
      borderRadius: BorderRadius.circular(10),
      child: SizedBox(
        // Narrow on purpose: six controls share the bar, and in portrait the
        // two click buttons still need room.
        width: 52,
        height: 48,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, size: 20, color: color),
            Text(label, style: TextStyle(fontSize: 10, color: color), maxLines: 1, overflow: TextOverflow.clip),
          ],
        ),
      ),
    );
  }
}

/// Draws a mouse-pointer arrow where the PC's pointer is.
class _CursorPainter extends CustomPainter {
  _CursorPainter(this.pos, this.rect);

  final Offset? pos; // fraction of the PC screen; outside 0..1 = other monitor
  final Rect rect; // where the picture is drawn

  @override
  void paint(Canvas canvas, Size size) {
    final p = pos;
    if (p == null || p.dx < 0 || p.dx > 1 || p.dy < 0 || p.dy > 1 || rect.isEmpty) return;
    final tip = Offset(rect.left + p.dx * rect.width, rect.top + p.dy * rect.height);
    // A classic arrow, ~16 logical px tall, tip at the exact pointer position.
    final path = Path()
      ..moveTo(tip.dx, tip.dy)
      ..lineTo(tip.dx, tip.dy + 16)
      ..lineTo(tip.dx + 4, tip.dy + 12.5)
      ..lineTo(tip.dx + 7.5, tip.dy + 19)
      ..lineTo(tip.dx + 10, tip.dy + 18)
      ..lineTo(tip.dx + 6.5, tip.dy + 11.5)
      ..lineTo(tip.dx + 11.5, tip.dy + 11.5)
      ..close();
    canvas.drawPath(path, Paint()..color = Colors.white);
    canvas.drawPath(
      path,
      Paint()
        ..color = Colors.black
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.2,
    );
  }

  @override
  bool shouldRepaint(_CursorPainter old) => old.pos != pos || old.rect != rect;
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
