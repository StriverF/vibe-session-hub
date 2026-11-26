import Foundation

let defaultHubPort = 17380
let launchAgentLabel = "com.vibesessionhub.app"
let launchAgentSoftFileLimit = 4096

func parseValidPort(_ raw: Any?) -> Int? {
    if let intVal = raw as? Int, (1...65535).contains(intVal) {
        return intVal
    }
    if let numVal = raw as? NSNumber {
        let intVal = numVal.intValue
        if (1...65535).contains(intVal) {
            return intVal
        }
    }
    if let strVal = raw as? String {
        let trimmed = strVal.trimmingCharacters(in: .whitespacesAndNewlines)
        if let intVal = Int(trimmed), (1...65535).contains(intVal) {
            return intVal
        }
    }
    return nil
}

func normalizeFilesystemPath(_ path: String) -> String {
    let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return "" }
    return URL(fileURLWithPath: trimmed).standardizedFileURL.resolvingSymlinksInPath().path
}

private struct ParsedSemver {
    let numbers: [Int]
    let prerelease: [String]
}

private func parseSemver(_ raw: String) -> ParsedSemver? {
    var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.hasPrefix("v") || s.hasPrefix("V") {
        s.removeFirst()
    }
    if let plusIdx = s.firstIndex(of: "+") {
        s = String(s[..<plusIdx])
    }
    guard !s.isEmpty else { return nil }

    let corePart: String
    let prereleasePart: String?
    if let dashIdx = s.firstIndex(of: "-") {
        corePart = String(s[..<dashIdx])
        prereleasePart = String(s[s.index(after: dashIdx)...])
    } else {
        corePart = s
        prereleasePart = nil
    }

    let rawNums = corePart.split(separator: ".", omittingEmptySubsequences: false)
    guard !rawNums.isEmpty else { return nil }
    var numbers: [Int] = []
    numbers.reserveCapacity(rawNums.count)
    for part in rawNums {
        guard !part.isEmpty, let n = Int(part), n >= 0 else { return nil }
        numbers.append(n)
    }

    let prerelease: [String]
    if let pre = prereleasePart, !pre.isEmpty {
        prerelease = pre.split(separator: ".").map(String.init)
    } else {
        prerelease = []
    }
    return ParsedSemver(numbers: numbers, prerelease: prerelease)
}

func compareSemver(_ lhs: String, _ rhs: String) -> Int {
    guard let a = parseSemver(lhs), let b = parseSemver(rhs) else {
        let al = lhs.trimmingCharacters(in: .whitespacesAndNewlines)
        let bl = rhs.trimmingCharacters(in: .whitespacesAndNewlines)
        if al == bl { return 0 }
        return al < bl ? -1 : 1
    }

    let maxLen = max(a.numbers.count, b.numbers.count)
    for i in 0..<maxLen {
        let av = i < a.numbers.count ? a.numbers[i] : 0
        let bv = i < b.numbers.count ? b.numbers[i] : 0
        if av != bv {
            return av < bv ? -1 : 1
        }
    }

    if a.prerelease.isEmpty && b.prerelease.isEmpty {
        return 0
    }
    if a.prerelease.isEmpty {
        return 1
    }
    if b.prerelease.isEmpty {
        return -1
    }

    let preLen = min(a.prerelease.count, b.prerelease.count)
    for i in 0..<preLen {
        let ap = a.prerelease[i]
        let bp = b.prerelease[i]
        if ap == bp { continue }
        let aInt = Int(ap)
        let bInt = Int(bp)
        if let ai = aInt, let bi = bInt {
            return ai < bi ? -1 : 1
        }
        if aInt != nil {
            return -1
        }
        if bInt != nil {
            return 1
        }
        return ap < bp ? -1 : 1
    }
    if a.prerelease.count == b.prerelease.count {
        return 0
    }
    return a.prerelease.count < b.prerelease.count ? -1 : 1
}

