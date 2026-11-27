import Foundation

struct MicroSessionSummary: Decodable, Equatable {
    let sessionId: String
    let projectId: String?
    let projectName: String?
    let status: String?
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case projectId = "project_id"
        case projectName = "project_name"
        case status
        case updatedAt = "updated_at"
    }
}

struct MicroSnapshotSummary: Decodable {
    let activeSessionId: String?
    let activeCount: Int?
    let sessions: [MicroSessionSummary]

    enum CodingKeys: String, CodingKey {
        case activeSessionId = "active_session_id"
        case activeCount = "active_count"
        case sessions
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        activeSessionId = try container.decodeIfPresent(String.self, forKey: .activeSessionId)
        activeCount = try container.decodeIfPresent(Int.self, forKey: .activeCount)
        sessions = try container.decodeIfPresent([MicroSessionSummary].self, forKey: .sessions) ?? []
    }
}

/// Supervises the local `xiaoyan-progress` FastAPI backend on `127.0.0.1:<port>`.
/// Spawns the backend on demand when ChatGPT Work is active, reuses an existing
/// healthy backend without duplicating it, and ONLY terminates the child process
/// that this supervisor itself spawned.
final class BackendSupervisor {
    static let projectRoot = "/Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress"
    static let venvPython = "/Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress/.venv/bin/python"
    static let cliScript = "/Users/abc/Desktop/gpt/分析软件/codex改装/xiaoyan-progress/cli.py"

    private let runtimeDir: URL
    private let pidFileURL: URL
    private let logFileURL: URL
    private let session: URLSession

    private var ownedProcess: Process?
    private var ownedLogHandle: FileHandle?
    private var isStarting: Bool = false
    private var readyWaiters: [() -> Void] = []
    private var readinessTimer: Timer?
    private var readinessAttempts: Int = 0

    private(set) var isHealthy: Bool = false

    var ownsBackendProcess: Bool {
        ownedProcess?.isRunning == true
    }

