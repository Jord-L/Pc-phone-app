// App entry point. Milestone 2: connect to the server and render frames.

import 'package:flutter/material.dart';

import 'screens/connect_screen.dart';

void main() {
  runApp(const PhoneDeskApp());
}

class PhoneDeskApp extends StatelessWidget {
  const PhoneDeskApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'PhoneDesk',
      debugShowCheckedModeBanner: false,
      // Dark by default: the remote view is mostly a black letterboxed
      // image, and a white UI around it is glaring.
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal, brightness: Brightness.dark),
        useMaterial3: true,
      ),
      home: const ConnectScreen(),
    );
  }
}
