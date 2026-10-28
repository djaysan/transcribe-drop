// notifier — posts Transcribe Drop's notifications, so they show the app's own name and icon
// (osascript's show Script Editor's). install.sh builds it into
// Contents/Helpers/Notifier.app, and the app runs:  open -g -n Notifier.app --args "message"
//
// Opened without a message, which is what macOS does when a notification is clicked, it
// asks the running app to show its window instead.
import Foundation
import UserNotifications

guard let message = CommandLine.arguments.dropFirst().first else {
  // Same name transcribe-app.py listens for (SHOW_NOTE).
  DistributedNotificationCenter.default().postNotificationName(
    Notification.Name("com.lverbeeck.transcribe-drop.show"), object: nil, userInfo: nil, deliverImmediately: true)
  exit(0)
}

let center = UNUserNotificationCenter.current()
// The first time, macOS asks whether "Transcribe Drop" may send notifications.
center.requestAuthorization(options: [.alert]) { granted, _ in
  guard granted else { exit(0) }
  let content = UNMutableNotificationContent()
  content.title = "Transcribe Drop"
  content.body = message
  center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { _ in
    exit(0)
  }
}
dispatchMain()
