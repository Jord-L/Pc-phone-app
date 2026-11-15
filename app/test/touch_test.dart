// Gesture -> network message tests for RemoteScreen.
//
// WHY a real local WebSocket server instead of a mock: it exercises the whole
// path (touch events -> coalescing -> JSON -> socket) exactly as it runs on the
// phone. flutter test normally blocks real network access (HttpOverrides), so
// each test lifts that and talks to 127.0.0.1 inside tester.runAsync.

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:phonedesk/screens/remote_screen.dart';

class _FakePc {
  late HttpServer server;
  final received = <Map<String, dynamic>>[];

  Future<void> start({bool input = true}) async {
    server = await HttpServer.bind('127.0.0.1', 0);
    server.listen((req) async {
      final ws = await WebSocketTransformer.upgrade(req);
      ws.add(jsonEncode({
        'type': 'hello',
        'input': input,
        'screen': {'width': 1920, 'height': 1080},
        'stream': {'max_width': 640, 'quality': 40, 'fps': 15},
      }));
      ws.listen((m) {
        if (m is String) received.add(jsonDecode(m) as Map<String, dynamic>);
      });
    });
  }

  List<Map<String, dynamic>> of(String type) => received.where((m) => m['type'] == type).toList();
}

Future<_FakePc> _open(WidgetTester tester, {bool input = true, String pin = ''}) async {
  HttpOverrides.global = null;
  final pc = _FakePc();
  await tester.runAsync(() => pc.start(input: input));
  await tester.pumpWidget(
    MaterialApp(home: RemoteScreen(host: '127.0.0.1', port: pc.server.port, pin: pin)),
  );
  await _settle(tester); // connect + receive hello
  return pc;
}

/// Lets real socket I/O happen, then lets fake-async timers (the 16 ms output
/// coalescing) fire.
Future<void> _settle(WidgetTester tester) async {
  // Real I/O only progresses inside runAsync, but the continuations of that
  // I/O (and the output timers) live in the fake-async zone and only run on
  // pump. A connect -> auth -> hello -> gesture round trip needs several
  // alternations of the two.
  // Each queued message can need its own turn of this loop (a socket write
  // completes in real time, its continuation runs on the next pump).
  for (var i = 0; i < 24; i++) {
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 15)));
    await tester.pump(const Duration(milliseconds: 20));
  }
  // The final pump can queue a socket write; give real I/O one last turn so
  // the server has actually received it before the test reads `received`.
  await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 80)));
}

Future<void> _close(WidgetTester tester, _FakePc pc) async {
  await tester.pumpWidget(const SizedBox()); // disposes the screen + socket
  await _settle(tester); // the close message (and any release) reaches the server
  await tester.runAsync(() => pc.server.close(force: true));
  // dart:io's WebSocket close arms a 5 s timer waiting for the peer's reply;
  // run fake time past it so the test ends with no timers pending.
  await tester.pump(const Duration(seconds: 6));
}

