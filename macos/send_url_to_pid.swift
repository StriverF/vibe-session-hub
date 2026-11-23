import AppKit
import CoreServices
import Foundation

guard CommandLine.arguments.count >= 3,
      let pidInt = Int32(CommandLine.arguments[1]) else {
    fputs("Usage: send_url_to_pid <pid> <url>\n", stderr)
    exit(2)
}

let pid = pid_t(pidInt)
let urlString = CommandLine.arguments[2]

let target = NSAppleEventDescriptor(processIdentifier: pid)
let event = NSAppleEventDescriptor(
    eventClass: AEEventClass(kInternetEventClass),
    eventID: AEEventID(kAEGetURL),
    targetDescriptor: target,
    returnID: AEReturnID(kAutoGenerateReturnID),
    transactionID: AETransactionID(kAnyTransactionID)
)
event.setParam(NSAppleEventDescriptor(string: urlString), forKeyword: AEKeyword(keyDirectObject))

var reply = AppleEvent()
let status = AESendMessage(event.aeDesc, &reply, AESendMode(kAENoReply), kAEDefaultTimeout)
if status != noErr {
    fputs("AESendMessage failed with OSStatus \(status)\n", stderr)
    exit(1)
}

if let runningApp = NSRunningApplication(processIdentifier: pid) {
    if #available(macOS 14.0, *) {
        runningApp.activate()
    } else {
        runningApp.activate(options: [.activateIgnoringOtherApps])
    }
}
print("ok")