struct ExpectedBackendIdentity: Equatable {
    let version: String
    let buildId: String
    let backendRoot: String
}

struct RunningBackendInfo: Equatable {
    let version: String?
    let buildId: String
    let backendRoot: String?
    let pid: Int?
}

enum BackendMatchStatus: Equatable {
    case current
    case newerRunning(version: String)
    case stale(reason: String)
}

func isVibeHubHealthPayload(_ health: [String: Any]) -> Bool {
    guard (health["ok"] as? Bool) == true else { return false }
    guard let machine = health["machine"] as? [String: Any],
          let machineId = machine["machine_id"] as? String,
          !machineId.isEmpty else {
        return false
    }
    return true
}

func extractHealthServerMetadata(_ health: [String: Any]) -> RunningBackendInfo {
    let serverInfo = health["server"] as? [String: Any]

    let rawVersion = (health["version"] as? String) ?? (serverInfo?["version"] as? String)
    let trimmedVersion = rawVersion?.trimmingCharacters(in: .whitespacesAndNewlines)
    let version = (trimmedVersion?.isEmpty == false) ? trimmedVersion : nil

    let rawBuildId = (health["build_id"] as? String) ?? (serverInfo?["build_id"] as? String)
    let buildId = rawBuildId?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

    let rawRoot = (health["backend_root"] as? String) ?? (serverInfo?["backend_root"] as? String)
    let normalizedRoot: String?
    if let rawRoot = rawRoot {
        let norm = normalizeFilesystemPath(rawRoot)
        normalizedRoot = norm.isEmpty ? nil : norm
    } else {
        normalizedRoot = nil
    }

    let pid: Int?
    if let intPid = serverInfo?["pid"] as? Int, intPid > 0 {
        pid = intPid
    } else if let numPid = serverInfo?["pid"] as? NSNumber, numPid.intValue > 0 {
        pid = numPid.intValue
    } else {
        pid = nil
    }

    return RunningBackendInfo(
        version: version,
        buildId: buildId,
        backendRoot: normalizedRoot,
        pid: pid
    )
}

func evaluateRunningBackend(
    health: [String: Any],
    expected: ExpectedBackendIdentity
) -> BackendMatchStatus {
    let running = extractHealthServerMetadata(health)
    guard let runningVersion = running.version else {
        return .stale(reason: "missing_version")
    }

    let cmp = compareSemver(runningVersion, expected.version)
    if cmp < 0 {
        return .stale(reason: "older_version:\(runningVersion)<\(expected.version)")
    }
    if cmp > 0 {
        return .newerRunning(version: runningVersion)
    }

    guard let runningRoot = running.backendRoot else {
        return .stale(reason: "missing_backend_root")
    }

    let expectedRoot = normalizeFilesystemPath(expected.backendRoot)
    if runningRoot != expectedRoot {
        return .stale(reason: "different_backend_root")
    }

    if !expected.buildId.isEmpty && running.buildId != expected.buildId {
        return .stale(reason: "different_build_id")
    }

    return .current
}

func readExpectedBackendIdentity(backendRoot: URL, bundleVersion: String) -> ExpectedBackendIdentity {
    let normalizedRoot = normalizeFilesystemPath(backendRoot.path)

    let versionURL = backendRoot.appendingPathComponent("VERSION")
    let resolvedVersion: String
    if let raw = try? String(contentsOf: versionURL, encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines),
       !raw.isEmpty {
        let stripped = raw.hasPrefix("v") || raw.hasPrefix("V") ? String(raw.dropFirst()) : raw
        resolvedVersion = stripped.isEmpty ? bundleVersion : stripped
    } else {
        let stripped = bundleVersion.trimmingCharacters(in: .whitespacesAndNewlines)
        resolvedVersion = stripped.isEmpty ? "1.0.0" : stripped
    }

    let buildIdURL = backendRoot.appendingPathComponent("BUILD_ID")
    let buildId = (try? String(contentsOf: buildIdURL, encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines)) ?? ""

    return ExpectedBackendIdentity(
        version: resolvedVersion,
        buildId: buildId,
        backendRoot: normalizedRoot
    )
}