    var onBackendBecameHealthy: (() -> Void)?
    var onStatusChanged: (() -> Void)?

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        self.runtimeDir = home.appendingPathComponent(".config/xy_progress/runtime", isDirectory: true)
        self.pidFileURL = runtimeDir.appendingPathComponent("backend.pid")
        self.logFileURL = runtimeDir.appendingPathComponent("backend.log")

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 8.0
        config.timeoutIntervalForResource = 10.0
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: config)
    }

    deinit {
        readinessTimer?.invalidate()
    }

    /// Lightweight `GET /api/health` check on loopback.
    func checkHealth(completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "\(ApprovalLauncher.baseURLString)/api/health") else {
            DispatchQueue.main.async { completion(false) }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 2.0
        request.cachePolicy = .reloadIgnoringLocalCacheData

        session.dataTask(with: request) { _, response, error in
            let ok: Bool
            if error == nil, let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                ok = true
            } else {
                ok = false
            }
            DispatchQueue.main.async {
                self.isHealthy = ok
                completion(ok)
            }
        }.resume()
    }

    /// Lightweight `GET /api/micro` poll for ChatGPT project-session linkage.
    func fetchMicroSummary(completion: @escaping (Result<MicroSnapshotSummary, Error>) -> Void) {
        guard let url = URL(string: "\(ApprovalLauncher.baseURLString)/api/micro") else {
            DispatchQueue.main.async {
                completion(.failure(URLError(.badURL)))
            }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 8.0
        request.cachePolicy = .reloadIgnoringLocalCacheData

        session.dataTask(with: request) { data, response, error in
            if let error = error {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode), let data = data else {
                DispatchQueue.main.async { completion(.failure(URLError(.badServerResponse))) }
                return
            }
            do {
                let decoded = try JSONDecoder().decode(MicroSnapshotSummary.self, from: data)
                DispatchQueue.main.async {
                    self.isHealthy = true
                    completion(.success(decoded))
                }
            } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
            }
        }.resume()
    }

    /// Ensures the local backend is healthy; spawns our own child process if not already up.
    func ensureRunning(onReady: (() -> Void)? = nil) {
        if let onReady = onReady {
            readyWaiters.append(onReady)
        }

        checkHealth { [weak self] healthy in
            guard let self = self else { return }
            if healthy {
                self.isStarting = false
                self.readinessTimer?.invalidate()
                self.readinessTimer = nil
                self.flushReadyWaiters()
                self.onStatusChanged?()
                return
            }

            if self.isStarting || self.ownedProcess?.isRunning == true {
                self.startReadinessPollingIfNeeded()
                return
            }

            self.spawnOwnedBackend()
        }
    }

    /// Restarts the owned backend (if spawned by us) or re-ensures backend availability.
    func restartBackend(onReady: (() -> Void)? = nil) {
        stopOwnedBackend(synchronous: true)
        ensureRunning(onReady: onReady)
    }

    /// Cleanly stops ONLY the backend child process spawned by this supervisor.
    /// Never kills an externally started backend server.
    func stopOwnedBackend(synchronous: Bool = false) {
        readinessTimer?.invalidate()
        readinessTimer = nil
        isStarting = false
        readyWaiters.removeAll()

        guard let proc = ownedProcess else {
            return
        }

        let pid = proc.processIdentifier
        ownedProcess = nil

        if proc.isRunning {
            proc.terminate()
            if synchronous {
                let deadline = Date().addingTimeInterval(1.8)
                while proc.isRunning && Date() < deadline {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                if proc.isRunning {
                    kill(pid, SIGKILL)
                }
            } else {
                DispatchQueue.global(qos: .utility).async {
                    let deadline = Date().addingTimeInterval(2.5)
                    while proc.isRunning && Date() < deadline {
                        Thread.sleep(forTimeInterval: 0.1)
                    }
                    if proc.isRunning {
                        kill(pid, SIGKILL)
                    }
                }
            }
        }

        cleanupPidFileIfMatching(pid: pid)
        closeLogHandle()
        isHealthy = false
        onStatusChanged?()
    }

    private func spawnOwnedBackend() {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: Self.venvPython),
              fm.fileExists(atPath: Self.cliScript) else {
            fputs("[BackendSupervisor] Missing venv python or cli.py; cannot spawn backend.\n", stderr)
            isStarting = false
            readyWaiters.removeAll()
            onStatusChanged?()
            return
        }

        do {
            try fm.createDirectory(at: runtimeDir, withIntermediateDirectories: true)
            if !fm.fileExists(atPath: logFileURL.path) {
                fm.createFile(atPath: logFileURL.path, contents: nil)
            }
            let logHandle = try FileHandle(forWritingTo: logFileURL)
            try logHandle.seekToEnd()
            if let banner = "\n--- [XiaoyanProgressHUD] Spawning backend on 127.0.0.1:\(ApprovalLauncher.serverPort) at \(ISO8601DateFormatter().string(from: Date())) ---\n".data(using: .utf8) {
                try? logHandle.write(contentsOf: banner)
            }
            closeLogHandle()
            ownedLogHandle = logHandle

            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: Self.venvPython)
            proc.arguments = [
                Self.cliScript,
                "serve",
                "--host", "127.0.0.1",
                "--port", "\(ApprovalLauncher.serverPort)"
            ]
            proc.currentDirectoryURL = URL(fileURLWithPath: Self.projectRoot)
            proc.environment = ProcessInfo.processInfo.environment
            proc.standardOutput = logHandle
            proc.standardError = logHandle
            proc.standardInput = FileHandle.nullDevice

            proc.terminationHandler = { [weak self] terminated in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    let exitedPid = terminated.processIdentifier
                    self.cleanupPidFileIfMatching(pid: exitedPid)
                    if self.ownedProcess === terminated {
                        self.ownedProcess = nil
                        self.closeLogHandle()
                        self.isHealthy = false
                        self.isStarting = false
                        self.onStatusChanged?()
                    }
                }
            }

            isStarting = true
            try proc.run()
            ownedProcess = proc

            let pidString = "\(proc.processIdentifier)\n"
            try? pidString.write(to: pidFileURL, atomically: true, encoding: .utf8)

            readinessAttempts = 0
            startReadinessPollingIfNeeded()
            onStatusChanged?()
        } catch {
            fputs("[BackendSupervisor] Failed to spawn backend: \(error.localizedDescription)\n", stderr)
            isStarting = false
            closeLogHandle()
            readyWaiters.removeAll()
            onStatusChanged?()
        }
    }

    private func startReadinessPollingIfNeeded() {
        guard readinessTimer == nil else { return }
        readinessTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.readinessAttempts += 1
            if self.readinessAttempts > 45 {
                self.readinessTimer?.invalidate()
                self.readinessTimer = nil
                self.isStarting = false
                self.readyWaiters.removeAll()
                self.onStatusChanged?()
                return
            }

            self.checkHealth { [weak self] ok in
                guard let self = self else { return }
                if ok {
                    self.readinessTimer?.invalidate()
                    self.readinessTimer = nil
                    self.isStarting = false
                    self.onBackendBecameHealthy?()
                    self.flushReadyWaiters()
                    self.onStatusChanged?()
                }
            }
        }
    }

    private func flushReadyWaiters() {
        let callbacks = readyWaiters
        readyWaiters.removeAll()
        for cb in callbacks {
            cb()
        }
    }

    private func cleanupPidFileIfMatching(pid: Int32) {
        guard let raw = try? String(contentsOf: pidFileURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
              raw == "\(pid)" else {
            return
        }
        try? FileManager.default.removeItem(at: pidFileURL)
    }

    private func closeLogHandle() {
        if let handle = ownedLogHandle {
            try? handle.close()
            ownedLogHandle = nil
        }
    }
}
