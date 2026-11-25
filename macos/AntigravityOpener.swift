import AppKit
import ApplicationServices
import Carbon.HIToolbox
import CoreServices
import Foundation

// MARK: - Pure Testable Logic for Antigravity FastPick Session Opening

let antigravityBundleId = "com.google.antigravity"
let antigravityFocusURL = "antigravity://focus"
let antigravityAppDefaultPath = "/Applications/Antigravity.app"

enum AntigravityFastPickMode: String, Equatable {
    case conversations
    case unified
    case otherCategory
    case unknownFastPick
}

struct AntigravityCandidateItem: Equatable {
    let index: Int
    let primaryText: String
    let fullText: String
    let isHighlighted: Bool
}

struct AntigravityOpenResult: Codable, Equatable {
    let ok: Bool
    let status: String
    let action: String
    let message: String
    let target: String
    let needsAccessibility: Bool
    let coldStarted: Bool
    let matchedIndex: Int?

    enum CodingKeys: String, CodingKey {
        case ok
        case status
        case action
        case message
        case target
        case needsAccessibility = "needs_accessibility"
        case coldStarted = "cold_started"
        case matchedIndex = "matched_index"
    }

    func toDictionary() -> [String: Any] {
        var dict: [String: Any] = [
            "ok": ok,
            "status": status,
            "action": action,
            "message": message,
            "target": target,
            "needs_accessibility": needsAccessibility,
            "cold_started": coldStarted,
        ]
        if let idx = matchedIndex {
            dict["matched_index"] = idx
        }
        return dict
    }

    func toJSONString() -> String {
        let encoder = JSONEncoder()
        if let data = try? encoder.encode(self),
           let str = String(data: data, encoding: .utf8) {
            return str
        }
        return "{\"ok\":false,\"status\":\"error\",\"action\":\"error\",\"message\":\"JSON encoding failed\",\"target\":\"antigravity://focus\",\"needs_accessibility\":false,\"cold_started\":false}"
    }
}

private let antigravityReservedPrefixes: [String] = [
    "commands:",
    ">",
    "files:",
    "conversations:",
    "projects:",
    "search:",
]

func normalizeAntigravitySearchText(_ raw: String) -> String {
    let replaced = raw.replacingOccurrences(
        of: "\\s+",
        with: " ",
        options: .regularExpression
    )
    return replaced.trimmingCharacters(in: .whitespacesAndNewlines)
}

func normalizeAntigravitySearchQuery(_ rawTitle: String, fallbackSessionId: String = "") -> String {
    let cleanTitle = normalizeAntigravitySearchText(rawTitle)
    if !cleanTitle.isEmpty {
        return cleanTitle
    }
    return normalizeAntigravitySearchText(fallbackSessionId)
}

func classifyAntigravityFastPickPlaceholder(
    _ placeholder: String?,
    hasFastPickAncestor: Bool
) -> AntigravityFastPickMode? {
    let lower = normalizeAntigravitySearchText(placeholder ?? "").lowercased()
    if lower.contains("search for conversations") {
        return .conversations
    }
    if lower.contains("search conversations") || lower.contains("select category to search") {
        return .unified
    }
    if hasFastPickAncestor {
        if lower.contains("search for commands")
            || lower.contains("search files")
            || lower.contains("search for files")
            || lower.contains("search projects")
            || lower.contains("search code")
        {
            return .otherCategory
        }
        return .unknownFastPick
    }
    return nil
}

func buildAntigravityPastePayload(query: String, mode: AntigravityFastPickMode) -> String {
    let clean = normalizeAntigravitySearchText(query)
    guard !clean.isEmpty else { return "" }

    let lower = clean.lowercased()
    if mode == .conversations {
        let hitsReservedPrefix = antigravityReservedPrefixes.contains { lower.hasPrefix($0) }
        return hitsReservedPrefix ? "conversations:\(clean)" : clean
    }
    return "conversations:\(clean)"
}

func extractWorkspaceHint(fromCwd cwd: String) -> String {
    let trimmed = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return "" }
    let normalized = (trimmed as NSString).standardizingPath
    let last = (normalized as NSString).lastPathComponent
    if last.isEmpty || last == "/" {
        return ""
    }
    return last
}

private func isIgnorableFastPickItemText(_ lowerText: String) -> Bool {
    if lowerText.isEmpty {
        return true
    }
    if lowerText == "no results found" {
        return true
    }
    if lowerText.hasPrefix("show ") && lowerText.contains(" more") {
        return true
    }
    return false
}