func extractLaunchAgentConfiguredPort(plistDict: [String: Any], fallbackPort: Int) -> Int {
    if let args = plistDict["ProgramArguments"] as? [String] {
        for i in 0..<args.count {
            if args[i] == "--port", i + 1 < args.count, let port = parseValidPort(args[i + 1]) {
                return port
            }
            if args[i].hasPrefix("--port=") {
                let val = String(args[i].dropFirst("--port=".count))
                if let port = parseValidPort(val) {
                    return port
                }
            }
        }
    }

    if let env = plistDict["EnvironmentVariables"] as? [String: Any],
       let envPort = parseValidPort(env["VIBE_HUB_PORT"]) {
        return envPort
    }

    return fallbackPort
}

func extractLaunchAgentWorkingDirectory(plistDict: [String: Any]) -> String? {
    guard let wd = plistDict["WorkingDirectory"] as? String else { return nil }
    let norm = normalizeFilesystemPath(wd)
    return norm.isEmpty ? nil : norm
}

func isEphemeralBundleLocation(_ bundleURL: URL) -> Bool {
    let resolvedPath = bundleURL.standardizedFileURL.resolvingSymlinksInPath().path
    if resolvedPath.contains("/AppTranslocation/") {
        return true
    }
    if let values = try? bundleURL.resourceValues(forKeys: [.volumeIsReadOnlyKey]),
       values.volumeIsReadOnly == true {
        return true
    }
    return false
}

func buildLaunchAgentPlist(
    label: String,
    backendRoot: URL,
    hubPort: Int,
    stateDir: URL,
    sendURLHelperPath: String?,
    antigravityOpenerHelperPath: String? = nil
) -> [String: Any] {
    var envVars: [String: String] = [
        "VIBE_HUB_PORT": String(hubPort),
        "PYTHONDONTWRITEBYTECODE": "1",
    ]
    if let helperPath = sendURLHelperPath, !helperPath.isEmpty {
        envVars["VIBE_HUB_SEND_URL_BIN"] = helperPath
    }
    if let agyHelperPath = antigravityOpenerHelperPath, !agyHelperPath.isEmpty {
        envVars["VIBE_HUB_ANTIGRAVITY_OPENER_BIN"] = agyHelperPath
    }

    return [
        "Label": label,
        "ProgramArguments": [
            "/usr/bin/python3",
            "-B",
            "-m",
            "hub.server",
            "--port",
            String(hubPort),
        ],
        "WorkingDirectory": normalizeFilesystemPath(backendRoot.path),
        "EnvironmentVariables": envVars,
        "RunAtLoad": true,
        "KeepAlive": true,
        "SoftResourceLimits": [
            "NumberOfFiles": launchAgentSoftFileLimit,
        ],
        "StandardOutPath": stateDir.appendingPathComponent("server.log").path,
        "StandardErrorPath": stateDir.appendingPathComponent("server.err.log").path,
    ]
}

func parsePsUidAndCommand(_ rawLine: String) -> (uid: UInt32, command: String)? {
    let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return nil }
    guard let splitIdx = trimmed.firstIndex(where: { $0.isWhitespace }) else { return nil }
    let uidToken = String(trimmed[..<splitIdx])
    guard let uid = UInt32(uidToken) else { return nil }
    let command = trimmed[splitIdx...].trimmingCharacters(in: .whitespacesAndNewlines)
    guard !command.isEmpty else { return nil }
    return (uid, command)
}

func isVibeHubServerCommand(_ command: String) -> Bool {
    let lower = command.lowercased()
    guard lower.contains("python") else { return false }
    return command.contains("hub.server") || command.contains("hub/server.py")
}

// MARK: - In-App Self-Update Pure Logic

