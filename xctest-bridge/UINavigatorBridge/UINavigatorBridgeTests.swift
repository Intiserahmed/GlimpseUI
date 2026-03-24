import XCTest

/// UINavigator XCTest Bridge
/// Runs a lightweight HTTP server inside a UI test.
/// No host app required — uses Springboard as coordinate base to tap anywhere on screen.

class UINavigatorBridgeTests: XCTestCase {

    let port: UInt16 = 22087
    var server: BridgeServer!

    override func setUp() {
        continueAfterFailure = true
        // Launch the host app to initialize XCTest UI automation context,
        // then immediately press Home so the home screen is visible.
        XCUIApplication().launch()
        XCUIDevice.shared.press(.home)
        server = BridgeServer(port: port)
        server.start()
    }

    override func tearDown() {
        server?.stopped = true
    }

    func testRunBridge() {
        // Keep running until client sends /stop or 1-hour timeout
        let timeout: TimeInterval = 3600
        let start = Date()
        while !server.stopped && Date().timeIntervalSince(start) < timeout {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))
        }
    }
}

// ── HTTP Server ────────────────────────────────────────────────────────────────

class BridgeServer {
    let port: UInt16
    var stopped = false
    private var socketFd: Int32 = -1
    private var thread: Thread?

    init(port: UInt16) {
        self.port = port
    }

    func start() {
        thread = Thread { self.serve() }
        thread?.start()
    }

    private func serve() {
        socketFd = socket(AF_INET, SOCK_STREAM, 0)
        guard socketFd >= 0 else { return }

        var opt: Int32 = 1
        setsockopt(socketFd, SOL_SOCKET, SO_REUSEADDR, &opt, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY

        withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(socketFd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }

        listen(socketFd, 5)

        while !stopped {
            let clientFd = accept(socketFd, nil, nil)
            if clientFd < 0 { continue }
            handleClient(clientFd)
            close(clientFd)
        }
        close(socketFd)
    }

    private func handleClient(_ fd: Int32) {
        var buffer = [UInt8](repeating: 0, count: 8192)
        let n = read(fd, &buffer, buffer.count)
        guard n > 0 else { return }

        let request = String(bytes: buffer[0..<n], encoding: .utf8) ?? ""
        let (path, body) = parseRequest(request)
        let response = handlePath(path, body: body)
        let httpResponse = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(response.utf8.count)\r\nConnection: close\r\n\r\n\(response)"
        httpResponse.withCString { write(fd, $0, strlen($0)) }
    }

    private func parseRequest(_ raw: String) -> (String, [String: Any]) {
        let lines = raw.components(separatedBy: "\r\n")
        let firstLine = lines.first?.components(separatedBy: " ") ?? []
        let path = firstLine.count > 1 ? firstLine[1] : "/"
        var body: [String: Any] = [:]
        if let range = raw.range(of: "\r\n\r\n") {
            let bodyStr = String(raw[range.upperBound...])
            if let data = bodyStr.data(using: .utf8),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                body = json
            }
        }
        return (path, body)
    }

    // Use Springboard as coordinate base — always running, gives us full-screen coordinate space
    private var springboard: XCUIApplication {
        XCUIApplication(bundleIdentifier: "com.apple.springboard")
    }

