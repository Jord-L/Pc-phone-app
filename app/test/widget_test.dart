// Placeholder so `flutter create` does not generate its default counter-app
// test, which would reference a widget that does not exist in this project.
import 'package:flutter_test/flutter_test.dart';
import 'package:phonedesk/main.dart';

void main() {
  testWidgets('app starts on the connect screen', (tester) async {
    await tester.pumpWidget(const PhoneDeskApp());
    expect(find.text('Connect'), findsOneWidget);
  });
}