let defaultUpdateGitHubRepo = "StriverF/vibe-session-hub"
let maxUpdateArchiveBytes: Int64 = 200 * 1024 * 1024

private let trustedGitHubHosts: Set<String> = [
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
]

struct ReleaseUpdateInfo: Equatable {
    let version: String
    let tag: String
    let name: String
    let publishedAt: String
    let notes: String
    let htmlURL: String
    let dmgName: String
    let dmgURL: String
    let dmgSize: Int64
    let sha256URL: String
    let apiSha256: String
}

struct UpdateRestartMarker: Codable, Equatable {
    let from: String
    let to: String
    let notes: String?
    let publishedAt: String?

    init(from: String, to: String, notes: String? = nil, publishedAt: String? = nil) {
        self.from = from
        self.to = to
        self.notes = notes
        self.publishedAt = publishedAt
    }
}

struct JustUpdatedOutcome: Equatable {
    let from: String
    let to: String
    let ok: Bool
    let notes: String
    let publishedAt: String

    init(from: String, to: String, ok: Bool, notes: String = "", publishedAt: String = "") {
        self.from = from
        self.to = to
        self.ok = ok
        self.notes = notes
        self.publishedAt = publishedAt
    }
}

enum Sha256ReconcileResult: Equatable {
    case ok(String)
    case missing
    case mismatch(api: String, sidecar: String)
}

func isTrustedUpdateURL(_ rawURL: String, allowLoopback: Bool = false) -> Bool {
    let trimmed = rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty,
          let components = URLComponents(string: trimmed),
          let scheme = components.scheme?.lowercased(),
          let host = components.host?.lowercased(),
          !host.isEmpty else {
        return false
    }
    if allowLoopback && scheme == "http" && (host == "127.0.0.1" || host == "localhost") {
        return true
    }
    guard scheme == "https" else { return false }
    if trustedGitHubHosts.contains(host) {
        return true
    }
    if host.hasSuffix(".githubusercontent.com") && host.count > ".githubusercontent.com".count {
        return true
    }
    return false
}

func isValidGitHubRepoSlug(_ raw: String) -> Bool {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    let parts = trimmed.split(separator: "/", omittingEmptySubsequences: false)
    guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return false }
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
    return parts.allSatisfy { part in
        part.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

func resolveUpdateFeedConfig(env: [String: String]) -> (url: String, allowLoopback: Bool) {
    if let custom = env["VIBE_HUB_UPDATE_FEED_URL"]?.trimmingCharacters(in: .whitespacesAndNewlines),
       !custom.isEmpty,
       let comps = URLComponents(string: custom) {
        let host = comps.host?.lowercased() ?? ""
        let allowLoopback = comps.scheme?.lowercased() == "http" && (host == "127.0.0.1" || host == "localhost")
        if isTrustedUpdateURL(custom, allowLoopback: allowLoopback) {
            return (custom, allowLoopback)
        }
    }

    let rawRepo = env["VIBE_HUB_UPDATE_REPO"]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let repo = isValidGitHubRepoSlug(rawRepo) ? rawRepo : defaultUpdateGitHubRepo
    return ("https://api.github.com/repos/\(repo)/releases/latest", false)
}

func extractCliPortOverride(arguments: [String]) -> Int? {
    for i in 1..<arguments.count {
        if arguments[i] == "--port", i + 1 < arguments.count, let port = parseValidPort(arguments[i + 1]) {
            return port
        }
        if arguments[i].hasPrefix("--port=") {
            let val = String(arguments[i].dropFirst("--port=".count))
            if let port = parseValidPort(val) {
                return port
            }
        }
    }
    return nil
}

func isHexSha256(_ raw: String) -> Bool {
    guard raw.count == 64 else { return false }
    for scalar in raw.unicodeScalars {
        let v = scalar.value
        let isDigit = (48...57).contains(v)
        let isLower = (97...102).contains(v)
        let isUpper = (65...70).contains(v)
        if !isDigit && !isLower && !isUpper {
            return false
        }
    }
    return true
}

func parseSha256Digest(_ raw: String?) -> String {
    guard var text = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
        return ""
    }
    if text.lowercased().hasPrefix("sha256:") {
        text = String(text.dropFirst(7)).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return isHexSha256(text) ? text.lowercased() : ""
}

func parseSha256SidecarFile(_ content: String, expectedFilename: String?) -> String? {
    let lines = content
        .replacingOccurrences(of: "\r\n", with: "\n")
        .split(separator: "\n")
        .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }

    let expectedBase = expectedFilename.map { URL(fileURLWithPath: $0).lastPathComponent }

    for line in lines {
        let parts = line.split(maxSplits: 1, whereSeparator: { $0.isWhitespace })
        guard let first = parts.first else { continue }
        let digest = parseSha256Digest(String(first))
        guard !digest.isEmpty else { continue }

        if parts.count == 2, let expectedBase = expectedBase, !expectedBase.isEmpty {
            var fileToken = String(parts[1]).trimmingCharacters(in: .whitespacesAndNewlines)
            if fileToken.hasPrefix("*") {
                fileToken.removeFirst()
            }
            let fileBase = URL(fileURLWithPath: fileToken).lastPathComponent
            if fileBase != expectedBase {
                continue
            }
        }
        return digest
    }
    return nil
}