void main() {
  testWidgets('sends the PIN first', (tester) async {
    final pc = await _open(tester, pin: '123456');
    expect(pc.received.first, {'type': 'auth', 'pin': '123456'});
    await _close(tester, pc);
  });

  testWidgets('touchpad: one-finger drag moves the cursor relatively', (tester) async {
    final pc = await _open(tester);
    final g = await tester.startGesture(const Offset(300, 200));
    await g.moveBy(const Offset(40, 0));
    await g.moveBy(const Offset(0, 20));
    await g.up();
    await _settle(tester);

    final moves = pc.of('move');
    expect(moves, isNotEmpty);
    final dx = moves.fold<double>(0, (a, m) => a + (m['dx'] as num));
    final dy = moves.fold<double>(0, (a, m) => a + (m['dy'] as num));
    expect(dx, greaterThan(0));
    expect(dy, greaterThan(0));
    expect(pc.of('button'), isEmpty, reason: 'a drag must not click');
    await _close(tester, pc);
  });

  testWidgets('touchpad: tap = left click, two-finger tap = right click', (tester) async {
    final pc = await _open(tester);

    final t = await tester.startGesture(const Offset(300, 200));
    await t.up();
    await _settle(tester);
    expect(pc.of('button').map((m) => [m['btn'], m['down']]).toList(), [
      ['left', true],
      ['left', false],
    ]);
    pc.received.clear();

    final a = await tester.startGesture(const Offset(300, 200), pointer: 1);
    final b = await tester.startGesture(const Offset(340, 200), pointer: 2);
    await a.up();
    await b.up();
    await _settle(tester);
    expect(pc.of('button').map((m) => [m['btn'], m['down']]).toList(), [
      ['right', true],
      ['right', false],
    ]);
    await _close(tester, pc);
  });

  testWidgets('two fingers dragging up scroll the page down', (tester) async {
    final pc = await _open(tester);
    final a = await tester.startGesture(const Offset(300, 300), pointer: 1);
    final b = await tester.startGesture(const Offset(340, 300), pointer: 2);
    await a.moveBy(const Offset(0, -60));
    await b.moveBy(const Offset(0, -60));
    await a.up();
    await b.up();
    await _settle(tester);

    final scrolls = pc.of('scroll');
    expect(scrolls, isNotEmpty);
    final dy = scrolls.fold<double>(0, (s, m) => s + (m['dy'] as num));
    expect(dy, greaterThan(0), reason: 'finger up = natural scroll = content down');
    expect(pc.of('move'), isEmpty);
    expect(pc.of('button'), isEmpty);
    await _close(tester, pc);
  });

  testWidgets('Left/Right buttons send press and release separately', (tester) async {
    final pc = await _open(tester);
    final g = await tester.startGesture(tester.getCenter(find.text('Left click')));
    await _settle(tester);
    expect(pc.of('button').map((m) => [m['btn'], m['down']]).toList(), [
      ['left', true],
    ], reason: 'still held down');
    await g.up();
    await _settle(tester);
    expect(pc.of('button').last, {'type': 'button', 'btn': 'left', 'down': false});

    pc.received.clear();
    final r = await tester.startGesture(tester.getCenter(find.text('Right click')));
    await r.up();
    await _settle(tester);
    expect(pc.of('button').map((m) => [m['btn'], m['down']]).toList(), [
      ['right', true],
      ['right', false],
    ]);
    await _close(tester, pc);
  });

  testWidgets('direct mode: touch puts the cursor under the finger, tap clicks there', (tester) async {
    final pc = await _open(tester);
    await tester.tap(find.text('Touchpad')); // bar toggle -> Direct
    await tester.pump();
    pc.received.clear();

    final centre = tester.getCenter(find.byType(RemoteScreen));
    final t = await tester.startGesture(Offset(centre.dx, 250));
    await t.up();
    await _settle(tester);

    final abs = pc.of('abs');
    expect(abs, isNotEmpty);
    expect((abs.last['x'] as num).toDouble(), closeTo(0.5, 0.02), reason: 'horizontal centre');
    final types = pc.received.map((m) => m['type']).toList();
    expect(types.indexOf('abs'), lessThan(types.indexOf('button')), reason: 'move before click');
    await _close(tester, pc);
  });

  testWidgets('Hold latches the left button until toggled off or the screen closes', (tester) async {
    final pc = await _open(tester);
    await tester.tap(find.text('Hold'));
    await _settle(tester);
    expect(pc.of('button').last, {'type': 'button', 'btn': 'left', 'down': true});
    await _close(tester, pc); // leaving while held must release
    expect(pc.of('button').last, {'type': 'button', 'btn': 'left', 'down': false});
  });

  group('keyboard', () {
    List<List<Object?>> keys(_FakePc pc) =>
        pc.of('key').map((m) => <Object?>[m['name'], m['down']]).toList();

    Future<void> openKeyboard(WidgetTester tester) async {
      await tester.tap(find.text('Keyboard'));
      await tester.pump();
    }

    testWidgets('typed characters arrive as text, deletions as Backspace', (tester) async {
      final pc = await _open(tester);
      await openKeyboard(tester);
      pc.received.clear();
      const s = '​'; // the invisible sentinel the field always starts with

      await tester.enterText(find.byType(TextField), '${s}hi');
      await _settle(tester);
      expect(pc.of('text').map((m) => m['text']).join(), 'hi');

      await tester.enterText(find.byType(TextField), '${s}h'); // deleted "i"
      await tester.enterText(find.byType(TextField), s); // deleted "h"
      await tester.enterText(find.byType(TextField), ''); // Backspace on the empty field
      await _settle(tester);
      expect(keys(pc), [
        ['backspace', true], ['backspace', false],
        ['backspace', true], ['backspace', false],
        ['backspace', true], ['backspace', false],
      ]);
      await _close(tester, pc);
    });

    testWidgets('strip keys send real key presses', (tester) async {
      final pc = await _open(tester);
      await openKeyboard(tester);
      pc.received.clear();

      await tester.tap(find.text('Esc'));
      await tester.tap(find.text('←'));
      await _settle(tester);
      expect(keys(pc), [
        ['escape', true], ['escape', false],
        ['left', true], ['left', false],
      ]);
      await _close(tester, pc);
    });

    testWidgets('Ctrl is one-shot: Ctrl+C, then plain typing again', (tester) async {
      final pc = await _open(tester);
      await openKeyboard(tester);
      pc.received.clear();
      const s = '​';

      await tester.tap(find.text('Ctrl'));
      await tester.enterText(find.byType(TextField), '${s}c');
      await _settle(tester);
      expect(keys(pc), [
        ['ctrl', true], ['c', true], ['c', false], ['ctrl', false],
      ]);

      pc.received.clear();
      await tester.enterText(find.byType(TextField), '${s}c'); // buffer was reset, "c" again
      await _settle(tester);
      expect(keys(pc), isEmpty, reason: 'modifier was released after one key');
      expect(pc.of('text').map((m) => m['text']).join(), 'c');
      await _close(tester, pc);
    });

    testWidgets('a latched modifier is released when the panel closes', (tester) async {
      final pc = await _open(tester);
      await openKeyboard(tester);
      await tester.tap(find.text('Alt'));
      await _settle(tester);
      expect(keys(pc), isEmpty, reason: 'latching alone presses nothing');
      await _close(tester, pc);
      expect(keys(pc), [['alt', false]]);
    });
  });

  testWidgets('view-only server: touches and buttons send nothing', (tester) async {
    final pc = await _open(tester, input: false);
    final g = await tester.startGesture(const Offset(300, 200));
    await g.moveBy(const Offset(40, 0));
    await g.up();
    final b = await tester.startGesture(tester.getCenter(find.text('Left click')));
    await b.up();
    await _settle(tester);
    expect(pc.received.where((m) => m['type'] != 'ack'), isEmpty);
    await _close(tester, pc);
  });
}