    private func screenCoord(x: Double, y: Double) -> XCUICoordinate {
        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: x, dy: y))
    }

    /// Run a block on the main thread and wait for it to complete.
    private func onMain(_ block: () -> Void) {
        if Thread.isMainThread {
            block()
        } else {
            DispatchQueue.main.sync { block() }
        }
    }

    private func handlePath(_ path: String, body: [String: Any]) -> String {
        switch path {

        case "/tap":
            let x = body["x"] as? Double ?? 0
            let y = body["y"] as? Double ?? 0
            // Fire async so response returns immediately (avoids timeout on app launches)
            DispatchQueue.main.async { self.screenCoord(x: x, y: y).tap() }
            Thread.sleep(forTimeInterval: 0.15)  // ensure tap is queued before responding
            return json(["ok": true])

        case "/doubletap":
            let x = body["x"] as? Double ?? 0
            let y = body["y"] as? Double ?? 0
            DispatchQueue.main.async { self.screenCoord(x: x, y: y).doubleTap() }
            Thread.sleep(forTimeInterval: 0.15)
            return json(["ok": true])

        case "/swipe":
            let x1 = body["x1"] as? Double ?? 0
            let y1 = body["y1"] as? Double ?? 0
            let x2 = body["x2"] as? Double ?? 0
            let y2 = body["y2"] as? Double ?? 0
            onMain { self.screenCoord(x: x1, y: y1).press(forDuration: 0.05, thenDragTo: self.screenCoord(x: x2, y: y2)) }
            return json(["ok": true])

        case "/type":
            let text = body["text"] as? String ?? ""
            // bundleId: optionally target a specific app; defaults to host app which routes keys to focused element
            let bundleId = body["bundleId"] as? String
            onMain {
                let app = bundleId != nil ? XCUIApplication(bundleIdentifier: bundleId!) : XCUIApplication()
                app.typeText(text)
            }
            return json(["ok": true])

        case "/keypress":
            let key = body["key"] as? String ?? ""
            let bundleId = body["bundleId"] as? String
            onMain {
                let app = bundleId != nil ? XCUIApplication(bundleIdentifier: bundleId!) : XCUIApplication()
                switch key {
                case "Return", "Enter":   app.typeText("\n")
                case "Backspace":         app.typeText(XCUIKeyboardKey.delete.rawValue)
                case "Escape":            app.typeText(XCUIKeyboardKey.escape.rawValue)
                case "Tab":               app.typeText(XCUIKeyboardKey.tab.rawValue)
                case "ArrowUp":           app.typeText(XCUIKeyboardKey.upArrow.rawValue)
                case "ArrowDown":         app.typeText(XCUIKeyboardKey.downArrow.rawValue)
                case "ArrowLeft":         app.typeText(XCUIKeyboardKey.leftArrow.rawValue)
                case "ArrowRight":        app.typeText(XCUIKeyboardKey.rightArrow.rawValue)
                default:                  app.typeText(key)
                }
            }
            return json(["ok": true])

        case "/screenshot":
            var result = ""
            onMain {
                let screenshot = XCUIScreen.main.screenshot()
                let jpegData = screenshot.image.jpegData(compressionQuality: 0.7) ?? Data()
                result = self.json(["ok": true, "screenshot": jpegData.base64EncodedString()])
            }
            return result

        case "/viewHierarchy":
            var result = ""
            let bundleId = body["bundleId"] as? String
            onMain {
                // Use the provided bundle ID, or fall back to Springboard (full screen access)
                let app = bundleId != nil
                    ? XCUIApplication(bundleIdentifier: bundleId!)
                    : XCUIApplication(bundleIdentifier: "com.apple.springboard")
                let elements = self.dumpElements(app)
                result = self.json(["ok": true, "elements": elements])
            }
            return result

        case "/stop":
            stopped = true
            return json(["ok": true])

        case "/health":
            return json(["ok": true, "status": "running"])

        default:
            return json(["ok": false, "error": "unknown path: \(path)"])
        }
    }

    /// Dump all accessible elements from the app's hierarchy.
    private func dumpElements(_ root: XCUIElement) -> [[String: Any]] {
        var elements: [[String: Any]] = []
        let query = root.descendants(matching: .any)
        let count = min(Int(query.count), 300)
        for i in 0..<count {
            let el = query.element(boundBy: i)
            let frame = el.frame
            guard frame.width > 0 && frame.height > 0 else { continue }
            let label = el.label
            let identifier = el.identifier
            guard !label.isEmpty || !identifier.isEmpty else { continue }
            elements.append([
                "label":      label,
                "identifier": identifier,
                "type":       el.elementType.rawValue,
                "x":          frame.minX,
                "y":          frame.minY,
                "w":          frame.width,
                "h":          frame.height,
                "enabled":    el.isEnabled,
                "hittable":   el.isHittable,
            ])
        }
        return elements
    }

    private func json(_ dict: [String: Any]) -> String {
        let data = try? JSONSerialization.data(withJSONObject: dict)
        return String(data: data ?? Data(), encoding: .utf8) ?? "{}"
    }
}