private func significantQueryTokens(_ normalizedLower: String) -> [String] {
    let parts = normalizedLower.components(separatedBy: CharacterSet.alphanumerics.inverted)
    return parts.filter { $0.count >= 2 }
}

private func stripTrailingEllipsis(_ text: String) -> String {
    var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
    while s.hasSuffix("...") || s.hasSuffix("…") {
        if s.hasSuffix("...") {
            s = String(s.dropLast(3)).trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            s = String(s.dropLast(1)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }
    return s
}

func selectBestAntigravityCandidate(
    items: [AntigravityCandidateItem],
    query: String,
    workspaceHint: String = ""
) -> Int? {
    let normQuery = normalizeAntigravitySearchText(query).lowercased()
    guard !normQuery.isEmpty else { return nil }

    let normWorkspace = normalizeAntigravitySearchText(workspaceHint).lowercased()
    let tokens = significantQueryTokens(normQuery)

    var bestIndex: Int?
    var bestScore = 0

    for item in items {
        let primary = normalizeAntigravitySearchText(item.primaryText).lowercased()
        let full = normalizeAntigravitySearchText(item.fullText).lowercased()
        if isIgnorableFastPickItemText(primary) && isIgnorableFastPickItemText(full) {
            continue
        }

        let primaryNoEllipsis = stripTrailingEllipsis(primary)
        var score = 0

        if primary == normQuery || full == normQuery {
            score = 1000
        } else if !primary.isEmpty && (primary.hasPrefix(normQuery) || full.hasPrefix(normQuery + " ")) {
            score = 800
        } else if !primaryNoEllipsis.isEmpty
            && primaryNoEllipsis.count >= 12
            && normQuery.hasPrefix(primaryNoEllipsis)
        {
            score = 700
        } else if !primary.isEmpty && primary.contains(normQuery) {
            score = 500
        } else if full.contains(normQuery) {
            score = 350
        } else if tokens.count >= 2 && tokens.allSatisfy({ full.contains($0) }) {
            score = 250
        }

        guard score > 0 else { continue }

        if !normWorkspace.isEmpty && full.contains(normWorkspace) {
            score += 50
        }
        if item.isHighlighted {
            score += 5
        }

        if score > bestScore {
            bestScore = score
            bestIndex = item.index
        }
    }

    return bestIndex
}

// MARK: - macOS Native Automation Implementation

private struct ClipboardSnapshot {
    let items: [[NSPasteboard.PasteboardType: Data]]

    static func capture(from pasteboard: NSPasteboard) -> ClipboardSnapshot {
        var captured: [[NSPasteboard.PasteboardType: Data]] = []
        for item in pasteboard.pasteboardItems ?? [] {
            var map: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) {
                    map[type] = data
                }
            }
            if !map.isEmpty {
                captured.append(map)
            }
        }
        return ClipboardSnapshot(items: captured)
    }

    func restore(to pasteboard: NSPasteboard, expectedChangeCount: Int) {
        guard pasteboard.changeCount == expectedChangeCount else {
            return
        }
        pasteboard.clearContents()
        guard !items.isEmpty else { return }
        let pbItems: [NSPasteboardItem] = items.map { map in
            let pbItem = NSPasteboardItem()
            for (type, data) in map {
                pbItem.setData(data, forType: type)
            }
            return pbItem
        }
        pasteboard.writeObjects(pbItems)
    }
}

private struct DetectedFastPick {
    let inputElement: AXUIElement
    let mode: AntigravityFastPickMode
    let container: AXUIElement?
}

private struct FastPickListSnapshot {
    let items: [AntigravityCandidateItem]
    let elements: [AXUIElement]
    let hasEmptyNotice: Bool
    let hasListbox: Bool
}

enum AntigravityOpener {
    private static func copyStringToPasteboard(_ text: String) -> Int {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
        return pb.changeCount
    }

    private static func findRunningAntigravityApp() -> NSRunningApplication? {
        let apps = NSRunningApplication.runningApplications(
            withBundleIdentifier: antigravityBundleId
        ).filter { !$0.isTerminated }
        if let regular = apps.first(where: { $0.activationPolicy == .regular }) {
            return regular
        }
        return apps.first
    }