func reconcileExpectedSha256(apiDigest: String?, sidecarDigest: String?) -> Sha256ReconcileResult {
    let api = parseSha256Digest(apiDigest)
    let sidecar = parseSha256Digest(sidecarDigest)
    if !api.isEmpty && !sidecar.isEmpty {
        if api == sidecar {
            return .ok(api)
        }
        return .mismatch(api: api, sidecar: sidecar)
    }
    if !sidecar.isEmpty {
        return .ok(sidecar)
    }
    if !api.isEmpty {
        return .ok(api)
    }
    return .missing
}

func cleanReleaseNotes(_ raw: String) -> String {
    let text = raw
        .replacingOccurrences(of: "\r\n", with: "\n")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return "" }

    let lines = text.components(separatedBy: "\n")
    var startIdx = 0
    if let first = lines.first?.trimmingCharacters(in: .whitespaces),
       first.lowercased().hasPrefix("## vibe session hub ") {
        startIdx = 1
        while startIdx < lines.count && lines[startIdx].trimmingCharacters(in: .whitespaces).isEmpty {
            startIdx += 1
        }
    }

    var kept: [String] = []
    for i in startIdx..<lines.count {
        let trimmed = lines[i].trimmingCharacters(in: .whitespaces)
        let lower = trimmed.lowercased()
        if lower.hasPrefix("### 下载与安装")
            || lower.hasPrefix("### 校验和")
            || lower.hasPrefix("### download")
            || lower.hasPrefix("### install")
            || lower.hasPrefix("### checksum") {
            break
        }
        kept.append(lines[i])
    }

    let cleaned = kept.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    if !cleaned.isEmpty {
        return cleaned
    }
    return text
}

