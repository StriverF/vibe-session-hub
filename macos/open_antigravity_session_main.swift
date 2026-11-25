import Foundation

@main
enum OpenAntigravitySessionCLI {
    static func main() {
        let args = CommandLine.arguments
        var sessionId = ""
        var title = ""
        var cwd = ""
        var promptAccessibility = false

        var i = 1
        while i < args.count {
            let arg = args[i]
            switch arg {
            case "--session-id":
                if i + 1 < args.count {
                    sessionId = args[i + 1]
                    i += 2
                } else {
                    i += 1
                }
            case "--title":
                if i + 1 < args.count {
                    title = args[i + 1]
                    i += 2
                } else {
                    i += 1
                }
            case "--cwd":
                if i + 1 < args.count {
                    cwd = args[i + 1]
                    i += 2
                } else {
                    i += 1
                }
            case "--prompt-accessibility":
                promptAccessibility = true
                i += 1
            default:
                i += 1
            }
        }

        let result = AntigravityOpener.openSession(
            sessionId: sessionId,
            title: title,
            cwd: cwd,
            promptAccessibility: promptAccessibility
        )
        print(result.toJSONString())
        exit(result.ok ? 0 : 1)
    }
}
