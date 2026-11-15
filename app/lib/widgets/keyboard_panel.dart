// Keyboard input panel: a text field that opens the phone's own keyboard, plus
// a strip of keys the phone keyboard lacks (Esc, Tab, arrows, Ctrl/Alt/Shift...).
//
// HOW TYPING IS FORWARDED: the field is only a capture device. Every change is
// compared with the previous text and turned into the keystrokes that explain
// the difference: added characters -> 'text' messages, removed characters ->
// Backspace presses. The PC is never sent "the whole text", so it can be typed
// into any application, at any cursor position.
//
// THE SENTINEL: the field always starts with one invisible character. Without
// it, pressing Backspace in an empty field changes nothing, so no event would
// reach us and the PC would never see the Backspace.

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/ws_client.dart';

class KeyboardPanel extends StatefulWidget {
  const KeyboardPanel({super.key, required this.client});

  final WsClient client;

  @override
  State<KeyboardPanel> createState() => _KeyboardPanelState();
}

class _KeyboardPanelState extends State<KeyboardPanel> {
  // Zero-width space: invisible, and not something a user would type.
  static const String _sentinel = '​';
  // Start over when the buffer grows: keeps the cursor near the end and the
  // diff cheap, however long the user types.
  static const int _maxBuffer = 40;

  final _text = TextEditingController(text: _sentinel);
  final _focus = FocusNode();
  String _prev = _sentinel;

  // Modifiers the user tapped and that apply to the NEXT key only ("sticky"
  // one-shot), because a phone keyboard cannot hold Ctrl while pressing C.
  final Set<String> _mods = <String>{};

  @override
  void initState() {
    super.initState();
    _text.selection = const TextSelection.collapsed(offset: _sentinel.length);
    // Opening the panel should bring up the phone keyboard immediately.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _focus.requestFocus();
    });
  }

  @override
  void dispose() {
    // Never leave Ctrl/Alt/Shift pressed on the PC.
    for (final m in _mods) {
      widget.client.sendKey(m, false);
    }
    _text.dispose();
    _focus.dispose();
    super.dispose();
  }

  // ----------------------------------------------------------------- output

  void _press(String name) {
    widget.client.sendKey(name, true);
    widget.client.sendKey(name, false);
  }

  /// Run [action] wrapped in the latched modifiers, then release them.
  void _withMods(void Function() action) {
    for (final m in _mods) {
      widget.client.sendKey(m, true);
    }
    action();
    for (final m in _mods) {
      widget.client.sendKey(m, false);
    }
    if (_mods.isNotEmpty) setState(_mods.clear);
  }

  /// A key from the strip (Esc, arrows, ...), with any latched modifiers.
  void _tapKey(String name) => _withMods(() => _press(name));

  void _toggleMod(String name) {
    setState(() {
      if (!_mods.remove(name)) _mods.add(name);
    });
  }

  static final RegExp _keyLike = RegExp(r'^[a-zA-Z0-9]$');

  void _onChanged(String value) {
    final prev = _prev;
    var p = 0;
    while (p < prev.length && p < value.length && prev[p] == value[p]) {
      p++;
    }
    final removed = prev.length - p;
    final added = value.substring(p);

    for (var i = 0; i < removed; i++) {
      _press('backspace');
    }
    var chord = false;
    if (added.isNotEmpty) {
      if (_mods.isEmpty) {
        widget.client.sendText(added);
      } else {
        chord = true;
        // Ctrl+C and friends need real key events; typed characters would
        // not trigger shortcuts. Latched modifiers apply to the first
        // character only, like every sticky key.
        final first = added.substring(0, 1);
        final rest = added.substring(1);
        if (_keyLike.hasMatch(first)) {
          _withMods(() => _press(first.toLowerCase()));
        } else {
          _withMods(() => widget.client.sendText(first));
        }
        if (rest.isNotEmpty) widget.client.sendText(rest);
      }
    }

    // After a shortcut the character must not stay in the field (the PC did
    // not receive it as text, so a visible "c" would be misleading).
    if (chord || value.length < _sentinel.length || value.length > _maxBuffer) {
      _text.value = const TextEditingValue(
        text: _sentinel,
        selection: TextSelection.collapsed(offset: 1),
      );
      _prev = _sentinel;
    } else {
      _prev = value;
    }
  }

  void _onEnter() {
    _tapKey('enter');
    _focus.requestFocus(); // keep the phone keyboard open for the next line
  }

  // ------------------------------------------------------------------ build

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF26262C),
      padding: const EdgeInsets.fromLTRB(8, 6, 8, 6),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: [
                for (final m in const [('ctrl', 'Ctrl'), ('alt', 'Alt'), ('shift', 'Shift')])
                  _Key(label: m.$2, active: _mods.contains(m.$1), onTap: () => _toggleMod(m.$1)),
                _Key(label: 'Win', onTap: () => _press('win')),
                const _Gap(),
                _Key(label: 'Esc', onTap: () => _tapKey('escape')),
                _Key(label: 'Tab', onTap: () => _tapKey('tab')),
                _Key(label: '⌫', onTap: () => _tapKey('backspace')),
                _Key(label: 'Del', onTap: () => _tapKey('delete')),
                _Key(label: 'Enter', onTap: _onEnter),
                const _Gap(),
                _Key(label: '←', onTap: () => _tapKey('left')),
                _Key(label: '↑', onTap: () => _tapKey('up')),
                _Key(label: '↓', onTap: () => _tapKey('down')),
                _Key(label: '→', onTap: () => _tapKey('right')),
                const _Gap(),
                _Key(label: 'Home', onTap: () => _tapKey('home')),
                _Key(label: 'End', onTap: () => _tapKey('end')),
                _Key(label: 'PgUp', onTap: () => _tapKey('pageup')),
                _Key(label: 'PgDn', onTap: () => _tapKey('pagedown')),
              ],
            ),
          ),
          const SizedBox(height: 6),
          TextField(
            controller: _text,
            focusNode: _focus,
            onChanged: _onChanged,
            // Finishing the line sends Enter; the default would also drop
            // focus, which closes the phone keyboard after every line.
            onEditingComplete: _onEnter,
            textInputAction: TextInputAction.send,
            // "visiblePassword" makes Android keyboards deliver raw
            // characters: no predictive text, no autocorrect rewriting words
            // we already sent to the PC.
            keyboardType: TextInputType.visiblePassword,
            autocorrect: false,
            enableSuggestions: false,
            enableInteractiveSelection: false,
            inputFormatters: [FilteringTextInputFormatter.deny(RegExp(r'[\r\n]'))],
            style: const TextStyle(color: Colors.white),
            decoration: const InputDecoration(
              isDense: true,
              hintText: 'Type here - goes to the PC',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
    );
  }
}

class _Gap extends StatelessWidget {
  const _Gap();

  @override
  Widget build(BuildContext context) => const SizedBox(width: 10);
}

class _Key extends StatelessWidget {
  const _Key({required this.label, required this.onTap, this.active = false});

  final String label;
  final VoidCallback onTap;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(right: 4),
      child: Material(
        color: active ? scheme.primary : Colors.white12,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: onTap,
          child: Container(
            constraints: const BoxConstraints(minWidth: 44, minHeight: 38),
            alignment: Alignment.center,
            padding: const EdgeInsets.symmetric(horizontal: 10),
            child: Text(
              label,
              style: TextStyle(
                fontWeight: FontWeight.w600,
                color: active ? scheme.onPrimary : Colors.white,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