    private static func activateApp(_ app: NSRunningApplication) {
        app.unhide()
        if #available(macOS 14.0, *) {
            app.activate()
        } else {
            app.activate(options: [.activateIgnoringOtherApps])
        }
    }

    private static func sendFocusAppleEvent(to pid: pid_t) -> Bool {
        let target = NSAppleEventDescriptor(processIdentifier: pid)
        let event = NSAppleEventDescriptor(
            eventClass: AEEventClass(kInternetEventClass),
            eventID: AEEventID(kAEGetURL),
            targetDescriptor: target,
            returnID: AEReturnID(kAutoGenerateReturnID),
            transactionID: AETransactionID(kAnyTransactionID)
        )
        event.setParam(
            NSAppleEventDescriptor(string: antigravityFocusURL),
            forKeyword: AEKeyword(keyDirectObject)
        )
        var reply = AppleEvent()
        let status = AESendMessage(
            event.aeDesc,
            &reply,
            AESendMode(kAENoReply),
            kAEDefaultTimeout
        )
        return status == noErr
    }

    @discardableResult
    private static func runOpenCommand(_ arguments: [String]) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = arguments
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus == 0
        } catch {
            return false
        }
    }

    private static func openFocusDeepLinkViaSystem() -> Bool {
        if runOpenCommand(["-b", antigravityBundleId, antigravityFocusURL]) {
            return true
        }
        if FileManager.default.fileExists(atPath: antigravityAppDefaultPath) {
            return runOpenCommand(["-a", antigravityAppDefaultPath, antigravityFocusURL])
        }
        return false
    }

    private static func wakeAndFocusAntigravity() -> (app: NSRunningApplication?, coldStarted: Bool, ok: Bool) {
        if let existing = findRunningAntigravityApp() {
            var woke = sendFocusAppleEvent(to: existing.processIdentifier)
            activateApp(existing)
            if !woke {
                woke = openFocusDeepLinkViaSystem()
                activateApp(existing)
            }
            return (existing, false, woke || !existing.isTerminated)
        }

        let launched = openFocusDeepLinkViaSystem()
        guard launched else {
            return (nil, true, false)
        }

        let deadline = Date().addingTimeInterval(10.0)
        while Date() < deadline {
            if let app = findRunningAntigravityApp() {
                activateApp(app)
                return (app, true, true)
            }
            Thread.sleep(forTimeInterval: 0.15)
        }
        return (nil, true, false)
    }

    // MARK: - Accessibility Helpers

    private static func copyAXAttribute(_ element: AXUIElement, _ attr: String) -> CFTypeRef? {
        var value: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(element, attr as CFString, &value)
        guard err == .success else { return nil }
        return value
    }

    private static func copyAXString(_ element: AXUIElement, _ attr: String) -> String? {
        guard let value = copyAXAttribute(element, attr) else { return nil }
        if let str = value as? String {
            return str
        }
        if let attrStr = value as? NSAttributedString {
            return attrStr.string
        }
        return nil
    }

    private static func copyAXBool(_ element: AXUIElement, _ attr: String) -> Bool {
        guard let value = copyAXAttribute(element, attr) else { return false }
        if let num = value as? NSNumber {
            return num.boolValue
        }
        return false
    }

    private static func copyAXChildren(_ element: AXUIElement) -> [AXUIElement] {
        guard let value = copyAXAttribute(element, kAXChildrenAttribute),
              let arr = value as? [AXUIElement] else {
            return []
        }
        return arr
    }

    private static func copyAXDOMClasses(_ element: AXUIElement) -> [String] {
        guard let value = copyAXAttribute(element, "AXDOMClassList") else { return [] }
        if let arr = value as? [String] {
            return arr
        }
        return []
    }

    private static func copyAXParent(_ element: AXUIElement) -> AXUIElement? {
        guard let value = copyAXAttribute(element, kAXParentAttribute),
              CFGetTypeID(value) == AXUIElementGetTypeID() else {
            return nil
        }
        return unsafeBitCast(value, to: AXUIElement.self)
    }

    private static func enableElectronAccessibility(_ appElem: AXUIElement) {
        AXUIElementSetAttributeValue(appElem, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        AXUIElementSetAttributeValue(appElem, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    }

    private static func hasVisibleWindow(_ appElem: AXUIElement) -> Bool {
        if copyAXAttribute(appElem, kAXFocusedWindowAttribute) != nil {
            return true
        }
        if let wins = copyAXAttribute(appElem, kAXWindowsAttribute) as? [AXUIElement], !wins.isEmpty {
            return true
        }
        return false
    }

    private static func waitUntilActiveAndWindowReady(
        app: NSRunningApplication,
        appElem: AXUIElement,
        timeoutSec: TimeInterval
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSec)
        var lastReactivate = Date.distantPast
        while Date() < deadline {
            if app.isTerminated {
                return false
            }
            if !app.isActive && Date().timeIntervalSince(lastReactivate) >= 0.4 {
                activateApp(app)
                lastReactivate = Date()
            }
            enableElectronAccessibility(appElem)
            if app.isActive && hasVisibleWindow(appElem) {
                return true
            }
            Thread.sleep(forTimeInterval: 0.08)
        }
        return app.isActive
    }

    private static func findFastPickContainer(from element: AXUIElement) -> (container: AXUIElement?, hasClassMarker: Bool) {
        var current: AXUIElement? = element
        var fallbackDialog: AXUIElement?
        var fallbackListboxHolder: AXUIElement?

        for _ in 0..<10 {
            guard let node = current else { break }
            let classes = copyAXDOMClasses(node)
            if classes.contains("agy-fastpick") {
                return (node, true)
            }
            let role = copyAXString(node, kAXRoleAttribute) ?? ""
            if fallbackDialog == nil && (role == "AXDialog" || role == "AXSheet" || role == "AXPopover") {
                fallbackDialog = node
            }
            if fallbackListboxHolder == nil && containsDirectOrNearListbox(node) {
                fallbackListboxHolder = node
            }
            current = copyAXParent(node)
        }
        return (fallbackListboxHolder ?? fallbackDialog, false)
    }

    private static func containsDirectOrNearListbox(_ root: AXUIElement) -> Bool {
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var visited = 0
        while !queue.isEmpty && visited < 40 {
            let (node, depth) = queue.removeFirst()
            visited += 1
            let role = copyAXString(node, kAXRoleAttribute) ?? ""
            if role == kAXListRole || role == "AXList" {
                return true
            }
            if depth < 3 {
                for child in copyAXChildren(node) {
                    queue.append((child, depth + 1))
                }
            }
        }
        return false
    }

    private static func detectFastPickFocus(appElem: AXUIElement) -> DetectedFastPick? {
        guard let focusedVal = copyAXAttribute(appElem, kAXFocusedUIElementAttribute),
              CFGetTypeID(focusedVal) == AXUIElementGetTypeID() else {
            return nil
        }
        let focused = unsafeBitCast(focusedVal, to: AXUIElement.self)
        let role = copyAXString(focused, kAXRoleAttribute) ?? ""
        let isInputRole = role == kAXTextFieldRole
            || role == kAXComboBoxRole
            || role == "AXTextField"
            || role == "AXComboBox"
            || role == "AXSearchField"
        guard isInputRole else {
            return nil
        }

        let placeholder = copyAXString(focused, kAXPlaceholderValueAttribute)
            ?? copyAXString(focused, kAXDescriptionAttribute)
            ?? copyAXString(focused, kAXTitleAttribute)
            ?? copyAXString(focused, kAXHelpAttribute)
        let (container, hasClassMarker) = findFastPickContainer(from: focused)

        guard let mode = classifyAntigravityFastPickPlaceholder(
            placeholder,
            hasFastPickAncestor: hasClassMarker
        ) else {
            return nil
        }
        return DetectedFastPick(inputElement: focused, mode: mode, container: container)
    }

    private static func copyAXActionNames(_ element: AXUIElement) -> [String] {
        var names: CFArray?
        let err = AXUIElementCopyActionNames(element, &names)
        guard err == .success, let arr = names as? [String] else {
            return []
        }
        return arr
    }

    private static func collectNonButtonStaticTexts(
        in root: AXUIElement,
        maxNodes: Int = 40,
        descendRoot: Bool = false
    ) -> [String] {
        var texts: [String] = []
        var stack: [(AXUIElement, Bool)] = [(root, descendRoot)]
        var visited = 0

        while !stack.isEmpty && visited < maxNodes {
            let (node, forceDescend) = stack.removeLast()
            visited += 1
            let role = copyAXString(node, kAXRoleAttribute) ?? ""
            if !forceDescend && (role == kAXButtonRole || role == "AXButton") {
                continue
            }
            let children = copyAXChildren(node)
            if !forceDescend && (role == kAXStaticTextRole || role == "AXStaticText") && children.isEmpty {
                if let val = copyAXString(node, kAXValueAttribute)
                    ?? copyAXString(node, kAXTitleAttribute)
                {
                    let clean = normalizeAntigravitySearchText(val)
                    if !clean.isEmpty {
                        texts.append(clean)
                    }
                }
                continue
            }
            if children.isEmpty {
                if let val = copyAXString(node, kAXValueAttribute)
                    ?? copyAXString(node, kAXTitleAttribute)
                    ?? copyAXString(node, kAXDescriptionAttribute)
                {
                    let clean = normalizeAntigravitySearchText(val)
                    if !clean.isEmpty {
                        texts.append(clean)
                    }
                }
                continue
            }
            for child in children.reversed() {
                stack.append((child, false))
            }
        }
        return texts
    }

    private static func isOptionLikeElement(_ element: AXUIElement) -> Bool {
        let role = copyAXString(element, kAXRoleAttribute) ?? ""
        if role == "AXMenuItem" || role == "AXRow" || role == "AXCell" {
            return true
        }
        let subrole = copyAXString(element, kAXSubroleAttribute) ?? ""
        if subrole == "AXOutlineRow" || subrole == "AXTableRow" {
            return true
        }
        let roleDesc = (copyAXString(element, kAXRoleDescriptionAttribute) ?? "").lowercased()
        if roleDesc.contains("option") || roleDesc.contains("menu item") {
            return true
        }
        // Base UI Combobox.Item renders <div role="option" class="group px-2.5 cursor-pointer ...">
        let classes = copyAXDOMClasses(element)
        if classes.contains("cursor-pointer") && (classes.contains("group") || classes.contains("rounded-lg")) {
            return true
        }
        // Fallback: role="option" in Chromium maps to AXStaticText/AXGroup with AXPress action
        if (role == kAXStaticTextRole || role == "AXStaticText" || role == kAXGroupRole || role == "AXGroup")
            && copyAXActionNames(element).contains(kAXPressAction)
        {
            return true
        }
        return false
    }

    private static func findControlledListbox(from inputElement: AXUIElement) -> AXUIElement? {
        for attr in ["AXControls", kAXLinkedUIElementsAttribute] {
            if let linked = copyAXAttribute(inputElement, attr) as? [AXUIElement] {
                for elem in linked {
                    let role = copyAXString(elem, kAXRoleAttribute) ?? ""
                    if role == kAXListRole || role == "AXList" {
                        return elem
                    }
                }
            }
        }
        return nil
    }

    private static func snapshotFastPickResults(
        inputElement: AXUIElement,
        container: AXUIElement?
    ) -> FastPickListSnapshot {
        // 1. Prefer direct ARIA aria-controls link (Combobox.Input -> Combobox.List)
        var listbox: AXUIElement? = findControlledListbox(from: inputElement)

        // 2. Fallback: BFS inside FastPick container for AXList
        if listbox == nil, let container = container {
            var queue: [(AXUIElement, Int)] = [(container, 0)]
            var visited = 0
            while !queue.isEmpty && visited < 80 {
                let (node, depth) = queue.removeFirst()
                visited += 1
                let role = copyAXString(node, kAXRoleAttribute) ?? ""
                if role == kAXListRole || role == "AXList" {
                    listbox = node
                    break
                }
                if depth < 5 {
                    for child in copyAXChildren(node) {
                        queue.append((child, depth + 1))
                    }
                }
            }
        }

        guard let listElem = listbox else {
            // Check if container mentions "No results found" even without an AXList
            if let container = container {
                let allTexts = collectNonButtonStaticTexts(in: container, maxNodes: 80, descendRoot: true)
                let emptyHit = allTexts.contains { $0.lowercased() == "no results found" }
                return FastPickListSnapshot(items: [], elements: [], hasEmptyNotice: emptyHit, hasListbox: false)
            }
            return FastPickListSnapshot(items: [], elements: [], hasEmptyNotice: false, hasListbox: false)
        }

        // Collect option elements inside listbox (Combobox.Group wraps Combobox.Item one level deep)
        var optionElements: [AXUIElement] = []
        var hasEmptyNotice = false
        var listQueue: [(AXUIElement, Int)] = [(listElem, 0)]
        var listVisited = 0

        while !listQueue.isEmpty && listVisited < 120 {
            let (node, depth) = listQueue.removeFirst()
            listVisited += 1

            if depth > 0 {
                if isOptionLikeElement(node) {
                    optionElements.append(node)
                    continue
                }

                let role = copyAXString(node, kAXRoleAttribute) ?? ""
                if role == kAXStaticTextRole || role == "AXStaticText" {
                    let txt = normalizeAntigravitySearchText(
                        copyAXString(node, kAXValueAttribute) ?? copyAXString(node, kAXTitleAttribute) ?? ""
                    ).lowercased()
                    if txt == "no results found" {
                        hasEmptyNotice = true
                    }
                    continue
                }
            }

            if depth < 3 {
                for child in copyAXChildren(node) {
                    listQueue.append((child, depth + 1))
                }
            }
        }

        var candidates: [AntigravityCandidateItem] = []
        var keptElements: [AXUIElement] = []

        for elem in optionElements {
            let texts = collectNonButtonStaticTexts(in: elem, maxNodes: 30, descendRoot: true)
            let directTitle = normalizeAntigravitySearchText(
                copyAXString(elem, kAXTitleAttribute)
                    ?? copyAXString(elem, kAXDescriptionAttribute)
                    ?? copyAXString(elem, kAXValueAttribute)
                    ?? ""
            )
            let primary = texts.first ?? directTitle
            let fullParts = texts.isEmpty ? (directTitle.isEmpty ? [] : [directTitle]) : texts
            let full = normalizeAntigravitySearchText(fullParts.joined(separator: " "))

            let lowerFull = full.lowercased()
            if lowerFull == "no results found" {
                hasEmptyNotice = true
                continue
            }
            if isIgnorableFastPickItemText(primary.lowercased()) && isIgnorableFastPickItemText(lowerFull) {
                continue
            }

            let isHighlighted = copyAXBool(elem, kAXSelectedAttribute)
                || copyAXBool(elem, kAXFocusedAttribute)
            let idx = candidates.count
            candidates.append(
                AntigravityCandidateItem(
                    index: idx,
                    primaryText: primary,
                    fullText: full,
                    isHighlighted: isHighlighted
                )
            )
            keptElements.append(elem)
        }

        return FastPickListSnapshot(
            items: candidates,
            elements: keptElements,
            hasEmptyNotice: hasEmptyNotice,
            hasListbox: true
        )
    }

    // MARK: - Keyboard Event Helpers

    private static func postKey(
        virtualKey: CGKeyCode,
        flags: CGEventFlags = []
    ) {
        let source = CGEventSource(stateID: .hidSystemState)
        if let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true) {
            down.flags = flags
            down.post(tap: .cghidEventTap)
        }
        usleep(12_000)
        if let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false) {
            up.flags = flags
            up.post(tap: .cghidEventTap)
        }
    }

    private static func pressCommandKey(_ virtualKey: Int) {
        postKey(virtualKey: CGKeyCode(virtualKey), flags: .maskCommand)
    }

    private static func pressPlainKey(_ virtualKey: Int) {
        postKey(virtualKey: CGKeyCode(virtualKey), flags: [])
    }

    // MARK: - Main Entry Point

    static func openSession(
        sessionId: String,
        title: String,
        cwd: String = "",
        promptAccessibility: Bool = false
    ) -> AntigravityOpenResult {
        let query = normalizeAntigravitySearchQuery(title, fallbackSessionId: sessionId)
        let workspaceHint = extractWorkspaceHint(fromCwd: cwd)

        // Step 1: Always wake & bring Antigravity to front via antigravity://focus first
        let wake = wakeAndFocusAntigravity()
        guard wake.ok, let runningApp = wake.app else {
            if !query.isEmpty {
                _ = copyStringToPasteboard(query)
            }
            return AntigravityOpenResult(
                ok: false,
                status: "error",
                action: "error",
                message: "未能唤起 Antigravity，请确认已安装 /Applications/Antigravity.app",
                target: antigravityFocusURL,
                needsAccessibility: false,
                coldStarted: wake.coldStarted,
                matchedIndex: nil
            )
        }

        // If we have no title/query at all, waking the app is all we can do
        guard !query.isEmpty else {
            return AntigravityOpenResult(
                ok: true,
                status: "focused",
                action: "focus_app",
                message: "已唤起并置顶 Antigravity 窗口",
                target: antigravityFocusURL,
                needsAccessibility: false,
                coldStarted: wake.coldStarted,
                matchedIndex: nil
            )
        }

        // Step 2: Check macOS Accessibility permission before any UI automation
        let isTrusted: Bool
        if promptAccessibility {
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            isTrusted = AXIsProcessTrustedWithOptions(opts)
        } else {
            isTrusted = AXIsProcessTrusted()
        }

        guard isTrusted else {
            _ = copyStringToPasteboard(query)
            return AntigravityOpenResult(
                ok: true,
                status: "needs_accessibility",
                action: "focus_and_copy",
                message: "已唤起 Antigravity 并复制会话标题（按 ⌘K 粘贴回车定位；在「系统设置 → 隐私与安全性 → 辅助功能」授权 Vibe Session Hub 后可自动直达会话）",
                target: antigravityFocusURL,
                needsAccessibility: true,
                coldStarted: wake.coldStarted,
                matchedIndex: nil
            )
        }

        // Step 3: Enable Electron AX tree & wait for Antigravity window to be active
        let appElem = AXUIElementCreateApplication(runningApp.processIdentifier)
        enableElectronAccessibility(appElem)

        let readyTimeout: TimeInterval = wake.coldStarted ? 10.0 : 2.5
        _ = waitUntilActiveAndWindowReady(app: runningApp, appElem: appElem, timeoutSec: readyTimeout)
        // Wait briefly for Chromium to serialize its AX tree after AXManualAccessibility is enabled
        let axWarmDeadline = Date().addingTimeInterval(wake.coldStarted ? 0.8 : 0.35)
        while Date() < axWarmDeadline {
            if copyAXAttribute(appElem, kAXFocusedUIElementAttribute) != nil {
                break
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        if wake.coldStarted {
            Thread.sleep(forTimeInterval: 0.35)
        } else {
            Thread.sleep(forTimeInterval: 0.08)
        }

        // Step 4: Trigger ⌘K and strictly verify FastPick search input has focus
        var detected = detectFastPickFocus(appElem: appElem)
        let maxOpenAttempts = wake.coldStarted ? 12 : 4

        if detected == nil {
            for attempt in 0..<maxOpenAttempts {
                if runningApp.isTerminated {
                    break
                }
                if !runningApp.isActive {
                    activateApp(runningApp)
                    Thread.sleep(forTimeInterval: 0.1)
                }
                enableElectronAccessibility(appElem)
                pressCommandKey(kVK_ANSI_K)

                let pollWindow: TimeInterval = wake.coldStarted ? 0.55 : 0.35
                let pollDeadline = Date().addingTimeInterval(pollWindow)
                while Date() < pollDeadline {
                    if let hit = detectFastPickFocus(appElem: appElem) {
                        detected = hit
                        break
                    }
                    Thread.sleep(forTimeInterval: 0.05)
                }
                if detected != nil {
                    break
                }
                if attempt + 1 < maxOpenAttempts {
                    Thread.sleep(forTimeInterval: wake.coldStarted ? 0.25 : 0.1)
                }
            }
        }

        // HARD SAFETY GUARD: Never paste or press Return unless FastPick input is confirmed focused
        guard let initialFastPick = detected else {
            _ = copyStringToPasteboard(query)
            return AntigravityOpenResult(
                ok: true,
                status: "fastpick_not_detected",
                action: "focus_and_copy",
                message: "已唤起 Antigravity 并复制会话标题（未检测到 ⌘K 搜索框，请手动按 ⌘K 粘贴回车定位）",
                target: antigravityFocusURL,
                needsAccessibility: false,
                coldStarted: wake.coldStarted,
                matchedIndex: nil
            )
        }

        // Step 5: Clear existing input if non-empty, then paste query via clipboard (IME-safe)
        let existingVal = normalizeAntigravitySearchText(
            copyAXString(initialFastPick.inputElement, kAXValueAttribute) ?? ""
        )
        if !existingVal.isEmpty {
            pressCommandKey(kVK_ANSI_A)
            Thread.sleep(forTimeInterval: 0.03)
            pressPlainKey(kVK_Delete)
            Thread.sleep(forTimeInterval: 0.05)
        }

        let pastePayload = buildAntigravityPastePayload(query: query, mode: initialFastPick.mode)
        let clipboardBackup = ClipboardSnapshot.capture(from: NSPasteboard.general)
        let pasteChangeCount = copyStringToPasteboard(pastePayload)
        Thread.sleep(forTimeInterval: 0.03)

        pressCommandKey(kVK_ANSI_A)
        Thread.sleep(forTimeInterval: 0.025)
        pressCommandKey(kVK_ANSI_V)

        // Step 6: Wait for input to settle & FastPick results to match target conversation
        let pasteTime = Date()
        let searchDeadline = pasteTime.addingTimeInterval(2.0)
        var matchedIndex: Int?
        var matchedElement: AXUIElement?
        var sawListbox = false
        var sawEmptyNotice = false
        var inputLanded = false

        while Date() < searchDeadline {
            Thread.sleep(forTimeInterval: 0.05)
            guard let liveFastPick = detectFastPickFocus(appElem: appElem) else {
                // FastPick may transiently re-focus while switching prefix from unified -> conversations
                continue
            }

            let curInputVal = normalizeAntigravitySearchText(
                copyAXString(liveFastPick.inputElement, kAXValueAttribute) ?? ""
            )
            if !curInputVal.isEmpty
                && (curInputVal.lowercased().contains(query.lowercased().prefix(16))
                    || query.lowercased().contains(curInputVal.lowercased()))
            {
                inputLanded = true
            }

            let elapsed = Date().timeIntervalSince(pasteTime)
            // Wait for unified -> conversations mode switch if we pasted "conversations:..."
            if initialFastPick.mode != .conversations && liveFastPick.mode != .conversations && elapsed < 0.6 {
                continue
            }

            let snapshot = snapshotFastPickResults(
                inputElement: liveFastPick.inputElement,
                container: liveFastPick.container
            )
            if snapshot.hasListbox {
                sawListbox = true
            }
            sawEmptyNotice = snapshot.hasEmptyNotice

            if sawEmptyNotice && elapsed >= 0.35 {
                // Confirmed empty result after LS search settled
                break
            }

            if !snapshot.items.isEmpty {
                if let candidateIdx = selectBestAntigravityCandidate(
                    items: snapshot.items,
                    query: query,
                    workspaceHint: workspaceHint
                ) {
                    // Guard against conversationSearchV2 unfiltered-list flash right after paste:
                    // - After 180ms, if index 0 already matches, it's safe to select immediately.
                    // - If match is at index > 0, wait at least 420ms so LS results finish replacing
                    //   the initial unfiltered list (which normally brings the target to index 0).
                    let minSettle: TimeInterval = (candidateIdx == 0) ? 0.18 : 0.42
                    if elapsed >= minSettle {
                        matchedIndex = candidateIdx
                        if candidateIdx < snapshot.elements.count {
                            matchedElement = snapshot.elements[candidateIdx]
                        }
                        break
                    }
                }
            }
        }

        // Step 7: Select the matched conversation (or safe fallback if no match found)
        var triggeredSelection = false

        if let idx = matchedIndex {
            if idx == 0 {
                pressPlainKey(kVK_Return)
                triggeredSelection = true
            } else {
                // Prefer direct AXPress on the exact matching option element
                if let elem = matchedElement,
                   AXUIElementPerformAction(elem, kAXPressAction as CFString) == .success {
                    Thread.sleep(forTimeInterval: 0.18)
                    if detectFastPickFocus(appElem: appElem) == nil {
                        triggeredSelection = true
                    }
                }
                if !triggeredSelection {
                    // Fallback: navigate down `idx` times and press Return
                    for _ in 0..<idx {
                        pressPlainKey(kVK_DownArrow)
                        Thread.sleep(forTimeInterval: 0.03)
                    }
                    Thread.sleep(forTimeInterval: 0.04)
                    pressPlainKey(kVK_Return)
                    triggeredSelection = true
                }
            }
        } else if !sawListbox && !sawEmptyNotice && inputLanded {
            // Fallback when Electron didn't expose listbox AX children despite confirmed FastPick input
            pressPlainKey(kVK_Return)
            triggeredSelection = true
        }

        if triggeredSelection {
            // Wait briefly for FastPick dialog to close after selection
            let closeDeadline = Date().addingTimeInterval(0.45)
            var closed = false
            while Date() < closeDeadline {
                if detectFastPickFocus(appElem: appElem) == nil {
                    closed = true
                    break
                }
                Thread.sleep(forTimeInterval: 0.05)
            }

            if closed {
                clipboardBackup.restore(
                    to: NSPasteboard.general,
                    expectedChangeCount: pasteChangeCount
                )
                return AntigravityOpenResult(
                    ok: true,
                    status: "opened",
                    action: "fastpick_open",
                    message: "已唤起 Antigravity 并打开对应会话",
                    target: antigravityFocusURL,
                    needsAccessibility: false,
                    coldStarted: wake.coldStarted,
                    matchedIndex: matchedIndex
                )
            }
        }

        // Did not confirm opening a matched conversation: leave search prefilled for user & keep clean title in clipboard
        _ = copyStringToPasteboard(query)
        return AntigravityOpenResult(
            ok: true,
            status: "search_prefilled",
            action: "search_prefilled",
            message: "已唤起 Antigravity 并在 ⌘K 搜索框填入会话标题，未自动匹配到唯一会话，请在列表中按回车确认",
            target: antigravityFocusURL,
            needsAccessibility: false,
            coldStarted: wake.coldStarted,
            matchedIndex: nil
        )
    }
}