func parseGitHubReleasePayload(
    _ payload: [String: Any],
    currentVersion: String,
    allowLoopback: Bool = false
) -> ReleaseUpdateInfo? {
    if (payload["draft"] as? Bool) == true || (payload["prerelease"] as? Bool) == true {
        return nil
    }
    guard let rawTag = payload["tag_name"] as? String else { return nil }
    let tag = rawTag.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !tag.isEmpty else { return nil }
    let version = (tag.hasPrefix("v") || tag.hasPrefix("V")) ? String(tag.dropFirst()) : tag
    guard !version.isEmpty else { return nil }
    guard compareSemver(version, currentVersion) > 0 else { return nil }

    let assets = payload["assets"] as? [[String: Any]] ?? []
    var dmgAsset: [String: Any]?
    var fallbackDmg: [String: Any]?

    for asset in assets {
        guard let name = (asset["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
              let dlURL = (asset["browser_download_url"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
              name.lowercased().hasSuffix(".dmg"),
              isTrustedUpdateURL(dlURL, allowLoopback: allowLoopback) else {
            continue
        }
        if name.lowercased().contains("universal") {
            dmgAsset = asset
            break
        }
        if fallbackDmg == nil {
            fallbackDmg = asset
        }
    }
    if dmgAsset == nil {
        dmgAsset = fallbackDmg
    }

    let dmgName = (dmgAsset?["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let dmgURL = (dmgAsset?["browser_download_url"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let dmgSize: Int64
    if let n = dmgAsset?["size"] as? NSNumber {
        dmgSize = max(0, n.int64Value)
    } else if let n = dmgAsset?["size"] as? Int64 {
        dmgSize = max(0, n)
    } else if let n = dmgAsset?["size"] as? Int {
        dmgSize = max(0, Int64(n))
    } else {
        dmgSize = 0
    }

    var sha256URL = ""
    if !dmgName.isEmpty {
        let expectedSidecar = dmgName + ".sha256"
        for asset in assets {
            guard let name = (asset["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
                  name == expectedSidecar,
                  let dlURL = (asset["browser_download_url"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
                  isTrustedUpdateURL(dlURL, allowLoopback: allowLoopback) else {
                continue
            }
            sha256URL = dlURL
            break
        }
    }

    let rawHTML = (payload["html_url"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let htmlURL = isTrustedUpdateURL(rawHTML, allowLoopback: allowLoopback) ? rawHTML : ""
    let name = ((payload["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)).flatMap {
        $0.isEmpty ? nil : $0
    } ?? "Vibe Session Hub \(tag)"
    let publishedAt = (payload["published_at"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let notes = cleanReleaseNotes((payload["body"] as? String) ?? "")
    let apiSha256 = parseSha256Digest(dmgAsset?["digest"] as? String)

    return ReleaseUpdateInfo(
        version: version,
        tag: tag,
        name: name,
        publishedAt: publishedAt,
        notes: notes,
        htmlURL: htmlURL,
        dmgName: dmgName,
        dmgURL: dmgURL,
        dmgSize: dmgSize,
        sha256URL: sha256URL,
        apiSha256: apiSha256
    )
}

func parseCodesignTeamIdentifier(_ output: String) -> String? {
    for rawLine in output.components(separatedBy: .newlines) {
        let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
        guard line.hasPrefix("TeamIdentifier=") else { continue }
        let val = String(line.dropFirst("TeamIdentifier=".count)).trimmingCharacters(in: .whitespacesAndNewlines)
        if val.isEmpty || val.lowercased() == "not set" {
            return nil
        }
        return val
    }
    return nil
}

func buildSwapOnExitScript() -> String {
    return """
    pid="$1"
    target="$2"
    new_app="$3"
    work="$4"
    trap '' HUP
    while /bin/kill -0 "$pid" 2>/dev/null; do
      /bin/sleep 0.05
    done
    if /bin/mv "$target" "$work/old"; then
      if /bin/mv "$new_app" "$target"; then
        /usr/bin/touch "$target" 2>/dev/null || true
      else
        /bin/mv "$work/old" "$target" 2>/dev/null || true
      fi
    fi
    relaunch_flag=0
    relaunch_port=""
    if [ -f "$work/relaunch" ]; then
      relaunch_flag=1
      relaunch_port="$(/bin/cat "$work/relaunch" 2>/dev/null || true)"
    fi
    /bin/rm -rf "$work" 2>/dev/null || true
    if [ "$relaunch_flag" -eq 1 ]; then
      if [ -n "$relaunch_port" ]; then
        exec /usr/bin/open "$target" --args --port "$relaunch_port"
      else
        exec /usr/bin/open "$target"
      fi
    fi
    """
}

